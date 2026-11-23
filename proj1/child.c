/*
 * child.c — the user-process side of os_proj1 (one child process).
 * parent.c calls child_main() once in each child right after fork();
 * shared message layout is msg.h. See ../README.md §3–§4.
 *
 * A child models one user process: it does no scheduling. It waits for a
 * time-slice message, makes one tick of progress, and always tells
 * the parent when a CPU burst ends. Blocking in msgrcv IS the simulation
 * of "not running" (§2.1) — do not busy-wait.
 */
#include <errno.h>
#include <stdlib.h>
#include <sys/ipc.h>
#include <sys/msg.h>
#include <sys/types.h>
#include <unistd.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "msg.h"

static void child_die(const char *what) {
    perror(what);
    _exit(1);
}

/*find own "task <idx> ..." line in the workload and return
cpu, io pairs in one array, for example, cpu1 io1 cpu2 io2 ...*/
static int64_t *load_pairs(int idx, const char *path, int *npairs_out) {
    FILE *f = fopen(path, "r");
    if (!f)
        child_die("child: open workload");
    char *line = NULL;
    size_t cap = 0;
    int64_t *pairs = NULL;
    int npairs = 0;
    while (!pairs && getline(&line, &cap, f) >= 0) {
        char *save = NULL;
        char *tok = strtok_r(line, " \t\r\n", &save);
        if (!tok || tok[0] == '#' || strcmp(tok, "task") != 0)
            continue;
        tok = strtok_r(NULL, " \t\r\n", &save);
        if (!tok || atoi(tok) != idx)
            continue;
        strtok_r(NULL, " \t\r\n", &save); /*arrival = parent's business*/
        tok = strtok_r(NULL, " \t\r\n", &save);
        if (!tok) continue;
        npairs = atoi(tok);
        if (npairs < 1) continue;
        pairs = malloc(sizeof(int64_t) * 2 * (size_t)npairs);
        if (!pairs)
            child_die("child: malloc");
        for (int k = 0; k < 2 * npairs; k++) {
            tok = strtok_r(NULL, " \t\r\n", &save);
            if (!tok)
                child_die("child: short task record");
            pairs[k] = (int64_t)strtoll(tok, NULL, 10);
        }
    }
    free(line);
    fclose(f);
    if (!pairs) {
        fprintf(stderr, "child %d: no task record\n", idx);
        _exit(1);
    }
    *npairs_out = npairs;
    return pairs;
}

void child_main(int idx, int msqid, const char *wl_path) {
    int npairs;
    int64_t *pairs = load_pairs(idx, wl_path, &npairs);
    /*our progress lives here, not in the parent*/
    int cur = 0;
    int64_t remaining = pairs[0]; /*CPU ticks left in the current burst*/

    for (;;) {
        struct smsg m;
        ssize_t r;
        /*sleep here until the parent gives a tick of CPU
        not running = blocked in msgrcv*/
        do {
            r = msgrcv(msqid, &m, MSGSZ, SLICE_BASE + idx, 0);
        } while (r < 0 && errno == EINTR);
        if (r < 0)
            child_die("child: msgrcv");
        remaining--; /*one slice message = one tick of work*/

        struct smsg reply;
        memset(&reply, 0, sizeof(reply));
        reply.mtype = REPLY_BASE + idx;
        reply.task = idx;
        /*burst finished -> tell parent and its io time, then move to
        next pair (wrapping to first after the last)*/
        if (remaining == 0) {
            reply.done = 1;
            reply.io_time = pairs[2 * cur + 1];
            cur = (cur + 1) % npairs; /*advance to next pair*/
            remaining = pairs[2 * cur];
        }
        do {
            r = msgsnd(msqid, &reply, MSGSZ, 0);
        } while (r < 0 && errno == EINTR);
        if (r < 0)
            child_die("child: msgsnd");
    }
}
