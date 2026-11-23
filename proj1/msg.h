/*
msg.h - message layout for the scheduler <-> child

one private System V queue is shared by the parent and the IO children.
Addressing is by mtype (always >= 1);
    parent -> child i : mtype = SLICE_BASE + i ("time" slice)
    child i -> parent : mtype = REPLY_BASE + i ("per slice reply")
A child only ever receives its own SLICE_BASE + i, and the parent only ever
waits for the REPLY_BASE + i of the child it just dispatched.
 */
#ifndef MSG_H
#define MSG_H

#include <stdint.h>

#define N 10 /*number of children / tasks (P0..P9)*/

#define SLICE_BASE 1 /*mtype 1..10 : slice for child 0..9*/
#define REPLY_BASE 101 /*mtype 101..110: reply from child 0..9*/

struct smsg {
    long mtype; /*who the message is for*/
    int task; /*logical task index 0..9*/
    int done; /*reply only: 1 = this slice finished a CPU burst*/
    int64_t io_time; /*reply only: io of the pair just finished (if done)*/
};

#define MSGSZ (sizeof(struct smsg) - sizeof(long))

/* The user-process side, implemented in child.c. parent.c calls this once
 * in each child right after fork(); it does not return. */
void child_main(int idx, int msqid, const char *wl_path);

#endif
