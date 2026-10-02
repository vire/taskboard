---
name: taskboard
description: Coordinate several coding agents (Claude Code, Codex, Herdr panes, others) that share one git clone and its worktrees, through a local Markdown board of plans and tasks with dependencies, claims, progress, handoffs and evidence. Use when the user mentions the taskboard or .taskboard, asks you to break work into tasks for other agents, to claim or pick up the next task, to work through a plan until it is done or blocked, or to report progress, hand off or release a task.
---

# Taskboard

One board per git clone, shared by every worktree of it: `<git-common-dir>/taskboard/board.md`, linked into each worktree as `.taskboard/`. It never enters commits. The helper locks, validates, backs up and logs every change, so several agents can work on the board at the same time.

## Run the helper

```sh
python3 <directory of this SKILL.md>/taskboard.py <command>
```

Run it from inside your worktree. The first call connects the worktree, and every later call repeats that check, so no setup step is needed. Do not read or edit `.taskboard/board.md` directly: `list` and `show` print what you need in fewer tokens, and the helper is the only safe way to write the board.

| Command | Use |
|---|---|
| `list [--plan P-001] [--all]` | One line per plan and open task: `ready`, `waiting [needs ...]`, `doing [owner age]`, `blocked`. `age` is time since last activity; `!quiet` marks a doing task silent for over `TASKBOARD_QUIET_MIN` minutes (default 30). Done and closed tasks only with `--all` |
| `show ID` | One task or plan in full |
| `plan "Title" --body "scope"` | Create a plan; prints `P-001` |
| `add "Title" --plan P-001 --outcome "..." --done-when "..." [--done-when ...] [--depends-on TB-0001,TB-0002]` | Add a task at the end of Todo; prints `TB-0001` |
| `claim [--plan P-001] [ID]` | Claim the first eligible task for this worktree and print it |
| `progress ID [--note "..."] [--blocked "reason" \| --blocked none] [--handoff "..."]` | Record progress, set or clear a blocker, leave handoff notes |
| `complete ID --evidence "..." [--evidence ...]` | Move to Done with evidence, one bullet per flag: commit SHA, PR link, file path, checks run |
| `release ID --handoff "..."` | Put the task back on top of Todo for someone else |
| `close ID --reason "..."` | Move a Todo or In progress task to Done as `closed` (not done), reason recorded as evidence. Dependents treat it like a done task |
| `restore [BACKUP]` | List backups or restore one (user-directed) |

Exit codes: `0` ok, `1` error (message says why, with `board.md:LINE` for a malformed board), `3` nothing eligible to claim (the output lists what remains and why), `75` board busy (wait a few seconds and retry).

A refused state-changing command (exit `1`) also leaves a line with its reason in `log.jsonl`.

## Working through a plan

1. If asked to prepare the work: `plan`, then `add` small tasks in the order they should be done. Each needs one outcome sentence and checkable `--done-when` criteria. Use `--depends-on` only for real prerequisites in the same plan.
2. `claim --plan P-001`. The output is your task. Work it in your own worktree.
3. Record `progress ID --note` at each milestone. Before stopping on any refusal or permission prompt you cannot resolve, run `progress ID --blocked "reason"`. For merge or deploy waits use `progress ID --blocked "awaiting ..."`. You may then claim another eligible task.
4. When every done-when criterion holds, `complete ID --evidence "..."`. A dependent task's owner reads your evidence, so name the commit, branch, PR or file.
5. Claim again. Stop when `claim` exits 3. Report the remaining tasks and blockers it printed.

## Prompting a worker

One line is enough: `Use the taskboard skill and claim TB-0007`.

## Rules

- One agent per worktree. The helper allows one unblocked claim per worktree. Parallel implementation needs separate worktrees.
- Stay inside the assigned plan. Add subtasks the plan needs. Put broader discoveries in a plan titled `Proposals` (create it once) and do not claim them.
- `close` only for a Proposals entry once the user has turned it into tickets, or a task the user says is superseded or dropped. Never to skip work.
- `--force` (acting on another worktree's task) and `restore` only when the user tells you to.
- `!quiet` in `list` means no progress for a while; the orchestrator may ask that agent for status.
- `!stale` in `list` means a claim is older than `TASKBOARD_TTL_HOURS` (default 24). It is for the user to decide. Do not take the task over on your own.
- On a malformed-board error, tell the user the line. Do not repair the file by hand unless asked.
