#!/usr/bin/env python3
"""Check an os_proj1 scheduling trace against its workload.

Usage:
    python3 check_trace.py TRACE WORKLOAD [--ticks N]

The checker independently replays the RR, MLFQ, and weighted VTIME scheduling
rules from README section 6 and reports six categories: FORMAT, STRUCTURE,
ORDER, QUANTUM, BURST, and ACCOUNTING. It never executes the submitted
scheduler.
"""

import argparse
from decimal import Decimal
import re
import sys


N_TASKS = 10
VTIME_SCALE = 2520  # lcm(1..10): every permitted weight divides exactly
VTIME_WEIGHTS = frozenset(range(1, 11))
DEFAULT_TICKS = 10000
MAXERR = 5
MAX_STORED_ERRORS = 50
MAX_WORKLOAD_CHARS = 1 << 20
MAX_TRACE_BYTES = 16 << 20
MAX_LINE_CHARS = 4096
MAX_TRACE_LINE_BYTES = 4096
MAX_EXTRA_TRACE_LINES = 100
MAX_DECIMAL_DIGITS = 18
CATEGORIES = ("FORMAT", "STRUCTURE", "ORDER", "QUANTUM", "BURST",
              "ACCOUNTING")

TASK_RE = re.compile(r"P([0-9])$")
WQ_ENTRY_RE = re.compile(r"P([0-9])\(io:([0-9]{1,18})\)$")
HEAD_RUN_RE = re.compile(
    r"\[([0-9]{4})\] RUN P([0-9]) q_left=([0-9]{1,18})$"
)
HEAD_IDLE_RE = re.compile(r"\[([0-9]{4})\] IDLE$")
RQ_LEVEL_RE = re.compile(r"RQ([0-9]{1,18}): \[([^\]]*)\]")

EVENT_ARG_RE = {
    "IODONE": re.compile(r"P([0-9])$"),
    "IOREQ": re.compile(r"P([0-9])\(io=([0-9]{1,18})\)$"),
    "BURST": re.compile(r"P([0-9])$"),
    "ARRIVE": re.compile(r"P([0-9])$"),
    "PREEMPT": re.compile(r"P([0-9])$"),
}

WAIT_RE = re.compile(
    r"WAIT P([0-9]) total_wait=([0-9]{1,18}) "
    r"total_cpu=([0-9]{1,18}) total_io=([0-9]{1,18})$"
)
METRIC_RE = re.compile(
    r"METRIC avg_wait=((?:0|[1-9][0-9]{0,17})\.[0-9]{2}) "
    r"io_resp=((?:0|[1-9][0-9]{0,17})\.[0-9]{2}) "
    r"fairness=((?:0|[1-9][0-9]{0,17})\.[0-9]{4}) "
    r"util=((?:0|[1-9][0-9]{0,17})\.[0-9]{4})$"
)
LOOSE_NUMBER = (
    r"[+-]?(?:[0-9]{1,18}(?:\.[0-9]{0,18})?|\.[0-9]{1,18})"
    r"(?:[eE][+-]?[0-9]{1,6})?"
)
METRIC_LOOSE_RE = re.compile(
    rf"METRIC avg_wait=({LOOSE_NUMBER}) "
    rf"io_resp=({LOOSE_NUMBER}) "
    rf"fairness=({LOOSE_NUMBER}) "
    rf"util=({LOOSE_NUMBER})$"
)
TIME_RE = re.compile(
    r"TIME expected_ms=([0-9]{1,18}) elapsed_ms=([0-9]{1,18})$"
)


class InputError(Exception):
    """A workload or command input is invalid."""


def fail(errs, category, tick, message):
    prefix = f"  [tick {tick}] " if tick is not None else "  "
    counts = errs.setdefault("_counts", {})
    counts[category] = counts.get(category, 0) + 1
    messages = errs.setdefault(category, [])
    if len(messages) < MAX_STORED_ERRORS:
        messages.append(prefix + message)


def error_count(errs, category):
    return errs.get("_counts", {}).get(category, 0)


def decimal(token, label, minimum=0):
    if (not re.fullmatch(r"[0-9]+", token)
            or len(token) > MAX_DECIMAL_DIGITS):
        raise InputError(f"{label}: expected a decimal integer, got '{token}'")
    try:
        value = int(token)
    except ValueError as exc:
        raise InputError(f"{label}: integer is too large") from exc
    if value < minimum:
        raise InputError(f"{label}: expected a value >= {minimum}, got {value}")
    return value


