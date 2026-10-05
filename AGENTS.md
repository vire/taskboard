# Agent rules

- Open pull requests ready for review, never as drafts: `gh pr create` without `--draft`.
- After an agent reviews a pull request, label the PR by the review's blockers. Use `ai-request-changes` if any blocker is still open, otherwise `ai-approved`. Keep exactly one of the two labels on the PR: `gh pr edit N --add-label ai-approved --remove-label ai-request-changes`, or the reverse. If a label does not exist yet, create it with `gh label create`.
