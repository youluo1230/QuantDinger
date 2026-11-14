# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the "Label in our tracker" column to match whatever vocabulary you actually use.

## Current tracker state

The table above is the intended vocabulary. On `OpenByteInc/QuantDinger` today only `wontfix` exists; the maintainers also use `bug`, `enhancement`, `documentation`, `question`, `needs-rfc`, `epic`, `roadmap`, `area:*`, `priority:*`, and `ready for contributors`.

The contributor token is READ-only on that repo, so only a maintainer can create the four missing labels:

```bash
gh label create needs-triage     --color FFFFFF --description "Maintainer needs to evaluate this issue"
gh label create needs-info       --color FFFFFF --description "Waiting on reporter for more information"
gh label create ready-for-agent  --color FFFFFF --description "Fully specified, ready for an AFK agent"
gh label create ready-for-human  --color FFFFFF --description "Requires human implementation"
```

Until they exist, `gh issue edit <n> --add-label needs-triage` fails with `HTTP 404: Not Found`. Write the intended triage state into a comment instead, and re-apply the label once a maintainer has created it. Do not let a failed label write block the actual work.
