---
name: retro
description: Run a retrospective over a taskboard (the local .taskboard board of plans and tasks shared by coding agents) for a time window. Mines log.jsonl for claims, blocks, completions, refusals, owner gate latency, idle-ready gaps and stale task text, optionally cross-checks PRs with gh, and writes a report with findings, a problems table and ranked recommendations. Use when the user asks for a taskboard retro, a retrospective of the board, or how the taskboard went.
---

# Taskboard retro

Reads only. Never edit the board, the log or the backups while doing a retro.

## Steps

1. From inside a worktree of the project, get the facts:

   ```sh
   python3 <directory of this SKILL.md>/retro.py [--since 2026-10-03] [--until 2026-10-04T19:00Z] [--gap-min 30]
   ```

   `--log PATH --board PATH` reads a copy instead of the live board. Every number in the report must come from this output or from a source you cite (PR, CI run, ticket, message). What the output measures:
   - **Gate latency.** Time from `awaiting owner:` / `reader:` / `decision:` to the next unblock, gate change or lifecycle move. `coordinator` counts as the owner.
   - **Idle-ready gaps.** Silences in the log while a Todo task had all its dependencies done.
   - **Stale task text.** Open tasks naming a PR that completion evidence or a "merged" note mentions. These are candidates, so confirm each one.
2. If `gh auth status` succeeds, cross-check the PRs it lists: `gh pr view N --json state,mergedAt,headRefName`. Skip this step otherwise and say so in Scope.
3. Read `python3 .taskboard/tb show ID` for the tasks behind the biggest gates, gaps and refusals before explaining them.
4. Write the report with these sections, in this order:
   - `# Taskboard retrospective: <topic>, <start> to <end>`, then a Scope paragraph listing every source read.
   - `## TL;DR`: five to eight bullets.
   - `## What went well`: numbered, each with evidence.
   - `## Problems`: a table `| # | Issue | Evidence | Impact | Fix |`, then a "Not verifiable from the record" list.
   - `## Recommendations (ranked by leverage)`: each tagged quick-win, medium or larger.
   - `## Missing information to add to the board`: a table `| Field / state | Where | Why (evidence) |`.
   - `## Proposed taskboard skill/helper changes`.
   - `## Second opinion`.
5. **Second opinion (optional).** If this session can reach a second model (another model's pane through Herdr, or a subagent on a different model), send it the draft. Ask it to challenge the numbers, the root cause and the ranking. Verify every claim it makes against the facts before adopting it, and summarise what you took and where you differ. If no second model is reachable, write "none consulted" and finish. Never block on this step.
6. Save the report to `.taskboard/retro-<UTC date>.md` unless the user names a path, then give the user the path and the TL;DR.
