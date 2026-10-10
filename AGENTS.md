# Agent guide

## Commits

Write **Conventional Commit** messages: `<type>(<scope>): <summary>`.

- **Types**: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`, `chore`.
- **Scope**: the area touched, lower-case and short (e.g. `aerial`, `background`, `settings`, `music`).
- **Summary**: imperative mood, no trailing period.
- **Body**: explain the why, not the what. Reference the issue (`Refs #11`, `Closes #12`) in the body or footer.

Example: `feat(aerial): add the aerial-video background source (#11)`

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, managed with the `gh` CLI; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use their default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
