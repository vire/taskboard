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
import hashlib
import json
import os
import re
import subprocess
import sys
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
HEAD = re.compile(r"^### ((?:TB|P)-\d+) - (\S.*)$")
FIELD = re.compile(r"^- ([a-z_]+):(.*)$")
EXIT_ERROR, EXIT_NOTHING, EXIT_BUSY = 1, 3, 75
READ_ONLY = ("list", "show", "init")


class BoardError(Exception):
    pass


class NothingEligible(BoardError):
    pass


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


def text_lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    if not lines:
        raise BoardError("empty text")
    if any(line.startswith("#") for line in lines):
        raise BoardError("task text may not have lines starting with '#'; they would break the board structure")
    return lines


def one_line(text: str) -> str:
    lines = text_lines(text)
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

    def add_entry(self, sub: str, text: str, who: str) -> None:
        """Append a timestamped bullet under `#### sub`, creating the subsection if needed."""
        first, *rest = [line.rstrip() for line in text.strip().splitlines()] or [""]
        if not first:
            raise BoardError(f"empty {sub.lower()} text")
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
        self.order: list[str] = []
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
                self.order.append(current)
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
        for name in self.order:
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

    def ready(self, it: Item) -> bool:
        done = {i.id for i in self.items["Done"]}
        return all(d in done for d in it.deps)


# ---------------------------------------------------------------- repo, storage, locking


class Repo:
    def __init__(self):
        common, self.top, self.exclude = git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel", "--git-path", "info/exclude")
        self.common = common
        self.dir = os.path.join(common, "taskboard")
        self.board = os.path.join(self.dir, "board.md")
        self.log = os.path.join(self.dir, "log.jsonl")
        self.lockfile = os.path.join(self.dir, ".lock")
        repo_root = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        slug = f"{os.path.basename(repo_root)}-{hashlib.sha1(common.encode()).hexdigest()[:8]}"
        data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        self.backups = os.path.join(data, "taskboard", slug)

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
        with open(self.board) as f:
            return f.read()

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

    def commit(self, old: str, board: Board, event: dict) -> None:
        board.validate()
        new = board.render()
        if new == old:
            return
        self.snapshot(old)
        write_atomic(self.board, new)
        with open(self.log, "a") as f:
            f.write(json.dumps({"ts": stamp(), **event}) + "\n")


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


class Ctx:
    def __init__(self, repo: Repo, owner: str | None):
        self.repo = repo
        self.top = repo.top
        agent = "claude" if os.environ.get("CLAUDECODE") else (
            "codex" if any(k.startswith("CODEX") for k in os.environ) else os.environ.get("USER", "agent"))
        self.owner = owner or os.environ.get("TASKBOARD_OWNER") or f"{agent}@{os.path.basename(self.top)}"

    def mutate(self, cmd: str, fn):
        """Lock, reread, apply fn(board) -> (output, event), validate, snapshot, write, log."""
        with self.repo.locked():
            old = self.repo.read()
            board = Board(old)
            out, event = fn(board)
            self.repo.commit(old, board, {"owner": self.owner, "worktree": self.top, "cmd": cmd, **event})
        return out

    def log_refusal(self, cmd: str, id: str | None, reason: str) -> None:
        """Best effort: a refusal is history too, but must never change the outcome of the command."""
        with contextlib.suppress(Exception), open(self.repo.log, "a") as f:
            event = {"ts": stamp(), "owner": self.owner, "worktree": self.top, "cmd": cmd, "exit": EXIT_ERROR,
                     "reason": reason, **({"id": id} if id else {})}
            f.write(json.dumps(event) + "\n")

    def own(self, it: Item, force: bool) -> None:
        if it.get("worktree") != self.top and not force:
            raise BoardError(f"{it.id} is held by {it.get('owner')} in {it.get('worktree')}, not this worktree "
                             f"({self.top}); only with the user's go-ahead, rerun with --force")


