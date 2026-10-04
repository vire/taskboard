---
name: taskboard
description: Coordinate several coding agents (Claude Code, Codex, Herdr panes, others) that share one git clone and its worktrees, through a local Markdown board of plans and tasks with dependencies, claims, progress, handoffs and evidence. Use when the user mentions the taskboard or .taskboard, asks you to break work into tasks for other agents, to claim or pick up the next task, to work through a plan until it is done or blocked, or to report progress, hand off or release a task.
---

# Taskboard

One board per git clone, shared by every worktree of it: `<git-common-dir>/taskboard/board.md`, linked into each worktree as `.taskboard/`. It never enters commits. The helper locks, validates, backs up and logs every change, so several agents can work on the board at the same time.

## Run the helper

```sh
python3 <directory of this SKILL.md>/taskboard.py [--owner NAME] <command>
```

Run it from inside your worktree. The first call connects the worktree, and every later call repeats that check, so no setup step is needed. After the first call, `python3 .taskboard/tb <command>` from the worktree root does the same. `--owner` goes before the command. Do not read or edit `.taskboard/board.md` directly: `list` and `show` print what you need in fewer tokens, and the helper is the only safe way to write the board.

| Command | Use |
|---|---|
| `list [--plan P-001] [--all]` | One line per plan and open task: `ready`, `waiting [needs ...]`, `doing [holder age]`, `waiting` (on CI, bot, review, a PR or task, or paused), `blocked` (on the owner, a reader, a decision, or untyped). `age` is time since the last update. Flags: `!quiet` (doing, silent 30 min), `!stale` (no update for 24 h), `!gate 6h` (a human gate open over 4 h), `!idle-ready` on a plan with ready tasks and nobody doing. Done and closed tasks only with `--all` |
| `list --check` | Only problems: past `due:`, `!idle-ready` plans, open task text naming a PR marked merged or done. Exit 4 if any. The last line (`check: scanned ...`) always prints, so a watchdog can tell it read the board |
| `gates [--plan P-001]` | The owner's queue: every owner, reader and decision gate, in `after:` order, oldest first, with age, `due` and `cmd:` |
| `show ID` | One task or plan in full |
| `plan "Title" --body "scope"` | Create a plan; prints `P-001` |
| `add "Title" --plan P-001 --outcome "..." --done-when "..." [--done-when ...] [--depends-on TB-0001,TB-0002] [--set due=2026-10-09]` | Add a task at the end of Todo; prints `TB-0001`. Optional fields with `--set`: `due`, `linear` (ticket id), `prs` (`#1677 packet, #1678 receipt`), `step` |
| `claim [--plan P-001] [--wip N] [ID]` | Claim the first eligible task for your owner and print it. Up to 2 doing or waiting tasks per owner (`--wip`); gated tasks do not count |
| `note ID "text"` | Append a fact to any task or plan, yours or not. Changes no state |
| `edit ID [--title] [--outcome] [--done-when ...] [--depends-on IDS\|none] [--set k=v]` | Rewrite a Todo task, or your In progress task, when its text is wrong. The log keeps old and new |
| `progress ID [--note "..."] [--blocked "reason" \| --blocked none] [--handoff "..."]` | Record progress, set or clear a blocker (see Waits and gates), leave handoff notes |
| `complete ID --evidence "..." [--evidence ...] [--spawn "title" ...] [--cleanup "..."]` | Move to Done with evidence, one bullet per flag: commit SHA, PR link, file path, checks run. `--spawn` adds each follow-up as a task; `--cleanup` says what happened to the worktree |
| `release ID --handoff "..."` | Put the task back on top of Todo for someone else |
| `close ID --reason "..."` | Move a Todo or In progress task to Done as `closed` (not done), reason recorded as evidence. Dependents treat it like a done task |
| `restore [BACKUP]` | List backups or restore one (user-directed) |
| `new [--force]` | Archive board, log and backups into `backup-<utc>.tar.gz` beside the board dir, check the archive, start a fresh board. Refuses while tasks are in progress (user-directed) |

Exit codes: `0` ok, `1` error (message says why, with `board.md:LINE` for a malformed board), `3` nothing eligible to claim (the output lists what remains and why), `4` `list --check` found problems, `75` board busy (wait a few seconds and retry).

## Working through a plan

