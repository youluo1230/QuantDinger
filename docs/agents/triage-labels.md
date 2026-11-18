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

The table above is the intended vocabulary. The tracker is the fork `youluo1230/QuantDinger`, where the token has ADMIN, so none of the labels are blocked on a maintainer. Today the fork carries the GitHub defaults (`bug`, `enhancement`, `documentation`, `question`, `help wanted`, `wontfix`, …) and the `wayfinder:*` family; the four triage roles below are created on demand, and the MattSkills deck creates them automatically when it first labels a ticket.

Create any missing role with:

```bash
gh label create -R youluo1230/QuantDinger needs-triage     --color FFFFFF --description "Maintainer needs to evaluate this issue"
gh label create -R youluo1230/QuantDinger needs-info       --color FFFFFF --description "Waiting on reporter for more information"
gh label create -R youluo1230/QuantDinger ready-for-agent  --color FFFFFF --description "Fully specified, ready for an AFK agent"
gh label create -R youluo1230/QuantDinger ready-for-human  --color FFFFFF --description "Requires human implementation"
```

`gh label` ignores `GH_REPO`, so the `-R` flag is required here: without it `gh` resolves to the `upstream` remote and the write fails against a read-only repo.

On `OpenByteInc/QuantDinger` the same token is READ-only, so those roles can only be added by an upstream maintainer. Do not let a failed label write block the actual work; write the intended state into a comment instead.
