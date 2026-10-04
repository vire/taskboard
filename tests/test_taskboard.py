"""Run with: python3 -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import os
import pathlib
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


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class TaskboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self.tmp.name)
        self.env = {"XDG_DATA_HOME": os.path.join(root, "xdg"), "TASKBOARD_OWNER": "", "HERDR_ENV": "", "HERDR_PANE_ID": ""}
        self._old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.main = os.path.join(root, "repo")
        os.makedirs(self.main)
        sh(self.main, "git", "init", "-q")
        sh(self.main, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
        self.wts = []
        for n in range(8):
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

    def test_claim_follows_board_order_dependencies_and_one_task_per_worktree(self):
        plan = self.ok(self.main, "plan", "P")
        a = self.ok(self.main, "add", "A", "--plan", plan, "--outcome", "a", "--done-when", "x")
        b = self.ok(self.main, "add", "B", "--plan", plan, "--outcome", "b", "--done-when", "x", "--depends-on", a)
        c = self.ok(self.main, "add", "C", "--plan", plan, "--outcome", "c", "--done-when", "x")
        self.assertIn(a, self.ok(self.wts[0], "claim", "--plan", plan).splitlines()[0])
        code, out = self.tb(self.wts[0], "claim", "--plan", plan)
        self.assertEqual(code, 1, out)
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
        self.assertEqual(self.tb(self.wts[0], "--owner", "x", "claim")[0], 1)
        self.assertIn(b, self.ok(self.wts[0], "--owner", "y", "claim"))

    def test_herdr_pane_is_the_default_owner(self):
        self.seed(1)
        os.environ.update(HERDR_ENV="1", HERDR_PANE_ID="p7")
        self.assertIn("- owner: herdr:p7", self.ok(self.wts[0], "claim"))

    def test_render_round_trips_and_hand_edit_errors_name_the_line(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim")
        self.ok(self.wts[0], "progress", a, "--note", "line one\nline two", "--handoff", "see commit")
        self.assertEqual(self.tb(self.wts[0], "progress", a, "--note", "ok\n## not a section")[0], 1)
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
        self.assertIn("cycle", out)

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


if __name__ == "__main__":
    unittest.main()
