"""Run with: python3 -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "skills", "taskboard", "taskboard.py")
spec = importlib.util.spec_from_file_location("taskboard", SCRIPT)
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)


# Fixture F from the standup spec: three owners, three plans (one Proposals), two gates (one past due), a quiet
# doing task, a stale wait, a merged-PR note, an unspawned follow-up, a prose ask, a refusal, a close, a note.
FX_BOARD = """# Taskboard

## Plans

### P-001 - Wave 2 payloads

### P-002 - Docs

### P-003 - Proposals

## Todo

### TB-0007 - Orphans packet
- plan: P-001
- depends_on: TB-0001

#### Outcome
Orphans packet merged.

#### Done when
- PR merged

### TB-0008 - Runbook page
- plan: P-002

#### Outcome
Runbook page published.

#### Done when
- page live

### TB-0010 - Dispatcher idea
- plan: P-003

#### Outcome
Proposal written.

#### Done when
- proposal noted

## In progress

### TB-0002 - Fibrosis receipt
- plan: P-001
- owner: w1
- worktree: /wt/w1
- claimed_at: 2026-10-05T08:00:00Z
- updated_at: 2026-10-05T11:18:00Z
- blocked: awaiting owner: merge #1751 (fibrosis receipt)
- gate_since: 2026-10-05T11:18:00Z
- cmd: gh pr merge 1751 --merge

#### Outcome
Receipt merged.

#### Done when
- receipt PR merged

### TB-0003 - IPF restore
- plan: P-001
- owner: w2
- worktree: /wt/w2
- claimed_at: 2026-10-04T08:00:00Z
- updated_at: 2026-10-05T12:00:00Z
- blocked: awaiting reader: independent restore of THE-2348
- gate_since: 2026-10-04T20:30:00Z
- due: 2026-10-04

#### Outcome
Restore done by a second identity.

#### Done when
- restore evidence recorded

### TB-0004 - Orphans-2 receipt
- plan: P-001
- owner: w2
- worktree: /wt/w2
- claimed_at: 2026-10-04T21:00:00Z
- updated_at: 2026-10-05T11:24:00Z
- blocked: awaiting ci: #1752

#### Outcome
Receipt merged.

#### Done when
- receipt PR merged

#### Progress
- 2026-10-05T11:24:00Z w2: DECISION NEEDED: convert or allowlist the 5 readers?

### TB-0005 - Hair evidence merge
- plan: P-001
- owner: orch
- worktree: /wt/orch
- claimed_at: 2026-10-04T09:00:00Z
- updated_at: 2026-10-04T10:00:00Z
- blocked: awaiting pr:#1710 (rebase after #1706)

#### Outcome
Evidence PR merged.

#### Done when
- evidence PR merged

#### Progress
- 2026-10-05T09:32:00Z w2: #1710 merged as e0dd38c

### TB-0006 - Viz cutover
- plan: P-001
- owner: w1
- worktree: /wt/w1
- claimed_at: 2026-10-05T09:10:00Z
- updated_at: 2026-10-05T11:00:00Z

#### Outcome
Viz readers use the resolver.

#### Done when
- CI green

#### Progress
- 2026-10-05T11:00:00Z w1: rebased #1731 onto main, 140 tests pass

## Done

### TB-0001 - Shared resolver
- plan: P-001
- owner: w1
- worktree: /wt/w1
- claimed_at: 2026-10-05T06:00:00Z
- updated_at: 2026-10-05T09:00:00Z
- completed_at: 2026-10-05T09:00:00Z

#### Outcome
Resolver merged.

#### Done when
- resolver PR merged

#### Evidence
- 2026-10-05T09:00:00Z w1: #1744 merged as 2927583; docs follow-up: #1757

### TB-0009 - Red main fix
- plan: P-001
- updated_at: 2026-10-05T08:00:00Z
- closed_at: 2026-10-05T08:00:00Z

#### Outcome
Main green.

#### Done when
- main green

