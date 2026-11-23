# Default target builds ./scheduler, as required by the spec (README §8).
# The parent (OS scheduler) and child (user process) are separate source
# files compiled into one binary; they share msg.h.
CC = cc
CFLAGS = -Wall

scheduler: parent.c child.c msg.h
	$(CC) $(CFLAGS) -o $@ parent.c child.c

clean:
	rm -f scheduler schedule_dump.*.txt

.PHONY: clean
