#!/usr/bin/env python3
"""retro - facts for a taskboard retrospective, mined from log.jsonl and board.md. Reads only.

Default inputs: <git-common-dir>/taskboard/log.jsonl and board.md. Prints Markdown tables: events,
owners, refusals, gate latency, idle-ready gaps, stale task text and the PRs referenced.
Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys

GATE_KINDS = ("owner", "reader", "decision")
# old logs keep the blocked text under "note"; "coordinator" is the owner under another name
GATE = re.compile(r"^\s*(?:awaiting\s+(owner|coordinator|reader|decision)\b|(owner|coordinator|reader|decision)\s*:)", re.I)
WAIT = re.compile(r"^\s*(?:awaiting|paused|blocked)\b", re.I)
PR = re.compile(r"(?:#|\bPR\s*#?)(\d+)\b")
ID = re.compile(r"\b(?:TB|P)-\d+\b")
HISTORY = ("Progress", "Handoff", "Evidence", "Notes")
FMT = "%Y-%m-%dT%H:%M:%SZ"


def when(s: str) -> dt.datetime:
    """ISO UTC timestamp or date; a date alone means midnight."""
    t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def hours(a: dt.datetime, b: dt.datetime) -> str:
    return f"{(b - a).total_seconds() / 3600:.1f}"


def cell(text: str, width: int = 60) -> str:
    text = " ".join(str(text).split())
    return (text[:width - 3] + "..." if len(text) > width else text).replace("|", "\\|")


def table(head: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["none"]
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(map(str, r)) + " |" for r in rows]


def parse_board(text: str) -> tuple[dict, dict]:
    """-> depends_on per task, and the current text of open tasks without their history subsections."""
    deps: dict[str, list[str]] = {}
    open_text: dict[str, list[str]] = {}
    section = tid = None
    history = False
    for line in text.splitlines():
        if line.startswith("## "):
            section, tid = line[3:].strip(), None
        elif line.startswith("### "):
            m = re.match(r"### ((?:TB|P)-\d+)\b", line)
            tid, history = (m.group(1) if m else None), False
            if tid and section in ("Todo", "In progress"):
                open_text[tid] = [line]
        elif tid:
            if line.startswith("- depends_on:"):
                deps[tid] = [d.strip() for d in line.split(":", 1)[1].split(",") if d.strip() not in ("", "none")]
            if line.startswith("#### "):
                history = line[5:].strip() in HISTORY
            elif not history and tid in open_text:
                open_text[tid].append(line)
    return deps, open_text


def gates(events: list[dict]) -> list[dict]:
    """Owner, reader and decision gates from both log formats: typed keys when logged, else the note prefix."""
    out: list[dict] = []
    current: dict[str, dict] = {}
    typed = False  # once a log carries the "blocked" key, notes are only notes

    def close(tid: str, at: dt.datetime, since: str | None = None) -> None:
        g = current.pop(tid, None)
        if g:
            g["cleared"] = at
            if since:
                g["since"] = when(since)

    def start(tid: str, kind: str, text: str, at: dt.datetime) -> None:
        g = current.get(tid)
        if g and g["text"] == text:
            return  # the same gate posted again keeps its start
        close(tid, at)
        current[tid] = {"id": tid, "kind": kind, "text": text, "since": at, "cleared": None}
        out.append(current[tid])

    for e in events:
        tid, at = e.get("id"), e["_t"]
        if not tid or "exit" in e:
            continue
        if "gate_cleared_at" in e:
            close(tid, when(e["gate_cleared_at"]), e.get("gate_since"))
        if "blocked" in e:
            typed = True
            kind = str(e.get("waiting_on", "")).replace("coordinator", "owner")
            if kind in GATE_KINDS:
                start(tid, kind, e["blocked"].splitlines()[0], at)  # gate_since here may be the replaced gate's
            else:
                close(tid, at)
        elif e["cmd"] in ("complete", "release", "close"):
            close(tid, at)
        elif e["cmd"] == "progress" and not typed:
            note = str(e.get("note", ""))
            m = GATE.match(note)
            if m:
                start(tid, (m.group(1) or m.group(2)).lower().replace("coordinator", "owner"), note.splitlines()[0], at)
            elif note.strip().lower() == "none" or WAIT.match(note):
                close(tid, at)
    return out


def idle_ready(events: list[dict], deps: dict, gap_min: float, until: dt.datetime | None) -> list[tuple]:
    """Silences longer than gap_min between log events while some Todo task had all its dependencies done."""
    where: dict[str, str] = {}
    done: set[str] = set()
    out = []
    for i, e in enumerate(events):
        if e.get("id") and "to" in e and "exit" not in e:
            where[e["id"]] = e["to"]
            if e["to"] == "Done":
                done.add(e["id"])
        nxt = events[i + 1]["_t"] if i + 1 < len(events) else until
        if nxt is None or (nxt - e["_t"]).total_seconds() <= gap_min * 60:
            continue
        ready = sorted(t for t, s in where.items() if s == "Todo" and t.startswith("TB-")
                       and all(d in done for d in deps.get(t, [])))
        if ready:
            out.append((e["_t"], nxt, ready))
    return out


def reason_key(e: dict) -> str:
    r = ID.sub("ID", str(e.get("reason", "")))
    if e.get("owner"):
        r = r.replace(e["owner"], "OWNER")
    r = re.sub(r"board\.md:\d+", "board.md:N", r)
    return " ".join(r.split()[:4]).rstrip(";:,.")


def default_paths() -> tuple[str, str]:
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True, check=False)
    if r.returncode:
        raise SystemExit(f"retro: {r.stderr.strip() or 'not in a git repository'}; pass --log and --board")
    d = os.path.join(r.stdout.strip(), "taskboard")
    return os.path.join(d, "log.jsonl"), os.path.join(d, "board.md")


def report(events: list[dict], board: str, args) -> list[str]:
    lo = when(args.since) if args.since else None
    hi = when(args.until) if args.until else None
    inside = lambda t: (lo is None or t >= lo) and (hi is None or t <= hi)
    overlaps = lambda a, b: (hi is None or a <= hi) and (lo is None or b is None or b >= lo)
    win = [e for e in events if inside(e["_t"])]
    deps, open_text = parse_board(board)
    span = f"{win[0]['ts']} to {win[-1]['ts']}" if win else "no events"
    out = [f"# Taskboard facts: {span}", "", f"{len(win)} of {len(events)} log events in the window.", ""]

    out += ["## Events by command", ""]
    out += table(["cmd", "count"], collections.Counter(e["cmd"] for e in win).most_common())

    out += ["", "## Owners", ""]
    owners: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for e in win:
        c = owners[e.get("owner", "?")]
        c["events"] += 1
        c["refusals" if "exit" in e else e["cmd"]] += 1
    out += table(["owner", "events", "claims", "progress", "completions", "closes", "refusals"],
                 [[o, c["events"], c["claim"], c["progress"], c["complete"], c["close"], c["refusals"]]
                  for o, c in sorted(owners.items(), key=lambda kv: -kv[1]["events"])])

    out += ["", "## Refusals", ""]
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for e in win:
        if "exit" in e:
            groups[reason_key(e)].append(e)
    out += table(["reason", "count", "example"],
                 [[cell(k), len(v), cell(f"{v[0].get('cmd')} {v[0].get('id', '')}: {v[0].get('reason', '')}", 100)]
                  for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))])

    out += ["", "## Gate latency (owner, reader, decision)", ""]
    gs = [g for g in gates(events) if overlaps(g["since"], g["cleared"])]
    out += table(["task", "kind", "text", "since", "cleared", "hours"],
                 [[g["id"], g["kind"], cell(g["text"]), g["since"].strftime(FMT),
                   g["cleared"].strftime(FMT) if g["cleared"] else "open",
                   hours(g["since"], g["cleared"]) if g["cleared"] else "-"] for g in gs])
    closed = sorted((g["cleared"] - g["since"]).total_seconds() / 3600 for g in gs if g["cleared"])
    if closed:
        out += ["", f"{len(closed)} cleared, {len(gs) - len(closed)} open; median {closed[len(closed) // 2]:.1f}h, "
                    f"max {closed[-1]:.1f}h."]

    out += ["", f"## Idle-ready gaps (no log event for over {args.gap_min:g} min while work was ready)", ""]
    rows = []
    for a, b, ready in idle_ready(events, deps, args.gap_min, hi):
        if overlaps(a, b):
            a, b = max(a, lo) if lo else a, min(b, hi) if hi else b
            rows.append([a.strftime(FMT), b.strftime(FMT), hours(a, b), ", ".join(ready)])
    out += table(["start", "end", "hours", "ready"], rows)

    out += ["", "## Stale task text (open task names a PR that evidence or a note says is merged)", ""]
    merged: dict[str, str] = {}
    for e in events:
        text = f"{e.get('note', '')}\n{e.get('blocked', '')}"
        if "exit" not in e and (e["cmd"] == "complete" or "merged" in text.lower()):
            for pr in PR.findall(text):
                merged.setdefault(pr, f"{e.get('id', '')} {e['cmd']} {e['ts']}")
    out += table(["task", "pr", "evidence"],
                 [[tid, f"#{pr}", merged[pr]] for tid, lines in sorted(open_text.items())
                  for pr in dict.fromkeys(PR.findall("\n".join(lines))) if pr in merged])

    prs = set(PR.findall(board))
    for e in win:
        prs.update(PR.findall(f"{e.get('note', '')}\n{e.get('blocked', '')}\n{e.get('reason', '')}"))
    out += ["", "## PRs referenced (cross-check with gh)", "", ", ".join(f"#{n}" for n in sorted(prs, key=int)) or "none"]
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="retro", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", help="ISO UTC start, e.g. 2026-10-03 or 2026-10-03T07:00Z (default: whole log)")
    p.add_argument("--until", help="ISO UTC end; also closes a trailing idle-ready gap")
    p.add_argument("--gap-min", type=float, default=30, help="minutes of silence that count as a gap (default 30)")
    p.add_argument("--log", help="log.jsonl to read (default: this clone's board)")
    p.add_argument("--board", help="board.md to read (default: this clone's board)")
    args = p.parse_args(argv)
    log, board = (args.log, args.board) if args.log and args.board else default_paths()
    log, board = args.log or log, args.board or board
    try:
        with open(log) as f:
            events = [json.loads(line) for line in f if line.strip()]
        with open(board) as f:
            text = f.read()
    except (OSError, ValueError) as e:
        print(f"retro: {e}", file=sys.stderr)
        return 1
    for e in events:
        e["_t"] = when(e["ts"])
    events.sort(key=lambda e: e["_t"])
    print("\n".join(report(events, text, args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
