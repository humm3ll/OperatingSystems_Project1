/*
 * parent.c — the OS-scheduler side of os_proj1 (the parent process).
 * The user-process side is child.c; the shared message layout is msg.h.
 * Read the assignment spec (../README.md) first; section numbers refer to it.
 *
 * WHAT THIS FRAMEWORK ALREADY DOES (provided plumbing; still examinable):
 *  - argument handling and trace-file management (§6 file naming, O_TRUNC)
 *  - the exact trace line / summary format strings (§6)
 *  - the recommended flag + sigsuspend timer pattern (grown from
 *    ../examples/signal.c, race-free — §2.1)
 *  - a workload text reader that strips full-line comments (§2.2)
 *  - forking the 10 children and cleanup on normal exit or errors (§2.3)
 *  It compiles and runs as-is: 10 children are forked, ticks fire, every
 *  line prints IDLE, and the run terminates cleanly. It will NOT pass
 *  check_trace.py until you implement the TODOs (here and in child.c).
 *
 * WHAT YOU IMPLEMENT — search "TODO(student)" in parent.c AND child.c,
 * each tagged with the rubric item of §7 it belongs to:
 *  [parse]    workload parsing and arrivals; the §2.2 information boundary
 *  [msgq]     the message-queue protocol and required burst-completion
 *             reporting (parent side here, child side in child.c)
 *  [rr]       run-queue, dispatch, quantum, preemption (§3, §6 steps ①–⑥)
 *  [account]  waiting time, per-line counts, the four metrics (§5.3, §6c)
 *  [io]       optional positive-I/O blocking, wait-queue and return (§4)
 *  [mlfq]     optional MLFQ (§5) — extend the structures below
 *  [vtime]    optional deterministic weighted VTIME (§5)
 *
 * DESIGN FREEDOMS (§2.1): the choices baked in here — a single private
 * queue, the flag-based handler — are recommendations. You may replace
 * them. Grading covers both the trace (§6) and live process/timer/IPC
 * behavior (§7); understand every framework line kept in your submission.
 */
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/msg.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "msg.h"

/* Declared params (README Section 3). Overridable with -D for experiments
in report. */
#ifndef TICK_MS
#define TICK_MS 2
#endif
#ifndef RR_QUANTUM
#define RR_QUANTUM INT64_C(5)
#endif
#ifndef VT_QUANTUM
#define VT_QUANTUM INT64_C(5)
#endif
#define VTIME_SCALE 2520 /*fixed by section 5.2; exact for weights 1..10*/
#define IMPL "optional" /*I/O wait-queue is implemented*/
#ifndef TICKS_TOTAL
#define TICKS_TOTAL 10000 /*fixed by spec (section 2.3) */
#endif

/* ---------------------------------------------------------------- state */
static int64_t arrival[N];
static int weight[N];
static pid_t cpid[N];
static int msqid = -1;
static int trace_fd = -1;
static int timer_armed;
static const char *wl_path;

/*scheduler state. only parent touches these*/
static int vtime; /*1 = vtime, 0 = rr*/
static int64_t quantum;
static int rq[N], nrq; /*run-queue, dispatch order, no running task*/
static int wq[N], nwq; /*wait-queue, entry order*/
static int64_t wrem[N]; /*remaining I/O ticks of a task in wq*/
static int64_t vrt[N], min_vrt; /*vtime: vruntime per task, monotonic floor*/
static int run = -1; /*task holding the CPU, -1 if none*/
static int64_t qleft; /*quantum left of the running task*/
static int preempt_pending; /*last line ended with PREEMPT of 'run'*/
static int slice_out = -1; /*task whose slice reply is still unread*/
static int64_t iodone_at[N]; /*tick of the last IODONE not yet followed by RUN*/

/*counts over printed lines (README section 6c)*/
static long total_wait[N], total_cpu[N], total_io[N];
static long busy, io_n, io_sum;

