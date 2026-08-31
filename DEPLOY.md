# wger-india — deployment

Fork of [wger](https://github.com/wger-project/wger) being extended with a
`wger_india` Django app: Indian food database (IFCT 2017 + custom fixtures),
fasting-window + water trackers, cooking-fat modifiers, a daily goal engine
with shortfall remediation, and a weekly markdown report exporter.

## Where things live

- **App code**: this repo, branch `india` (master tracks upstream for merges).
- **Production deployment**: the personal **orca** cluster —
  `personal-infra/services/wger/` (service.toml, nginx.conf, runbook).
  Domain: `https://fit.meghsakha.com`. Deploys happen by pushing to
  `mighty840/personal-infra` main (webhook → master reconciles).
- **Image**: milestone 1 runs vanilla `docker.io/wger/server:latest`.
  Once `wger_india` lands, CI here builds `ghcr.io/mighty840/wger-india`
  (Dockerfile: `extras/docker/production/Dockerfile`, built from repo root)
  and the three image refs in service.toml get bumped.

## Local smoke stack

A compose stack mirroring the production topology (postgres 15, redis,
web, celery worker + beat, nginx, no powersync) is used to validate config
changes before deploying. It lives with the orca service definition; run it
with any recent docker compose and check `http://localhost:8817`.

## Branch policy

- `master`: pristine upstream mirror (`git pull upstream master`).
- `india`: all custom work; rebase/merge master in periodically.
- New code goes into the `wger_india` app, not core wger files, wherever
  possible, to keep upstream merges clean.