#### Evidence
- 2026-10-05T08:00:00Z orch: closed: superseded by #1739
"""
FX_LOG = """{"ts": "2026-10-04T08:00:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "claim", "id": "TB-0003", "from": "Todo", "to": "In progress"}
{"ts": "2026-10-04T09:00:00Z", "owner": "orch", "worktree": "/wt/orch", "cmd": "claim", "id": "TB-0005", "from": "Todo", "to": "In progress"}
{"ts": "2026-10-04T10:00:00Z", "owner": "orch", "worktree": "/wt/orch", "cmd": "progress", "id": "TB-0005", "blocked": "awaiting pr:#1710 (rebase after #1706)", "waiting_on": "pr:1710"}
{"ts": "2026-10-04T20:30:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "progress", "id": "TB-0003", "gate_since": "2026-10-04T20:30:00Z", "blocked": "awaiting reader: independent restore of THE-2348", "waiting_on": "reader"}
{"ts": "2026-10-04T21:00:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "claim", "id": "TB-0004", "from": "Todo", "to": "In progress"}
{"ts": "2026-10-05T04:54:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "progress", "id": "TB-0004", "gate_since": "2026-10-05T04:54:00Z", "blocked": "awaiting owner: live upload", "waiting_on": "owner"}
{"ts": "2026-10-05T06:00:00Z", "owner": "w1", "worktree": "/wt/w1", "cmd": "claim", "id": "TB-0001", "from": "Todo", "to": "In progress"}
{"ts": "2026-10-05T08:00:00Z", "owner": "orch", "worktree": "/wt/orch", "cmd": "close", "id": "TB-0009", "from": "Todo", "to": "Done", "note": "closed: superseded by #1739"}
{"ts": "2026-10-05T08:00:00Z", "owner": "w1", "worktree": "/wt/w1", "cmd": "claim", "id": "TB-0002", "from": "Todo", "to": "In progress"}
{"ts": "2026-10-05T08:30:00Z", "owner": "orch", "worktree": "/wt/orch", "cmd": "add", "id": "TB-0008", "to": "Todo"}
{"ts": "2026-10-05T09:00:00Z", "owner": "w1", "worktree": "/wt/w1", "cmd": "complete", "id": "TB-0001", "from": "In progress", "to": "Done", "note": "#1744 merged as 2927583; docs follow-up: #1757"}
{"ts": "2026-10-05T09:10:00Z", "owner": "w1", "worktree": "/wt/w1", "cmd": "claim", "id": "TB-0006", "from": "Todo", "to": "In progress"}
{"ts": "2026-10-05T09:25:00Z", "owner": "orch", "worktree": "/wt/orch", "cmd": "note", "id": "TB-0007", "note": "Decision: orphans go to data/_orphans/, owner agreed"}
{"ts": "2026-10-05T09:30:00Z", "owner": "w1", "worktree": "/wt/w1", "cmd": "claim", "exit": 1, "reason": "w1 already works on TB-0006, TB-0002", "id": "TB-0007"}
{"ts": "2026-10-05T09:32:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "note", "id": "TB-0005", "note": "#1710 merged as e0dd38c"}
{"ts": "2026-10-05T11:00:00Z", "owner": "w1", "worktree": "/wt/w1", "cmd": "progress", "id": "TB-0006", "note": "rebased #1731 onto main, 140 tests pass"}
{"ts": "2026-10-05T11:14:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "progress", "id": "TB-0004", "note": "Owner upload DONE", "gate_since": "2026-10-05T04:54:00Z", "gate_cleared_at": "2026-10-05T11:14:00Z", "blocked": "awaiting ci: #1752", "waiting_on": "ci"}
{"ts": "2026-10-05T11:18:00Z", "owner": "w1", "worktree": "/wt/w1", "cmd": "progress", "id": "TB-0002", "gate_since": "2026-10-05T11:18:00Z", "blocked": "awaiting owner: merge #1751 (fibrosis receipt)", "waiting_on": "owner"}
{"ts": "2026-10-05T11:24:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "progress", "id": "TB-0004", "note": "DECISION NEEDED: convert or allowlist the 5 readers?"}
{"ts": "2026-10-05T12:00:00Z", "owner": "w2", "worktree": "/wt/w2", "cmd": "progress", "id": "TB-0003", "note": "restore script rehearsed"}
"""
FX_HUMAN = """standup for vil, 2026-10-05T07:00:00Z to 2026-10-05T12:30:00Z (12 events; last event 12:00Z, 30m ago)

NEEDS YOU (2)
  1. [reader 16h, due 2026-10-04] independent restore of THE-2348 - TB-0003 P-001 w2
  2. [owner 1h] merge #1751 (fibrosis receipt) - TB-0002 P-001 w1
     cmd: gh pr merge 1751 --merge

PEOPLE
  orch
    closed  TB-0009  superseded by #1739
    waiting TB-0005  26h pr:#1710 (rebase after #1706)
    next    TB-0007  Orphans packet
  w1
    done    TB-0001  #1744 merged as 2927583; docs follow-up: #1757
    doing   TB-0006  2h Viz cutover
                     last 11:00Z: rebased #1731 onto main, 140 tests pass
    gated   TB-0002  (needs you 2)
    next    TB-0007  Orphans packet
  w2
    waiting TB-0004  1h ci: #1752
    gated   TB-0003  (needs you 1)
    next    TB-0007  Orphans packet

PLANS
  P-001  Wave 2 payloads
         todo 1 (1 ready) | in progress 5 (1 doing, 2 waiting, 2 gated) | done 2 (+2)
  P-002  Docs
         todo 1 (1 ready) | in progress 0 (0 doing, 0 waiting, 0 gated) | done 0 !idle-ready TB-0008
  P-003  Proposals
         todo 1 (1 ready) | in progress 0 (0 doing, 0 waiting, 0 gated) | done 0

RISKS (6)
  !stale TB-0005 no update for 26h (orch)
  !quiet TB-0006 doing, silent 2h (w1)
  !due TB-0003 is past due 2026-10-04 (awaiting reader: independent restore of THE-2348)
  !stale-text TB-0005 names #1710, marked merged or done; update it with a note
  !follow-up TB-0001 evidence says "docs follow-up: #1757", nothing spawned
  !ask TB-0004 11:24Z w2: "DECISION NEEDED: convert or allowlist the 5 readers?" is not a gate

CHANGES
  new     TB-0008 P-002 Runbook page (orch, now ready)
  closed  TB-0009 superseded by #1739
  gates cleared (1): TB-0004 6.3h
  note    TB-0007 09:25Z orch: Decision: orphans go to data/_orphans/, owner agreed
  note    TB-0005 09:32Z w2: #1710 merged as e0dd38c

marker: next standup for vil starts 2026-10-05T12:30:00Z
"""
NOW = "2026-10-05T12:30:00Z"


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class BoardCase(unittest.TestCase):
    """A throwaway repo with worktrees; helpers only, no tests."""
    worktrees = 8
    extra_env: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self.tmp.name)
        self.env = {"XDG_DATA_HOME": os.path.join(root, "xdg"), "TASKBOARD_OWNER": "", "HERDR_ENV": "", "HERDR_PANE_ID": "",
                    **self.extra_env}
        self._old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.main = os.path.join(root, "repo")
        os.makedirs(self.main)
        sh(self.main, "git", "init", "-q")
        sh(self.main, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
        self.wts = []
        for n in range(self.worktrees):
            wt = os.path.join(root, f"wt{n}")
            sh(self.main, "git", "worktree", "add", "-q", "-b", f"b{n}", wt)
            self.wts.append(wt)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        for k, v in self._old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def tb(self, where, *argv):
        os.chdir(where)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tb.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def ok(self, where, *argv):
        code, out = self.tb(where, *argv)
        self.assertEqual(code, 0, out)
        return out.strip()

    def board_text(self):
        with open(os.path.join(self.main, ".taskboard", "board.md")) as f:
            return f.read()

    def seed(self, n=3):
        plan = self.ok(self.main, "plan", "Retry work", "--body", "Make retries safe.")
        ids = [self.ok(self.main, "add", f"Task {i}", "--plan", plan, "--outcome", "It works.",
                       "--done-when", "checks pass") for i in range(n)]
        return plan, ids


class TaskboardTest(BoardCase):
    def test_any_run_links_every_existing_worktree(self):
        self.ok(self.main, "list")
        for wt in self.wts:
            self.assertTrue(os.path.exists(os.path.join(wt, ".taskboard", "tb")), wt)

    def test_init_is_idempotent_and_hidden_from_git(self):
        for where in (self.main, self.wts[0], self.main):
            self.ok(where, "init")
        self.assertEqual(os.path.realpath(os.path.join(self.wts[0], ".taskboard")),
                         os.path.realpath(os.path.join(self.main, ".taskboard")))
        with open(os.path.join(self.main, ".git", "info", "exclude")) as f:
            self.assertEqual(f.read().splitlines().count("/.taskboard"), 1)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.wts[0], capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout, "")
        self.assertTrue(os.path.exists(os.path.join(self.main, ".taskboard", "tb")))

    def test_claim_follows_board_order_dependencies_and_the_wip_limit(self):
        plan = self.ok(self.main, "plan", "P")
        a = self.ok(self.main, "add", "A", "--plan", plan, "--outcome", "a", "--done-when", "x")
        b = self.ok(self.main, "add", "B", "--plan", plan, "--outcome", "b", "--done-when", "x", "--depends-on", a)
        c = self.ok(self.main, "add", "C", "--plan", plan, "--outcome", "c", "--done-when", "x")
        self.assertIn(a, self.ok(self.wts[0], "claim", "--plan", plan).splitlines()[0])
        code, out = self.tb(self.wts[0], "claim", "--plan", plan, "--wip", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("WIP limit 1", out)
        self.assertIn(c, self.ok(self.wts[1], "claim", "--plan", plan).splitlines()[0])
        code, out = self.tb(self.wts[2], "claim", "--plan", plan)
        self.assertEqual(code, 3)
        self.assertIn(f"needs {a}", out)
        self.ok(self.wts[0], "complete", a, "--evidence", "commit abc123")
        self.assertIn(b, self.ok(self.wts[0], "claim", "--plan", plan).splitlines()[0])
        self.ok(self.wts[0], "complete", b, "--evidence", "e")
        self.ok(self.wts[1], "complete", c, "--evidence", "e")
        code, out = self.tb(self.wts[0], "claim", "--plan", plan)
        self.assertEqual(code, 3)
        self.assertIn("every task is done", out)

    def test_ownership_force_release_and_progress(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim", "--plan", plan)
        self.assertEqual(self.tb(self.wts[1], "complete", a, "--evidence", "x")[0], 1)
        self.ok(self.wts[0], "progress", a, "--blocked", "waiting on API key", "--note", "half done")
        self.assertIn("blocked", self.ok(self.main, "list"))
        self.ok(self.wts[0], "claim", "--plan", plan)  # blocked task does not count against the worktree
        self.ok(self.wts[1], "release", a, "--handoff", "owner left", "--force")
        shown = self.ok(self.main, "show", a)
        self.assertIn("[Todo]", shown)
        self.assertIn("owner left", shown)
        self.assertNotIn("- worktree:", shown)
        self.assertIn(a, self.ok(self.wts[1], "claim", "--plan", plan).splitlines()[0])

    def test_close_moves_todo_and_in_progress_tasks_to_done_as_closed(self):
        plan = self.ok(self.main, "plan", "P")
        a, b, c = (self.ok(self.main, "add", n, "--plan", plan, "--outcome", "o", "--done-when", "x") for n in "ABC")
        self.ok(self.main, "add", "D", "--plan", plan, "--outcome", "o", "--done-when", "x", "--depends-on", a)
        self.ok(self.wts[0], "claim", b)
        self.assertEqual(self.tb(self.wts[1], "close", b, "--reason", "r")[0], 1)
        self.ok(self.main, "close", a, "--reason", "moved to ticket X-1")
        self.ok(self.wts[0], "close", b, "--reason", "dropped")
        shown = self.ok(self.main, "show", b)
        for text in ("closed: dropped", "- closed_at:", "- updated_at:"):
            self.assertIn(text, shown)
        for gone in ("- owner:", "- worktree:", "- claimed_at:"):
            self.assertNotIn(gone, shown)
        listed = self.ok(self.main, "list", "--all")
        self.assertEqual([l.split()[1] for l in listed.splitlines() if l.startswith("TB-")], ["ready", "ready", "closed", "closed"])
        self.assertIn(c, self.ok(self.wts[2], "claim", "--plan", plan))
        self.assertIn("TB-0004", self.ok(self.wts[3], "claim", "--plan", plan))  # D no longer waits on closed A
        self.assertEqual(self.tb(self.main, "close", a, "--reason", "again")[0], 1)
        with open(os.path.join(self.main, ".taskboard", "log.jsonl")) as f:
            self.assertIn('"cmd": "close", "id": "TB-0002", "from": "In progress", "to": "Done"', f.read())

    def test_same_owner_moves_worktree_and_other_owner_is_refused(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "--owner", "me", "claim", a)
        code, out = self.tb(self.wts[1], "--owner", "you", "complete", a, "--evidence", "x")
        self.assertEqual(code, 1)
        self.assertIn("--owner me", out)
        self.ok(self.wts[1], "--owner", "me", "complete", a, "--evidence", "x")
        shown = self.ok(self.main, "show", a)
        self.assertIn("[Done]", shown)
        self.assertIn(f"- worktree: {self.wts[1]}", shown)

    def test_two_owners_can_claim_in_one_worktree(self):
        plan, (a, b, _) = self.seed()
        self.ok(self.wts[0], "--owner", "x", "claim")
        self.assertEqual(self.tb(self.wts[0], "--owner", "x", "claim", "--wip", "1")[0], 1)
        self.assertIn(b, self.ok(self.wts[0], "--owner", "y", "claim"))

    def test_herdr_pane_is_the_default_owner(self):
        self.seed(1)
        os.environ.update(HERDR_ENV="1", HERDR_PANE_ID="p7")
        self.assertIn("- owner: herdr:p7", self.ok(self.wts[0], "claim"))

    def test_render_round_trips_and_hand_edit_errors_name_the_line(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim")
        self.ok(self.wts[0], "progress", a, "--note", "line one\nline two", "--handoff", "see commit")
        self.ok(self.wts[0], "progress", a, "--note", "#1657 merged\n## not a section")
        text = self.board_text()
        self.assertEqual(tb.Board(text).render(), text)
        with open(os.path.join(self.main, ".taskboard", "board.md"), "a") as f:
            f.write("\n### not a task\n")
        code, out = self.tb(self.main, "list")
        self.assertEqual(code, 1)
        self.assertIn(f"board.md:{len(text.splitlines()) + 2}:", out)
        with open(os.path.join(self.main, ".taskboard", "board.md"), "w") as f:
            f.write(text.replace(f"### {a} - Task 0\n- plan: {plan}", f"### {a} - Task 0\n- plan: {plan}\n- depends_on: {a}"))
        code, out = self.tb(self.main, "list")
        self.assertIn(f"{a} cannot depend on itself", out)

    def test_every_change_is_backed_up_logged_and_restorable(self):
        self.seed(2)
        good = self.board_text()
        with open(os.path.join(self.main, ".taskboard", "log.jsonl")) as f:
            self.assertEqual([l.count('"cmd"') for l in f], [1, 1, 1])
        os.remove(os.path.join(self.main, ".taskboard", "board.md"))
        self.assertNotIn("Task", self.ok(self.main, "list"))  # board recreated empty
        backups = self.ok(self.main, "restore").splitlines()[1:]
        self.assertEqual(len(backups), 3)  # each change saved the board as it was before
        self.ok(self.main, "restore", backups[0])  # newest: plan + first task
        self.ok(self.main, "add", "Task 1", "--plan", "P-001", "--outcome", "It works.", "--done-when", "checks pass")
        self.assertEqual(self.board_text(), good)

    def test_refused_mutation_is_logged_but_reads_and_exit_3_are_not(self):
        plan, (a, *_) = self.seed(1)
        self.ok(self.wts[0], "claim", a)
        log = os.path.join(self.main, ".taskboard", "log.jsonl")
        lines = lambda: pathlib.Path(log).read_text().splitlines()
        before = len(lines())
        code, out = self.tb(self.wts[1], "complete", a, "--evidence", "x")
        self.assertEqual(code, 1)
        self.assertIn("is held by", out)
        self.assertEqual(len(lines()), before + 1)
        entry = tb.json.loads(lines()[-1])
        self.assertEqual((entry["cmd"], entry["exit"], entry["id"], entry["worktree"]), ("complete", 1, a, self.wts[1]))
        self.assertIn("is held by", entry["reason"])
        self.assertEqual(self.tb(self.main, "show", "TB-9999")[0], 1)
        self.assertEqual(self.tb(self.wts[1], "claim", "--plan", plan)[0], 3)
        self.assertEqual(len(lines()), before + 1)

    def test_concurrent_claims_each_take_a_distinct_task(self):
        plan, ids = self.seed(8)
        procs = [subprocess.Popen([sys.executable, SCRIPT, "claim", "--plan", plan], cwd=wt,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for wt in self.wts]
        outs = [p.communicate()[0] for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 8, outs)
        claimed = sorted(o.split()[1] for o in outs)
        self.assertEqual(claimed, sorted(ids))
        board = tb.Board(self.board_text())
        self.assertEqual(len(board.items["In progress"]), 8)

    def test_quiet_flags_silent_doing_tasks_but_not_blocked_ones(self):
        plan, (a, b, _) = self.seed()
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[1], "claim", b)
        self.assertNotIn("!quiet", self.ok(self.main, "list"))
        with mock.patch.object(tb, "QUIET_MIN", 0):
            time.sleep(1.1)
            self.ok(self.wts[1], "progress", b, "--blocked", "waiting")
            rows = {l.split()[0]: l for l in self.ok(self.main, "list").splitlines()}
        self.assertIn("!quiet", rows[a])
        self.assertNotIn("!quiet", rows[b])

    def test_complete_takes_repeatable_evidence(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[0], "complete", a, "--evidence", "commit abc", "--evidence", "tests ok")
        shown = self.ok(self.main, "show", a)
        self.assertEqual(sum(1 for l in shown.splitlines() if l.startswith("- 20") and ("commit abc" in l or "tests ok" in l)), 2)
        self.assertLess(shown.index("commit abc"), shown.index("tests ok"))

    def rewrite(self, old, new):
        """Hand edit, as a user would between commands."""
        text = self.board_text()
        self.assertIn(old, text)
        pathlib.Path(self.main, ".taskboard", "board.md").write_text(text.replace(old, new))

    def set_field(self, key, value):
        """Hand edit the first `- key:` line on the board."""
        text = self.board_text()
        line = next(l for l in text.splitlines() if l.startswith(f"- {key}:"))
        self.rewrite(line, f"- {key}: {value}")

    def rows(self, *argv):
        return {l.split()[0]: l for l in self.ok(self.main, "list", *argv).splitlines()}

    def last_log(self):
        return tb.json.loads(pathlib.Path(self.main, ".taskboard", "log.jsonl").read_text().splitlines()[-1])

    def test_typed_waits_show_blocked_only_for_owner_reader_and_decision(self):
        plan, ids = self.seed(6)
        texts = ["awaiting owner: merge #12", "awaiting reader: restore", "awaiting decision: keep orphans?",
                 "awaiting ci", "paused for TB-0001", "awaiting pr:#12"]
        for n, (tid, text) in enumerate(zip(ids, texts)):
            self.ok(self.wts[n], "claim", tid)
            self.ok(self.wts[n], "progress", tid, "--blocked", text)
        rows = self.rows()
        self.assertEqual([rows[t].split()[1] for t in ids], ["blocked"] * 3 + ["waiting"] * 3)
        self.assertEqual(self.last_log()["waiting_on"], "pr:12")

    def test_gates_follow_after_order_print_commands_and_log_latency(self):
        plan, (a, b, c) = self.seed()
        for n, tid in enumerate((a, b, c)):
            self.ok(self.wts[n], "claim", tid)
        self.ok(self.wts[0], "progress", a, "--blocked", "awaiting owner: merge #1677\nafter: #1678\ncmd: gh pr merge 1677")
        self.ok(self.wts[1], "progress", b, "--blocked", "awaiting owner: merge #1678\ncmd: gh pr merge 1678")
        self.ok(self.wts[2], "progress", c, "--blocked", "awaiting reader: restore THE-1\ndue: 2026-10-09")
        out = self.ok(self.main, "gates")
        self.assertLess(out.index("merge #1678"), out.index("merge #1677"))
        self.assertIn("   after: #1678\n   cmd: gh pr merge 1677", out)
        self.assertIn("[reader ", out)
        self.assertIn("due 2026-10-09", out)
        self.ok(self.wts[1], "progress", b, "--blocked", "none")
        entry = self.last_log()
        self.assertEqual((entry["blocked"], entry["waiting_on"]), ("none", "none"))
        self.assertTrue(entry["gate_since"] <= entry["gate_cleared_at"])
        self.assertNotIn("merge #1678", self.ok(self.main, "gates"))
        self.ok(self.wts[0], "complete", a, "--evidence", "merged")
        self.assertIn("gate_cleared_at", self.last_log())
        self.assertNotIn("- cmd:", self.ok(self.main, "show", a))

    def test_gates_with_none_open_and_bad_gate_lines(self):
        plan, (a, *_) = self.seed()
        self.assertEqual(self.ok(self.main, "gates"), "no open gates")
        self.ok(self.wts[0], "claim", a)
        code, out = self.tb(self.wts[0], "progress", a, "--blocked", "awaiting owner: x\nwhen: later")
        self.assertEqual(code, 1)
        self.assertIn("after:, cmd:, due:", out)
        self.assertEqual(self.tb(self.wts[0], "progress", a, "--blocked", "awaiting owner: x\ndue: soon")[0], 1)

    def test_log_keeps_blocked_note_and_handoff_apart(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[0], "progress", a, "--blocked", "awaiting ci", "--note", "pushed", "--handoff", "see PR")
        entry = self.last_log()
        self.assertEqual((entry["blocked"], entry["note"], entry["handoff"]), ("awaiting ci", "pushed", "see PR"))

    def test_stale_counts_from_last_update_and_old_gates_are_flagged(self):
        plan, (a, b, _) = self.seed()
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[1], "claim", b)
        self.ok(self.wts[1], "progress", b, "--blocked", "awaiting owner: upload")
        self.set_field("claimed_at", "2000-01-01T00:00:00Z")  # old claim, fresh update
        self.assertNotIn("!stale", self.ok(self.main, "list"))
        self.set_field("updated_at", "2000-01-01T00:00:00Z")
        self.set_field("gate_since", "2000-01-01T00:00:00Z")
        rows = self.rows()
        self.assertIn("!stale", rows[a])
        self.assertIn("!gate", rows[b])

    def test_idle_ready_flags_a_plan_with_ready_work_and_nobody_doing(self):
        plan, (a, *_) = self.seed()
        self.assertIn("!idle-ready", self.rows()[plan])
        self.ok(self.wts[0], "claim", a)
        self.assertNotIn("!idle-ready", self.rows()[plan])
        self.ok(self.wts[0], "progress", a, "--blocked", "awaiting owner: merge")
        self.assertIn("!idle-ready", self.rows()[plan])
        proposals = self.ok(self.main, "plan", "Proposals")
        self.ok(self.main, "add", "Idea", "--plan", proposals, "--outcome", "o", "--done-when", "x")
        self.assertNotIn("!idle-ready", self.rows()[proposals])

    def test_check_exits_4_on_overdue_gate_idle_plan_or_stale_pr_text_and_always_self_tests(self):
        plan, (a, b, c) = self.seed()
        code, out = self.tb(self.main, "list", "--check")
        self.assertEqual(code, 4, out)
        self.assertIn("!idle-ready", out)
        self.assertIn("check: scanned 3 open tasks", out)
        self.ok(self.wts[0], "claim", a)
        self.assertIn("0 problems", self.ok(self.main, "list", "--check"))
        self.ok(self.wts[1], "claim", b)
        self.ok(self.wts[1], "progress", b, "--blocked", "awaiting reader: restore\ndue: 2000-01-01")
        self.assertIn(f"{b} is past due 2000-01-01", self.tb(self.main, "list", "--check")[1])
        self.ok(self.wts[1], "progress", b, "--blocked", "none")
        self.rewrite("- due: 2000-01-01", "- due: 2999-01-01")
        self.ok(self.main, "add", "Close out", "--plan", plan, "--outcome", "o", "--done-when", "PR 1642 re-approved")
        self.ok(self.wts[0], "progress", a, "--note", "merged origin/main into PR 1642")
        self.assertIn("0 problems", self.ok(self.main, "list", "--check"))
        self.ok(self.wts[0], "complete", a, "--evidence", "PR #1642 merged as abc123")
        code, out = self.tb(self.main, "list", "--check")
        self.assertEqual(code, 4, out)
        self.assertIn("TB-0004 names #1642", out)

    def test_boards_written_before_typed_waits_still_read(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[0], "progress", a, "--blocked", "waiting")
        self.rewrite("- blocked: waiting", "- blocked: awaiting coordinator: run DuckLake publish")
        self.assertEqual(self.rows()[a].split()[1], "blocked")
        self.assertIn("[owner ", self.ok(self.main, "gates"))


    def test_hash_lines_are_kept_in_notes_and_escaped_in_raw_text(self):
        plan = self.ok(self.main, "plan", "P", "--body", "#1 goal\n## not a section")
        a = self.ok(self.main, "add", "#12 fix", "--plan", plan, "--outcome", "#1657 merged\n## x", "--done-when", "#1 ok")
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[0], "progress", a, "--note", "#1657 merged\n## not a section")
        shown = self.ok(self.main, "show", a)
        for text in ("### TB-0001 - #12 fix", "\\#1657 merged\n\\## x", "- #1 ok", ": #1657 merged\n  ## not a section"):
            self.assertIn(text, shown)
        self.assertIn("\\#1 goal", self.ok(self.main, "show", plan))
        self.assertEqual(tb.Board(self.board_text()).render(), self.board_text())

    def test_note_appends_for_anyone_without_changing_state(self):
        plan, (a, b, _) = self.seed()
        self.ok(self.wts[0], "claim", a)
        before = self.ok(self.main, "show", a)
        self.ok(self.wts[1], "note", a, "#1642 merged 08:34Z; done-when 2 is moot")
        after = self.ok(self.main, "show", a)
        self.assertIn("claude@wt1: #1642 merged 08:34Z", after)
        self.assertEqual([l for l in before.splitlines() if l.startswith("- ") and not l.startswith("- 20")],
                         [l for l in after.splitlines() if l.startswith("- ") and not l.startswith("- 20")])
        for target in (b, plan):
            self.ok(self.wts[2], "note", target, "fact")
        entry = self.last_log()
        self.assertEqual((entry["cmd"], entry["id"], entry["note"]), ("note", plan, "fact"))

    def test_edit_rewrites_task_text_with_old_and_new_in_the_log(self):
        plan, (a, b, c) = self.seed()
        self.ok(self.wts[1], "edit", a, "--title", "Renamed", "--outcome", "New outcome.", "--done-when", "y",
                "--done-when", "z", "--depends-on", b, "--set", "due=2026-10-09", "--set", "prs=#1677 packet")
        shown = self.ok(self.main, "show", a)
        for text in ("### TB-0001 - Renamed", f"- depends_on: {b}", "- due: 2026-10-09", "- prs: #1677 packet",
                     "#### Outcome\nNew outcome.\n\n#### Done when\n- y\n- z"):
            self.assertIn(text, shown)
        entry = self.last_log()
        self.assertEqual((entry["old"]["title"], entry["new"]["title"]), ("Task 0", "Renamed"))
        self.assertEqual((entry["old"]["done_when"], entry["new"]["done_when"]), (["checks pass"], ["y", "z"]))
        self.assertNotIn("step", entry["new"])
        self.assertEqual(tb.Board(self.board_text()).render(), self.board_text())
        self.ok(self.wts[1], "edit", a, "--depends-on", "none", "--set", "due=none")
        self.assertNotIn("- due:", self.ok(self.main, "show", a))
        code, out = self.tb(self.wts[1], "edit", a, "--depends-on", a)
        self.assertIn(f"{a} cannot depend on itself", out)
        for bad in ("owner=me", "due=soon"):
            self.assertEqual(self.tb(self.wts[1], "edit", a, "--set", bad)[0], 1, bad)
        self.ok(self.wts[0], "claim", c)
        self.assertEqual(self.tb(self.wts[1], "edit", c, "--title", "x")[0], 1)
        self.ok(self.wts[1], "edit", c, "--title", "x", "--force")
        self.ok(self.wts[0], "complete", c, "--evidence", "e")
        self.assertIn("use note", self.tb(self.wts[0], "edit", c, "--title", "y")[1])
        self.assertIn("- linear: THE-1", self.ok(self.main, "show", self.ok(
            self.main, "add", "T", "--plan", plan, "--outcome", "o", "--done-when", "x", "--set", "linear=THE-1")))

    def test_wip_counts_doing_and_waiting_but_not_gated_tasks(self):
        plan, ids = self.seed(4)
        self.ok(self.wts[0], "claim")
        self.ok(self.wts[0], "claim")
        self.assertEqual(self.tb(self.wts[0], "claim")[0], 1)
        self.ok(self.wts[0], "progress", ids[0], "--blocked", "awaiting ci")
        self.assertEqual(self.tb(self.wts[0], "claim")[0], 1)
        self.ok(self.wts[0], "progress", ids[0], "--blocked", "awaiting owner: merge #1")
        self.ok(self.wts[0], "claim")
        self.ok(self.wts[0], "claim", "--wip", "3")

    def test_complete_spawns_follow_ups_and_warns_when_one_is_mentioned_but_missing(self):
        plan, (a, b, _) = self.seed()
        self.ok(self.wts[0], "claim", a)
        out = self.ok(self.wts[0], "complete", a, "--evidence", "PR #1 merged", "--spawn", "Fix the README",
                      "--cleanup", "worktree removed")
        self.assertIn("spawned TB-0004", out)
        shown = self.ok(self.main, "show", a)
        self.assertIn("- spawned: TB-0004", shown)
        self.assertIn("- cleanup: worktree removed", shown)
        spawned = self.ok(self.main, "show", "TB-0004")
        self.assertIn("[Todo]", spawned)
        self.assertIn(f"Follow-up of {a}", spawned)
        self.ok(self.wts[0], "claim", b)
        self.assertIn("warning:", self.ok(self.wts[0], "complete", b, "--evidence", "body lines left to their owners"))

    def test_new_archives_board_log_and_backups_beside_the_board_dir_then_resets(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[0], "complete", a, "--evidence", "e")
        pathlib.Path(self.main, ".taskboard", "retro-x.md").write_text("report")
        old_board, backups = self.board_text(), self.ok(self.main, "restore").splitlines()[1:]
        out = self.ok(self.wts[1], "new")
        archives = list(pathlib.Path(self.main, ".git").glob("backup-*Z.tar.gz"))
        self.assertEqual(len(archives), 1, out)
        self.assertIn(str(archives[0]), out)
        with tb.tarfile.open(archives[0]) as tar:
            names = set(tar.getnames())
            self.assertEqual(tar.extractfile("taskboard/board.md").read().decode(), old_board)
        expected = {"taskboard/board.md", "taskboard/log.jsonl", "taskboard/retro-x.md", *(f"backups/{n}" for n in backups)}
        self.assertEqual(names, expected)
        for name in expected:
            self.assertIn(name, out)
        self.assertEqual(self.board_text(), tb.TEMPLATE)
        self.assertEqual([l["cmd"] for l in map(tb.json.loads, pathlib.Path(self.main, ".taskboard", "log.jsonl")
                                                .read_text().splitlines())], ["new"])
        self.assertIn("no backups", self.ok(self.main, "restore"))
        self.assertTrue(pathlib.Path(self.main, ".taskboard", "retro-x.md").exists())

    def test_new_refuses_while_tasks_are_in_progress_unless_forced(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim", a)
        code, out = self.tb(self.main, "new")
        self.assertEqual(code, 1)
        self.assertIn(f"{a} still in progress", out)
        self.assertEqual(list(pathlib.Path(self.main, ".git").glob("backup-*.tar.gz")), [])
        self.assertIn(a, self.board_text())
        self.ok(self.main, "new", "--force")
        self.assertEqual(self.board_text(), tb.TEMPLATE)


class StandupTest(BoardCase):
    worktrees = 0
    extra_env = {"TASKBOARD_OPERATOR": "vil", "CLAUDECODE": ""}

    def setUp(self):
        super().setUp()
        codex = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("CODEX")}
        self.addCleanup(os.environ.update, codex)
        self.now = NOW
        patch = mock.patch.object(tb, "utcnow", lambda: tb.parse_ts(self.now))
        patch.start()
        self.addCleanup(patch.stop)
        self.ok(self.main, "init")
        self.dir = pathlib.Path(self.main, ".git", "taskboard")
        self.write(FX_BOARD, FX_LOG, "2026-10-05T07:00:00Z")

    def write(self, board=None, log=None, marker=None):
        for name, text in (("board.md", board), ("log.jsonl", log), ("standup-vil", marker)):
            if text is not None:
                (self.dir / name).write_text(text if name != "standup-vil" else text + "\n")

    def append(self, line):
        with open(self.dir / "log.jsonl", "a") as f:
            f.write(line + "\n")

    def marker(self, name="vil"):
        path = self.dir / f"standup-{name}"
        return path.read_text().strip() if path.is_file() else None

    def standup(self, *argv):
        return self.ok(self.main, "standup", *argv)

    def section(self, out, head):
        lines = out.splitlines()
        start = next(n for n, line in enumerate(lines) if line.startswith(head)) + 1
        end = next((n for n in range(start, len(lines)) if not lines[n].strip()), len(lines))
        return lines[start:end]

    def test_default_run_prints_the_round_gates_first_and_reads_only(self):  # spec tests 1, 2, 3, 5, 6, 9, 20, 31
        before = [(self.dir / n).read_bytes() for n in ("board.md", "log.jsonl")]
        out = self.standup()
        self.assertEqual(out, FX_HUMAN.strip())
        self.assertEqual(self.section(out, "NEEDS YOU"), ["  " + line for line in self.ok(self.main, "gates").splitlines()])
        self.assertEqual([(self.dir / n).read_bytes() for n in ("board.md", "log.jsonl")], before)
        self.assertFalse(os.path.exists(tb.Repo().backups))

    def test_next_is_the_claimable_task_or_why_not(self):  # 4
        self.ok(self.main, "--owner", "w2", "claim", "TB-0007")
        out = self.standup("--no-mark")
        people = "\n".join(self.section(out, "PEOPLE"))
        self.assertIn("  w2\n    waiting TB-0004  1h ci: #1752\n    doing   TB-0007  0m Orphans packet\n"
                      "    gated   TB-0003  (needs you 1)\n    next    -        (WIP 2/2)", people)
        self.assertEqual(people.count("    next    -        (nothing ready in P-001)"), 2)

    def test_spawned_follow_up_and_gated_ask_are_not_risks(self):  # 7, 8
        board = FX_BOARD.replace("- completed_at: 2026-10-05T09:00:00Z", "- completed_at: 2026-10-05T09:00:00Z\n- spawned: TB-0011")
        board = board.replace("## In progress", "### TB-0011 - Docs line\n- plan: P-001\n\n#### Outcome\nx\n\n#### Done when\n- y\n\n"
                              "## In progress")
        board = board.replace("- blocked: awaiting ci: #1752", "- blocked: awaiting decision: convert or allowlist?\n"
                              "- gate_since: 2026-10-05T11:24:00Z")
        self.write(board, FX_LOG.replace('"note": "#1744 merged as 2927583; docs follow-up: #1757"',
                                         '"note": "#1744 merged as 2927583; docs follow-up: #1757", "spawned": ["TB-0011"]'))
        out = self.standup("--no-mark")
        self.assertNotIn("!follow-up", out)
        self.assertNotIn("!ask", out)
        self.assertIn("NEEDS YOU (3)", out)
        self.assertIn("  new     TB-0011 P-001 Docs line (w1, now ready)", out)

    def test_marker_advances_once_and_the_window_is_half_open(self):  # 10, 11
        self.append(json.dumps({"ts": "2026-10-05T12:29:59Z", "owner": "orch", "cmd": "note", "id": "TB-0007", "note": "early"}))
        self.assertIn("orch: early", self.standup())
        self.assertEqual(self.marker(), NOW)
        self.append(json.dumps({"ts": NOW, "owner": "orch", "cmd": "note", "id": "TB-0007", "note": "late"}))
        self.now = "2026-10-05T12:40:00Z"
        out = self.standup()
        self.assertIn("(1 events; last event 12:30Z, 10m ago)", out)
        self.assertIn("orch: late", out)
        self.assertNotIn("orch: early", out)
        self.assertEqual([r.split()[:2] for r in self.section(out, "RISKS")],
                         [["!stale", "TB-0005"], ["!quiet", "TB-0006"], ["!due", "TB-0003"], ["!stale-text", "TB-0005"],
                          ["!ask", "TB-0004"]])
        self.assertEqual(self.marker(), "2026-10-05T12:40:00Z")
        self.now = "2026-10-05T12:50:00Z"
        out = self.standup()
        self.assertIn("(0 events;", out)
        self.assertEqual(self.section(out, "CHANGES"), ["  none"])

    def test_only_a_default_human_run_moves_the_marker(self):  # 12, 13
        for argv, why in ((["--no-mark"], "--no-mark"), (["--since", "2h"], "filtered"), (["--plan", "P-001"], "filtered"),
                          (["--owner", "w1"], "filtered")):
            self.assertTrue(self.standup(*argv).endswith(f"marker: unchanged ({why})"), argv)
            self.assertEqual(self.marker(), "2026-10-05T07:00:00Z")
        (self.dir / "standup-vil").unlink()
        for argv in (["--no-mark"], ["--since", "2h"], ["--plan", "P-001"], ["--owner", "w1"]):
            self.standup(*argv)
            self.assertIsNone(self.marker(), argv)
        for key in ("CLAUDECODE", "CODEX_THREAD_ID"):
            os.environ[key] = "1"
            self.addCleanup(os.environ.pop, key, None) if key != "CLAUDECODE" else None
            self.assertTrue(self.standup().endswith("marker: unchanged (agent run; --mark to advance)"), key)
            self.assertIsNone(self.marker())
            self.standup("--mark")
            self.assertEqual(self.marker(), NOW)
            (self.dir / "standup-vil").unlink()
            os.environ.pop(key) if key != "CLAUDECODE" else os.environ.__setitem__(key, "")

    def test_missing_future_or_unreadable_marker_means_the_last_24h(self):  # 14, 17, 32
        (self.dir / "standup-vil").unlink()
        out = self.standup()
        self.assertIn("2026-10-04T12:30:00Z to 2026-10-05T12:30:00Z (16 events;", out)
        self.assertIn("no previous standup for vil, last 24h", out)
        self.assertEqual(self.marker(), NOW)
        self.write(marker="2026-10-06T00:00:00Z")
        self.assertIn("marker 2026-10-06T00:00:00Z is in the future, last 24h", self.standup())
        self.assertEqual(self.marker(), NOW)
        (self.dir / "standup-vil").unlink()
        (self.dir / "standup-vil").mkdir()
        code, out = self.tb(self.main, "standup")
        self.assertEqual(code, 0, out)
        self.assertIn("no previous standup for vil", out)
        self.assertIn("taskboard: marker not saved:", out)
        self.assertIn("\nCHANGES\n", out)
        self.assertIn("\nmarker: unchanged (write failed)\ntaskboard: marker not saved:", out)
        self.assertEqual([p.name for p in self.dir.glob("standup-vil.tmp*")], [])
        data = json.loads(self.tb(self.main, "standup", "--json")[1].split("taskboard: marker not saved")[0])
        self.assertEqual((data["marked"], data["marker_reason"]), (False, "write failed"))

    def test_since_forms_and_errors(self):  # 15
        for arg, start in (("90m", "2026-10-05T11:00:00Z"), ("2d", "2026-10-03T12:30:00Z"), ("2026-10-05", "2026-10-05T00:00:00Z"),
                           ("2026-10-05T07:00Z", "2026-10-05T07:00:00Z"), ("2026-10-05T07:00:00Z", "2026-10-05T07:00:00Z")):
            self.assertTrue(self.standup("--since", arg).startswith(f"standup for vil, {start} to {NOW}"), arg)
        self.assertTrue(self.standup("--since", "2026-10-05T09:00+02:00").startswith("standup for vil, 2026-10-05T07:00:00Z to"))
        log = (self.dir / "log.jsonl").read_bytes()
        for arg in ("yesterday", "2026-10-06", "9999999d"):
            code, out = self.tb(self.main, "standup", "--since", arg)
            self.assertEqual(code, 1, out)
            self.assertIn("--since", out)
        self.assertEqual((self.dir / "log.jsonl").read_bytes(), log)

    def test_operators_keep_separate_markers(self):  # 16
        os.environ["TASKBOARD_OPERATOR"] = "hynek"
        self.standup()
        self.assertEqual(self.marker("hynek"), NOW)
        self.assertEqual(self.marker(), "2026-10-05T07:00:00Z")
        os.environ["TASKBOARD_OPERATOR"] = "a/b c"
        self.standup()
        self.assertEqual(self.marker("a_b_c"), NOW)

    def test_malformed_board_fails_and_bad_log_lines_are_skipped(self):  # 18, 19, 29
        log = (self.dir / "log.jsonl").read_bytes()
        self.write(FX_BOARD.replace("\n## Done\n", "\n## Finished\n"))
        code, out = self.tb(self.main, "standup")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"board\.md:\d+: unknown section")
        self.assertEqual((self.dir / "log.jsonl").read_bytes(), log)
        self.assertEqual(self.marker(), "2026-10-05T07:00:00Z")
        self.write(FX_BOARD)
        self.append(json.dumps({"ts": "2026-10-05T12:05:00Z", "owner": "orch", "cmd": "note", "id": "TB-0099", "note": "gone"}))
        self.append('{"cmd": "note"}')
        self.append('{"ts": "2026-10-05T12:10:00Z", "own')
        out = self.standup()
        self.assertIn("(12 events; last event 12:05Z, 25m ago; skipped 2 unreadable log lines)", out)
        self.assertNotIn("TB-0099", out)

    def test_long_note_lists_are_capped_in_text_but_not_in_json(self):  # 21
        for n in range(12):
            self.append(json.dumps({"ts": f"2026-10-05T10:{n:02d}:00Z", "owner": "orch", "cmd": "note", "id": "TB-0007",
                                    "note": f"note {n}"}))
        out = self.standup("--no-mark")
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("  note ")), 8)
        self.assertIn("  ... +6 more: TB-0007\n", out)
        notes = json.loads(self.standup("--json", "--no-mark"))["changes"]["notes"]
        self.assertEqual(len(notes), 14)
        gated = "".join(f"### TB-{n:04d} - Gate {n}\n- plan: P-001\n- owner: w3\n- worktree: /wt/w3\n- claimed_at: {NOW}\n"
                        f"- updated_at: {NOW}\n- blocked: awaiting owner: merge #{n}\n- gate_since: {NOW}\n\n"
                        for n in range(20, 30))
        self.write(FX_BOARD.replace("\n## Done\n", "\n" + gated + "## Done\n"))
        needs = self.section(self.standup("--no-mark"), "NEEDS YOU (12)")
        self.assertEqual([line.split(".")[0].strip() for line in needs if not line.startswith("     ")],
                         [str(n) for n in range(1, 13)])

    def test_markdown_json_and_brief(self):  # 22, 23, 24
        md = self.standup("--markdown")
        self.assertFalse([line for line in md.splitlines() if line.startswith(("#", "|"))])
        for head in ("*Needs you (2)*", "*People*", "*Plans*", "*Risks (6)*", "*Changes*"):
            self.assertIn("\n" + head + "\n", md)
        self.assertIn("\n   cmd: `gh pr merge 1751 --merge`\n", md)
        self.assertIn("- gated `TB-0002` (needs you 2)", md)
        for line in md.splitlines():  # the id columns: gate lines, owner rows, plans, risks, changes
            head = re.match(r"(\d+\. \[.*?\] .* - .*|- \w[\w-]* [^ ]+|- !\S+ \S+|- `P-\d+`)", line)
            if head and not line.startswith("- next -"):
                self.assertNotRegex(re.sub(r"`[^`]*`", "", head.group(0)).split("] ")[-1].split(" - ")[-1],
                                    r"\b(TB|P)-\d+\b", line)
        self.assertNotIn("marker:", md)
        self.write(marker="2026-10-05T07:00:00Z")
        data = json.loads(self.standup("--json"))
        self.assertEqual(list(data), ["operator", "window", "filters", "marked", "marker_reason", "needs_you", "owners",
                                      "plans", "risks", "changes"])
        self.assertEqual((data["window"]["source"], data["marked"]), ("marker", True))
        w1 = next(o for o in data["owners"] if o["owner"] == "w1")
        self.assertEqual(w1["done"][0]["evidence"], "#1744 merged as 2927583; docs follow-up: #1757")
        self.assertEqual(["  " + r for r in data["risks"]], self.section(FX_HUMAN, "RISKS"))
        self.write(marker="2026-10-05T07:00:00Z")
        brief = self.standup("--brief").splitlines()
        self.assertEqual([line.split()[0] for line in brief[-5:]], ["PEOPLE", "PLANS", "RISKS", "CHANGES", "marker:"])
        self.assertEqual(brief[1:5], ["NEEDS YOU (2)", *self.section(FX_HUMAN, "NEEDS YOU")])

    def test_owner_and_plan_filters(self):  # 25, 26, 30
        out = self.ok(self.main, "--owner", "orch", "standup", "--owner", "w1")
        self.assertEqual(self.section(out, "NEEDS YOU (1)"), ["  2. [owner 1h] merge #1751 (fibrosis receipt) - TB-0002 P-001 w1",
                                                              "     cmd: gh pr merge 1751 --merge"])
        self.assertEqual(self.section(out, "PEOPLE"), self.section(FX_HUMAN, "PEOPLE")[4:10])
        self.assertEqual([line for line in self.section(out, "PLANS") if line.startswith("  P-")], ["  P-001  Wave 2 payloads"])
        self.assertEqual([r.split()[:2] for r in self.section(out, "RISKS")], [["!quiet", "TB-0006"], ["!follow-up", "TB-0001"]])
        self.assertEqual(self.section(out, "CHANGES"), ["  none"])
        self.assertEqual(tb.parser().parse_args(["--owner", "orch", "standup", "--owner", "w1"]).owner, "orch")
        out = self.standup("--plan", "P-002")
        self.assertEqual(self.section(out, "NEEDS YOU (0)"), ["  no open gates"])
        self.assertEqual(self.section(out, "PEOPLE"), ["  none"])
        self.assertEqual([line for line in self.section(out, "PLANS") if line.startswith("  P-")], ["  P-002  Docs"])
        self.assertEqual(self.section(out, "CHANGES"), ["  new     TB-0008 P-002 Runbook page (orch, now ready)"])
        code, out = self.tb(self.main, "standup", "--owner", "")
        self.assertEqual((code, self.marker()), (1, "2026-10-05T07:00:00Z"), out)
        code, out = self.tb(self.main, "standup", "--owner", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("no such owner nobody", out)
        code, out = self.tb(self.main, "standup", "--plan", "P-009")
        self.assertEqual(code, 1)
        self.assertIn("no such plan P-009", out)

    def test_reads_only_on_every_kind_of_run(self):  # 20
        before = [(self.dir / n).read_bytes() for n in ("board.md", "log.jsonl")]
        for argv in ([], ["--no-mark"], ["--since", "2h"], ["--plan", "P-001"], ["--owner", "w1"], ["--brief"], ["--markdown"],
                     ["--json"], ["--mark"], ["--since", "nope"], ["--plan", "P-009"]):
            self.tb(self.main, "standup", *argv)
            self.assertEqual([(self.dir / n).read_bytes() for n in ("board.md", "log.jsonl")], before, argv)
        self.assertFalse(os.path.exists(tb.Repo().backups))

    def test_ask_is_answered_by_a_later_gate_or_unblock_and_reads_any_owner_label(self):
        progress = ("- 2026-10-05T11:24:00Z w2: DECISION NEEDED: convert or allowlist the 5 readers?\n"
                    "- 2026-10-05T11:30:00Z w2: blocked: awaiting decision: convert or allowlist?\n"
                    "- 2026-10-05T12:00:00Z w2: blocked: awaiting ci: #1752")
        self.write(FX_BOARD.replace("- 2026-10-05T11:24:00Z w2: DECISION NEEDED: convert or allowlist the 5 readers?", progress))
        self.assertNotIn("!ask", self.standup("--no-mark"))
        board = FX_BOARD.replace("- 2026-10-05T11:00:00Z w1: rebased #1731 onto main, 140 tests pass",
                                 "- 2026-10-05T11:00:00Z claude@my repo: rebased #1731 onto main\n  second line\n"
                                 "- 2026-10-05T11:05:00Z claude@my repo: first line\n  decision needed: keep both readers?")
        self.write(board.replace("- blocked: awaiting pr:#1710 (rebase after #1706)", "- blocked: awaiting legal sign-off"))
        out = self.standup("--no-mark")
        self.assertIn("                     last 11:05Z: first line", out)
        self.assertIn('!ask TB-0006 11:05Z claude@my repo: "decision needed: keep both readers?" is not a gate', out)
        self.assertIn("    blocked TB-0005  26h awaiting legal sign-off", out)
        self.write(FX_BOARD.replace("- 2026-10-05T11:00:00Z w1: rebased #1731 onto main, 140 tests pass",
                                    "- 2026-10-05T11:00:00Z w1: rebased\n- see the PR thread for details: decision needed"))
        out = self.standup("--no-mark")
        self.assertIn('!ask TB-0006 11:00Z w1: "- see the PR thread for details: decision needed"', out)  # a continuation line
        self.assertIn("last 11:00Z: rebased", out)

    def test_a_render_crash_leaves_the_marker(self):
        with mock.patch.object(tb, "gate_block", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self.tb(self.main, "standup")
        self.assertEqual(self.marker(), "2026-10-05T07:00:00Z")

    def test_odd_log_events_and_hand_edited_times_are_skipped_not_fatal(self):
        base = {"ts": "2026-10-05T12:05:00Z", "owner": "orch", "cmd": "note", "id": "TB-0007", "note": "x"}
        for odd in ({"owner": None}, {"cmd": None}, {"id": ["TB-0007"]}, {"cmd": "complete", "spawned": None},
                    {"cmd": "edit", "new": None}, {"cmd": "close", "note": None}, {"cmd": "progress", "gate_since": "2026-10-05", "gate_cleared_at": NOW}):
            self.append(json.dumps({k: v for k, v in {**base, **odd}.items() if v is not None or k in ("spawned", "new", "note")}))
        self.append("[1, 2]")
        self.write(FX_BOARD.replace("- completed_at: 2026-10-05T09:00:00Z", "- completed_at: 2026-10-05 09:00"))
        out = self.standup("--no-mark")
        self.assertIn("; skipped 8 unreadable log lines)", out)
        self.assertNotIn("!follow-up", out)

    def test_old_logs_and_a_fresh_board(self):  # 27, 28
        old = [json.loads(line) for line in FX_LOG.splitlines()]
        for e in old:
            if "blocked" in e:
                e["note"] = e.pop("blocked")
                e.pop("waiting_on", None), e.pop("gate_since", None), e.pop("gate_cleared_at", None)
        self.write(log="".join(json.dumps(e) + "\n" for e in old))
        out = self.standup("--no-mark")
        self.assertNotIn("gates cleared", out)
        self.assertEqual(out.count("!ask"), 1)
        self.ok(self.main, "--owner", "orch", "new", "--force")
        self.now = "2026-10-05T12:31:00Z"
        out = self.standup()
        self.assertIn("  board   new by orch at 12:30Z", out)
        self.assertIn("  no open gates", out)
        for head in ("PEOPLE", "PLANS", "RISKS"):
            self.assertEqual(self.section(out, head), ["  none"], head)


if __name__ == "__main__":
    unittest.main()