/* ------------------------------------------------------------- plumbing */
/* Best-effort cleanup: a failure in one operation must not skip the others.
 * Only positive PIDs returned by successful parent-side forks are used. */
static int cleanup(void)
{
	int failed = 0;
	if (timer_armed) {
		struct itimerval stopped = {0};
		if (setitimer(ITIMER_REAL, &stopped, NULL) < 0) {
			perror("disarm timer");
			failed = 1;
		} else {
			timer_armed = 0;
		}
	}
	for (int i = 0; i < N; i++) {
		if (cpid[i] > 0 && kill(cpid[i], SIGTERM) < 0 && errno != ESRCH) {
			perror("kill child");
			failed = 1;
		}
	}
	for (int i = 0; i < N; i++) {
		if (cpid[i] <= 0)
			continue;
		pid_t reaped;
		do {
			reaped = waitpid(cpid[i], NULL, 0);
		} while (reaped < 0 && errno == EINTR);
		if (reaped < 0 && errno != ECHILD) {
			perror("waitpid");
			failed = 1;
		} else {
			cpid[i] = 0;
		}
	}
	if (msqid >= 0) {
		if (msgctl(msqid, IPC_RMID, NULL) < 0) {
			perror("remove message queue");
			failed = 1;
		} else {
			msqid = -1;
		}
	}
	if (trace_fd >= 0) {
		/* Do not retry close after EINTR: the descriptor may be closed. */
		if (close(trace_fd) < 0) {
			perror("close trace");
			failed = 1;
		}
		trace_fd = -1;
	}
	return failed;
}

static void die(const char *msg)
{
	/* Report the original error before cleanup can change errno. */
	perror(msg);
	(void)cleanup();
	exit(EXIT_FAILURE);
}

static void trace(const char *fmt, ...) /* buffered sprintf + write (§6) */
{
	char local[1024];
	char *buf = local;
	va_list ap, copy;
	va_start(ap, fmt);
	va_copy(copy, ap);
	int n = vsnprintf(local, sizeof(local), fmt, ap);
	va_end(ap);
	if (n < 0) {
		va_end(copy);
		die("format trace");
	}
	if ((size_t)n >= sizeof(local)) {
		buf = malloc((size_t)n + 1);
		if (!buf) {
			va_end(copy);
			die("allocate trace buffer");
		}
		int written = vsnprintf(buf, (size_t)n + 1, fmt, copy);
		if (written != n) {
			va_end(copy);
			free(buf);
			errno = EIO;
			die("format trace");
		}
	}
	va_end(copy);
	for (size_t offset = 0; offset < (size_t)n;) {
		ssize_t written = write(trace_fd, buf + offset, (size_t)n - offset);
		if (written < 0 && errno == EINTR)
			continue;
		if (written <= 0) {
			int saved_errno = written == 0 ? EIO : errno;
			if (buf != local)
				free(buf);
			errno = saved_errno;
			die("write trace");
		}
		offset += (size_t)written;
	}
	if (buf != local)
		free(buf);
}

/* Read complete physical lines and remove full-line '#' comments.
 * Return caller-owned text; free it after parsing. Non-comment line endings
 * remain intact so the student parser can enforce one task record per line.
 * getline and a growing buffer support the §2.2/§7 legal input sizes,
 * including UTF-8 comments and large amounts of blank space. This is a text
 * reader, not the workload grammar/range validator. */
