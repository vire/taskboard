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
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
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
        with open(os.path.join(self.main, ".taskboard", "log.jsonl")) as f:
            self.assertIn(f'"moved_from": "{self.wts[0]}"', f.read())

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
        os.environ["TASKBOARD_QUIET_MIN"] = "0"
        try:
            time.sleep(1.1)
            self.ok(self.wts[1], "progress", b, "--blocked", "waiting")
            rows = {l.split()[0]: l for l in self.ok(self.main, "list").splitlines()}
        finally:
            del os.environ["TASKBOARD_QUIET_MIN"]
        self.assertIn("!quiet", rows[a])
        self.assertNotIn("!quiet", rows[b])

    def test_complete_takes_repeatable_evidence(self):
        plan, (a, *_) = self.seed()
        self.ok(self.wts[0], "claim", a)
        self.ok(self.wts[0], "complete", a, "--evidence", "commit abc", "--evidence", "tests ok")
        shown = self.ok(self.main, "show", a)
        self.assertEqual(sum(1 for l in shown.splitlines() if l.startswith("- 20") and ("commit abc" in l or "tests ok" in l)), 2)
        self.assertLess(shown.index("commit abc"), shown.index("tests ok"))


if __name__ == "__main__":
    unittest.main()
