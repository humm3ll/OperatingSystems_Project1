/* Fault injection for provided plumbing; creates no OS processes or IPC. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
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

static ssize_t fake_write(int, const void *, size_t);
static pid_t fake_fork(void);
static int fake_msgget(key_t, int);
static int fake_open(const char *, int, ...);
static int fake_kill(pid_t, int);
static pid_t fake_waitpid(pid_t, int *, int);
static int fake_msgctl(int, int, struct msqid_ds *);
static int fake_close(int);
static int fake_setitimer(int, const struct itimerval *, struct itimerval *);
_Noreturn static void fake_exit(int);

#define main scheduler_main
#define write fake_write
#define fork fake_fork
#define msgget fake_msgget
#define open fake_open
#define kill fake_kill
#define waitpid fake_waitpid
#define msgctl fake_msgctl
#define close fake_close
#define setitimer fake_setitimer
#define exit fake_exit
#include "parent.c"
#undef main
#undef write
#undef fork
#undef msgget
#undef open
#undef kill
#undef waitpid
#undef msgctl
#undef close
#undef setitimer
#undef exit

static const char *scenario;
static char output[8192];
static size_t output_len;
static int writes, forks, kills, waits, removed, closed, disarmed;
static pid_t killed[N], waited[N];
static int interrupted_wait;

static ssize_t fake_write(int fd, const void *buf, size_t len)
{
	assert(fd == 52);
	writes++;
	if (!strcmp(scenario, "zero_write"))
		return 0;
	if (writes == 1) {
		errno = EINTR;
		return -1;
	}
	if (len > 7)
		len = 7;
	assert(output_len + len < sizeof(output));
	memcpy(output + output_len, buf, len);
	output_len += len;
	return (ssize_t)len;
}

static pid_t fake_fork(void)
{
	if (!strcmp(scenario, "fork_failure") && forks == 3) {
		errno = EAGAIN;
		return -1;
	}
	return 1000 + forks++;
}

static int fake_msgget(key_t key, int flags)
{
	assert(key == IPC_PRIVATE);
	(void)flags;
	return 41;
}

static int fake_open(const char *path, int flags, ...)
{
	(void)path;
	(void)flags;
	assert(!strcmp(scenario, "open_failure"));
	errno = EACCES;
	return -1;
}

static int fake_kill(pid_t pid, int signo)
{
	assert(pid > 0 && signo == SIGTERM && kills < N);
	if (strcmp(scenario, "fork_failure") && strcmp(scenario, "open_failure"))
		assert(disarmed == 1); /* stop ticks before terminating any child */
	killed[kills++] = pid;
	return 0;
}

static pid_t fake_waitpid(pid_t pid, int *status, int options)
{
	(void)status;
	assert(pid > 0 && options == 0 && waits < N);
	if (!interrupted_wait++) {
		errno = EINTR;
		return -1;
	}
	waited[waits++] = pid;
	return pid;
}

static int fake_msgctl(int id, int command, struct msqid_ds *data)
{
	assert(id == 41 && command == IPC_RMID && data == NULL);
	removed++;
	if (!strcmp(scenario, "cleanup_failure")) {
		errno = EIO;
		return -1;
	}
	return 0;
}

static int fake_close(int fd)
{
	assert(fd == 52);
	closed++;
	if (!strcmp(scenario, "cleanup_failure")) {
		errno = EIO;
		return -1;
	}
	return 0;
}

static int fake_setitimer(int which, const struct itimerval *value,
			 struct itimerval *old)
{
	assert(which == ITIMER_REAL && value != NULL && old == NULL);
	assert(value->it_value.tv_sec == 0 && value->it_value.tv_usec == 0);
	assert(value->it_interval.tv_sec == 0 && value->it_interval.tv_usec == 0);
	disarmed++;
	return 0;
}

static void check_cleanup(int children, int expect_close)
{
	assert(kills == children && waits == children);
	for (int i = 0; i < children; i++) {
		assert(killed[i] == 1000 + i);
		assert(waited[i] == killed[i]);
	}
	assert(removed == 1 && closed == expect_close);
	int expect_disarm = strcmp(scenario, "fork_failure") &&
		strcmp(scenario, "open_failure");
	assert(disarmed == expect_disarm);
}

_Noreturn static void fake_exit(int status)
{
	assert(status != 0);
	if (!strcmp(scenario, "fork_failure"))
		check_cleanup(3, 0);
	else if (!strcmp(scenario, "open_failure"))
		check_cleanup(N, 0);
	else if (!strcmp(scenario, "zero_write"))
		check_cleanup(1, 1);
	else
		abort();
	puts("fatal path cleaned up");
	exit(0);
}

void child_main(int idx, int queue, const char *path)
{
	(void)idx;
	(void)queue;
	(void)path;
	abort(); /* fake_fork never creates a child */
}

int main(int argc, char **argv)
{
	assert(argc == 3);
	scenario = argv[1];
	if (!strcmp(scenario, "read_workload")) {
		char *text = slurp_tokens(argv[2]);
		assert(fputs(text, stdout) >= 0);
		free(text);
		return 0;
	}
	if (!strcmp(scenario, "fork_failure") ||
	    !strcmp(scenario, "open_failure")) {
		char *args[] = {"scheduler", "rr", argv[2], NULL};
		(void)scheduler_main(3, args);
		abort(); /* these failures must terminate through die() */
	}
	trace_fd = 52;
	if (!strcmp(scenario, "long_trace")) {
		char text[4097];
		memset(text, 'x', sizeof(text) - 1);
		text[sizeof(text) - 1] = '\0';
		trace("prefix:%s:%d\n", text, 123);
		char expected[sizeof(text) + 32];
		int len = snprintf(expected, sizeof(expected), "prefix:%s:%d\n", text, 123);
		assert(output_len == (size_t)len);
		assert(!memcmp(output, expected, output_len));
		assert(writes > 2);
		return 0;
	}
	msqid = 41;
	timer_armed = 1;
	cpid[0] = 1000;
	if (!strcmp(scenario, "zero_write")) {
		trace("must fail");
		abort();
	}
	/* A hole must never turn cleanup into kill(0, ...) or waitpid(0, ...). */
	cpid[3] = 1001;
	int status = cleanup();
	check_cleanup(2, 1);
	if (!strcmp(scenario, "cleanup_failure"))
		assert(status != 0);
	else {
		assert(!strcmp(scenario, "cleanup_success") && status == 0);
		assert(cleanup() == 0);
		check_cleanup(2, 1); /* successful cleanup is safe to repeat */
	}
	return 0;
}
