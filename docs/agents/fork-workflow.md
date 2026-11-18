# Fork workflow

This checkout is a long-lived fork of an upstream product. It carries its own
feature branches, its own sibling frontend checkout, and its own issue tracker
on the fork. That changes which branch is the base for new work, where issues
live, and where frontend changes actually live.

Keep fork-specific rules in this file. It has no upstream counterpart, so it
never conflicts when upstream is merged.

## Remotes

| Remote | Repository | Role |
| --- | --- | --- |
| `upstream` | `OpenByteInc/QuantDinger` | read-only baseline; never file here |
| `origin` | `youluo1230/QuantDinger` | push target and issue tracker |

`origin` must stay on the fork: the MattSkills deck resolves the repository from
`git remote get-url origin` alone and has no picker, so an upstream `origin`
makes the issue panel show the upstream backlog instead of this fork's tickets.
`brokermr810/QuantDinger` used to hold that slot; it 301-redirects to
`OpenByteInc/QuantDinger`, so it was only an alias of upstream and was dropped.

## Branch topology

- The base branch for new backend work is `feat/tradingview-pipeline`, which
  tracks `origin/feat/tradingview-pipeline`. `main` tracks `upstream/main` and
  is only an upstream mirror.
- That branch carries the TradingView/Pine pipeline, the indicator workspace
  changes, and the `market_data` service. Check how far it has drifted with
  `git rev-list --left-right --count upstream/main...HEAD`.
- Sync upstream by merging `upstream/main` into the feature branch. Do not
  rebase or rewrite history that has already been pushed to `origin`.
- Cut new work from `feat/tradingview-pipeline`, not from `main`.

## Repositories and local layout

| Repo | Upstream | Fork | Local path |
| --- | --- | --- | --- |
| Backend (this tree) | `OpenByteInc/QuantDinger` | `youluo1230/QuantDinger` | project root |
| Web frontend | `OpenByteInc/QuantDinger-Vue` | `youluo1230/QuantDinger-Vue` | `../QuantDinger-Vue` (sibling) |
| Mobile | `OpenByteInc/QuantDinger-Mobile` | — | not checked out |

The frontend and mobile repositories are **source-available, not Apache 2.0**.
The backend licence in `LICENSE` does not cover them, and their own licences
travel with those repositories.

This checkout keeps the Vue source as a **sibling** directory, while
[docker-compose.build.yml](../../docker-compose.build.yml) defaults the build
context to `./QuantDinger-Vue`. Local frontend builds therefore need the path
override:

```bash
FRONTEND_SRC_PATH=../QuantDinger-Vue \
docker compose -f docker-compose.yml -f docker-compose.build.yml build frontend
```

`frontend` inherits `pull_policy: always`. Pass `-f docker-compose.build.yml`
whenever local UI work is under test, or the next `docker compose up` replaces
the locally built image with the GHCR `latest` one.

## Paired branches

Backend `feat/tradingview-pipeline` is developed against frontend
`feat/tradingview-chart`. Chart, indicator, and Pine-runtime changes usually
span both repositories, so state which frontend branch a change assumes.

## Local environment constraints

- `docker` requires `sudo` on the development host, so
  `docker compose config --quiet` fails from an unprivileged shell. Report the
  permission error instead of skipping the validation.
- The root `.env` belongs to the container user (`10001`) and is not readable
  from the host account. Do not try to read or rewrite it; use `.env.example`
  for documented keys. `FRONTEND_SRC_PATH` and `MOBILE_SRC_PATH` are currently
  documented only in [docker-compose.build.yml](../../docker-compose.build.yml).
- Backend `VERSION` and the frontend `package.json` version move independently.
  Do not assume they match when pinning images or writing release notes.

## Local Python environment

Tests do not have to run in a container. The host has `python3` 3.12 and `uv`,
though `uv` lives in `~/.local/bin` and is not on `PATH` for non-interactive
shells. No virtualenv is committed (`.venv/` is gitignored), so build one:

```bash
cd backend_api_python
uv venv .venv --python 3.12
```

Install the lock **without dependency resolution**. The lock pins
`filelock==4.2.4` while `litellm==1.106.3` requires `filelock>=3.16.1,<4`, so a
resolving install fails with "No solution found". CI settles this with
`--no-deps`; do the same:

```bash
uv pip install --no-deps -r requirements.lock
uv pip install -r requirements-dev.txt
```

## Local caches

`tv_import/` is gitignored, not repository content: it holds harvested
TradingView sources (the local checkout has about 124 scripts under
`tv_import/indicators/`). TradingView code is publicly readable but not freely
redistributable, so it is a cache and must never be committed.

The corpus regression only runs against an explicit directory:

```bash
cd backend_api_python
PINE_TRANSPILER_CORPUS=../tv_import/indicators \
python -m pytest tests/test_pine_transpiler_corpus.py -q
```

Without a corpus the suite skips, so treat a skip as "not verified" rather than
"passed". With `PINE_TRANSPILER_CORPUS=../tv_import/indicators` the local corpus
yields `7 passed, 2 skipped` in about two minutes. See
[TRADINGVIEW_PIPELINE.md](../trading/TRADINGVIEW_PIPELINE.md) for the harvest
flow.
