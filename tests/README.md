# Repository regression tests

Run from the repository root with Python's standard library:

```sh
python3 -m unittest discover -s tests -v
```

The suite contains 23 trace-checker tests and 8 starter-plumbing tests. It uses
Python's standard library and a C compiler (`cc`); no third-party Python
package is needed. Build products and generated inputs go in temporary
directories. The C tests are skipped with an explicit message if `cc` is
unavailable.

## Trace checker

The 23 checker tests call `check_trace.main()` with actual temporary input files and
`--ticks N`, exercising the complete parser, validators, summary checks,
category report, and exit status. They finish without building or running a
scheduler.

Each fixture in `fixtures/check_trace/` is a fixed, hand-authored workload and
trace containing both headers, 1–7 tick lines, and the complete summary. The
expected scheduling decisions and summary numbers are stored in those files;
the tests never generate expected output using the checker or another replay
implementation. Mutations assert the exact PASS/FAIL/SKIP category map, so a
correct rejection that incorrectly disables independent checks also fails.

| Fixture | Boundary being checked |
| --- | --- |
| `rr_basic` | Basic mode ignores nonzero I/O; one-tick bursts keep the current quantum; a pending preemption precedes an arrival. |
| `rr_zero_io` | Optional mode treats `io=0` as BURST, with no wait-queue entry or quantum reset. |
| `rr_io_arrivals` | IODONE precedes simultaneous P2/P3 arrivals; the completed response from tick 2 to tick 4 is 2, while the IODONE at tick 6 is excluded because P0 never runs again. |
| `rr_idle` | No task arrives during the observation window: every total is zero, fairness is 1, and utilization is 0. |
| `rr_unserved_io` | I/O completes but the task never runs again in the observation window: there are no valid response samples, so `io_resp` is zero. |
| `rr_large_values` | CPU/I/O durations beyond signed 32-bit range and arrival ticks up to the 18-digit maximum remain valid; unfinished I/O is still counted. |
| `mlfq_pending_boost` | A tick-0 preemption is applied before the tick-1 boost, leaving the preempted P0 in level 0. |
| `mlfq_boost_collision` | P0's level-1 quantum and CPU burst finish together at tick 2; the tick-3 boost overrides `io_return=same`, so tick-4 dispatch uses the level-0 quantum. |
| `vtime_tie` | With weights 1 and 2 and quantum 1, the dispatch sequence is P0, P1, P1, P0; reinsertion at equal runtime preserves existing ready order. |
| `vtime_arrival_clamp` | At tick 2, P0 has runtime 5040. Arriving P1 is clamped to 5040 and inserted after P0, then receives its weighted service. |
| `vtime_wakeup_clamp` | At tick 4, P0 wakes with stored runtime 2520 and is clamped to 3780, joining after already-ready P1. |

Tasks not exercised by a fixture arrive after its short observation window
(usually at tick 99; the large-value fixture also uses much later arrivals).
The fake but distinct PID mappings are deliberate: this checker validates
trace syntax and semantics; live processes, IPC, and timer behavior require
the separate execution and walkthrough gates. Likewise, fixture TIME values
are synthetic declared values, not measurements of test execution.

When checking a copy of these tests outside the repository, select the actual
checker explicitly:

```sh
OS_PROJ1_CHECKER=/absolute/path/to/check_trace.py python3 -m unittest discover -s tests -v
```

These focused cases protect documented boundaries; they are not an exhaustive
proof of every scheduling policy or malformed-input path.

## Starter plumbing

`test_skeleton.py` compiles `skeleton_harness.c` against the provided
`skeleton/parent.c`. The harness replaces process, IPC, timer, and trace-file
operations with controlled responses; it creates no child processes or
message queues. Its eight scenarios check:

- formatted output longer than the local buffer, including interrupted and
  partial writes;
- cleanup after only some forks succeed;
- cleanup when opening the trace fails after all children were created;
- timer disarming before child termination, sparse PID bookkeeping,
  interrupted waits, and repeated successful cleanup;
- reporting both queue-removal and file-close failures;
- failing and cleaning up when a write makes no progress;
- removing long ASCII and UTF-8 full-line comments without leaking their
  continuation into workload records;
- preserving records and line boundaries after more than 64 KiB of blank
  lines.

These are tests of the provided text reader and error-handling code. A live run on the grading
environment is still needed to verify actual timer delivery, child execution,
IPC, and resource removal as required by Gate 1.
