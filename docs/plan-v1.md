# Local agent taskboard - v1 plan

Saved: 2026-10-02  
Status: Design specification; implementation is not part of this document.

> Implemented with deliberate changes; see "Design notes" in the [README](../README.md).

## Purpose

A shared Markdown board, a skill that teaches agents the workflow, and one small Python helper that coordinates changes.

The initial version supports local Herdr sessions, Codex, Claude Code, and other agents working in one local Git clone and its worktrees. The user starts agents manually and assigns work; agents then work autonomously within the assigned plan. Each task defines its own completion criteria, whether the result is a plan, implementation, review, or another deliverable.

## Storage

Every worktree exposes `.taskboard` as a symlink to the same directory:

```text
<git-common-dir>/taskboard/
├── index.md
├── todo.md
├── inprogress.md
├── done.md
├── skills/
│   └── how-to-use-taskboard/
│       ├── SKILL.md
│       └── taskboard.py
└── .internal/
    ├── lock
    └── pending.json    # Only present during an update or recovery
```

The helper discovers shared storage with:

```sh
git rev-parse --path-format=absolute --git-common-dir
```

This gives all worktrees of one local clone the same board. Separate clones have separate boards. Initialization adds `/.taskboard` to Git's local exclude file and creates the checkout's symlink. Each new worktree runs initialization once.

| File | Purpose |
|---|---|
| `index.md` | Entry instructions, defaults, plan descriptions, and link to the skill |
| `todo.md` | Ordered tasks available to be claimed when dependencies permit |
| `inprogress.md` | Claimed tasks, including blocked tasks and expired claims |
| `done.md` | Completed tasks with evidence of their outcome |

A task's containing file determines its column. There is no separate status field to keep synchronized.

## Local-first Git ignore policy

The entire `.taskboard` stays local and uncommitted in v1, including:

- `index.md`, `todo.md`, `inprogress.md`, and `done.md`.
- The skill and helper script under `skills/`.
- Locks, recovery records, and temporary files under `.internal/` or elsewhere in the board.

Initialization adds this rule to the repository's local exclude file, without changing the tracked `.gitignore`:

```gitignore
/.taskboard
```

Locate the local exclude file with:

```sh
git rev-parse --path-format=absolute --git-path info/exclude
```

Keep the rule without a trailing slash so it matches the `.taskboard` symlink. The exclude file is local to the clone and shared by its worktrees.

The actual board contents live under the common Git directory, outside tracked project files. The exclude rule hides each checkout's convenience symlink. Worktrees share the board locally; commits and pushes carry none of it.

Completed tasks may reference commits and PRs, but a committed ledger is deferred. Git commits do not back up the board. Later, the reusable skill and helper may be versioned separately while task contents remain local.

## Task format

Use Markdown task blocks with a small set of structured fields. Example of a claimed task:

```markdown
## TB-0012 - Implement retry handling

- plan: P-003
- depends_on: TB-0011
- owner: codex-session-abc
- worktree: /path/to/retry-worktree
- claim: <generated token>
- claimed_at: 2026-10-01T09:00:00Z
- expires_at: 2026-10-02T09:00:00Z
- updated_at: 2026-10-01T10:15:00Z
- blocked: none

### Outcome
Retry transient connection failures.

### Done when
- Transient failures retry within the agreed limit.
- Relevant checks pass.
- Implementation is committed and available for review.

### Progress
Implementation complete; checking cancellation behavior.

### Handoff
Continue from commit abc123 in the worktree above.

### Evidence
Added on completion: result location, checks, and commit or PR.
```

Todo tasks need only an ID, title, plan, dependencies, outcome, and completion criteria. Claim fields are added by the helper. Completion evidence can reference a plan, document, review, commit, or PR depending on the task.

IDs remain stable when tasks move. The helper allocates them under the shared lock.

## Agent workflow

The user points an agent at the skill and assigns a plan, for example:

> Read `.taskboard/index.md`. Prepare tasks for the retry work, then work through that plan until complete or blocked.

The agent:

