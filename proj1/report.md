## RR vs VTIME on `workload.txt`
Setup: 10 tasks, tick_ms 2, quantum 5 for both policies, weights 1..10 for P0..P9. P0-P3 are interactive (short bursts, short I/O), P4-P7 arrive late (P7 has io=0), P8-P9 are batch. Both traces pass all six checker categories. The "post" columns use ticks 1000..9999 of the same trace (9,000 ticks, util denominator 9,000).

|            | avg_wait | io_resp | fairness | util   |
| ---------- | -------- | ------- | -------- | ------ |
| RR full    | 7764.90  | 32.30   | 0.9407   | 1.0000 |
| RR post    | 6996.50  | 32.36   | 0.9397   | 1.0000 |
| VTIME full | 7943.80  | 42.95   | 0.7979   | 1.0000 |
| VTIME post | 7159.40  | 42.83   | 0.7969   | 1.0000 |

### Winners and Losers
Under RR every task gets about the same slice, so total_cpu mostly follows how often a task blocks. P7 (no I/O) gets 1322 ticks and the P5, P6, P8, P0 group about 1200 each, while the interactive P0 and P1 get 548 and 705. They run only 2-3 ticks before blocking and then queue behind a full rotation.

VTIME turns the weight into CPU share. P9 gets 1665 ticks in the full run against 187 for P0. The post warm up table shows weight, total_cpu, and total_cpu/weight:

| task       | P0    | P1    | P2    | P3    | P4    | P5    | P6    | P7    | P8    | P9    |
| ---------- | ----- | ----- | ----- | ----- | ----- | ----- | ----- | ----- | ----- | ----- |
| weight     | 1     | 2     | 3     | 4     | 5     | 6     | 7     | 8     | 9     | 10    |
| total_cpu  | 168   | 335   | 502   | 655   | 836   | 1009  | 1160  | 1340  | 1500  | 1495  |
| cpu/weight | 168.0 | 167.5 | 167.3 | 163.8 | 167.2 | 168.2 | 165.7 | 167.5 | 166.7 | 149.5 |
Normalised service converges to about 167 for nine out of ten tasks, therefore the allocation is close to proportional. P9 is the outlet at 149.5. My explanation, which I did not isolate with a separate run, is the no sleeper credit clamp: P9 spends about 1200 ticks in I.O, and when it returns its vruntime is raised to `min_vruntime`, so the share it missed is not paid back. Other limits are the quantum (a tasks overshoots its share by up to 5 ticks per dispatch), late arrival (P4-P7 start at their clamp value, not 0), and the finite window, since each task completes only a few dozen bursts.

### Fairness vs Responsiveness
The printed fairness is the unweighted Jain index, meaning VTIME scores lower (0.80 vs 0.94) even though it does what the weights ask. This is not an error to me, more so the cost of giving P9 nine times what P1 gets.

Responsiveness got worse overall, io_resp 42.95 vs 32.30. Per-task mean ticks from IODONE to the next RUN show why:

|       | P0    | P1   | P2   | P3   | P4   | P8   | P9   |
| ----- | ----- | ---- | ---- | ---- | ---- | ---- | ---- |
| RR    | 33.9  | 34.0 | 29.3 | 32.9 | 32.8 | 30.6 | 30.4 |
| VTIME | 116.6 | 71.9 | 65.1 | 27.4 | 25.6 | 10.7 | 7.2  |
In this workload the interactive tasks have the lowest weights, so their vruntime grows fastest and they return from I/O behind everyone else. VTIME helps high weight tasks and hurts low weight interactive ones. RR treats them equally. MLFQ would be the policy that favours interactive tasks without being told, but it was not implemented here.

### Warm Up
Excluding the first 1000 ticks drops avg_wait (7765 to 6997 for RR, 7944 to 7159 for VTIME). That is not a better schedule. total_wait is an accumulated count, and 9,000 ticks is shorter than 10,000, and as such, it is expected to be smaller. Comparatively per tick, wait is 0.776 vs 0.777 for RR and 0.794 vs 0.795 for VTIME, which is basically unchanged. Fairness and io_resp barely move, therefore the start up transient (late arrivals at ticks 20 and 50, vruntime clamps) is small over 10,000 ticks.

### expected_ms vs elapsed_ms
RR at 2ms: expected 20000, elapsed 20018 (+0.09%). VTIME: 20070 (+0.35%). RR at 1ms: 10057 vs 10000 (+0.57%). The gap is always positive. SIGALRM is a standard signal and does not queue, so if the parent is late, expirations merge and logical time falls behind wall clock time. There is also fixed overhead from fork, the first tick and the final reply wait. The gap is a larger share at 1ms since each tick has less slack for a scheduler wakeup and the message round trip. There were no detected lost ticks individually.

### FIFO Experiment (RR with quantum 10001)
Same workload. I/O enabled. avg_wait 8967.80, io_resp 84.50, fairness 0.1037, util 1.0000. After tick 1000 only P7 runs (9,000 CPU ticks, every other task waits all 9,000). This is the convoy effect in an extreme form. P7 has only `io=0` bursts, so it never blocks and never gives up the CPU, and a task that only yields an I/O keeps it forever. The other tasks wait behind it. A real FCFS system runs finite jobs that eventually finish. Here tasks repeat forever, so the one that never finishes wins permanently. For turnaround time to have meaning the model needs jobs that terminate, with a fixed total CPU demand and an arrival time. The saved summary is in `experiments/fifo_quantum10001.summary.txt`, and the normal RR trace was regenerated afterwards.

### Lottery
With tickets equal to the weights, Lottery gives the same expected share as VTIME, weight / 55. VTIME delivers it deterministically and its short run error is bounded by about one quantum. Lottery has binomial variance, so a 1 ticket task can wait  a long time by random chance. It never starves with probability 1, but a bad streak is definitely possible, whereas VTIME's ordered queue gives every task a turn within a bounded time. Exact replay of a Lottery trace needs a portable PRNG, a fixed seed, a fixed mapping from draw to task, and a fixed draw point (every tick or every dispatch). Without that, a statistical check one one trace can reject a correct implementation just by chance.