# ---------------------------------------------------------------- workload
def parse_workload(path):
    tasks = {}
    header_seen = False
    weights = None
    total_chars = 0
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            lineno = 0
            while True:
                # Bound the read itself so a newline-free input cannot be
                # materialized as one enormous Python string.
                raw = stream.readline(MAX_LINE_CHARS + 2)
                if not raw:
                    break
                lineno += 1
                total_chars += len(raw)
                if total_chars > MAX_WORKLOAD_CHARS:
                    raise InputError(
                        f"workload exceeds {MAX_WORKLOAD_CHARS} characters"
                    )

                content = raw
                if content.endswith("\n"):
                    content = content[:-1]
                    if content.endswith("\r"):
                        content = content[:-1]
                elif content.endswith("\r"):
                    content = content[:-1]
                if len(content) > MAX_LINE_CHARS:
                    raise InputError(
                        f"workload line {lineno} exceeds {MAX_LINE_CHARS} "
                        "characters"
                    )

                line = content.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split()

                if not header_seen:
                    if len(fields) != 2 or fields[0] != "tasks":
                        raise InputError(
                            f"workload line {lineno}: expected exactly "
                            f"'tasks {N_TASKS}'"
                        )
                    count = decimal(
                        fields[1],
                        f"workload line {lineno} task count",
                    )
                    if count != N_TASKS:
                        raise InputError(
                            f"workload line {lineno}: tasks must be "
                            f"{N_TASKS}, got {count}"
                        )
                    header_seen = True
                    continue

                if fields[0] == "weights":
                    if weights is not None:
                        raise InputError(
                            f"workload line {lineno}: duplicate weights "
                            "record"
                        )
                    if tasks:
                        raise InputError(
                            f"workload line {lineno}: weights must be the "
                            "second logical record"
                        )
                    if len(fields) != N_TASKS + 1:
                        raise InputError(
                            f"workload line {lineno}: expected exactly "
                            "'weights w0 w1 ... w9' as the second record"
                        )
                    weights = []
                    for task, token in enumerate(fields[1:]):
                        weight = decimal(
                            token,
                            f"workload line {lineno} weight for P{task}",
                            1,
                        )
                        if weight not in VTIME_WEIGHTS:
                            allowed = ", ".join(
                                str(value) for value in sorted(VTIME_WEIGHTS)
                            )
                            raise InputError(
                                f"workload line {lineno} weight for P{task}: "
                                f"expected one of {{{allowed}}}, got {weight}"
                            )
                        weights.append(weight)
                    if len(set(weights)) < 2:
                        raise InputError(
                            f"workload line {lineno}: weights must contain "
                            "at least two distinct values"
                        )
                    continue

                if len(fields) < 4 or fields[0] != "task":
                    raise InputError(
                        f"workload line {lineno}: expected a task record"
                    )
                idx = decimal(
                    fields[1], f"workload line {lineno} task index"
                )
                arrival = decimal(
                    fields[2], f"workload line {lineno} arrival"
                )
                npairs = decimal(
                    fields[3], f"workload line {lineno} npairs", 1
                )
                expected_fields = 4 + 2 * npairs
                if len(fields) != expected_fields:
                    raise InputError(
                        f"workload line {lineno}: npairs={npairs} requires "
                        f"{expected_fields} fields, got {len(fields)}"
                    )
                if idx >= N_TASKS:
                    raise InputError(
                        f"workload line {lineno}: task index {idx} is "
                        "outside 0..9"
                    )
                if idx in tasks:
                    raise InputError(
                        f"workload line {lineno}: duplicate task index {idx}"
                    )

                pairs = []
                for pair in range(npairs):
                    cpu = decimal(
                        fields[4 + 2 * pair],
                        f"workload line {lineno} cpu{pair + 1}",
                        1,
                    )
                    io = decimal(
                        fields[5 + 2 * pair],
                        f"workload line {lineno} io{pair + 1}",
                    )
                    pairs.append((cpu, io))
                tasks[idx] = (arrival, pairs)
    except (OSError, UnicodeError) as exc:
        raise InputError(f"cannot read workload '{path}': {exc}") from exc

    if not header_seen:
        raise InputError("workload: file contains no records")
    missing = sorted(set(range(N_TASKS)) - set(tasks))
    if missing:
        names = ", ".join(f"P{i}" for i in missing)
        raise InputError(f"workload: missing task record(s): {names}")
    return tasks, weights