static char *slurp_tokens(const char *path)
{
	FILE *f = fopen(path, "r");
	if (!f)
		die("workload open");
	size_t used = 0, capacity = 4096, line_capacity = 0;
	char *buf = malloc(capacity);
	char *line = NULL;
	ssize_t length;
	int saved_errno;
	if (!buf)
		goto fail;
	buf[0] = '\0';
	while ((length = getline(&line, &line_capacity, f)) >= 0) {
		char *p = line;
		while (isspace((unsigned char)*p))
			p++;
		if (*p == '#')
			continue;
		if ((size_t)length > SIZE_MAX - used - 1) {
			errno = EOVERFLOW;
			goto fail;
		}
		size_t needed = used + (size_t)length + 1;
		if (needed > capacity) {
			size_t grown = capacity <= SIZE_MAX / 2 ? capacity * 2 : SIZE_MAX;
			if (grown < needed)
				grown = needed;
			char *larger = realloc(buf, grown);
			if (!larger)
				goto fail;
			buf = larger;
			capacity = grown;
		}
		memcpy(buf + used, line, (size_t)length);
		used += (size_t)length;
		buf[used] = '\0';
	}
	if (!feof(f))
		goto fail;
	int closed = fclose(f);
	f = NULL;
	if (closed < 0)
		goto fail;
	free(line);
	return buf;

fail:
	saved_errno = errno;
	free(line);
	free(buf);
	if (f)
		(void)fclose(f);
	errno = saved_errno;
	die("read workload");
	return NULL; /* die() exits */
}

/*queue_ops*/
/*run queue (rq) is a plain array of task numbers, head at rq[0].
rr: append at the tail. vtime: insert after every entry with vruntime <=
the new one, so ties keep read entry order.*/
static void rq_insert(int i) {
    int pos = nrq;
    if (vtime)
        while (pos > 0 && vrt[rq[pos - 1]] > vrt[i])
            pos--;
    memmove(&rq[pos + 1], &rq[pos], (size_t)(nrq - pos) * sizeof(int));
    rq[pos] = i;
    nrq++;
}

/*take task at position pos out of run queue*/
static void rq_remove(int pos) {
    memmove(&rq[pos], &rq[pos + 1], (size_t)(nrq - pos - 1) * sizeof(int));
    nrq--;
}

/*seeing where task i is in the run queue. -1 if it is not there*/
static int rq_find(int i) {
    for (int p = 0; p < nrq; p++)
        if (rq[p] == i)
            return p;
    return -1;
}

/*a task entering the run queue from arrival or I/O might not carry
credit from the time it was away (README section 5.2)*/
static void ready_entry(int i) {
    if (vtime && vrt[i] < min_vrt)
        vrt[i] = min_vrt;
    rq_insert(i);
}

/*ipc*/
/*parent taks to children only through message queue.
give child k one tick of CPU (message itself doesnt carry data)*/
static void send_slice(int k) {
    struct smsg s = {.mtype = SLICE_BASE + k, .task = k};
    while (msgsnd(msqid, &s, MSGSZ, 0) < 0)
        if (errno != EINTR)
            die("msgsnd slice");
}

/*wait until child k answers. this is how we know it really ran*/
static void recv_reply(int k, struct smsg *m) {
    while (msgrcv(msqid, m, MSGSZ, REPLY_BASE + k, 0) < 0)
        if (errno != EINTR)
            die("msgrcv reply");
}

/*event string*/
/*ev holds events of current tick; for example, "IODONE P1 ARRIVE P4"*/
static char ev[1024];
static size_t evlen;

static void ev_add(const char *fmt, ...) {
    va_list ap;
    if (evlen > 0 && evlen < sizeof(ev) - 1)
        ev[evlen++] = ' ';
    va_start(ap, fmt);
    int n = vsnprintf(ev + evlen, sizeof(ev) - evlen, fmt, ap);
    va_end(ap);
    if (n > 0)
        evlen += (size_t)n < sizeof(ev) - evlen ? (size_t)n : sizeof(ev) - evlen - 1;
}

/* parsing */
static int have_weights;

/*return next work of workload text*/
static char *next_tok(char **sp) {
    char *t = strtok_r(NULL, " \t\r\n", sp);
    if (!t) {
        fprintf(stderr, "workload: unexpected end of input\n");
        (void)cleanup();
        exit(EXIT_FAILURE);
    }
    return t;
}

