# Example: an implementer group

This example shows a group of agents shipping a multi-PR change to a fictional app while the operator stays mostly away. It uses the taskboard and Herdr. Copy the blocks, replace the placeholders and adapt the hard rules to your repo.

Placeholders: `<TRACK>` (short track name, also the tab label, e.g. `ACME-123`), `<PLAN-ID>` (e.g. `P-001`), `<OPERATOR_CHANNEL>` (where agents post to the operator), `<BRIEF_DIR>` (a folder outside the repo, e.g. `~/acme-orchestration`), `<SKILLS>` (where the taskboard skill is installed). Below, `tb` means `python3 .taskboard/tb`, run from the root of your own worktree.

**The fictional project.** ACME has a Bun API (`api/`), a Bun + React frontend (`web/`) and PostgreSQL. Ticket ACME-123: move file uploads from the `uploads.data` bytea column to object storage. That takes about six PRs and five owner gates: merges, a prod migration, a live backfill, a feature-flag flip, and an irreversible column drop.

## 1. When to use a group

Use a single agent when the work fits one PR, or when every step waits on the same human anyway.

Use a group when:

- the work is several PRs with a real merge order;
- some tasks can run in parallel (API and frontend, backfill script and read path);
- there are owner gates you want prepared ahead, so the operator can clear several at once;
- the operator is away for hours and wants the agents to keep working between gates.

A group costs more tokens and more review rounds. If two workers would just wait on each other, use one.

## 2. Roles

| Role | Model, effort | Started by | Does | Never |
|---|---|---|---|---|
| Operator | human | - | Merges, runs prod and live commands, decides. Clears gates in `tb gates` order | - |
| Coordinator | the operator's own session | operator | Creates one Herdr tab per track, starts the orchestrator, relays approvals and decisions, runs the watchdog, posts updates to `<OPERATOR_CHANNEL>` | Runs gated commands for the operator |
| Orchestrator | Opus, medium | coordinator | Owns the plan and the gate list, starts workers, the reviewer and the consultant, records decisions on the board | Writes the diff, merges, closes panes it did not start |
| Worker | Sonnet, high | orchestrator | One claimed task, in its own worktree, with its own `--owner`. Runs spec, failing test, implementation, PR. At most 2-3 at a time | Shares a worktree, runs anything gated |
| Reviewer | Opus, high | orchestrator | Reads the full diff at the exact head against the task and the checklist, then posts findings on the PR | Reviews a diff it wrote |
| Consultant | Fable | orchestrator | Answers one real dilemma (design fork, unclear evidence, conflicting rules). Closed after the one question | Stays open, writes code |

## 3. Launch

Coordinator, once. Create the plan and its tasks in merge order. Each owner gate goes on the task that needs it; it is not a task of its own.

```sh
cd ~/code/acme
TB="python3 <SKILLS>/taskboard/taskboard.py --owner operator"
$TB plan "ACME-123 uploads to object storage (API)" --body "Move uploads.data (bytea) to object storage behind flags uploads_os_write and uploads_os_read. Merge order: TB-0001, TB-0002, TB-0003, TB-0004, TB-0005, TB-0006."

$TB add "Migration: add uploads.storage_key" --plan P-001 \
  --outcome "uploads has a nullable storage_key column with an index, applied in prod." \
  --done-when "PR merged" --done-when "owner ran the prod migration; storage_key exists in prod"
$TB add "Dual-write uploads behind uploads_os_write" --plan P-001 --depends-on TB-0001 \
  --outcome "New uploads go to the bucket and to bytea while the flag is on; flag defaults off." \
  --done-when "PR merged" --done-when "tests cover flag on and flag off"
$TB add "Backfill script for existing uploads" --plan P-001 --depends-on TB-0001 \
  --outcome "An idempotent, batched, resumable script copies bytea rows to the bucket and sets storage_key." \
  --done-when "PR merged" --done-when "dry run on a staging snapshot in evidence" \
  --done-when "owner ran the live backfill; rows with storage_key null = 0"
$TB add "Read path: signed download URLs behind uploads_os_read" --plan P-001 --depends-on TB-0002 \
  --outcome "GET /uploads/:id returns a signed URL when storage_key is set and the flag is on, else streams bytea." \
  --done-when "PR merged"
$TB add "Enable uploads_os_read in prod" --plan P-001 --depends-on TB-0003,TB-0004 \
  --outcome "Prod serves every upload from object storage." \
  --done-when "owner flipped the flag" --done-when "24 h error rate on /uploads unchanged, numbers in evidence"
$TB add "Drop uploads.data" --plan P-001 --depends-on TB-0005 --set due=2026-11-02 \
  --outcome "The bytea column is gone after a soak period." \
  --done-when "PR merged" --done-when "owner ran the prod migration"

$TB plan "ACME-123 uploads to object storage (web)" --body "Frontend uses downloadUrl from the API. Waits on TB-0004 from P-001."
$TB add "Upload list and preview use downloadUrl" --plan P-002 \
  --outcome "web/ renders uploads from the signed URL when the API returns one, else the old stream URL." \
  --done-when "PR merged" --done-when "component tests cover both responses"
```

