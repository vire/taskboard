# taskboard

A local Markdown taskboard for coding agents that share one git clone. Claude Code, Codex, Herdr panes and other agents can each work in their own worktree. They plan work into tasks, claim them in order, respect dependencies, and leave progress, handoffs and evidence that the other agents can read.

It ships as one agent skill: a `SKILL.md` that teaches the workflow, plus one Python file (stdlib only, Python 3.9+) that does every write. There is no daemon, database or setup step.

## Install

Pick one:

```sh
# Any agent (Claude Code, Codex, ...), via the skills CLI
npx skills add vire/taskboard

# Claude Code plugin
/plugin marketplace add vire/taskboard
/plugin install taskboard@vire

# Manual: clone anywhere, link into each agent's skill directory
git clone https://github.com/vire/taskboard ~/code/taskboard
ln -s ~/code/taskboard/skills/taskboard ~/.agents/skills/taskboard
ln -s ~/code/taskboard/skills/taskboard ~/.claude/skills/taskboard
ln -s ~/code/taskboard/skills/retro ~/.agents/skills/taskboard-retro    # optional retro skill
ln -s ~/code/taskboard/skills/retro ~/.claude/skills/taskboard-retro
```

## Use

Tell an agent, in any worktree of the clone:

> Use the taskboard. Plan the retry work into tasks, then work through that plan until it is done or blocked.

Start more agents in other worktrees with "Use the taskboard and work through plan P-001". Each one claims the next eligible task.

To check the board yourself:

```sh
python3 .taskboard/tb list            # plans and open tasks, one line each
python3 .taskboard/tb list --all      # including done
python3 .taskboard/tb show TB-0003    # one task in full
python3 .taskboard/tb gates           # the owner's queue in merge order, with commands
python3 .taskboard/tb list --check    # problems only, exit 4 if any (for a cron watchdog)
python3 .taskboard/tb --help
```

`.taskboard/` appears in every worktree of the clone the first time the helper runs in any of them, and in a worktree added later on the next run. It is hidden from git through `.git/info/exclude`, so no tracked file changes.

## How it works

- **Storage.** `<git-common-dir>/taskboard/board.md` holds four sections: `## Plans`, `## Todo`, `## In progress`, `## Done`. The section a task sits in is its status. All worktrees of a clone share one board; separate clones have separate boards.
- **Tasks.** Each task is a `### TB-0001 - Title` block. Below the heading come `- key: value` fields (`plan`, `depends_on`, `owner`, `worktree`, `claimed_at`, `blocked`, ...), followed by `#### Outcome`, `#### Done when`, `#### Progress`, `#### Handoff` and `#### Evidence`.
- **Claims.** `claim` takes the first task in Todo order whose dependencies are all done. A task belongs to its owner label, and each owner holds at most two doing or waiting tasks (`claim --wip N`); tasks waiting on a human gate do not count. The default owner is `herdr:<pane id>` inside Herdr, else `<agent>@<worktree name>`, so nothing has to be remembered between agent turns. Give agents distinct owners (`--owner`, `$TASKBOARD_OWNER`) and several can share one checkout; a task follows its owner to another worktree. Acting on another owner's task needs `--force`, which the skill reserves for user instructions.
- **Waits and gates.** `progress --blocked` takes a typed first line. `awaiting owner|reader|decision: ...` is a gate: `list` shows it as `blocked`, `gates` lists it in `after:` order with its `cmd:`, and the log records `gate_since` and `gate_cleared_at`. `awaiting ci|bot|review|pr:N|task:TB-x` and `paused` show as `waiting`. Untyped text still reads as `blocked`, so older boards need no migration.
- **Notes and edits.** `note ID "text"` appends a fact to any task or plan, by anyone, without changing state. `edit ID` rewrites a task's title, outcome, done-when, dependencies or optional fields (`due`, `linear`, `prs`, `step`) and logs old and new. `complete --spawn "title"` adds follow-ups; `--cleanup` records what happened to the worktree.
- **Closing.** `close ID --reason` moves a task to Done marked `closed` (superseded by an external ticket, dropped). Dependents treat it like a done task.
- **Concurrency.** Every write takes one `flock`, rereads and validates the board, applies the change, and replaces `board.md` atomically (temp file, fsync, `os.replace`). A crash leaves either the old board or the new one. Reads take no lock. If the lock stays busy for 10 s, the command exits `75`.
- **Hand edits.** You can edit `board.md` by hand while no agent is writing. The next command validates the file and refuses to continue on errors, naming the line, such as an unknown section, a duplicate id, a missing plan or dependency, or a cycle. Edits made while agents write are last-writer-wins.

## Backup and restore

Before each change, the previous board is saved to `~/.local/share/taskboard/<repo>-<hash>/board-<utc>.md` (or under `$XDG_DATA_HOME`), and the newest 30 are kept. The backups live outside the clone, so they survive deleting it.

