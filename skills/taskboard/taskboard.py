#!/usr/bin/env python3
"""taskboard - a shared Markdown task board for agents working in the worktrees of one git clone.

The board is <git-common-dir>/taskboard/board.md, linked into every worktree as .taskboard/.
Every change takes one lock, validates, snapshots the previous board and appends to log.jsonl.
Stdlib only, Python 3.9+, macOS and Linux.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import graphlib
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import time

TEMPLATE = """# Taskboard

Shared by every worktree of this clone. Change it through the helper
(`python3 .taskboard/tb --help`), not by hand while agents are running.

## Plans

## Todo

## In progress

## Done
"""
SECTIONS = ("Plans", "Todo", "In progress", "Done")
KEEP_BACKUPS = 30
LOCK_WAIT_S = 10.0
TTL_HOURS = 24  # tasks without an update for longer show !stale in list
QUIET_MIN = 30  # doing tasks silent for longer show !quiet in list
GATE_HOURS = 4  # owner, reader and decision gates open for longer show !gate in list
HEAD = re.compile(r"^### ((?:TB|P)-\d+) - (\S.*)$")
FIELD = re.compile(r"^- ([a-z_]+):(.*)$")
WAIT = re.compile(r"^(?:awaiting\s+)?(owner|coordinator|reader|decision|ci|bot|review|paused|pr:\s*#?\d+|task:\s*TB-\d+)\b:?\s*",
                  re.I)
GATES = ("owner", "reader", "decision")  # waits only a human can clear; list shows them as blocked
GATE_LINES = ("after", "cmd", "due")  # optional lines under a --blocked reason, stored as fields
FIELDS = ("due", "linear", "prs", "step")  # optional task fields set with --set key=value
FOLLOW_UP = re.compile(r"left to|follow[- ]?up|\bTODO\b", re.I)
PR_REF = re.compile(r"(?:#|\bPR\s*#?)(\d{2,})\b")
MERGED = re.compile(r"(?:#|\bPR\s*#?)(\d{2,})\b(?:\s+is)?\s+\(?merged\b(?!\s+(?:origin/)?main\b|\s+with\b)", re.I)
EXIT_ERROR, EXIT_NOTHING, EXIT_ATTENTION, EXIT_BUSY = 1, 3, 4, 75


class BoardError(Exception):
    pass


class Notice(BoardError):
    """Not an error: printed on stdout, not logged, exits with its own code."""

    def __init__(self, text: str, code: int):
        super().__init__(text)
        self.code = code


class Busy(BoardError):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*args: str) -> list[str]:
    r = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if r.returncode:
        raise BoardError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout.splitlines()


def lines_of(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    if not lines:
        raise BoardError("empty text")
    return lines


def text_lines(text: str) -> list[str]:
    """For raw Markdown blocks (outcome, plan body): a line starting with '#' is escaped so it stays text."""
    return ["\\" + line if line.startswith("#") else line for line in lines_of(text)]


def one_line(text: str) -> str:
    lines = lines_of(text)
    if len(lines) > 1:
        raise BoardError("titles must be a single line")
    return lines[0]


# ---------------------------------------------------------------- board model


class Item:
    """One `### ID - title` block: `- key: value` field lines right below the heading, then free Markdown."""

    def __init__(self, lines: list[str], lineno: int = 0):
        self.lines = lines
        self.lineno = lineno
        self.id, self.title = HEAD.match(lines[0]).groups()

    def _fields_end(self) -> int:
        n = 1
        while n < len(self.lines) and FIELD.match(self.lines[n]):
            n += 1
        return n

    def get(self, key: str) -> str:
        for line in self.lines[1:self._fields_end()]:
            k, v = FIELD.match(line).groups()
            if k == key:
                return v.strip()
        return ""

    def set(self, key: str, value: str) -> None:
        end = self._fields_end()
        for i in range(1, end):
            if FIELD.match(self.lines[i]).group(1) == key:
                self.lines[i] = f"- {key}: {value}"
                return
        self.lines.insert(end, f"- {key}: {value}")

    def drop(self, *keys: str) -> None:
        end = self._fields_end()
        self.lines[1:end] = [line for line in self.lines[1:end] if FIELD.match(line).group(1) not in keys]

    @property
    def deps(self) -> list[str]:
        raw = self.get("depends_on")
        return [] if raw in ("", "none") else [d.strip() for d in raw.split(",") if d.strip()]

    @property
    def blocked(self) -> str:
        return "" if self.get("blocked") in ("", "none") else self.get("blocked")

    @property
    def waiting_on(self) -> str:
        """Type of the blocker, read from its text so older boards need no migration; 'blocked' if untyped."""
        m = WAIT.match(self.blocked)
        if not m:
            return "blocked" if self.blocked else ""
        kind, _, ref = m.group(1).partition(":")
        kind = "owner" if kind.lower() == "coordinator" else kind.lower()
        return f"{kind}:{ref.strip().lstrip('#')}" if ref else kind

    @property
    def status(self) -> str:
        """In progress only: doing, waiting (on an agent-side wait) or blocked (on a human or untyped)."""
        kind = self.waiting_on
        return "doing" if not kind else "blocked" if kind in (*GATES, "blocked") else "waiting"

    def unblock(self) -> dict:
        """Drop the blocker and its gate lines; the returned log keys measure how long the gate was open."""
        since = self.get("gate_since")
        self.drop("blocked", "gate_since", "after", "cmd")
        return {"gate_since": since, "gate_cleared_at": stamp()} if since else {}

    def replace(self, sub: str, lines: list[str]) -> None:
        """Replace the body of `#### sub`, creating the subsection if needed."""
        if f"#### {sub}" not in self.lines:
            self.lines += ["", f"#### {sub}", *lines]
            return
        start = self.lines.index(f"#### {sub}") + 1
        end = start + len(self.section(sub))
        self.lines[start:end] = lines + ([""] if end < len(self.lines) else [])

    def facts(self) -> dict:
        """What edit can change, for its log entry."""
        return {"title": self.title, "outcome": self.section("Outcome"), "depends_on": self.get("depends_on"),
                "done_when": [line[2:] for line in self.section("Done when") if line.startswith("- ")],
                **{k: self.get(k) for k in FIELDS}}

    def section(self, sub: str) -> list[str]:
        """Lines under `#### sub`, up to the next subsection."""
        if f"#### {sub}" not in self.lines:
            return []
        rest = self.lines[self.lines.index(f"#### {sub}") + 1:]
        return rest[:next((n for n, line in enumerate(rest) if line.startswith("#### ")), len(rest))]

    def claims(self) -> str:
        """The parts of a task that go stale: its done-when criteria, blocker and gate command."""
        return "\n".join([*self.section("Done when"), self.blocked, self.get("cmd")])

    def add_entry(self, sub: str, text: str, who: str) -> None:
        """Append a timestamped bullet under `#### sub`, creating the subsection if needed."""
        first, *rest = lines_of(text)  # prefixed and indented, so '#' lines are safe as they are
        new = [f"- {stamp()} {who}: {first}"] + [f"  {line}".rstrip() for line in rest]
        head = f"#### {sub}"
        if head not in self.lines:
            self.lines += ["", head, *new]
            return
        start = self.lines.index(head) + 1
        end = next((i for i in range(start, len(self.lines)) if self.lines[i].startswith("#### ")), len(self.lines))
        while end > start and not self.lines[end - 1].strip():
            end -= 1
        self.lines[end:end] = new
        after = end + len(new)
        if after < len(self.lines) and self.lines[after].strip():
            self.lines.insert(after, "")


class Board:
    """board.md parsed into sections of Items. render() is the canonical form: render(parse(render(x))) == render(x)."""

    def __init__(self, text: str):
        self.preamble: list[str] = []
        self.intro: dict[str, list[str]] = {}
        self.items: dict[str, list[Item]] = {}
        target: list[str] = self.preamble
        current = None
        for n, line in enumerate(text.splitlines(), 1):
            if line.startswith("## "):
                current = line[3:].strip()
                if current not in SECTIONS:
                    raise BoardError(f"board.md:{n}: unknown section '## {current}' (expected {', '.join(SECTIONS)})")
                if current in self.items:
                    raise BoardError(f"board.md:{n}: duplicate section '## {current}'")
                self.items[current], self.intro[current] = [], []
                target = self.intro[current]
            elif line.startswith("### "):
                if current is None:
                    raise BoardError(f"board.md:{n}: task heading before any '## ' section")
                if not HEAD.match(line):
                    raise BoardError(f"board.md:{n}: expected '### TB-0001 - Title' or '### P-001 - Title'")
                item = Item([line], n)
                self.items[current].append(item)
                target = item.lines
            else:
                target.append(line)
        for block in [self.preamble, *self.intro.values(), *(i.lines for items in self.items.values() for i in items)]:
            while block and not block[-1].strip():
                block.pop()
            while block and not block[0].strip():
                block.pop(0)
        self.validate()

    def render(self) -> str:
        blocks = ["\n".join(self.preamble)] if self.preamble else []
        for name in SECTIONS:
            blocks.append(f"## {name}")
            if self.intro[name]:
                blocks.append("\n".join(self.intro[name]))
            blocks += ["\n".join(i.lines) for i in self.items[name]]
        return "\n\n".join(blocks) + "\n"

    def validate(self) -> None:
        missing = [s for s in SECTIONS if s not in self.items]
        if missing:
            raise BoardError(f"board.md: missing section(s) {', '.join('## ' + s for s in missing)}")
        seen: dict[str, Item] = {}
        for section, items in self.items.items():
            for it in items:
                prefix = "P-" if section == "Plans" else "TB-"
                if not it.id.startswith(prefix):
                    raise BoardError(f"board.md:{it.lineno}: {it.id} cannot live under '## {section}'")
                if it.id in seen:
                    raise BoardError(f"board.md:{it.lineno}: duplicate id {it.id}")
                seen[it.id] = it
        for section, it in self.tasks():
            plan = it.get("plan")
            if plan not in seen or not plan.startswith("P-"):
                raise BoardError(f"board.md:{it.lineno}: {it.id} has unknown plan '{plan}'")
            if it.id in it.deps:
                raise BoardError(f"board.md:{it.lineno}: {it.id} cannot depend on itself")
            for d in it.deps:
                if d not in seen or not d.startswith("TB-"):
                    raise BoardError(f"board.md:{it.lineno}: {it.id} depends on unknown task '{d}'")
                if seen[d].get("plan") != plan:
                    raise BoardError(f"board.md:{it.lineno}: {it.id} depends on {d} from another plan")
            if section == "In progress" and not it.get("worktree"):
                raise BoardError(f"board.md:{it.lineno}: {it.id} is in progress without a worktree field")
        ok: set[str] = set()

        def visit(tid: str, path: list[str]) -> None:
            if tid in path:
                raise BoardError(f"board.md: dependency cycle {' -> '.join(path + [tid])}")
            if tid not in ok:
                for d in seen[tid].deps:
                    visit(d, path + [tid])
                ok.add(tid)

        for _, it in self.tasks():
            visit(it.id, [])

    def tasks(self):
        for section in ("Todo", "In progress", "Done"):
            for it in self.items[section]:
                yield section, it

    def find(self, tid: str) -> tuple[str, Item]:
        for section, items in self.items.items():
            for it in items:
                if it.id == tid:
                    return section, it
        raise BoardError(f"no such id {tid}")

    def task_in(self, tid: str, section: str) -> Item:
        where, it = self.find(tid)
        if where != section:
            raise BoardError(f"{tid} is in '{where}', not '{section}'")
        return it

    def move(self, it: Item, src: str, dst: str, top: bool = False) -> None:
        self.items[src].remove(it)
        self.items[dst].insert(0, it) if top else self.items[dst].append(it)

    def next_id(self, prefix: str, width: int) -> str:
        # ponytail: max+1, so hand-deleting the newest task frees its id again; keep a counter if that bites
        nums = [int(i.id.split("-")[1]) for items in self.items.values() for i in items if i.id.startswith(prefix)]
        return f"{prefix}{max(nums, default=0) + 1:0{width}d}"

    def idle_ready(self, plan: str) -> list[str]:
        """Ready Todo tasks of a plan that nobody is doing. Proposals are never claimed, so never idle."""
        doing = any(i.status == "doing" for i in self.items["In progress"] if i.get("plan") == plan)
        if doing or self.find(plan)[1].title == "Proposals":
            return []
        return [i.id for i in self.items["Todo"] if i.get("plan") == plan and not self.waiting(i)]

    def waiting(self, it: Item) -> list[str]:
        done = {i.id for i in self.items["Done"]}
        return [d for d in it.deps if d not in done]


# ---------------------------------------------------------------- repo, storage, locking


class Repo:
    def __init__(self, owner: str | None = None):
        common, self.top, self.exclude = git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel", "--git-path", "info/exclude")
        self.dir = os.path.join(common, "taskboard")
        self.board = os.path.join(self.dir, "board.md")
        self.log = os.path.join(self.dir, "log.jsonl")
        self.lockfile = os.path.join(self.dir, ".lock")
        repo_root = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        slug = f"{os.path.basename(repo_root)}-{hashlib.sha1(common.encode()).hexdigest()[:8]}"
        data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        self.backups = os.path.join(data, "taskboard", slug)
        agent = "claude" if os.environ.get("CLAUDECODE") else (
            "codex" if any(k.startswith("CODEX") for k in os.environ) else os.environ.get("USER", "agent"))
        herdr = os.environ.get("HERDR_PANE_ID") if os.environ.get("HERDR_ENV") == "1" else None
        # ponytail: worktrees sharing a basename share the default owner; set TASKBOARD_OWNER if that bites
        self.owner = (owner or os.environ.get("TASKBOARD_OWNER") or (f"herdr:{herdr}" if herdr else None)
                      or f"{agent}@{os.path.basename(self.top)}")

    def ensure_init(self) -> None:
        """Idempotent: board dir and file, git exclude rule, .taskboard link in every worktree, tb link."""
        os.makedirs(self.dir, exist_ok=True)
        with contextlib.suppress(FileExistsError), open(self.board, "x") as f:
            f.write(TEMPLATE)
        os.makedirs(os.path.dirname(self.exclude), exist_ok=True)
        with open(self.exclude, "a+") as f:
            f.seek(0)
            if "/.taskboard" not in f.read().splitlines():
                f.write("\n/.taskboard\n")
        link(os.path.join(self.top, ".taskboard"), self.dir)
        for line in git("worktree", "list", "--porcelain"):
            wt = line[len("worktree "):]
            if line.startswith("worktree ") and wt != self.top and os.path.exists(os.path.join(wt, ".git")):
                with contextlib.suppress(BoardError, OSError):  # a stray file there only matters to its own worktree
                    link(os.path.join(wt, ".taskboard"), self.dir)
        link(os.path.join(self.dir, "tb"), os.path.realpath(__file__))

    def read(self) -> str:
        return pathlib.Path(self.board).read_text()

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise Busy("board busy (another update holds the lock); retry in a few seconds") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def snapshot(self, text: str) -> None:
        os.makedirs(self.backups, exist_ok=True)
        write_atomic(os.path.join(self.backups, f"board-{utcnow().strftime('%Y%m%dT%H%M%S%fZ')}.md"), text)
        for old in self.list_backups()[KEEP_BACKUPS:]:
            os.remove(os.path.join(self.backups, old))

    def list_backups(self) -> list[str]:
        """Newest first."""
        if not os.path.isdir(self.backups):
            return []
        return sorted((f for f in os.listdir(self.backups) if f.startswith("board-")), reverse=True)

    def commit(self, old: str, new: str, event: dict) -> None:
        if new == old:
            return
        self.snapshot(old)
        write_atomic(self.board, new)
        self.append_log(event)

    def append_log(self, event: dict) -> None:
        with open(self.log, "a") as f:
            f.write(json.dumps({"ts": stamp(), "owner": self.owner, "worktree": self.top, **event}) + "\n")

    def mutate(self, cmd: str, fn) -> str:
        """Lock, reread, apply fn(board) -> (output, event), validate, snapshot, write, log."""
        with self.locked():
            old = self.read()
            board = Board(old)
            out, event = fn(board)
            board.validate()
            self.commit(old, board.render(), {"cmd": cmd, **event})
        return out

    def own(self, it: Item, force: bool) -> None:
        """The owner decides, not the worktree; the owner acting from another worktree moves the task there."""
        if it.get("owner") != self.owner and not force:
            raise BoardError(f"{it.id} is held by {it.get('owner')} (worktree {it.get('worktree')}); you are {self.owner}. "
                             "If you did not claim it yourself, stop and ask the user (--force needs their go-ahead). "
                             f"If you claimed it and only changed worktree, rerun with --owner {it.get('owner')} "
                             "placed before the command")
        if it.get("owner") == self.owner:
            it.set("worktree", self.top)


def link(path: str, target: str) -> None:
    if os.path.islink(path):
        if os.readlink(path) == target:
            return
        os.unlink(path)
    elif os.path.exists(path):
        raise BoardError(f"{path} exists and is not a symlink; move it away and rerun")
    with contextlib.suppress(FileExistsError):
        os.symlink(target, path)


def write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- commands


def age(ts: str) -> tuple[str, float]:
    try:
        hours = (utcnow() - dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600
    except ValueError:
        return "?", 0.0
    return (f"{hours / 24:.0f}d" if hours >= 48 else f"{hours:.0f}h" if hours >= 1 else f"{hours * 60:.0f}m"), hours


def row(board: Board, section: str, it: Item) -> str:
    extra = ""
    if section == "Todo":
        waiting = board.waiting(it)
        status, extra = ("waiting", f"needs {','.join(waiting)}") if waiting else ("ready", "")
    elif section == "In progress":
        status = it.status
        label, idle = age(it.get("updated_at") or it.get("claimed_at"))
        extra = f"{it.get('owner')} {label}"
        if it.waiting_on in GATES:
            gate, hours = age(it.get("gate_since") or it.get("updated_at"))
            extra += f" !gate {gate}" if hours > GATE_HOURS else ""
        elif idle > TTL_HOURS:
            extra += " !stale"
        elif status == "doing" and idle * 60 > QUIET_MIN:
            extra += " !quiet"
        if it.blocked:
            extra += f" ({it.blocked})"
    else:
        status = "closed" if it.get("closed_at") else "done"
    return f"{it.id:<8} {status:<8} {it.get('plan'):<6} {it.title}" + (f"  [{extra}]" if extra else "")


def cmd_init(args, r: Repo) -> str:
    return (f"board:   {r.board}\nlink:    {os.path.join(r.top, '.taskboard')}\nlog:     {r.log}\n"
            f"backups: {r.backups}\nrun:     python3 .taskboard/tb --help")


def cmd_list(args, r: Repo) -> str:
    board = Board(r.read())
    out = []
    if not args.plan:
        for p in board.items["Plans"]:
            counts = {s: sum(1 for i in board.items[s] if i.get("plan") == p.id) for s in SECTIONS[1:]}
            out.append(f"{p.id:<8} plan     {counts['Todo']} todo, {counts['In progress']} in progress, "
                       f"{counts['Done']} done  {p.title}" + ("  [!idle-ready]" if board.idle_ready(p.id) else ""))
    elif args.plan not in {p.id for p in board.items["Plans"]}:
        raise BoardError(f"no such plan {args.plan}")
    if args.check:
        return check(board)
    for section, it in board.tasks():
        if (args.all or section != "Done") and args.plan in (None, it.get("plan")):
            out.append(row(board, section, it))
    return "\n".join(out) if out else "board is empty; create a plan with: plan \"Title\" --body \"...\""


def check(b: Board) -> str:
    """Problems a watchdog should raise; the last line always proves the board was read."""
    open_ = [(s, i) for s, i in b.tasks() if s != "Done"]
    today = utcnow().date().isoformat()
    out = [f"!due {i.id} is past due {i.get('due')} ({i.blocked or s})" for s, i in open_ if i.get("due") and i.get("due") < today]
    out += [f"!idle-ready {p.id} has ready {', '.join(b.idle_ready(p.id))} and nobody doing" for p in b.items["Plans"]
            if b.idle_ready(p.id)]
    merged = {n for items in b.items.values() for i in items for line in i.lines for n in MERGED.findall(line)}
    merged |= {n for i in b.items["Done"] for line in i.section("Evidence") for n in PR_REF.findall(line)}
    out += [f"!stale-text {i.id} names #{n}, marked merged or done; update it with a note" for _, i in open_
            for n in sorted(set(PR_REF.findall(i.claims())) & merged)]
    gates = sum(1 for _, i in open_ if i.waiting_on in GATES)
    out.append(f"check: scanned {len(open_)} open tasks, {gates} gates, {len(b.items['Plans'])} plans at {stamp()}; "
               f"{len(out)} problems")
    if len(out) > 1:
        raise Notice("\n".join(out), EXIT_ATTENTION)
    return out[0]


def cmd_gates(args, r: Repo) -> str:
    b = Board(r.read())
    gates = {i.id: i for s, i in b.tasks() if s == "In progress" and i.waiting_on in GATES and args.plan in (None, i.get("plan"))}

    def names(it: Item, ref: str) -> bool:
        return ref == it.id or bool(re.search(rf"(?<!\d){ref.lstrip('#')}(?!\d)", f"{it.blocked} {it.get('cmd')}"))

    graph = {tid: {o for ref in re.findall(r"#\d+|TB-\d+", it.get("after")) for o, other in gates.items()
                   if o != tid and names(other, ref)} for tid, it in gates.items()}
    order = graphlib.TopologicalSorter(graph)
    try:
        order.prepare()
    except graphlib.CycleError as e:
        raise BoardError(f"gate order cycle: {' -> '.join(e.args[1])}") from None
    ids = []
    while order.is_active():  # oldest gate first among those whose 'after' gates are listed
        ready = sorted(order.get_ready(), key=lambda t: (gates[t].get("gate_since") or gates[t].get("updated_at"), t))
        order.done(*ready)
        ids += ready
    out = []
    for n, it in enumerate((gates[t] for t in ids), 1):
        label, _ = age(it.get("gate_since") or it.get("updated_at"))
        due = f", due {it.get('due')}" if it.get("due") else ""
        out.append(f"{n}. [{it.waiting_on} {label}{due}] {WAIT.sub('', it.blocked)} - {it.id} {it.get('plan')} {it.get('owner')}")
        out += [f"   {k}: {it.get(k)}" for k in ("after", "cmd") if it.get(k)]
    return "\n".join(out) if out else "no open gates"


def cmd_show(args, r: Repo) -> str:
    section, it = Board(r.read()).find(args.id)
    return f"[{section}]\n" + "\n".join(it.lines)


def cmd_plan(args, r: Repo) -> str:
    def fn(b: Board):
        pid = b.next_id("P-", 3)
        lines = [f"### {pid} - {one_line(args.title)}"] + (["", *text_lines(args.body)] if args.body else [])
        b.items["Plans"].append(Item(lines))
        return pid, {"id": pid, "to": "Plans"}
    return r.mutate("plan", fn)


def new_task(b: Board, title: str, plan: str, outcome: str, done_when: list[str], deps: str | None = None) -> Item:
    if b.find(plan)[0] != "Plans":
        raise BoardError(f"{plan} is not a plan")
    deps = ", ".join(d.strip() for d in (deps or "").split(",") if d.strip())
    tid = b.next_id("TB-", 4)
    lines = [f"### {tid} - {one_line(title)}", f"- plan: {plan}"] + ([f"- depends_on: {deps}"] if deps else [])
    lines += ["", "#### Outcome", *text_lines(outcome), "", "#### Done when", *(f"- {one_line(c)}" for c in done_when)]
    b.items["Todo"].append(Item(lines))
    return b.items["Todo"][-1]


def set_fields(it: Item, pairs: list[str] | None) -> None:
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or key not in FIELDS:
            raise BoardError(f"--set takes key=value with key one of {', '.join(FIELDS)}; got '{pair}'")
        if value.strip() in ("", "none"):
            it.drop(key)
            continue
        if key == "due":
            gate_line(f"due: {value}")
        it.set(key, one_line(value))


def cmd_add(args, r: Repo) -> str:
    def fn(b: Board):
        it = new_task(b, args.title, args.plan, args.outcome, args.done_when, args.depends_on)
        set_fields(it, args.set)
        return it.id, {"id": it.id, "to": "Todo"}
    return r.mutate("add", fn)


def cmd_note(args, r: Repo) -> str:
    def fn(b: Board):
        _, it = b.find(args.id)
        it.add_entry("Progress", args.text, r.owner)
        return f"{it.id} noted", {"id": it.id, "note": args.text}
    return r.mutate("note", fn)


def cmd_edit(args, r: Repo) -> str:
    def fn(b: Board):
        section, it = b.find(args.id)
        if section not in ("Todo", "In progress"):
            raise BoardError(f"{it.id} is in '{section}'; only Todo and In progress tasks can be edited, use note")
        if section == "In progress":
            r.own(it, args.force)
        old = it.facts()
        if args.title:
            it.title = one_line(args.title)
            it.lines[0] = f"### {it.id} - {it.title}"
        if args.outcome:
            it.replace("Outcome", text_lines(args.outcome))
        if args.done_when:
            it.replace("Done when", [f"- {one_line(c)}" for c in args.done_when])
        if args.depends_on in ("none", ""):
            it.drop("depends_on")
        elif args.depends_on:
            it.set("depends_on", ", ".join(d.strip() for d in args.depends_on.split(",") if d.strip()))
        set_fields(it, args.set)
        new = it.facts()
        changed = [k for k in old if old[k] != new[k]]
        return f"{it.id} edited", {"id": it.id, "old": {k: old[k] for k in changed}, "new": {k: new[k] for k in changed}}
    return r.mutate("edit", fn)


def cmd_claim(args, r: Repo) -> str:
    def fn(b: Board):
        held = [i.id for i in b.items["In progress"] if i.get("owner") == r.owner and i.status != "blocked"]
        if len(held) >= args.wip:
            raise BoardError(f"{r.owner} already works on {', '.join(held)} (WIP limit {args.wip}; owner, reader and "
                             "decision gates do not count); complete one, gate it, release it, or use another --owner")
        if args.id:
            it = b.task_in(args.id, "Todo")
            if b.waiting(it):
                raise Notice(f"{it.id} still waits on {', '.join(b.waiting(it))}", EXIT_NOTHING)
        else:
            todo = [i for i in b.items["Todo"] if args.plan in (None, i.get("plan"))]
            it = next((i for i in todo if not b.waiting(i)), None)
            if it is None:
                raise Notice(why_nothing(b, args.plan), EXIT_NOTHING)
        it.set("owner", r.owner)
        it.set("worktree", r.top)
        it.set("claimed_at", stamp())
        it.set("updated_at", stamp())
        it.set("blocked", "none")
        b.move(it, "Todo", "In progress")
        return "\n".join(it.lines), {"id": it.id, "from": "Todo", "to": "In progress"}
    return r.mutate("claim", fn)


def why_nothing(b: Board, plan: str | None) -> str:
    scope = f"plan {plan}" if plan else "the board"
    rows = [row(b, s, i) for s, i in b.tasks() if s != "Done" and plan in (None, i.get("plan"))]
    if not rows:
        return f"nothing left in {scope}: every task is done"
    return f"no eligible task in {scope}; remaining:\n" + "\n".join(rows)


def cmd_progress(args, r: Repo) -> str:
    if not (args.note or args.blocked or args.handoff):
        raise BoardError("give --note, --blocked or --handoff")

    def fn(b: Board):
        it = b.task_in(args.id, "In progress")
        r.own(it, args.force)
        event = {"id": it.id, "note": args.note, "handoff": args.handoff}
        if args.blocked:
            reason, *rest = lines_of(args.blocked)
            lines = dict(gate_line(line) for line in rest)
            if reason == it.blocked:
                it.drop("after", "cmd")
            else:
                event.update(it.unblock())
            it.set("blocked", reason)
            for k, v in lines.items():
                it.set(k, v)
            if it.waiting_on in GATES and not it.get("gate_since"):
                it.set("gate_since", stamp())
                event.setdefault("gate_since", it.get("gate_since"))
            event.update(blocked=reason, waiting_on=it.waiting_on or "none")
            it.add_entry("Progress", f"blocked: {args.blocked}" if args.blocked != "none" else "unblocked", r.owner)
        if args.note:
            it.add_entry("Progress", args.note, r.owner)
        if args.handoff:
            it.add_entry("Handoff", args.handoff, r.owner)
        it.set("updated_at", stamp())
        return f"{it.id} updated", {k: v for k, v in event.items() if v}
    return r.mutate("progress", fn)


def gate_line(line: str) -> tuple[str, str]:
    key, sep, value = line.partition(":")
    if not sep or key.strip() not in GATE_LINES:
        raise BoardError(f"--blocked takes a reason line, then only {', '.join(k + ':' for k in GATE_LINES)} lines; got '{line}'")
    if key.strip() == "due":
        try:
            dt.date.fromisoformat(value.strip())
        except ValueError:
            raise BoardError(f"due: wants a date like 2026-10-09, got '{value.strip()}'") from None
    return key.strip(), value.strip()


def cmd_complete(args, r: Repo) -> str:
    def fn(b: Board):
        it = b.task_in(args.id, "In progress")
        r.own(it, args.force)
        for e in args.evidence:
            it.add_entry("Evidence", e, r.owner)
        spawned = [new_task(b, t, it.get("plan"), f"Follow-up of {it.id}: {one_line(t)}",
                            ["the follow-up in the title is done"]).id for t in args.spawn or []]
        if spawned:
            it.set("spawned", ", ".join(spawned))
        if args.cleanup:
            it.set("cleanup", one_line(args.cleanup))
        gate = it.unblock()
        it.set("completed_at", stamp())
        it.set("updated_at", stamp())
        b.move(it, "In progress", "Done")
        out = f"{it.id} done" + (f", spawned {', '.join(spawned)}" if spawned else "") + f". Next: claim --plan {it.get('plan')}"
        if not spawned and FOLLOW_UP.search(" ".join(args.evidence)):
            out += "\nwarning: the evidence mentions a follow-up but none was spawned; add it as a task (add, or --spawn next time)"
        return out, {"id": it.id, "from": "In progress", "to": "Done", "note": "; ".join(args.evidence),
                     **({"spawned": spawned} if spawned else {}), **gate}
    return r.mutate("complete", fn)


def cmd_release(args, r: Repo) -> str:
    def fn(b: Board):
        it = b.task_in(args.id, "In progress")
        r.own(it, args.force)
        who = it.get("owner")
        note = args.handoff if who == r.owner else f"(released from {who}) {args.handoff}"
        it.add_entry("Handoff", note, r.owner)
        gate = it.unblock()
        it.drop("owner", "worktree", "claimed_at")
        it.set("updated_at", stamp())
        b.move(it, "In progress", "Todo", top=True)
        return f"{it.id} back on top of Todo", {"id": it.id, "from": "In progress", "to": "Todo", "note": note, **gate}
    return r.mutate("release", fn)


def cmd_close(args, r: Repo) -> str:
    def fn(b: Board):
        src, it = b.find(args.id)
        if src not in ("Todo", "In progress"):
            raise BoardError(f"{it.id} is in '{src}', not 'Todo' or 'In progress'")
        if src == "In progress":
            r.own(it, args.force)
        note = f"closed: {args.reason}"
        it.add_entry("Evidence", note, r.owner)
        gate = it.unblock()
        it.drop("owner", "worktree", "claimed_at")
        it.set("closed_at", stamp())
        it.set("updated_at", stamp())
        b.move(it, src, "Done")
        return f"{it.id} closed", {"id": it.id, "from": src, "to": "Done", "note": note, **gate}
    return r.mutate("close", fn)


def cmd_restore(args, r: Repo) -> str:
    if not args.backup:
        names = r.list_backups()
        return f"backups in {r.backups} (newest first):\n" + "\n".join(names) if names else f"no backups in {r.backups}"
    path = args.backup if os.path.isfile(args.backup) else os.path.join(r.backups, args.backup)
    text = pathlib.Path(path).read_text()
    Board(text)  # refuse to restore something invalid
    with r.locked():
        r.commit(r.read(), text, {"cmd": "restore", "note": path})
    return f"restored {path} (previous board saved as a new backup)"


def cmd_new(args, r: Repo) -> str:
    """Archive the board dir and backups beside the board dir, check the archive lists every file, then reset."""
    with r.locked():
        if not args.force:
            busy = [i.id for i in Board(r.read()).items["In progress"]]
            if busy:
                raise BoardError(f"{', '.join(busy)} still in progress; complete or release them first, "
                                 "or rerun with --force (user-directed only)")
        files = {f"taskboard/{n}": os.path.join(r.dir, n) for n in sorted(os.listdir(r.dir))
                 if n != ".lock" and os.path.isfile(os.path.join(r.dir, n)) and not os.path.islink(os.path.join(r.dir, n))}
        files.update({f"backups/{n}": os.path.join(r.backups, n) for n in r.list_backups()})
        archive = os.path.join(os.path.dirname(r.dir), f"backup-{utcnow():%Y%m%dT%H%M%SZ}.tar.gz")
        with tarfile.open(archive, "x:gz") as tar:
            for name, path in files.items():
                tar.add(path, arcname=name)
        with tarfile.open(archive) as tar:
            missing = sorted(set(files) - set(tar.getnames()))
        if missing:
            raise BoardError(f"{archive} lacks {', '.join(missing)}; nothing was reset")
        write_atomic(r.board, TEMPLATE)
        for name, path in files.items():
            if name == "taskboard/log.jsonl" or name.startswith("backups/"):  # ponytail: other files stay, archived
                os.remove(path)
        r.append_log({"cmd": "new", "note": archive})
    return f"archived {len(files)} files to {archive}:\n" + "\n".join(f"  {n}" for n in files) + f"\nfresh board at {r.board}"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="taskboard", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--owner", help="identity for claims and ownership (default: $TASKBOARD_OWNER, else herdr:$HERDR_PANE_ID in Herdr, else <agent>@<worktree>)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def cmd(name, fn, help_):
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    cmd("init", cmd_init, "connect this worktree (every command does this implicitly) and print paths")
    s = cmd("list", cmd_list, "one line per plan and open task; done tasks only with --all")
    s.add_argument("--plan")
    s.add_argument("--all", action="store_true", help="include done and closed tasks")
    s.add_argument("--check", action="store_true",
                   help=f"print only problems (overdue, idle-ready plans, text naming merged PRs) and exit {EXIT_ATTENTION} if any")
    s = cmd("gates", cmd_gates, "owner, reader and decision gates in 'after:' order with age and command; post it verbatim")
    s.add_argument("--plan")
    s = cmd("show", cmd_show, "print one task or plan in full")
    s.add_argument("id")
    s = cmd("plan", cmd_plan, "create a plan, prints its id")
    s.add_argument("title")
    s.add_argument("--body", help="scope and goal of the plan")
    s = cmd("add", cmd_add, "add a task to the end of Todo, prints its id")
    s.add_argument("title")
    s.add_argument("--plan", required=True)
    s.add_argument("--outcome", required=True, help="what is true when the task is done")
    s.add_argument("--done-when", required=True, action="append", help="checkable criterion (repeatable)")
    s.add_argument("--depends-on", help="comma separated task ids from the same plan")
    s.add_argument("--set", action="append", metavar="KEY=VALUE", help=f"optional field, one of {', '.join(FIELDS)} (repeatable)")
    s = cmd("note", cmd_note, "append a fact to any task or plan, by anyone; changes no state")
    s.add_argument("id")
    s.add_argument("text")
    s = cmd("edit", cmd_edit, "rewrite a Todo or In progress task's title, outcome, done-when, dependencies or fields")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--outcome")
    s.add_argument("--done-when", action="append", help="replaces all criteria (repeatable)")
    s.add_argument("--depends-on", help="comma separated task ids, or none")
    s.add_argument("--set", action="append", metavar="KEY=VALUE", help=f"one of {', '.join(FIELDS)}; VALUE none clears it")
    s.add_argument("--force", action="store_true", help="edit another owner's task (user-directed only)")
    s = cmd("claim", cmd_claim, "claim the first eligible task (or ID) for your owner and print it")
    s.add_argument("id", nargs="?")
    s.add_argument("--plan")
    s.add_argument("--wip", type=int, default=2, help="most doing or waiting tasks per owner (default 2); gates do not count")
    s = cmd("progress", cmd_progress, "record progress, a blocker (--blocked none clears it) or a handoff note")
    s.add_argument("id")
    s.add_argument("--note")
    s.add_argument("--blocked", help="'awaiting owner|reader|decision: ...' (a gate), 'awaiting ci|bot|review|pr:N|task:TB-x', "
                   "'paused ...' or free text; then optional after: #N|TB-x, cmd: ..., due: YYYY-MM-DD lines")
    s.add_argument("--handoff")
    s.add_argument("--force", action="store_true", help="act on another owner's task (user-directed only)")
    s = cmd("complete", cmd_complete, "move a task to Done with evidence (commit, PR, path, checks run)")
    s.add_argument("id")
    s.add_argument("--evidence", required=True, action="append", help="repeatable; one bullet each")
    s.add_argument("--spawn", action="append", metavar="TITLE", help="add a follow-up task to the same plan (repeatable)")
    s.add_argument("--cleanup", help="what happened to the worktree, e.g. 'removed' or 'kept: PR 12 open'")
    s.add_argument("--force", action="store_true", help="act on another owner's task (user-directed only)")
    s = cmd("release", cmd_release, "return a task to the top of Todo with a handoff note")
    s.add_argument("id")
    s.add_argument("--handoff", required=True)
    s.add_argument("--force", action="store_true", help="release another owner's task (user-directed only)")
    s = cmd("close", cmd_close, "move a Todo or In progress task to Done as closed (superseded, dropped) with a reason")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.add_argument("--force", action="store_true", help="close another owner's task (user-directed only)")
    s = cmd("new", cmd_new, "archive board, log and backups to backup-<utc>.tar.gz beside the board dir, then start "
            "a fresh board (user-directed only)")
    s.add_argument("--force", action="store_true", help="even with tasks in progress")
    s = cmd("restore", cmd_restore, "list backups, or restore one by name or path")
    s.add_argument("backup", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    r = None
    try:
        r = Repo(args.owner)
        r.ensure_init()
        print(args.fn(args, r))
        return 0
    except Notice as e:
        print(e)
        return e.code
    except Busy as e:
        print(f"taskboard: {e}", file=sys.stderr)
        return EXIT_BUSY
    except (BoardError, OSError) as e:
        print(f"taskboard: {e}", file=sys.stderr)
        if r and args.cmd not in ("list", "show", "init"):
            with contextlib.suppress(OSError):  # a refusal is history too, but must not change the outcome
                r.append_log({"cmd": args.cmd, "exit": EXIT_ERROR, "reason": str(e),
                              **({"id": args.id} if getattr(args, "id", None) else {})})
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