`--depends-on` works within one plan only. A worker that needs another track's task sets `tb progress TB-0007 --blocked "awaiting task:TB-0004"`, which shows as `waiting`, and claims something else meanwhile.

Coordinator, once per track. Name the tab after the ticket:

```sh
PANE=$(herdr tab create --label <TRACK> --cwd ~/code/acme --no-focus | jq -r .result.root_pane.pane_id)
herdr agent start <TRACK>-orch --kind claude --pane "$PANE" -- --model opus --effort medium --permission-mode auto
herdr agent prompt <TRACK>-orch "$(cat <BRIEF_DIR>/<TRACK>-opening.md)"
```

Orchestrator, per worker: a worktree first, then a pane in its own tab, then the agent.

```sh
git -C ~/code/acme worktree add ../acme-wt-<TRACK>-w1 -b uploads-dual-write origin/main
P=$(herdr pane split --current --direction right --cwd ~/code/acme-wt-<TRACK>-w1 --no-focus | jq -r .result.pane.pane_id)
herdr agent start <TRACK>-w1 --kind claude --pane "$P" -- --model sonnet --effort high --permission-mode auto
herdr agent prompt <TRACK>-w1 "<worker prompt, section 6>"
```

The reviewer and the consultant start the same way, in a pane on the shared checkout. The reviewer uses `-- --model opus --effort high --permission-mode auto`. The consultant uses `-- --model claude-fable-5-1 --permission-mode auto` and is closed with `herdr pane close <pane-id>` after its answer.

## 4. Orchestrator opening prompt

Keep it short. The brief carries the rules.

```text
You are the ORCHESTRATOR for track <TRACK> in your own Herdr tab.
Goal: ACME-123, move file uploads from the uploads.data bytea column to object storage, with no downtime and no lost files.
Plan: <PLAN-ID>. Repo: ~/code/acme. The shared checkout stays on main; never switch it.

Starting facts:
- uploads has about 180k rows and about 40 GB in prod. Largest row: 95 MB.
- Bucket acme-uploads-prod exists. The API reads its name from UPLOADS_BUCKET.
- Flags live in the flags table. `bun run flags:set <name> on|off --env <env>` toggles one.
- Migrations: `bun run db:migrate --env <env>`. Staging is yours; prod is the owner's.

Read <BRIEF_DIR>/BRIEF.md first, then the taskboard skill.

Steps:
1. `tb list --plan <PLAN-ID>`. Fix wrong task text with `tb edit` before anyone claims it.
2. Start up to 2 workers on ready tasks, each with its own worktree and --owner.
3. Before a PR goes to the owner: reviewer pass at the exact head, and the BRIEF checklist.
4. Turn every owner action into a typed gate with `after:` and `cmd:`. Post `tb gates` verbatim to <OPERATOR_CHANNEL>.
5. While gates are open, keep workers on ready tasks. Stop when only gates remain, then report.
```

## 5. BRIEF.md template

Save as `<BRIEF_DIR>/BRIEF.md`, outside the repo. All tracks share it.

````markdown
# ACME-123 orchestration brief

The owner is mostly away. Prepare everything; the owner clears gates in batches.

## Coordination

- Taskboard: `python3 <SKILLS>/taskboard/taskboard.py --owner <your-owner> <cmd>`. Read its SKILL.md first.
  Your plan ID is in your prompt. Add the subtasks your plan needs. Record progress at milestones.