def read_trace_lines(path, ticks_expected):
    """Read a byte-bounded trace without first allocating an oversized line."""
    lines = []
    total_bytes = 0
    max_lines = ticks_expected + N_TASKS + 4 + MAX_EXTRA_TRACE_LINES
    try:
        with open(path, "rb") as stream:
            lineno = 0
            while True:
                # The extra two bytes allow exactly MAX_TRACE_LINE_BYTES plus
                # CRLF.  A newline-free input is therefore rejected after a
                # bounded read instead of being materialized as one huge line.
                raw = stream.readline(MAX_TRACE_LINE_BYTES + 2)
                if not raw:
                    break
                lineno += 1
                if lineno > max_lines:
                    raise InputError(
                        f"trace has more than {max_lines} lines"
                    )
                total_bytes += len(raw)
                if total_bytes > MAX_TRACE_BYTES:
                    raise InputError(
                        f"trace exceeds {MAX_TRACE_BYTES} bytes"
                    )
                if raw.endswith(b"\n"):
                    raw = raw[:-1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                if len(raw) > MAX_TRACE_LINE_BYTES:
                    raise InputError(
                        f"trace line {lineno} exceeds "
                        f"{MAX_TRACE_LINE_BYTES} bytes"
                    )
                lines.append(raw.decode("utf-8"))
    except (OSError, UnicodeError) as exc:
        raise InputError(f"cannot read trace '{path}': {exc}") from exc
    return lines


# --------------------------------------------------------------- headers
def parse_kv_header(line, errs):
    if not line.startswith("#"):
        fail(errs, "FORMAT", None, "first line must start with '# '")
        return None

    tokens = line[1:].split()
    if not tokens:
        fail(errs, "FORMAT", None, "first header line contains no fields")
        return {}, set()
    if line != "# " + " ".join(tokens):
        fail(errs, "FORMAT", None,
             "header fields must be separated by one space")

    header = {}
    duplicates = set()
    for token in tokens:
        if token.count("=") != 1:
            fail(errs, "FORMAT", None,
                 f"header token must be key=value: '{token}'")
            continue
        key, value = token.split("=", 1)
        if not key or not value:
            fail(errs, "FORMAT", None, f"empty header key/value in '{token}'")
            continue
        if key in header:
            fail(errs, "FORMAT", None, f"duplicate header field '{key}'")
            duplicates.add(key)
            continue
        header[key] = value
    return header, duplicates


def header_decimal(header, key, errs, minimum=0):
    token = header.get(key)
    if token is None:
        return None
    if (not re.fullmatch(r"[0-9]+", token)
            or len(token) > MAX_DECIMAL_DIGITS):
        fail(errs, "FORMAT", None,
             f"header {key} must be a decimal integer, got '{token}'")
        return None
    try:
        value = int(token)
    except ValueError:
        fail(errs, "FORMAT", None, f"header {key} integer is too large")
        return None
    if value < minimum:
        fail(errs, "FORMAT", None,
             f"header {key} must be >= {minimum}, got {value}")
        return None
    return value


def parse_headers(lines, errs):
    if len(lines) < 2:
        fail(errs, "FORMAT", None, "missing the two header lines")
        return None

    parsed_header = parse_kv_header(lines[0], errs)
    if parsed_header is None:
        return None
    header, duplicates = parsed_header

    policy = header.get("policy")
    # Accept the policy case-insensitively: the spec uses lowercase policy
    # names for the CLI argument and schedule_dump.<policy>.txt filename, so
    # a student may echo argv into the header. Normalize to uppercase below.
    policy_norm = policy.upper() if isinstance(policy, str) else policy
    if (policy_norm not in ("RR", "MLFQ", "VTIME")
            or "policy" in duplicates):
        fail(errs, "FORMAT", None,
             "header policy must be RR, MLFQ, or VTIME "
             "(case-insensitive)")
        return None
    policy = policy_norm

    common = {"policy", "tick_ms", "workload", "tasks"}
    if policy == "RR":
        policy_fields = {"impl", "quantum"}
        optional_fields = set()
    elif policy == "MLFQ":
        policy_fields = {"levels", "quanta", "boost", "io_return"}
        optional_fields = {"impl"}
    else:
        policy_fields = {"quantum"}
        optional_fields = {"impl"}
    # MLFQ and VTIME imply the optional I/O implementation.  Accept
    # impl=optional as optional metadata for students who state it explicitly.
    required = common | policy_fields
    allowed = required | optional_fields
    missing = sorted(required - set(header))
    unknown = sorted(set(header) - allowed)
    for key in missing:
        fail(errs, "FORMAT", None, f"missing header field '{key}'")
    for key in unknown:
        fail(errs, "FORMAT", None, f"unknown header field '{key}'")

    tasks_count = header_decimal(header, "tasks", errs)
    tick_ms = header_decimal(header, "tick_ms", errs, 1)
    if tasks_count is not None and tasks_count != N_TASKS:
        fail(errs, "FORMAT", None,
             f"header tasks must be {N_TASKS}, got {tasks_count}")
    if tick_ms is not None and tick_ms > 10:
        fail(errs, "FORMAT", None,
             f"header tick_ms must be in 1..10, got {tick_ms}")
        tick_ms = None
    if "tick_ms" in duplicates:
        tick_ms = None

    config = {
        "policy": policy,
        "tick_ms": tick_ms,
        "workload": header.get("workload"),
    }

    if policy == "RR":
        impl = header.get("impl")
        quantum = header_decimal(header, "quantum", errs, 1)
        if impl is not None and impl not in ("basic", "optional"):
            fail(errs, "FORMAT", None,
                 f"header impl must be basic or optional, got '{impl}'")
            impl = None
        if "impl" in duplicates:
            impl = None
        if "quantum" in duplicates:
            quantum = None
        config.update({
            "impl": impl,
            "io_enabled": None if impl is None else impl == "optional",
            "nlevels": 1,
            "quanta": [quantum] if quantum is not None else None,
            "boost": 0,
            "io_return": "top",
        })
    elif policy == "MLFQ":
        nlevels = header_decimal(header, "levels", errs, 2)
        boost = header_decimal(header, "boost", errs)
        io_return = header.get("io_return")
        if io_return is not None and io_return not in ("same", "top"):
            fail(errs, "FORMAT", None,
                 "header io_return must be exactly 'same' or 'top'")
            io_return = None

        quanta = None
        raw_quanta = header.get("quanta")
        if raw_quanta is not None:
            parts = raw_quanta.split(",")
            if any(not re.fullmatch(r"[0-9]+", part)
                   or len(part) > MAX_DECIMAL_DIGITS for part in parts):
                fail(errs, "FORMAT", None,
                     "header quanta must be comma-separated decimal integers")
            else:
                try:
                    quanta = [int(part) for part in parts]
                except ValueError:
                    quanta = None
                    fail(errs, "FORMAT", None,
                         "a header quantum integer is too large")
                if quanta is not None and any(value < 1 for value in quanta):
                    fail(errs, "FORMAT", None,
                         "every MLFQ quantum must be >= 1")
                    quanta = None
                if (quanta is not None and nlevels is not None
                        and len(quanta) != nlevels):
                    fail(errs, "FORMAT", None,
                         f"levels={nlevels} but {len(quanta)} quanta supplied")
                    quanta = None
        if "levels" in duplicates:
            nlevels = None
        if "quanta" in duplicates:
            quanta = None
        if "boost" in duplicates:
            boost = None
        if "io_return" in duplicates:
            io_return = None
        if "impl" in header:  # optional metadata; MLFQ is an optional impl
            if header["impl"] != "optional":
                fail(errs, "FORMAT", None,
                     "header impl for MLFQ must be 'optional', got "
                     f"'{header['impl']}'")
        config.update({
            "impl": "optional",
            "io_enabled": True,
            "nlevels": nlevels,
            "quanta": quanta,
            "boost": boost,
            "io_return": io_return,
        })
    else:
        quantum = header_decimal(header, "quantum", errs, 1)
        if quantum is not None and quantum > 100:
            fail(errs, "FORMAT", None,
                 f"header VTIME quantum must be in 1..100, got {quantum}")
            quantum = None
        if "quantum" in duplicates:
            quantum = None
        if "impl" in header and header["impl"] != "optional":
            fail(errs, "FORMAT", None,
                 "header impl for VTIME must be 'optional', got "
                 f"'{header['impl']}'")
        config.update({
            "impl": "optional",
            "io_enabled": True,
            "nlevels": 1,
            "quanta": [quantum] if quantum is not None else None,
            "boost": 0,
            "io_return": "top",
        })

    if not lines[1].startswith("#"):
        fail(errs, "FORMAT", None, "second line must start with '# '")
    else:
        tokens = lines[1][1:].split()
        if lines[1] != "# " + " ".join(tokens):
            fail(errs, "FORMAT", None,
                 "pid mappings must be separated by one space")
        mappings = []
        valid = bool(tokens)
        if not tokens:
            fail(errs, "FORMAT", None,
                 "pid mapping line must contain P0 through P9")
        for token in tokens:
            match = re.fullmatch(r"P([0-9])=([1-9][0-9]{0,17})", token)
            if not match:
                fail(errs, "FORMAT", None,
                     f"bad pid mapping token '{token}'")
                valid = False
                continue
            mappings.append((int(match.group(1)), int(match.group(2))))
        if valid:
            indices = [idx for idx, _ in mappings]
            pids = [pid for _, pid in mappings]
            if indices != list(range(N_TASKS)):
                fail(errs, "FORMAT", None,
                     "pid mapping must list P0 through P9 exactly once in order")
            if len(set(pids)) != N_TASKS:
                fail(errs, "FORMAT", None,
                     "pid mapping must contain 10 distinct positive pids")

    # The policy and MLFQ level count determine the tick-line grammar.  Other
    # invalid declarations disable only the categories that depend on them;
    # STRUCTURE and independent summary checks can still run.
    if config["nlevels"] is None:
        return None
    return config


# ------------------------------------------------------------ trace parse
def parse_task_list(body, label, errs, tick):
    if body == "":
        return []
    tokens = body.split(" ")
    if any(not token for token in tokens):
        fail(errs, "FORMAT", tick,
             f"{label} entries must be separated by one space")
        return None
    result = []
    for token in tokens:
        match = TASK_RE.fullmatch(token)
        if not match:
            fail(errs, "FORMAT", tick, f"bad {label} task '{token}'")
            return None
        result.append(int(match.group(1)))
    return result


def parse_rq_field(field, config, errs, tick):
    if config["policy"] in ("RR", "VTIME"):
        match = re.fullmatch(r"RQ: \[([^\]]*)\]", field)
        if not match:
            fail(errs, "FORMAT", tick, f"bad RQ field '{field}'")
            return None
        queue = parse_task_list(match.group(1), "RQ", errs, tick)
        return None if queue is None else [queue]

    matches = []
    position = 0
    while position < len(field):
        match = RQ_LEVEL_RE.match(field, position)
        if not match:
            fail(errs, "FORMAT", tick, f"bad MLFQ RQ field '{field}'")
            return None
        raw_level = match.group(1)
        level = int(raw_level)
        if raw_level != str(level):
            fail(errs, "FORMAT", tick,
                 f"MLFQ level must use canonical decimal form: RQ{raw_level}")
            return None
        matches.append((level, match.group(2)))
        position = match.end()
        if position < len(field):
            if field[position] != " " or position + 1 == len(field):
                fail(errs, "FORMAT", tick,
                     f"bad MLFQ RQ separator in '{field}'")
                return None
            position += 1

    levels = [level for level, _ in matches]
    levels_valid = (
        len(levels) == config["nlevels"]
        and all(level == position for position, level in enumerate(levels))
    )
    if not levels_valid:
        fail(errs, "FORMAT", tick,
             "MLFQ RQ levels must be RQ0 through "
             f"RQ{config['nlevels'] - 1} exactly once in order; got {levels}")
        return None

    queues = []
    for level, body in matches:
        queue = parse_task_list(body, f"RQ{level}", errs, tick)
        if queue is None:
            return None
        queues.append(queue)
    return queues


def parse_wq_field(field, errs, tick):
    match = re.fullmatch(r"WQ: \[([^\]]*)\]", field)
    if not match:
        fail(errs, "FORMAT", tick, f"bad WQ field '{field}'")
        return None
    body = match.group(1)
    if body == "":
        return []
    tokens = body.split(" ")
    if any(not token for token in tokens):
        fail(errs, "FORMAT", tick,
             "WQ entries must be separated by one space")
        return None

    result = []
    for token in tokens:
        entry = WQ_ENTRY_RE.fullmatch(token)
        if not entry:
            fail(errs, "FORMAT", tick, f"bad WQ entry '{token}'")
            return None
        task, remaining = int(entry.group(1)), int(entry.group(2))
        result.append((task, remaining))
    return result


def parse_events(field, errs, tick):
    if field == "-":
        return []
    tokens = field.split(" ")
    if any(not token for token in tokens):
        fail(errs, "FORMAT", tick,
             "events must be separated by one space")
        return None
    events = []
    position = 0
    while position < len(tokens):
        kind = tokens[position]
        if kind == "BOOST":
            events.append((kind,))
            position += 1
            continue
        pattern = EVENT_ARG_RE.get(kind)
        if pattern is None or position + 1 >= len(tokens):
            fail(errs, "FORMAT", tick,
                 f"unparseable event sequence '{field}'")
            return None
        argument = pattern.fullmatch(tokens[position + 1])
        if not argument:
            fail(errs, "FORMAT", tick,
                 f"bad argument '{tokens[position + 1]}' for {kind}")
            return None
        values = tuple(int(value) for value in argument.groups())
        events.append((kind,) + values)
        position += 2
    return events


def parse_tick_line(line, sequence, config, errs):
    parts = line.split(" | ")
    if len(parts) != 4 or not parts[3].startswith("ev: "):
        fail(errs, "FORMAT", sequence,
             f"tick line must contain four ' | '-separated fields: '{line}'")
        return None

    run_match = HEAD_RUN_RE.fullmatch(parts[0])
    idle_match = HEAD_IDLE_RE.fullmatch(parts[0])
    if not run_match and not idle_match:
        fail(errs, "FORMAT", sequence, f"bad tick head '{parts[0]}'")
        return None

    head = run_match or idle_match
    printed_tick = int(head.group(1))
    if printed_tick != sequence:
        fail(errs, "FORMAT", sequence,
             f"tick number {printed_tick} is out of sequence")

    running = int(run_match.group(2)) if run_match else None
    q_left = int(run_match.group(3)) if run_match else None
    run_queues = parse_rq_field(parts[1], config, errs, sequence)
    wait_queue = parse_wq_field(parts[2], errs, sequence)
    events = parse_events(parts[3][4:], errs, sequence)
    if run_queues is None or wait_queue is None or events is None:
        return None
    return {
        "running": running,
        "q_left": q_left,
        "run_queues": run_queues,
        "wait_queue": wait_queue,
        "events": events,
    }


def parse_trace_body(lines, ticks_expected, config, errs):
    tick_lines = []
    summary_lines = []
    in_summary = False

    for file_lineno, line in enumerate(lines[2:], 3):
        if not line.strip():
            continue  # ignore blank/whitespace-only lines (e.g. a trailing
            # newline at EOF); the tick count and summary are still validated
        if line.startswith("["):
            if in_summary:
                fail(errs, "FORMAT", None,
                     f"tick line appears after summary at file line {file_lineno}")
            tick_lines.append(line)
        else:
            in_summary = True
            summary_lines.append(line)

    count_matches = len(tick_lines) == ticks_expected
    if not count_matches:
        fail(errs, "FORMAT", None,
             f"found {len(tick_lines)} tick lines, expected {ticks_expected}")

    parsed = []
    parse_ok = count_matches
    for sequence, line in enumerate(tick_lines):
        record = parse_tick_line(line, sequence, config, errs)
        if record is None:
            parse_ok = False
        else:
            parsed.append(record)
    return parsed, summary_lines, parse_ok, len(tick_lines)


# -------------------------------------------------------------- semantics
def validate_structure(ticks, tasks, errs):
    """Check task placement without requiring scheduling declarations."""
    for tick, printed in enumerate(ticks):
        places = {}
        if printed["running"] is not None:
            task = printed["running"]
            places[task] = places.get(task, 0) + 1
        for queue in printed["run_queues"]:
            for task in queue:
                places[task] = places.get(task, 0) + 1
        for task, _ in printed["wait_queue"]:
            places[task] = places.get(task, 0) + 1

        for task in tasks:
            count = places.get(task, 0)
            has_arrived = tasks[task][0] <= tick
            if has_arrived and count != 1:
                fail(errs, "STRUCTURE", tick,
                     f"P{task} appears {count} times")
            if not has_arrived and count != 0:
                fail(errs, "STRUCTURE", tick,
                     f"P{task} appears before its arrival")


def compare_replay_events(actual, expected, errs, tick):
    """Compare scheduler-controlled events against the canonical replay.

    BURST/IOREQ and PREEMPT correctness are checked from the student's
    printed execution by their own validators.  Keeping those models
    separate prevents an ORDER error from destroying independent partial
    credit.
    """
    other_kinds = {"BOOST", "IODONE", "ARRIVE"}
    actual_other = [event for event in actual if event[0] in other_kinds]
    expected_other = [event for event in expected if event[0] in other_kinds]
    if actual_other != expected_other:
        fail(errs, "ORDER", tick,
             f"scheduler events {actual_other}, expected {expected_other}")

    phase = {
        "BOOST": 0,
        "IODONE": 1,
        "IOREQ": 2,
        "BURST": 2,
        "ARRIVE": 3,
        "PREEMPT": 4,
    }
    ranks = [phase[event[0]] for event in actual]
    if ranks != sorted(ranks):
        fail(errs, "ORDER", tick,
             f"events are out of processing order: {actual}")


def validate_observed_quantum(ticks, tasks, config, errs):
    """Validate q_left and PREEMPT from the execution the student printed.

    ORDER has a separate canonical replay.  This local model deliberately
    does not assume that the student selected the correct task: a wrong task
    can still obey its declared quantum and earn QUANTUM partial credit.
    For MLFQ, the reset quantum is derived from the levels visible in the
    student's prior RQ/WQ/RUN state.  When an ORDER error makes that source
    ambiguous, all plausible levels are retained instead of choosing the
    canonical task or level.  RR and VTIME each have one declared quantum.
    """
    nlevels = config["nlevels"]
    quanta = config["quanta"]
    all_levels = set(range(nlevels))
    previous_running = None
    previous_preempted = False
    previous_states = set()
    previous_rq_levels = {}
    previous_wq_tasks = set()
    return_levels = {}
    last_run_levels = {}

    for tick, printed in enumerate(ticks):
        boost_due = (
            config["policy"] == "MLFQ"
            and config["boost"] > 0
            and tick > 0
            and tick % config["boost"] == 0
        )

        def after_current_boost(levels):
            levels = set(levels)
            if not levels:
                return set()
            if boost_due:
                return {0}
            return levels

        boosted_previous_states = set()
        for level, expected_after in previous_states:
            for boosted_level in after_current_boost({level}):
                boosted_previous_states.add(
                    (boosted_level, expected_after)
                )
        for task in list(return_levels):
            return_levels[task] = after_current_boost(return_levels[task])
        for task in list(last_run_levels):
            last_run_levels[task] = after_current_boost(
                last_run_levels[task]
            )

        running = printed["running"]
        q_left = printed["q_left"]
        actual_preempt = [event for event in printed["events"]
                          if event[0] == "PREEMPT"]

        if running is not None:
            same_owner = running == previous_running
            current_states = set()

            # A continuing epoch decrements its normative value, not the
            # student's preceding q_left.  One typo therefore does not steer
            # the checker or create a cascade on the next line.
            if same_owner:
                for level, expected_after in boosted_previous_states:
                    if expected_after > 0:
                        current_states.add((level, expected_after - 1))

            new_dispatch_possible = (
                not same_owner
                or not previous_states
                or previous_preempted
                or any(expected_after == 0
                       for _, expected_after in previous_states)
            )
            if new_dispatch_possible:
                candidates = set()
                if tick == 0 and tasks[running][0] == 0:
                    candidates.add(0)
                candidates.update(after_current_boost(
                    previous_rq_levels.get(running, set())
                ))
                if same_owner:
                    expiring_levels = {
                        level for level, expected_after in previous_states
                        if expected_after == 0 or previous_preempted
                    }
                    demoted = {
                        (min(level + 1, nlevels - 1)
                         if config["policy"] == "MLFQ" else 0)
                        for level in expiring_levels
                    }
                    candidates.update(after_current_boost(demoted))
                if running in previous_wq_tasks:
                    candidates.update(
                        return_levels.get(running, all_levels)
                    )
                if tasks[running][0] == tick:
                    candidates.add(0)
                if not candidates:
                    candidates = set(all_levels)

                current_states.update(
                    (level, quanta[level] - 1) for level in candidates
                )

            if not current_states:
                current_states = {
                    (level, quanta[level] - 1) for level in all_levels
                }

            expected_values = sorted({expected_after
                                      for _, expected_after in current_states})
            if q_left not in expected_values:
                fail(errs, "QUANTUM", tick,
                     f"q_left={q_left}, expected one of {expected_values} "
                     "from the observed dispatch/continuation")
            else:
                current_states = {
                    state for state in current_states
                    if state[1] == q_left
                }

            preempt_options = []
            expected_values = {expected_after
                               for _, expected_after in current_states}
            if any(value != 0 for value in expected_values):
                preempt_options.append([])
            if 0 in expected_values:
                preempt_options.append([("PREEMPT", running)])
            if actual_preempt not in preempt_options:
                fail(errs, "QUANTUM", tick,
                     f"preemption events {actual_preempt}, expected one "
                     f"of {preempt_options} from the observed quantum")

            current_run_levels = {level for level, _ in current_states}
            last_run_levels[running] = set(current_run_levels)
        else:
            current_states = set()
            if actual_preempt:
                fail(errs, "QUANTUM", tick,
                     f"preemption events {actual_preempt}, expected [] "
                     "on an IDLE line")

        current_wq_tasks = {task for task, _ in printed["wait_queue"]}
        for task in current_wq_tasks - previous_wq_tasks:
            if config["io_return"] == "top":
                return_levels[task] = {0}
            else:
                return_levels[task] = set(
                    last_run_levels.get(task, all_levels)
                )

        current_rq_levels = {}
        for level, queue in enumerate(printed["run_queues"]):
            for task in queue:
                current_rq_levels.setdefault(task, set()).add(level)

        previous_running = running
        previous_preempted = (
            running is not None
            and ("PREEMPT", running) in actual_preempt
        )
        previous_states = current_states
        previous_rq_levels = current_rq_levels
        previous_wq_tasks = current_wq_tasks


def validate_observed_bursts(ticks, tasks, config, errs):
    """Validate burst and I/O timing from the student's printed RUN slots."""
    burst_left = {task: tasks[task][1][0][0] for task in tasks}
    pair_index = {task: 0 for task in tasks}
    pending = {}
    io_end = {}
    io_order = []
    burst_kinds = {"BURST", "IOREQ"}

    for tick, printed in enumerate(ticks):
        completed_io = [task for task in io_order if io_end[task] == tick]
        for task in completed_io:
            io_order.remove(task)
            del io_end[task]

        expected = []
        due = sorted(
            (task, event) for task, event in pending.items()
            if event[0] == tick
        )
        for task, (_, kind, io_value) in due:
            del pending[task]
            if kind == "IOREQ":
                expected.append(("IOREQ", task, io_value))
                if task in io_end:
                    fail(errs, "BURST", tick,
                         f"P{task} issued I/O while already waiting")
                else:
                    io_end[task] = tick + io_value
                    io_order.append(task)
            else:
                expected.append(("BURST", task))

        actual = [event for event in printed["events"]
                  if event[0] in burst_kinds]
        if actual != expected:
            fail(errs, "BURST", tick,
                 f"burst events {actual}, expected {expected}")

        expected_wq = {task: io_end[task] - tick for task in io_order}
        printed_wq_tasks = {task for task, _ in printed["wait_queue"]}
        if printed_wq_tasks != set(expected_wq):
            fail(errs, "BURST", tick,
                 f"WQ tasks {sorted(printed_wq_tasks)}, expected "
                 f"{sorted(expected_wq)}")
        bad_countdowns = [
            (task, remaining, expected_wq[task])
            for task, remaining in printed["wait_queue"]
            if task in expected_wq and remaining != expected_wq[task]
        ]
        if bad_countdowns:
            fail(errs, "BURST", tick,
                 "wrong WQ countdown(s) "
                 f"(task, printed, expected): {bad_countdowns}")

        running = printed["running"]
        if running is None:
            continue
        burst_left[running] -= 1
        if burst_left[running] == 0:
            pair = pair_index[running]
            io_value = tasks[running][1][pair][1]
            kind = ("IOREQ" if config["io_enabled"] and io_value > 0
                    else "BURST")
            if running in pending:
                fail(errs, "BURST", tick,
                     f"P{running} completed another burst before its prior "
                     "event was consumed")
            pending[running] = (tick + 1, kind, io_value)
            next_pair = (pair + 1) % len(tasks[running][1])
            pair_index[running] = next_pair
            burst_left[running] = tasks[running][1][next_pair][0]


def compute_observed_stats(ticks, ticks_expected):
    """Recompute summary values from the student's printed tick lines."""
    total_wait = {task: 0 for task in range(N_TASKS)}
    total_cpu = {task: 0 for task in range(N_TASKS)}
    total_io = {task: 0 for task in range(N_TASKS)}
    response_pending = {}
    response_sum = 0
    response_count = 0
    busy = 0

    for tick, printed in enumerate(ticks):
        for event in printed["events"]:
            if event[0] == "IODONE":
                response_pending[event[1]] = tick

        running = printed["running"]
        if running is not None:
            busy += 1
            total_cpu[running] += 1
            if running in response_pending:
                response_sum += tick - response_pending.pop(running)
                response_count += 1

        waiting = {task for queue in printed["run_queues"] for task in queue}
        doing_io = {task for task, _ in printed["wait_queue"]}
        for task in waiting:
            total_wait[task] += 1
        for task in doing_io:
            total_io[task] += 1

    return {
        "total_wait": total_wait,
        "total_cpu": total_cpu,
        "total_io": total_io,
        "busy": busy,
        "response_sum": response_sum,
        "response_count": response_count,
        "ticks_expected": ticks_expected,
    }


def replay_trace(ticks, tasks, weights, config, errs):
    nlevels = config["nlevels"]
    quanta = config["quanta"]
    is_vtime = config["policy"] == "VTIME"
    run_queues = [[] for _ in range(nlevels)]
    wait_queue = []
    io_remaining = {}
    return_level = {}
    dispatch_level = {}
    running = None
    q_left = 0
    pending_preempt = None
    vruntime = {task: 0 for task in tasks}
    min_vruntime = 0

    def enqueue_ready(task, level=0):
        """Insert into a policy's canonical ready-queue order."""
        queue = run_queues[level]
        if not is_vtime:
            queue.append(task)
            return

        # Stable virtual-time order: a new entry goes after all existing
        # entries with the same vruntime, preserving ready-entry order as the
        # deterministic tie breaker.
        position = len(queue)
        for index, queued_task in enumerate(queue):
            if vruntime[task] < vruntime[queued_task]:
                position = index
                break
        queue.insert(position, task)

    initially_arrived = [task for task in sorted(tasks)
                         if tasks[task][0] == 0]
    for task in initially_arrived:
        enqueue_ready(task)

    burst_left = {task: tasks[task][1][0][0] for task in tasks}
    pair_index = {task: 0 for task in tasks}
    pending_burst = {}
    wait_entry = {}

    for tick, printed in enumerate(ticks):
        expected_events = []

        # Previous tick's PREEMPT becomes visible in the queues now.
        if pending_preempt is not None:
            task, level = pending_preempt
            next_level = min(level + 1, nlevels - 1)
            if config["policy"] != "MLFQ":
                next_level = 0
            enqueue_ready(task, next_level)
            dispatch_level[task] = level
            pending_preempt = None

        # VTIME's floor advances before this tick's I/O completions and
        # arrivals.  Sleeping and not-yet-arrived tasks do not hold it back.
        # A returning/new task is clamped to this monotonic floor below.
        if is_vtime:
            active = [task for queue in run_queues for task in queue]
            if running is not None:
                active.append(running)
            if active:
                min_vruntime = max(
                    min_vruntime,
                    min(vruntime[task] for task in active),
                )

        # Step 0: optional MLFQ priority boost.
        if (config["policy"] == "MLFQ" and config["boost"] > 0
                and tick > 0 and tick % config["boost"] == 0):
            expected_events.append(("BOOST",))
            merged = [task for queue in run_queues for task in queue]
            run_queues = [[] for _ in range(nlevels)]
            run_queues[0] = merged
            for task in tasks:
                return_level[task] = 0
                dispatch_level[task] = 0

        # Step 1: wait-queue countdown and completion in entry order.
        completed = []
        for task in wait_queue:
            io_remaining[task] -= 1
            if io_remaining[task] == 0:
                completed.append(task)
        for task in completed:
            expected_events.append(("IODONE", task))
            wait_queue.remove(task)
            level = return_level.get(task, 0) if nlevels > 1 else 0
            if is_vtime:
                vruntime[task] = max(vruntime[task], min_vruntime)
            enqueue_ready(task, level)
            entered, io_value = wait_entry.pop(task)
            if tick - entered != io_value:
                fail(errs, "BURST", tick,
                     f"internal replay error for P{task} I/O duration")

        # Step 2: every burst completion due now is mandatory. Apply the
        # workload-derived event even when the printed trace omits it, so a
        # bad event cannot steer the independent replay state.
        due_bursts = sorted(
            (task, event) for task, event in pending_burst.items()
            if event[0] == tick
        )
        overdue = sorted(
            task for task, event in pending_burst.items() if event[0] < tick
        )
        for task in overdue:
            fail(errs, "BURST", tick,
                 f"expected burst event for P{task} was not resolved")
            del pending_burst[task]

        for task, (_, kind, io_value) in due_bursts:
            del pending_burst[task]
            event = (("IOREQ", task, io_value) if kind == "IOREQ"
                     else ("BURST", task))
            expected_events.append(event)
            if kind == "IOREQ":
                if running == task:
                    running = None
                else:
                    found = False
                    for queue in run_queues:
                        if task in queue:
                            queue.remove(task)
                            found = True
                            break
                    if not found:
                        fail(errs, "ORDER", tick,
                             f"P{task} completed a burst outside RUN/RQ")
                wait_queue.append(task)
                io_remaining[task] = io_value
                wait_entry[task] = (tick, io_value)
                if config["io_return"] == "top":
                    return_level[task] = 0
                else:
                    return_level[task] = dispatch_level.get(task, 0)

        # Step 3: arrivals in ascending task-index order.
        arrivals = [task for task in sorted(tasks)
                    if tick > 0 and tasks[task][0] == tick]
        for task in arrivals:
            expected_events.append(("ARRIVE", task))
            if is_vtime:
                vruntime[task] = max(vruntime[task], min_vruntime)
            enqueue_ready(task)

        # Step 4: dispatch the head of the policy-ordered ready queue (from
        # the highest non-empty level for MLFQ).
        if running is None:
            for level, queue in enumerate(run_queues):
                if queue:
                    running = queue.pop(0)
                    q_left = quanta[level]
                    dispatch_level[running] = level
                    break

        # Step 5: deliver one slice.
        if running is not None:
            q_left -= 1
            burst_left[running] -= 1
            if is_vtime:
                vruntime[running] += VTIME_SCALE // weights[running]
            if burst_left[running] == 0:
                pair = pair_index[running]
                io_value = tasks[running][1][pair][1]
                kind = ("IOREQ" if config["io_enabled"] and io_value > 0
                        else "BURST")
                if running in pending_burst:
                    fail(errs, "BURST", tick,
                         f"a new completion overwrote an event for P{running}")
                pending_burst[running] = (tick + 1, kind, io_value)
                next_pair = (pair + 1) % len(tasks[running][1])
                pair_index[running] = next_pair
                burst_left[running] = tasks[running][1][next_pair][0]

        # Compare the printed state before applying step 6's queue move.
        if printed["running"] != running:
            shown = (f"P{printed['running']}" if printed["running"] is not None
                     else "IDLE")
            wanted = f"P{running}" if running is not None else "IDLE"
            fail(errs, "ORDER", tick,
                 f"line says RUN {shown}, replay says {wanted}")

        if printed["run_queues"] != run_queues:
            fail(errs, "ORDER", tick,
                 f"RQ {printed['run_queues']} != replay {run_queues}")
        printed_wq_tasks = [task for task, _ in printed["wait_queue"]]
        if printed_wq_tasks != wait_queue:
            fail(errs, "ORDER", tick,
                 f"WQ task order {printed_wq_tasks} != replay {wait_queue}")

        # Step 6: PREEMPT is the last event; move on the next line.
        if running is not None and q_left == 0:
            expected_events.append(("PREEMPT", running))
        compare_replay_events(printed["events"], expected_events, errs, tick)
        if running is not None and q_left == 0:
            pending_preempt = (running, dispatch_level.get(running, 0))
            running = None


# --------------------------------------------------------------- summary
def parse_summary(lines, errs):
    waits = {}
    metric = None
    timing = None
    order = []

    for line in lines:
        wait_match = WAIT_RE.fullmatch(line)
        metric_match = METRIC_LOOSE_RE.fullmatch(line)
        time_match = TIME_RE.fullmatch(line)
        if wait_match:
            task = int(wait_match.group(1))
            order.append(f"WAIT P{task}")
            if task in waits:
                fail(errs, "FORMAT", None, f"duplicate WAIT line for P{task}")
            else:
                waits[task] = tuple(
                    int(wait_match.group(index)) for index in (2, 3, 4)
                )
        elif metric_match:
            order.append("METRIC")
            if metric is not None:
                fail(errs, "FORMAT", None, "duplicate METRIC line")
            else:
                metric = metric_match.groups()
                if METRIC_RE.fullmatch(line) is None:
                    fail(errs, "FORMAT", None,
                         "METRIC values must use %.2f, %.2f, %.4f, %.4f")
        elif time_match:
            order.append("TIME")
            if timing is not None:
                fail(errs, "FORMAT", None, "duplicate TIME line")
            else:
                timing = (int(time_match.group(1)), int(time_match.group(2)))
        else:
            fail(errs, "FORMAT", None,
                 f"unrecognized summary line '{line}'")

    expected_order = [f"WAIT P{task}" for task in range(N_TASKS)]
    expected_order.extend(("METRIC", "TIME"))
    if order != expected_order:
        fail(errs, "FORMAT", None,
             "summary must contain WAIT P0..P9, METRIC, TIME exactly once "
             "and in that order")
    return waits, metric, timing


def check_summary(parsed, stats, config, errs):
    waits, metric, timing = parsed
    for task in range(N_TASKS):
        wanted = (
            stats["total_wait"][task],
            stats["total_cpu"][task],
            stats["total_io"][task],
        )
        if task not in waits:
            fail(errs, "ACCOUNTING", None, f"missing WAIT line for P{task}")
        elif waits[task] != wanted:
            fail(errs, "ACCOUNTING", None,
                 f"WAIT P{task}: printed {waits[task]}, recomputed {wanted}")

    sum_cpu = sum(stats["total_cpu"].values())
    sum_cpu_squared = sum(value * value
                          for value in stats["total_cpu"].values())
    ticks_expected = stats["ticks_expected"]
    io_response = (
        stats["response_sum"] / stats["response_count"]
        if stats["response_count"] else 0.0
    )
    fairness = (
        (sum_cpu * sum_cpu) / (N_TASKS * sum_cpu_squared)
        if sum_cpu else 1.0
    )
    expected_metric = (
        f"{sum(stats['total_wait'].values()) / N_TASKS:.2f}",
        f"{io_response:.2f}",
        f"{fairness:.4f}",
        f"{stats['busy'] / ticks_expected:.4f}",
    )
    if metric is None:
        fail(errs, "ACCOUNTING", None, "missing or malformed METRIC line")
    elif (tuple(Decimal(value) for value in metric)
          != tuple(Decimal(value) for value in expected_metric)):
        fail(errs, "ACCOUNTING", None,
             f"METRIC printed {metric}, recomputed {expected_metric}")

    if timing is None:
        fail(errs, "ACCOUNTING", None, "missing or malformed TIME line")
    elif config["tick_ms"] is not None:
        expected_ms = config["tick_ms"] * ticks_expected
        if timing[0] != expected_ms:
            fail(errs, "ACCOUNTING", None,
                 f"expected_ms={timing[0]}, should be {expected_ms}")
        if timing[1] < 0.9 * expected_ms:
            fail(errs, "ACCOUNTING", None,
                 f"elapsed_ms={timing[1]} is implausibly small")


# ---------------------------------------------------------------- report
def report(trace_path, workload_path, config, tick_count, errs, skipped):
    if config is None:
        description = f"ticks={tick_count}"
    else:
        impl = config["impl"] if config["impl"] is not None else "?"
        description = (
            f"policy={config['policy']} impl={impl} ticks={tick_count}"
        )
    print(f"check_trace: {trace_path} vs {workload_path} ({description})")

    bad = bool(skipped)
    for category in CATEGORIES:
        errors = errs.get(category, [])
        count = error_count(errs, category)
        if count:
            bad = True
            plural = "s" if count != 1 else ""
            print(f"  {category:<11} FAIL ({count} error{plural})")
            for error in errors[:MAXERR]:
                print(error)
            if count > MAXERR:
                print(f"  ... and {count - MAXERR} more")
        elif category in skipped:
            print(f"  {category:<11} SKIP "
                  "(required input is missing, invalid, or unparseable)")
        else:
            print(f"  {category:<11} PASS")
    return 1 if bad or any(error_count(errs, category)
                           for category in CATEGORIES) else 0


def tick_count_argument(token):
    if not re.fullmatch(r"[0-9]{1,5}", token):
        raise argparse.ArgumentTypeError(
            "tick count must be a decimal integer in 1..10000"
        )
    value = int(token)
    if not 1 <= value <= DEFAULT_TICKS:
        raise argparse.ArgumentTypeError("tick count must be in 1..10000")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", help="schedule_dump.<policy>.txt")
    parser.add_argument("workload", help="workload input used for the run")
    parser.add_argument(
        "--ticks",
        type=tick_count_argument,
        default=DEFAULT_TICKS,
        metavar="N",
        help="expected tick-line count (staff/testing only; default: 10000)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        tasks, weights = parse_workload(args.workload)
        lines = read_trace_lines(args.trace, args.ticks)
    except (InputError, OSError, UnicodeError) as exc:
        print(f"check_trace: INPUT FAIL: {exc}", file=sys.stderr)
        return 1

    errs = {}
    config = parse_headers(lines, errs)
    skipped = set()
    tick_count = 0

    if (config is not None and config["policy"] == "VTIME"
            and weights is None):
        print("check_trace: INPUT FAIL: workload: VTIME requires a weights "
              "record", file=sys.stderr)
        return 1

    if config is None:
        skipped.update(CATEGORIES[1:])
        if len(lines) >= 2:
            tick_count = sum(line.startswith("[") for line in lines[2:])
        return report(args.trace, args.workload, config, tick_count,
                      errs, skipped)

    ticks, summary_lines, ticks_parseable, raw_tick_count = parse_trace_body(
        lines, args.ticks, config, errs
    )
    tick_count = raw_tick_count
    summary = parse_summary(summary_lines, errs)

    if not ticks_parseable:
        skipped.update(CATEGORIES[1:])
    else:
        validate_structure(ticks, tasks, errs)

        quantum_ready = all(
            config[key] is not None
            for key in ("quanta", "boost", "io_return")
        )
        burst_ready = config["io_enabled"] is not None
        if quantum_ready:
            validate_observed_quantum(ticks, tasks, config, errs)
        else:
            skipped.add("QUANTUM")
        if burst_ready:
            validate_observed_bursts(ticks, tasks, config, errs)
        else:
            skipped.add("BURST")
        if quantum_ready and burst_ready:
            replay_trace(ticks, tasks, weights, config, errs)
        else:
            skipped.add("ORDER")

        stats = compute_observed_stats(ticks, args.ticks)
        check_summary(summary, stats, config, errs)
        if config["tick_ms"] is None:
            skipped.add("ACCOUNTING")

    return report(args.trace, args.workload, config, tick_count, errs, skipped)


if __name__ == "__main__":
    sys.exit(main())
