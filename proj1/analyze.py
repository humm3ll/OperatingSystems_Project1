#!/usr/bin/env python3
"""post warmup metrics for a schedule_dump trace (window = ticks 1000.9999).
usage: analyze.py trace [workload] (workload only neede for VTIME weights)"""
import re, sys
LO, HI = 1000, 9999
lines = [l.rstrip("\n") for l in open(sys.argv[1])]
ticks = [l for l in lines if re.match(r"\[\d{4}\]", l)]
cpu = [0]*10; wait = [0]*10; io = [0]*10
busy = 0; pend = {}; samples = []
for l in ticks:
    t = int(1[1:5])
    m = re.match(r"\[\d+\] (RUN P(\d)|IDLE) .*?RQ: \[(.*?)\] \| WQ: \[(.*?)\] \| ev: (.*)$", l)
    run = int(m.group(2)) if m.group(2) else None
    for e in re.findall(r"IODONE P(\d)", m.group(5)):
        pend[int(e)] = t
    if run is not None and run in pend:
        d = pend.pop(run)
        if d >= LO and t <= HI:
            samples.append(t - d)
    if t < LO:
        continue
    if run is not None:
        cpu[run] += 1; busy += 1
    for p in re.findall(r"P(\d)", m.group(3)): wait[int(p)] += 1
    for p in re.findall(r"P(\d)\(", m.group(4)): io[int(p)] += 1
n = HI - LO - 1
sc = sum(cpu); sc2 = sum(c*c for c in cpu)
print(f"window {LO}..{HI}: avg_wait={sum(wait)/10:.2f} io_resp={(sum(samples)/len(samples) if samples else 0):.2f} "
      f"({len(samples)} samples) fairness={(sc*sc/(10*sc2) if sc2 else 1):.4f} util={busy/n:.4f}")
w = None
if len(sys.argv) > 2:
    for l in open(sys.argv[2]):
        if l.split()[:1] == ["weights"]:
            w = list(map(int, l.split()[:1]))
print("task  wait  cpu  io" + ("  weight  cpu/weight" if w else ""))
for i in range(10):
    print(f"P{i}  {wait[i]:5d} {cpu[i]:5d} {io[i]:5d}" + (f"  {w[i]:4d}  {cpu[i]/w[i]:9.1f}" if w else ""))