- Herdr: stay inside your own tab and split panes with `--no-focus`. Name agents `<TRACK>-w1`, `<TRACK>-review`, `<TRACK>-consult`.
  Close the panes you started once their task is done. Never close panes or tabs you did not start.
- One worker = one --owner, one worktree (`~/code/acme-wt-<TRACK>-wN`), one claimed task. Max 2 workers at a time.
- Decisions go on the board first (`tb note`), chat second.

## Merge order

TB-0001 migration, TB-0002 dual-write, TB-0003 backfill, TB-0004 read path, web PR, TB-0005 flag flip, TB-0006 drop column.
State it in every PR body. Encode it as `after:` on the gates. Never hand-type it in chat.

## Hard rules (never without the owner)

- Never merge a PR, approve your own PR, or add a merge label.
- Never run anything against prod: migrations, backfills, flag flips, deploys, `psql` writes. Prepare the exact command
  (env, revision, batch size, flags), test it on staging, set an owner gate with `cmd:`, and ping. The permission
  classifier blocks relayed approvals; do not work around a denial.
- Never drop or rewrite data outside staging, rewrite Git history, run broad `rm` on tracked files, or touch another worker's worktree.
- Every behaviour change: spec, failing test, implementation. Targeted tests only (`bun test api/uploads`); CI runs the rest.
- Branch names never contain a ticket ID (it auto-closes the ticket on merge). Put "Part of ACME-123" in the PR body.
- Poll GitHub at most once every 5 minutes per PR. Several agents share one API budget.
- Report only what you verified. An exit code is not a result.

## PR readiness checklist ("ready for the owner")

1. Rebased on the current main, with all CI checks green on that head. If the PR was green before another PR merged, re-run it.
2. The review bot's verdict is for the current head (reviewed commit == `gh pr view N --json headRefOid`). A stale approval does not count.
3. Every bot finding is fixed (test first) or answered on the PR with evidence.
4. Our reviewer read this exact head and found nothing blocking.
5. The PR body has the spec, "Part of ACME-123", the merge order, its gates with commands, and evidence links.
6. Migrations: reversible, or marked IRREVERSIBLE in the title. Backfills: idempotent, batched, resumable, with a dry-run count.

Batch review fixes into one push. Every new head resets the bot, CI and the reviewer.

## Pinging the owner

- `<OPERATOR_CHANNEL>` only, never a DM. Say what is ready, what you need, the PR links, and the exact commands.
- First set the gate on the board: `tb progress TB-x --blocked $'awaiting owner: <what>\nafter: TB-y\ncmd: <exact command>'`.
  Then post `tb gates` output verbatim.
- At most one ping per hour per track, unless main is red, every PR is red, or you are stuck.
- If posting fails, leave the request on the board and in your pane. The coordinator watches both.

## References

- `AGENTS.md`, `api/db/migrations/README.md`, `docs/feature-flags.md`, `docs/storage.md`
- Earlier migration with a backfill, merged on main: `api/db/migrations/0031_avatars_to_cdn.ts` and its backfill script.

## Lessons learned (append as you go)

- Green alone, red together: two PRs passed CI separately and broke main merged back to back. Rebase and re-run after each merge.
- A typed-out merge order in chat was replaced by a later message without it. Only `tb gates` carries order.
- Watchdogs print a self-test line every run. A silent watchdog is a broken one.
````

## 6. Worker and reviewer prompts

Worker:

```text
Use the taskboard skill with --owner <TRACK>-w1 and claim TB-0002.
Read <BRIEF_DIR>/BRIEF.md first. Your worktree is ~/code/acme-wt-<TRACK>-w1 on branch uploads-dual-write; it is yours alone.
Spec, failing test, implementation. Targeted tests only. Open the PR ready for review, with the body the checklist asks for.
When CI is green on the head, set `--blocked "awaiting review"` and send me the PR link and head SHA.
Anything prod-facing: prepare the exact command, test it on staging, set an owner gate with cmd:, and do not run it.
When every done-when holds, `complete` with evidence. Anything left for later goes in as --spawn. Never merge.
```

Reviewer:

```text
You are the REVIEWER for <TRACK>. Review <PR URL> at head <SHA>. Check `gh pr view N --json headRefOid` first; if the head moved, review the new one and say so.
You did not write this diff. If you did, stop and tell me.
Read the full diff against `tb show TB-x` (outcome, done-when) and the BRIEF checklist. Look for: migration reversibility and lock time
on a 180k-row table, backfill idempotency and resume, flags defaulting off, signed URL expiry and auth, tests that would fail without the change.
Post one review on the PR: blocking and non-blocking findings, each with file:line. Then reply here: "head <SHA>: N blocking". Do not push.
```

Consultant (one question, then close the pane):

```text
One decision for ACME-123, no code. <Dilemma, e.g. backfill in one long job or per-tenant batches?>
Facts: <numbers, constraints, links>. Options: A <...>, B <...>.
Answer with: the choice, why, and what fact would flip it.
```

The orchestrator records the answer with `tb note TB-x "consultant: chose B because ...; would flip if ..."`.

## 7. The operator's loop

1. **Standup.** `tb standup` shows gates first, then what each owner did since your last standup. Ask the orchestrators for `tb standup --markdown`, posted verbatim, never summarized.
2. **Gates.** `tb gates` is your queue, in `after:` order with each `cmd:`. Clear gates top down: merge, then the prod migration it unlocks, then the next merge. Do not skip ahead. A PR merged out of order turns main red.
3. **Approvals.** Merge only PRs that meet the readiness checklist. Run prod commands yourself, from `cmd:`. Tell the tab through the coordinator so the gate is cleared on the board:
   ```sh
   herdr agent prompt <TRACK>-orch "Owner: merged TB-0001 PR and ran the prod migration at 14:05Z. Clear the gate and continue."
   ```
   Decisions go the same way, and the orchestrator notes them on the board.
4. **Watchdog.** In the coordinator session, every 20 minutes (`/loop 20m`, or cron):
   ```sh
   cd ~/code/acme && python3 .taskboard/tb list --check; rc=$?; echo "watchdog ok $(date -u +%H:%MZ) exit=$rc"
   ```
   `list --check` always prints a last line, `check: scanned ...`. If it is missing, the watchdog is broken. Exit 4 lists `!idle-ready` plans, past-`due` gates and stale PR text. Re-prompt the orchestrator, or post the problem to `<OPERATOR_CHANNEL>`.
5. **Cleanup.** After each merge the worker deletes its worktree and branch, and records it:
   ```sh
   git -C ~/code/acme worktree remove ../acme-wt-<TRACK>-w1 && git -C ~/code/acme branch -d uploads-dual-write
   tb complete TB-0002 --evidence "PR <url>, merge <sha>, checks green" --cleanup "worktree removed"
   ```
   The orchestrator closes the panes it started. At the end, run the `retro` skill over the window and append its lessons to BRIEF.md.

## 8. Pitfalls

- **Ticket IDs in branch names auto-close tickets.** A ticket showed Done while the work was half merged. Keep IDs in the PR body only.
- **State the merge order** in every PR body and as `after:` on the gates. Post `tb gates` verbatim; a hand-typed list goes stale.
- **PRs that are green alone can go red together.** Rebase on main and re-run CI after every merge, before calling the next PR ready.
- **Stale bot approvals do not count.** The verdict must be for the current head SHA. A label left over from an earlier head is not a review.
- **No merges without approval**, whatever the checklist says. Agents prepare; the owner merges and runs prod.
- **Poll GitHub gently.** Once every 5 minutes per PR at most. For a gate with a date, use `due:` instead of re-check notes.
- **One worktree per worker, deleted after merge.** Shared worktrees mix diffs. Kept worktrees fill the disk.
- **Watchdog self-test lines.** A monitor with a bug can check nothing for hours. Print a line every run, and read it.
- **Batch operator pings and post `tb gates` verbatim.** One message per window, with every gate ready and its command. Clearing five gates in one sitting beats five pings.
- **Idle with ready work.** `!idle-ready` on a plan means nobody is claiming. Waiting on a gate is a reason to claim the next task, not to stop.
- **Needs from a second person go first.** If a gate needs someone besides the operator (a DBA sign-off, a second reviewer), make it the first task, on day 0.
- **One name for the human.** Gate text says `owner`, never coordinator or orchestrator, so `tb gates` finds every ask.