```sh
python3 .taskboard/tb restore                      # list backups, newest first
python3 .taskboard/tb restore board-2026...Z.md    # restore one; the current board is backed up first
python3 .taskboard/tb restore /path/to/board.md    # restore from any file, e.g. another clone's backups
```

`python3 .taskboard/tb init` prints the board, log and backup paths.

To start over in a clone, `python3 .taskboard/tb new` archives `board.md`, `log.jsonl`, any other file in the board dir, and the backups into `<git-common-dir>/backup-<utc>.tar.gz`. It checks that the archive lists every file, prints them, then writes an empty board and clears the log and backups. It refuses while a task is in progress unless given `--force`. To get the old board back: `tar -xzf .git/backup-<utc>.tar.gz taskboard/board.md`, then `restore` that file.

## Log

Each change appends a line to `<git-common-dir>/taskboard/log.jsonl`:

```json
{"ts": "2026-10-02T09:00:00Z", "owner": "codex@retry-wt", "worktree": "/path/retry-wt", "cmd": "complete", "id": "TB-0003", "from": "In progress", "to": "Done", "note": "commit abc123"}
```

A refused state-changing command adds a line with `"exit": 1` and its `reason`. The log is an audit trail, not a recovery source: hand edits do not appear in it. For recovery, use the backups.

```sh
tail -n 20 .taskboard/log.jsonl
```

## Retrospective

The companion `retro` skill (`taskboard:retro` as a plugin) reviews how a board went over a time window. Its helper only reads, and prints the facts the report is built from: events per command and owner, refusals by reason, owner gate latency, idle-ready gaps, open tasks whose text names a PR that is already merged, and every PR referenced. It reads logs from before and after typed waits.

```sh
python3 ~/code/taskboard/skills/retro/retro.py --since 2026-10-03 --until 2026-10-04T19:00Z
python3 ~/code/taskboard/skills/retro/retro.py --log copy/log.jsonl --board copy/board.md   # any copy
```

The skill turns that output into a report (TL;DR, what went well, problems table, ranked recommendations), can cross-check PRs with `gh`, and asks a second model to challenge the draft when one is reachable. It saves the report to `.taskboard/retro-<date>.md`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `TASKBOARD_OWNER` | `herdr:$HERDR_PANE_ID` inside Herdr, else `<agent>@<worktree name>` | Owner identity for claims; `--owner` overrides it |
| `XDG_DATA_HOME` | `~/.local/share` | Backup root |

## Known limits

- **Sandboxes.** The board sits under the clone's `.git` directory, outside every linked worktree. If a Codex `workspace-write` sandbox or the Claude Code sandbox denies the write, add `<clone>/.git/taskboard` to its writable roots, or approve the command once. Claude Code's subagent worktree guard has been seen refusing `complete` while allowing `claim` and `progress`; the skill tells agents to record the evidence with `progress` and leave `complete` to the orchestrator or user.
- **Editors.** ripgrep does not follow the `.taskboard` symlink. VS Code does, so add `"**/.taskboard": true` to `files.exclude` / `search.exclude` if board hits get in the way.
- **Stale `tb` link.** `.taskboard/tb` links to the helper that ran last. After a plugin update moves the skill, the next run through the skill path repairs the link.
- **Platforms.** macOS and Linux only, because the helper needs `fcntl` and symlinks. Requires git 2.31+.
- **Same-named worktrees.** Two worktrees whose folders share a name get the same default owner. Set `TASKBOARD_OWNER` in one of them.
- **Scope.** One machine, one clone. There is no sync between clones or machines, no automatic reassignment of stale claims, and no history beyond the backups and log.

## Design notes

v1 departs from its original plan (four board files, a `pending.json` intent record, claim tokens, `renew`, `sweep` and `edit` commands) in these deliberate ways, from a design review:

- **One `board.md` instead of `index.md`, `todo.md`, `inprogress.md` and `done.md`.** Moving a task becomes one atomic file replace, so the `pending.json` intent record and the recovery pass are not needed.
- **The helper lives in the installed skill only.** No copy goes inside each board, so one version is used everywhere.
- **Ownership is an owner label, not a claim token.** A token stored in plaintext on the board protects nothing, and agents would have to carry it across turns. v1 tied ownership to the worktree; real use showed agents moving to a fresh worktree after claiming and several read-only agents sharing one checkout, so ownership moved to the owner label.
- **Expiry is a `!stale` flag in `list`** for tasks without an update for 24 h, `!quiet` marks doing tasks silent for 30 min, and `!gate` marks human gates open over 4 h. There are no `expires_at`, `renew` or `sweep` commands. A takeover is `release --force` with a handoff note.
- **No `edit` command that holds the lock.** Hand edits are validated on the next command instead.

## Development

```sh
python3 -m unittest discover -s tests    # about 5 s; builds throwaway repos with 8 worktrees
claude plugin validate .
```