def age(ts: str) -> tuple[str, float]:
    try:
        hours = (utcnow() - dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600
    except ValueError:
        return "?", 0.0
    return (f"{hours / 24:.0f}d" if hours >= 48 else f"{hours:.0f}h" if hours >= 1 else f"{hours * 60:.0f}m"), hours


def row(board: Board, section: str, it: Item) -> str:
    ttl = float(os.environ.get("TASKBOARD_TTL_HOURS", "24"))
    extra = ""
    if section == "Todo":
        waiting = [d for d in it.deps if d not in {i.id for i in board.items["Done"]}]
        status, extra = ("waiting", f"needs {','.join(waiting)}") if waiting else ("ready", "")
    elif section == "In progress":
        hours = age(it.get("claimed_at"))[1]
        label, idle = age(it.get("updated_at") or it.get("claimed_at"))
        status = "blocked" if it.blocked else "doing"
        quiet = not it.blocked and idle * 60 > float(os.environ.get("TASKBOARD_QUIET_MIN", "30"))
        extra = f"{it.get('owner')} {label}" + (" !stale" if hours > ttl else "") + (" !quiet" if quiet else "")
        if it.blocked:
            extra += f" ({it.blocked})"
    else:
        status = "closed" if it.get("closed_at") else "done"
    return f"{it.id:<8} {status:<8} {it.get('plan'):<6} {it.title}" + (f"  [{extra}]" if extra else "")


def cmd_init(args, ctx: Ctx) -> int:
    r = ctx.repo
    print(f"board:   {r.board}\nlink:    {os.path.join(r.top, '.taskboard')}\nlog:     {r.log}\n"
          f"backups: {r.backups}\nrun:     python3 .taskboard/tb --help")
    return 0


def cmd_list(args, ctx: Ctx) -> int:
    board = Board(ctx.repo.read())
    out = []
    if not args.plan:
        for p in board.items["Plans"]:
            counts = {s: sum(1 for i in board.items[s] if i.get("plan") == p.id) for s in SECTIONS[1:]}
            out.append(f"{p.id:<8} plan     {counts['Todo']} todo, {counts['In progress']} in progress, "
                       f"{counts['Done']} done  {p.title}")
    elif args.plan not in {p.id for p in board.items["Plans"]}:
        raise BoardError(f"no such plan {args.plan}")
    for section, it in board.tasks():
        if (args.all or section != "Done") and args.plan in (None, it.get("plan")):
            out.append(row(board, section, it))
    print("\n".join(out) if out else "board is empty; create a plan with: plan \"Title\" --body \"...\"")
    return 0


def cmd_show(args, ctx: Ctx) -> int:
    section, it = Board(ctx.repo.read()).find(args.id)
    print(f"[{section}]\n" + "\n".join(it.lines))
    return 0


def cmd_plan(args, ctx: Ctx) -> int:
    def fn(b: Board):
        pid = b.next_id("P-", 3)
        lines = [f"### {pid} - {one_line(args.title)}"] + (["", *text_lines(args.body)] if args.body else [])
        b.items["Plans"].append(Item(lines))
        return pid, {"id": pid, "to": "Plans"}
    print(ctx.mutate("plan", fn))
    return 0


def cmd_add(args, ctx: Ctx) -> int:
    def fn(b: Board):
        if b.find(args.plan)[0] != "Plans":
            raise BoardError(f"{args.plan} is not a plan")
        deps = [d.strip() for d in (args.depends_on or "").split(",") if d.strip()]
        tid = b.next_id("TB-", 4)
        lines = [f"### {tid} - {one_line(args.title)}", f"- plan: {args.plan}"]
        if deps:
            lines.append(f"- depends_on: {', '.join(deps)}")
        lines += ["", "#### Outcome", *text_lines(args.outcome), "", "#### Done when"]
        lines += [f"- {one_line(c)}" for c in args.done_when]
        b.items["Todo"].append(Item(lines))
        return tid, {"id": tid, "to": "Todo"}
    print(ctx.mutate("add", fn))
    return 0


def cmd_claim(args, ctx: Ctx) -> int:
    def fn(b: Board):
        held = [i for i in b.items["In progress"] if i.get("worktree") == ctx.top and not i.blocked]
        if held:
            raise BoardError(f"this worktree already works on {held[0].id}; complete, block or release it first "
                             "(one unblocked task per worktree, use another worktree for parallel work)")
        if args.id:
            it = b.task_in(args.id, "Todo")
            if not b.ready(it):
                raise NothingEligible(f"{it.id} still waits on {', '.join(d for d in it.deps if b.find(d)[0] != 'Done')}")
        else:
            todo = [i for i in b.items["Todo"] if args.plan in (None, i.get("plan"))]
            it = next((i for i in todo if b.ready(i)), None)
            if it is None:
                raise NothingEligible(why_nothing(b, args.plan))
        it.set("owner", ctx.owner)
        it.set("worktree", ctx.top)
        it.set("claimed_at", stamp())
        it.set("updated_at", stamp())
        it.set("blocked", "none")
        b.move(it, "Todo", "In progress")
        return "\n".join(it.lines), {"id": it.id, "from": "Todo", "to": "In progress"}
    print(ctx.mutate("claim", fn))
    return 0


def why_nothing(b: Board, plan: str | None) -> str:
    scope = f"plan {plan}" if plan else "the board"
    rows = [row(b, s, i) for s, i in b.tasks() if s != "Done" and plan in (None, i.get("plan"))]
    if not rows:
        return f"nothing left in {scope}: every task is done"
    return f"no eligible task in {scope}; remaining:\n" + "\n".join(rows)


def cmd_progress(args, ctx: Ctx) -> int:
    if not (args.note or args.blocked or args.handoff):
        raise BoardError("give --note, --blocked or --handoff")

    def fn(b: Board):
        it = b.task_in(args.id, "In progress")
        ctx.own(it, args.force)
        if args.blocked:
            it.set("blocked", one_line(args.blocked))
            it.add_entry("Progress", f"blocked: {args.blocked}" if args.blocked != "none" else "unblocked", ctx.owner)
        if args.note:
            it.add_entry("Progress", args.note, ctx.owner)
        if args.handoff:
            it.add_entry("Handoff", args.handoff, ctx.owner)
        it.set("updated_at", stamp())
        return f"{it.id} updated", {"id": it.id, "note": args.blocked or args.note or args.handoff}
    print(ctx.mutate("progress", fn))
    return 0


def cmd_complete(args, ctx: Ctx) -> int:
    def fn(b: Board):
        it = b.task_in(args.id, "In progress")
        ctx.own(it, args.force)
        for e in args.evidence:
            it.add_entry("Evidence", e, ctx.owner)
        it.drop("blocked")
        it.set("completed_at", stamp())
        it.set("updated_at", stamp())
        b.move(it, "In progress", "Done")
        return f"{it.id} done. Next: claim --plan {it.get('plan')}", {
            "id": it.id, "from": "In progress", "to": "Done", "note": "; ".join(args.evidence)}
    print(ctx.mutate("complete", fn))
    return 0


def cmd_release(args, ctx: Ctx) -> int:
    def fn(b: Board):
        it = b.task_in(args.id, "In progress")
        ctx.own(it, args.force)
        who = it.get("owner")
        note = args.handoff if who == ctx.owner else f"(released from {who}) {args.handoff}"
        it.add_entry("Handoff", note, ctx.owner)
        it.drop("owner", "worktree", "claimed_at", "blocked")
        it.set("updated_at", stamp())
        b.move(it, "In progress", "Todo", top=True)
        return f"{it.id} back on top of Todo", {"id": it.id, "from": "In progress", "to": "Todo", "note": note}
    print(ctx.mutate("release", fn))
    return 0


def cmd_close(args, ctx: Ctx) -> int:
    def fn(b: Board):
        src, it = b.find(args.id)
        if src not in ("Todo", "In progress"):
            raise BoardError(f"{it.id} is in '{src}', not 'Todo' or 'In progress'")
        if src == "In progress":
            ctx.own(it, args.force)
        note = f"closed: {args.reason}"
        it.add_entry("Evidence", note, ctx.owner)
        it.drop("owner", "worktree", "claimed_at", "blocked")
        it.set("closed_at", stamp())
        it.set("updated_at", stamp())
        b.move(it, src, "Done")
        return f"{it.id} closed", {"id": it.id, "from": src, "to": "Done", "note": note}
    print(ctx.mutate("close", fn))
    return 0


def cmd_restore(args, ctx: Ctx) -> int:
    r = ctx.repo
    if not args.backup:
        names = r.list_backups()
        print(f"backups in {r.backups} (newest first):\n" + "\n".join(names) if names else f"no backups in {r.backups}")
        return 0
    path = args.backup if os.path.isfile(args.backup) else os.path.join(r.backups, args.backup)
    with open(path) as f:
        text = f.read()
    Board(text)  # refuse to restore something invalid
    with r.locked():
        old = r.read()
        if old != text:
            r.snapshot(old)
            write_atomic(r.board, text)
            with open(r.log, "a") as f:
                f.write(json.dumps({"ts": stamp(), "owner": ctx.owner, "worktree": ctx.top,
                                    "cmd": "restore", "note": path}) + "\n")
    print(f"restored {path} (previous board saved as a new backup)")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="taskboard", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--owner", help="label for claims (default: $TASKBOARD_OWNER or <agent>@<worktree>)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def cmd(name, fn, help_):
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    cmd("init", cmd_init, "connect this worktree (every command does this implicitly) and print paths")
    s = cmd("list", cmd_list, "one line per plan and open task; done tasks only with --all")
    s.add_argument("--plan")
    s.add_argument("--all", action="store_true", help="include done and closed tasks")
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
    s = cmd("claim", cmd_claim, "claim the first eligible task (or ID) for this worktree and print it")
    s.add_argument("id", nargs="?")
    s.add_argument("--plan")
    s = cmd("progress", cmd_progress, "record progress, a blocker (--blocked none clears it) or a handoff note")
    s.add_argument("id")
    s.add_argument("--note")
    s.add_argument("--blocked")
    s.add_argument("--handoff")
    s.add_argument("--force", action="store_true", help="act on another worktree's task (user-directed only)")
    s = cmd("complete", cmd_complete, "move a task to Done with evidence (commit, PR, path, checks run)")
    s.add_argument("id")
    s.add_argument("--evidence", required=True, action="append", help="repeatable; one bullet each")
    s.add_argument("--force", action="store_true", help="act on another worktree's task (user-directed only)")
    s = cmd("release", cmd_release, "return a task to the top of Todo with a handoff note")
    s.add_argument("id")
    s.add_argument("--handoff", required=True)
    s.add_argument("--force", action="store_true", help="release another worktree's task (user-directed only)")
    s = cmd("close", cmd_close, "move a Todo or In progress task to Done as closed (superseded, dropped) with a reason")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.add_argument("--force", action="store_true", help="close another worktree's task (user-directed only)")
    s = cmd("restore", cmd_restore, "list backups, or restore one by name or path")
    s.add_argument("backup", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    ctx = None
    try:
        repo = Repo()
        repo.ensure_init()
        ctx = Ctx(repo, args.owner)
        return args.fn(args, ctx)
    except NothingEligible as e:
        print(e)
        return EXIT_NOTHING
    except Busy as e:
        print(f"taskboard: {e}", file=sys.stderr)
        return EXIT_BUSY
    except (BoardError, OSError) as e:
        print(f"taskboard: {e}", file=sys.stderr)
        if ctx and isinstance(e, BoardError) and args.cmd not in READ_ONLY:
            ctx.log_refusal(args.cmd, getattr(args, "id", None), str(e))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