1. Creates a bounded plan and tasks with explicit completion criteria.
2. Claims the first eligible task in that plan, in board order.
3. Records its session identity and worktree.
4. Performs the work and records useful progress or handoff notes.
5. Completes the task with evidence, then claims the next eligible task.
6. Stops when the plan is complete or no remaining task is eligible, reporting the blockers.

Several agents can work on the same plan. Each task has one owner, and each agent works on one task at a time.

Agents may add necessary subtasks within the assigned scope. Broader discoveries become proposed work in `todo.md`; they do not automatically expand the current assignment.

For concurrent implementation, agents use separate worktrees. A dependent task receives its prerequisite's result references. Code integration follows the project's instructions and the task's completion criteria.

## Dependencies

`depends_on` is a list of task IDs, with four rules:

- Dependencies must exist and belong to the same plan.
- Self-dependencies and cycles are rejected.
- A task is eligible only when all its dependencies are in `done.md`.
- There are no nested plans, dependency types, or automatic downstream task generation.

Blocked work stays in `inprogress.md` with an explanation. The owner may work on another eligible task or explicitly release the blocked task with a handoff note.

## TTL and daily sweep

Each claim defaults to 24 hours, configurable by the user.

Expiry means **needs attention**. It does not revoke ownership, move the task, or allow another agent to claim it automatically. Progress updates change `updated_at` but do not silently extend the TTL.

A daily invocation of `sweep` reports:

- Task ID and title.
- Owner and worktree.
- Expiry time.
- Latest progress and handoff notes.
- Any recorded blocker.

The user decides whether to extend, release, or reassign the claim. The existing host scheduler runs the sweep and presents the report; the board itself needs no running service or manager agent.

Release and takeover require a handoff note. Takeover issues a new claim token, preventing the previous owner from modifying that task through the helper. It cannot stop that agent from editing its own worktree.

## Helper interface

`taskboard` below is shorthand for invoking the bundled Python script.

| Command | Behavior |
|---|---|
| `init` | Initialize shared storage and connect the current worktree |
| `list --plan P-003` | Show tasks, eligibility, owners, and expired claims |
| `add --plan P-003` | Create a task |
| `claim --plan P-003` | Atomically claim the first eligible task |
| `progress ID` | Update progress, blocker, or handoff information |
| `complete ID` | Validate ownership and move the task to done with evidence |
| `release ID` | Return a task to todo with a handoff |
| `renew ID --ttl 24h` | Extend expiry at the user's direction |
| `takeover ID` | Reassign ownership at the user's direction, preserving the handoff |
| `sweep` | Report expired claims without changing them |
| `edit [file]` | Open a coordinated editing session |

Owner operations require the current claim token. Human-directed actions are a workflow rule enforced by the skill, not a separate authentication system.

## Safe editing and recovery

All helper mutations acquire one shared filesystem lock, reread the board, validate the change, and then write it.

`taskboard edit` holds that lock while the user edits a working copy in their normal editor. On exit, the helper validates and applies the changes. Agents can keep coding; board updates return a clear "board busy" result and retry later.

Task content, ordering, dependencies, and expiry are editable. Ownership and column changes use the lifecycle commands so their checks and handoff requirements remain consistent.

Moving a task touches two files. A lock alone cannot make that crash-safe, so the helper temporarily records the intended update in `pending.json`. The next invocation finishes an interrupted update before accepting new changes. If unexpected file contents prevent safe recovery, it preserves them and requests reconciliation.

Ordinary editor saves outside `taskboard edit` bypass coordination. The supported concurrent workflow is the skill, helper, and coordinated editor command.

## Deferred scope

- Remote machines and synchronization between separate clones.
- Automatic agent launching or a taskboard-manager agent.
- Automatic reassignment of expired claims.
- Background claim renewal.
- A committed ledger or board history.
- A database, web UI, or continuously running manager.

## References

- [Git worktree details](https://git-scm.com/docs/git-worktree#_details)
- [Git rev-parse documentation](https://git-scm.com/docs/git-rev-parse)
- [Git repository layout](https://git-scm.com/docs/gitrepository-layout)
- [Git ignore pattern rules](https://git-scm.com/docs/gitignore)