/*parent keeps only public metadata (weights and arrival ticks).
burst tokens are skipped (each child reads its own entry).*/
static void parse_task_metadata(const char *path)
{
	char *toks = slurp_tokens(path);
    char *sp = NULL;
    for (char *t = strtok_r(toks, " \t\r\n", &sp); t;
        t = strtok_r(NULL, " \t\r\n", &sp)) {
            if (strcmp(t, "tasks") == 0) {
                next_tok(&sp);
            } else if (strcmp(t, "weights") == 0) {
                for (int i = 0; i < N; i++)
                    weight[i] = atoi(next_tok(&sp));
                have_weights = 1;
            } else if (strcmp(t, "task") == 0) {
                int idx = atoi(next_tok(&sp));
                int64_t arr = (int64_t)strtoll(next_tok(&sp), NULL, 10);
                long np = atoi(next_tok(&sp));
                for (long k = 0; k < 2 * np; k++)
                    next_tok(&sp);
                if (idx >= 0 && idx < N)
                    arrival[idx] = arr;
            }
        }
	free(toks);
}

/* main */
static volatile sig_atomic_t got_tick;
static void on_alarm(int signo) { (void)signo; got_tick = 1; }

int main(int argc, char **argv)
{
	if (argc != 3 ||
	    (strcmp(argv[1], "rr") != 0 && strcmp(argv[1], "mlfq") != 0 &&
	     strcmp(argv[1], "vtime") != 0)) {
		fprintf(stderr, "usage: %s <rr|mlfq|vtime> <workload_file>\n", argv[0]);
		return 1;
	}
	wl_path = argv[2];
    vtime = strcmp(argv[1], "vtime") == 0;
    quantum = vtime ? VT_QUANTUM : RR_QUANTUM;

    for (int i = 0; i < N; i++)
        weight[i] = 1;
    parse_task_metadata(wl_path);
    if (vtime && !have_weights) {
        fprintf(stderr, "vtime needs a weights record in %s\n", wl_path);
        return 1;
    }

	parse_task_metadata(wl_path);

	/* One private queue: inherited across fork, no key collisions on a
	 * shared machine (§2.1). Replace with your own topology if you like. */
	msqid = msgget(IPC_PRIVATE, IPC_CREAT | 0666);
	if (msqid < 0)
		die("msgget");

	for (int i = 0; i < N; i++) { /* fork order defines P0..P9 (§2.2) */
		pid_t p = fork();
		if (p < 0)
			die("fork");
		if (p == 0) {
			child_main(i, msqid, wl_path); /* runs in child.c; no return */
			_exit(1);
		}
		cpid[i] = p;
	}

	char fname[64]; /* §6: one trace file per policy */
	snprintf(fname, sizeof(fname), "schedule_dump.%s.txt", argv[1]);
	trace_fd = open(fname, O_CREAT | O_WRONLY | O_TRUNC, 0644);
	if (trace_fd < 0)
		die("open trace");

	/* header (§6a) — this is the basic RR form; an advanced submission
	 * replaces this line with its MLFQ or VTIME header. */
    if (vtime)
        trace("# policy=VTIME quantum=%" PRId64 " tick_ms=%d workload=%s tasks=%d\n",
            quantum, TICK_MS, wl_path, N);
    else {
        trace("# policy=RR impl=%s quantum=%" PRId64 " tick_ms=%d workload=%s tasks=%d\n",
            IMPL, quantum, TICK_MS, wl_path, N);
    }
	trace("#");
	for (int i = 0; i < N; i++)
		trace(" P%d=%d", i, (int)cpid[i]);
	trace("\n");

	/*tasks with arrival 0 are ready before tick 0.*/
    for (int i = 0; i < N; i++) {
        iodone_at[i] = -1;
        if (arrival[i] == 0) /*present before tick 0, ascending index*/
            rq_insert(i);
    }

	/* timer: SIGALRM stays blocked except inside sigsuspend, so there is no
    lost wakeup race and the parent's msgrcv never sees EINTR*/
	struct sigaction sa = {0};
	sa.sa_handler = on_alarm;
	if (sigemptyset(&sa.sa_mask) < 0 || sigaction(SIGALRM, &sa, NULL) < 0)
		die("install SIGALRM handler");
	sigset_t blocked, waitmask;
	if (sigemptyset(&blocked) < 0 || sigaddset(&blocked, SIGALRM) < 0 ||
	    sigprocmask(SIG_BLOCK, &blocked, &waitmask) < 0 ||
	    sigdelset(&waitmask, SIGALRM) < 0)
		die("configure SIGALRM mask");

	struct timespec t0, t1; /* §2.4: expected vs elapsed */
	if (clock_gettime(CLOCK_MONOTONIC, &t0) < 0)
		die("read start time");
	struct itimerval it = {{0, TICK_MS * 1000}, {0, TICK_MS * 1000}};
	if (setitimer(ITIMER_REAL, &it, NULL) < 0)
		die("arm timer");
	timer_armed = 1;

	for (int t = 0; t < TICKS_TOTAL; t++) {
		while (!got_tick) {
			/* one handled SIGALRM = one tick; EINTR is expected */
			if (sigsuspend(&waitmask) < 0 && errno != EINTR)
				die("wait for tick");
		}
		got_tick = 0;
        ev[0] = '\0';
        evlen = 0; /*start a fresh event list for this tick*/

        /*each tick below runs same steps in same order :)*/

        /*previous line ended in PREEMPT, move task now*/
        if (preempt_pending) {
            int k = run;
            run = -1;
            preempt_pending = 0;
            rq_insert(k); /*rr: tail: vtime: by updated vruntime*/
        }

        /*vtime min_vruntime = smallest vruntime among runnable tasks.
        ONLY moves up. waking tasks arent allowed below it.*/
        if (vtime && (run >= 0 || nrq > 0)) {
            int64_t m = run >= 0 ? vrt[run] : vrt[rq[0]];
            for (int p = 0; p < nrq; p++)
                if (vrt[rq[p]] < m)
                    m = vrt[rq[p]];
            if (m > min_vrt)
                min_vrt = m;
        }

        /*step 1 -> every task in I/O has 1 less tick to wait, at 0 it is done
        and goes back to the run queue.*/
        for (int p = 0; p < nwq;) {
            int k = wq[p];
            if (--wrem[k] > 0) {
                p++;
                continue;
            }
            memmove(&wq[p], &wq[p + 1], (size_t)(nwq - p - 1) * sizeof(int));
            nwq--;
            ev_add("IODONE P%d", k);
            iodone_at[k] = t;
            ready_entry(k);
        }

        /*step 2 -> read child's answer to last ticket's slice.
        burst finished with io > 0 = task goes to wall queue
        burst finished with i0 = 0 = just log BURST, task stays*/
        if (slice_out >= 0) {
            int k = slice_out;
            struct smsg m;
            slice_out = -1;
            recv_reply(k, &m);
            if (m.done && m.io_time > 0) {
                if (run == k) {
                    run = -1;
                } else {
                    int p = rq_find(k);
                    if (p < 0) {
                        errno = EPROTO;
                        die("burst reply from a task not in RUN or RQ");
                    }
                    rq_remove(p);
                }
                wq[nwq++] = k;
                wrem[k] = m.io_time;
                ev_add("IOREQ P%d(io=%" PRId64 ")", k, m.io_time);
            } else if (m.done) {
                ev_add("BURST P%d", k);
            }
        }

        /*step 3 -> tasks whose arrival time is now join the run queue*/
        for (int i = 0; i < N; i++) {
            if (t > 0 && arrival[i] == t) {
                ev_add("ARRIVE P%d", i);
                ready_entry(i);
            }
        }

        /*step 4 -> if the CPU is free, head of the run queue runs.
        fresh quantum is given only here.*/
        if (run < 0 && nrq > 0) {
            run = rq[0];
            rq_remove(0);
            qleft = quantum;
        }

        /*step 5 -> running task gets one tick; we count it and
        charge vruntime (heavier weight = charged less)*/
        if (run >= 0) {
            send_slice(run);
            slice_out = run;
            qleft--;
            if (vtime)
                vrt[run] += VTIME_SCALE / weight[run];
            if (iodone_at[run] >= 0) {
                io_sum += t - iodone_at[run];
                io_n++;
                iodone_at[run] = -1;
            }
            if (qleft == 0) {
                ev_add("PREEMPT P%d", run);
                preempt_pending = 1;
            }
            total_cpu[run]++;
            busy++;
        }
        /*waiting = sitting in run queue. io = sitting in wait queue.*/
        for (int p = 0; p < nrq; p++)
            total_wait[rq[p]]++;
        for (int p = 0; p < nwq; p++)
            total_io[wq[p]]++;

        /*step 6 -> write this tick's line to trace file*/
        char rqs[256], wqs[512];
        size_t a = 0, b = 0;
        rqs[0] = wqs[0] = '\0';
        for (int p = 0; p < nrq; p++)
            a += (size_t)snprintf(rqs + a, sizeof(rqs) - a, "%sP%d", p ? " " : "", rq[p]);
        for (int p = 0; p < nwq; p++)
            b += (size_t)snprintf(wqs + b, sizeof(wqs) - b, "%sP%d(io:%" PRId64 ")",
            p ? " " : "", wq[p], wrem[wq[p]]);
        const char *evs = evlen ? ev : "-";
        if (run >= 0)
            trace("[%04d] RUN P%d q_left=%" PRId64 " | RQ: [%s] | WQ: [%s] | ev: %s\n",
        t, run, qleft, rqs, wqs, evs);
        else
            trace("[%04d] IDLE | RQ: [%s] | WQ: [%s] | ev: %s\n", t, rqs, wqs, evs);
	}

    /* after loop: last slice must have really been processed, but its reuslt would
    belong to tick 10000: read reply and drop it.*/
    if (slice_out >= 0) {
        struct smsg m;
        recv_reply(slice_out, &m);
        slice_out = -1;
    }

	if (clock_gettime(CLOCK_MONOTONIC, &t1) < 0)
		die("read end time");
	long elapsed_ms = (t1.tv_sec - t0.tv_sec) * 1000 +
			  (t1.tv_nsec - t0.tv_nsec) / 1000000;
    /*summary: total per task, then the four metrics.
    Jain fairness = (sum cpu)^2 / (10 * sum of cpu^2): 1.0 means everyone got equal CPU*/
    double sw = 0, sc = 0, sc2 = 0;
	for (int i = 0; i < N; i++) {
		trace("WAIT P%d total_wait=%ld total_cpu=%ld total_io=%ld\n",
		      i, total_wait[i], total_cpu[i], total_io[i]);
        sw += (double)total_wait[i];
        sc += (double)total_cpu[i];
        sc2 += (double)total_cpu[i] * (double)total_cpu[i];
    }
    double avg_wait = sw / N;
    double io_resp = io_n ? (double)io_sum / (double)io_n : 0.0;
    double fairness = sc2 > 0 ? sc * sc / (N* sc2) : 1.0;
    double util = (double)busy / TICKS_TOTAL;
	trace("METRIC avg_wait=%.2f io_resp=%.2f fairness=%.4f util=%.4f\n",
	      avg_wait, io_resp, fairness, util);
	trace("TIME expected_ms=%ld elapsed_ms=%ld\n",
	      (long)TICK_MS * TICKS_TOTAL, elapsed_ms);

    /*section 2.3: disarm SIGTERM, wait, IPC_RMID, close*/
	return cleanup() ? EXIT_FAILURE : EXIT_SUCCESS;
}
