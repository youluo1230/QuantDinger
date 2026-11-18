# QuantDinger Claude Code Instructions

@AGENTS.md

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues in the fork, `youluo1230/QuantDinger`; upstream `OpenByteInc/QuantDinger` is a read-only baseline. See `docs/agents/issue-tracker.md`.

### Triage labels

Five default roles: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Fork workflow

Two remotes (`origin` = fork and tracker, `upstream` = read-only baseline), a non-`main` base branch, the sibling Vue checkout, and the local frontend build override: see `docs/agents/fork-workflow.md`.

### Domain docs

Single-context: root `GLOSSARY.md` and `docs/adr/`. See `docs/agents/domain.md`.
