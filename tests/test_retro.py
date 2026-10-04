"""Run with: python3 -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "skills", "retro", "retro.py")
spec = importlib.util.spec_from_file_location("retro", SCRIPT)
retro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retro)

BOARD = """# Taskboard

## Plans

### P-001 - Plan

## Todo

### TB-0003 - Rebase onto main
- plan: P-001

#### Outcome
Rebase after #42 lands and PR 77 is reviewed.

#### Progress
- 2026-10-03T07:00:00Z w1: mentions #99 only in progress

## In progress

### TB-0002 - Second
- plan: P-001
- depends_on: TB-0001

## Done

### TB-0001 - First
- plan: P-001
"""


def ev(ts, cmd, tid=None, owner="w1", **kw):
    return {"ts": f"2026-10-03T{ts}:00Z", "owner": owner, "worktree": "/wt", "cmd": cmd,
            **({"id": tid} if tid else {}), **kw}


class RetroTest(unittest.TestCase):
    def run_retro(self, events, *argv, board=BOARD):
        with tempfile.TemporaryDirectory() as d:
            log, bpath = os.path.join(d, "log.jsonl"), os.path.join(d, "board.md")
            with open(log, "w") as f:
                f.writelines(json.dumps(e) + "\n" for e in events)
            with open(bpath, "w") as f:
                f.write(board)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(retro.main(["--log", log, "--board", bpath, *argv]), 0)
        return out.getvalue()

    def row(self, out, start):
        rows = [l for l in out.splitlines() if l.startswith(start)]
        self.assertEqual(len(rows), 1, out)
        return [c.strip() for c in rows[0].strip("|").split("|")]

    def test_old_format_gates_open_on_prefix_and_close_on_none_or_lifecycle(self):
        out = self.run_retro([
            ev("07:00", "progress", "TB-0001", note="awaiting owner: merge #12"),
            ev("07:10", "progress", "TB-0001", note="half done"),  # plain note leaves the gate open
            ev("08:00", "progress", "TB-0001", note="none"),
            ev("08:00", "progress", "TB-0002", note="Awaiting coordinator: run publish"),
            ev("10:00", "complete", "TB-0002", note="done", **{"from": "In progress", "to": "Done"}),
            ev("10:00", "progress", "TB-0004", note="awaiting reader: restore"),
            ev("10:30", "progress", "TB-0004", note="awaiting CI on head abc"),  # not a gate
            ev("11:00", "progress", "TB-0005", note="owner: upload"),
        ])
        self.assertEqual(self.row(out, "| TB-0001 |")[1:], ["owner", "awaiting owner: merge #12", "2026-10-03T07:00:00Z",
                                                          "2026-10-03T08:00:00Z", "1.0"])
        self.assertEqual(self.row(out, "| TB-0002 |")[1], "owner")  # coordinator is the owner
        self.assertEqual(self.row(out, "| TB-0002 |")[-1], "2.0")
        self.assertEqual(self.row(out, "| TB-0004 |")[1:], ["reader", "awaiting reader: restore", "2026-10-03T10:00:00Z",
                                                          "2026-10-03T10:30:00Z", "0.5"])
        self.assertEqual(self.row(out, "| TB-0005 |")[-2:], ["open", "-"])

    def test_new_format_gates_use_typed_keys(self):
        out = self.run_retro([
            ev("07:00", "progress", "TB-0001", blocked="awaiting owner: merge #12", waiting_on="owner",
               gate_since="2026-10-03T07:00:00Z"),
            ev("09:30", "progress", "TB-0001", blocked="none", gate_since="2026-10-03T07:00:00Z",
               gate_cleared_at="2026-10-03T09:30:00Z"),
            ev("09:40", "progress", "TB-0002", blocked="awaiting ci", waiting_on="ci"),
            ev("09:50", "progress", "TB-0002", note="awaiting owner: just a note in the new format"),
        ])
        self.assertEqual(self.row(out, "| TB-0001 |")[-1], "2.5")
        self.assertNotIn("| TB-0002 |", out)

    def test_a_gate_replaced_by_another_starts_when_the_new_one_is_set(self):
        since = lambda t: f"2026-10-03T{t}:00Z"
        out = self.run_retro([
            ev("07:00", "progress", "TB-0001", blocked="awaiting owner: merge #12", waiting_on="owner", gate_since=since("07:00")),
            ev("08:00", "progress", "TB-0001", blocked="awaiting reader: restore", waiting_on="reader",
               gate_since=since("07:00"), gate_cleared_at=since("08:00")),  # still open: its own start is the event ts
        ])
        rows = [[c.strip() for c in l.strip("|").split("|")] for l in out.splitlines() if l.startswith("| TB-0001 |")]
        self.assertEqual([(r[1], r[3], r[4]) for r in rows], [("owner", since("07:00"), since("08:00")),
                                                              ("reader", since("08:00"), "open")])

    def test_idle_ready_gap_needs_ready_work_and_silence(self):
        out = self.run_retro([
            ev("07:00", "add", "TB-0001", to="Todo"),
            ev("07:00", "add", "TB-0002", to="Todo"),
            ev("07:01", "claim", "TB-0001", **{"from": "Todo", "to": "In progress"}),
            ev("08:00", "complete", "TB-0001", note="x", **{"from": "In progress", "to": "Done"}),
            ev("10:00", "claim", "TB-0002", **{"from": "Todo", "to": "In progress"}),
        ], "--gap-min", "30")
        self.assertEqual(self.row(out, "| 2026-10-03T08:00:00Z |"), ["2026-10-03T08:00:00Z", "2026-10-03T10:00:00Z",
                                                                     "2.0", "TB-0002"])
        self.assertNotIn("| 2026-10-03T07:01:00Z |", out)  # TB-0002 still waited on TB-0001

    def test_stale_text_refusals_prs_and_window(self):
        out = self.run_retro([
            ev("06:00", "add", "TB-0001", to="Todo"),
            ev("07:00", "complete", "TB-0001", note="commit abc, PR #42 merged",
               **{"from": "In progress", "to": "Done"}),
            ev("07:05", "progress", "TB-0002", owner="o1", exit=1,
               reason="TB-0002 is held by w1 (worktree /a); you are o1. ask"),
            ev("07:06", "complete", "TB-0001", owner="o2", exit=1,
               reason="TB-0001 is held by w2 (worktree /b); you are o2. ask"),
            ev("07:07", "claim", owner="o2", exit=1, reason="o2 already works on TB-0002; complete it"),
        ], "--since", "2026-10-03T06:30")
        self.assertEqual(self.row(out, "| TB-0003 |")[:2], ["TB-0003", "#42"])
        self.assertNotIn("#99 |", out)  # progress text is history, not task text
        self.assertEqual(self.row(out, "| ID is held by |")[0], "ID is held by")
        self.assertEqual(self.row(out, "| ID is held by |")[1], "2")
        self.assertEqual(self.row(out, "| OWNER already works on |")[1], "1")
        self.assertNotIn("| add |", out)  # the 06:00 add is outside the window
        self.assertEqual(self.row(out, "| complete |")[1], "2")  # refusals count as events too
        self.assertIn("#42, #77", out)


if __name__ == "__main__":
    unittest.main()