1. If asked to prepare the work: `plan`, then `add` small tasks in the order they should be done. Each needs one outcome sentence and checkable `--done-when` criteria. Use `--depends-on` only for real prerequisites in the same plan.
2. `claim --plan P-001`. The output is your task. Work it in your own worktree.
3. Record `progress ID --note` at each milestone. Before stopping on anything you cannot resolve yourself, set a typed `--blocked` (next section). While your task waits on a gate, claim another eligible task; `paused` is a wait, not a gate, and counts against your WIP limit.
4. When every done-when criterion holds, `complete ID --evidence "..."`. A dependent task's owner reads your evidence, so name the commit, branch, PR or file: repo-relative paths, commit SHAs, URLs, checks run, no absolute paths outside your worktree. Anything left for later ("left to", "follow-up", "TODO") goes in as `--spawn "title"`, never only in the evidence. If your sandbox refuses `complete`, record the evidence with `progress ID --note "done-when met; evidence: ..."` and report it; the orchestrator or user completes the task with `--force`. Do not work around the refusal.
5. Claim again. Stop when `claim` exits 3. Report the remaining tasks and blockers it printed.

## Waits and gates

The first line of `--blocked` says what you wait on. In gate text the owner is the user who merges, uploads and decides; always call them `owner`, never coordinator or orchestrator. (`--owner` is the agent's claim label, a different thing.)

- Gates, which only a human clears and `gates` lists: `awaiting owner: merge #1677`, `awaiting reader: independent restore of THE-2348`, `awaiting decision: keep orphans?`.
- Agent-side waits, shown as `waiting`: `awaiting ci`, `awaiting bot`, `awaiting review`, `awaiting pr:1677`, `awaiting task:TB-0012`, `paused ...`.
- Optional lines under a gate: `after: #1678` or `after: TB-0012` (this gate comes after that one), `cmd: <exact command for the owner, with pins>`, `due: 2026-10-09`.

```sh
progress TB-0015 --blocked $'awaiting owner: merge #1677\nafter: #1678\ncmd: gh pr merge 1677 --squash'
```

`--blocked none` clears it. The log records when each gate opened and cleared.

Orchestrators: the owner's queue is the output of `gates`, posted verbatim. Never hand-type a merge order or a list of owner asks; set `after:` on the gates and post the list again.

Do not write a note when nothing changed. Waiting on an external gate with a date is `due:`, not a re-check note every few hours.

## Notes

Notes are append-only facts: what happened, with SHAs, numbers and links. To correct one, add a new note that says what it supersedes. When a task's own text is wrong (a done-when made moot, a new scope), `edit` it, so the board never states something false. `note` works on any task, so an orchestrator records decisions on the board first and in chat second.

## Batch pipelines

Run a batch pipeline as one plan per batch with one task per step, so `list --plan` shows the step without reading notes. If any step needs a second identity or another external gate (an independent reader, a second human), make that the first task and have every task that needs it depend on it. Start it on day 0.

## Prompting a worker

One line is enough: `Use the taskboard skill with --owner worker-2 and claim TB-0007`. Give each worker in a shared checkout its own `--owner`: subagents inherit the orchestrator's environment, and a Herdr pane identifies only itself, not the agents it spawns.

## Rules

- Identity is the owner label: `--owner`, else `$TASKBOARD_OWNER`, else `herdr:$HERDR_PANE_ID` inside Herdr, else `<agent>@<worktree>`. Each owner holds at most 2 doing or waiting tasks, and only the owner may progress, complete, release, close or edit a claimed task; anyone may `note` it. Parallel work needs separate owners; work that changes files also needs separate worktrees.
- If you claimed a task and then moved to another worktree (or your Herdr pane moved and got a new id), pass `--owner <owner shown on the task>` on every later command; the task then follows you. Never pass another agent's owner: that needs the user's go-ahead, like `--force`.
- Stay inside the assigned plan. Add subtasks the plan needs. Put broader discoveries in a plan titled `Proposals` (create it once) and do not claim them.
- `close` only for a Proposals entry once the user has turned it into tickets, or a task the user says is superseded or dropped. Never to skip work.
- `--force` (acting on another owner's task), `restore` and `new` only when the user tells you to.
- `!stale` in `list` means a task had no update for 24 hours. It is for the user to decide. Do not take the task over on your own.
- On a malformed-board error, tell the user the line. Do not repair the file by hand unless asked.
