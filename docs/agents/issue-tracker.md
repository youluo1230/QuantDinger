# Issue tracker: GitHub

Issues and specs for this fork live as GitHub issues in `youluo1230/QuantDinger`. The upstream repo `OpenByteInc/QuantDinger` is a read-only baseline, not this fork's work queue. Use the `gh` CLI for all operations.

## Repository targeting

This clone has two remotes and they are not interchangeable:

| Remote | Repository | Access |
| --- | --- | --- |
| `upstream` | `OpenByteInc/QuantDinger` | read-only baseline — do not file here |
| `origin` | `youluo1230/QuantDinger` | the tracker and the push target |

Bare `gh` commands do **not** land on the fork from inside this clone: when a remote named `upstream` exists, `gh` prefers it over `origin`, so `gh issue create` without `-R` files into `OpenByteInc/QuantDinger`. Pass `-R youluo1230/QuantDinger` on every issue command, as this file does, and export `GH_REPO=youluo1230/QuantDinger` for the session as a second line of defence (`GH_REPO` scopes `gh issue` / `gh pr`; it does not affect `gh repo`, `gh label`, or `gh api`, which still need `-R` / an explicit `repos/youluo1230/QuantDinger` path). File into `OpenByteInc/QuantDinger` only when the user explicitly asks: those issues are the product's public backlog, triaged by the upstream maintainers rather than by this fork.

Authentication: the contributor token is available through the git credential helper, so export it before calling `gh`:

```bash
export GH_TOKEN=$(git credential fill <<<'protocol=https
host=github.com' | sed -n 's/^password=//p')
export GH_REPO=youluo1230/QuantDinger
```

That token belongs to `youluo1230` and holds **ADMIN** on `youluo1230/QuantDinger`: issues, labels, assignments, and sub-issues all work there. The same token is **READ**-only on `OpenByteInc/QuantDinger`, so upstream labels cannot be created or edited from this clone (see `triage-labels.md`).

## Conventions

Every command below names the fork, because `gh` would otherwise resolve to the `upstream` remote:

- **Create an issue**: `gh issue create -R youluo1230/QuantDinger --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view -R youluo1230/QuantDinger <number> --json number,title,body,labels,comments`.
- **List issues**: `gh issue list -R youluo1230/QuantDinger --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Make an issue a sub-issue of a parent**: `gh issue create -R youluo1230/QuantDinger --parent <parent> ...`, or `gh issue edit -R youluo1230/QuantDinger <parent> --add-sub-issue <child>` afterwards (`gh` 2.94+). Older `gh`: `gh api --method POST repos/youluo1230/QuantDinger/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>` (database id, as in **Blocking** below). Without sub-issues, put `Part of #<parent>` at the top of the child body.
- **Blocking**: GitHub's **native issue dependencies**, the canonical, UI-visible representation. Add an edge with `gh issue create -R youluo1230/QuantDinger --blocked-by <n>,<n> ...` or `gh issue edit -R youluo1230/QuantDinger <child> --add-blocked-by <blocker>` (`gh` 2.94+). Older `gh`: `gh api --method POST repos/youluo1230/QuantDinger/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/youluo1230/QuantDinger/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Count open blockers**: `gh api repos/youluo1230/QuantDinger/issues/<n> --jq .issue_dependencies_summary.blocked_by` (open blockers only; 0 means startable). `gh issue view -R youluo1230/QuantDinger <n> --json blockedBy` also lists closed blockers, so filter it: `--jq '[.blockedBy.nodes[] | select(.state == "OPEN")] | length'`.
- **Comment on an issue**: `gh issue comment -R youluo1230/QuantDinger <number> --body "..."`
- **Apply / remove labels**: `gh issue edit -R youluo1230/QuantDinger <number> --add-label "..."` / `--remove-label "..."`
- **Close**: post the explanation with `gh issue comment -R youluo1230/QuantDinger <number> --body "..."`, then `gh issue close -R youluo1230/QuantDinger <number>`. On an already-closed issue, `gh issue close --comment` drops the comment and still exits 0.

## Repo-specific conventions

- Use the labels that exist on the fork: the GitHub defaults (`bug`, `enhancement`, `documentation`, `question`, `help wanted`, `wontfix`, …) plus the `wayfinder:*` family. The upstream families (`needs-rfc`, `epic`, `roadmap`, `area:*`, `priority:*`, `ready for contributors`) are not mirrored to the fork; create a label only when a convention here calls for it. See `triage-labels.md` for the triage roles.
- `.github/ISSUE_TEMPLATE/` defines bug-report and feature-request forms. When filing by CLI, mirror the fields those forms ask for rather than dropping them.
- `AGENTS.md` requires machine-readable contracts and identifiers in English, so issue titles and bodies are English. Quote user-facing copy and Chinese reports verbatim inside the body where they carry the evidence.
- Trading, tenancy, migration, and distributed-ownership issues name the regression tests they expect under `backend_api_python/tests/` or `mcp_server/tests/`; put that expectation in the body so a `ready-for-agent` ticket is truly self-contained.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view -R youluo1230/QuantDinger <number>`, then `gh pr view -R youluo1230/QuantDinger <number> --comments` (off a TTY, `--comments` prints only the comments, not the body), and `gh pr diff -R youluo1230/QuantDinger <number>` for the diff.
- **List external PRs for triage**: `gh api --paginate 'repos/youluo1230/QuantDinger/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`.
- **Comment / label / close**: `gh pr comment -R youluo1230/QuantDinger`, `gh pr edit -R youluo1230/QuantDinger --add-label`/`--remove-label`, `gh pr close -R youluo1230/QuantDinger`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view -R youluo1230/QuantDinger 42` and fall back to `gh issue view -R youluo1230/QuantDinger 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue in `youluo1230/QuantDinger`.

## When a skill says "fetch the relevant ticket"

Read it as in **Read an issue** above.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create -R youluo1230/QuantDinger --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (see **Make an issue a sub-issue of a parent**). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: see **Blocking** under Conventions.
- **Frontier query**: list the map's open children (`gh issue list -R youluo1230/QuantDinger --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (see **Count open blockers**, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit -R youluo1230/QuantDinger <n> --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment -R youluo1230/QuantDinger <n> --body "<answer>"`, then `gh issue close -R youluo1230/QuantDinger <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

The `wayfinder:*` labels exist on the fork (`wayfinder:map`, `:research`, `:prototype`, `:grilling`, `:task`). They do not exist upstream, so keep wayfinding work on the fork.
