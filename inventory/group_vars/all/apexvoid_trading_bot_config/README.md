# `apexvoid_trading_bot_config` domain fragments

`apexvoid_trading_bot_config` used to be one ~900-line file. It's now
split into one file per domain, matching the same boundaries
apexvoid-trading-bot's own `config/*.yml` category files use (see that
repo's `docs/configuration.md`) — `runtime.yml`, `transport.yml`,
`instruments.yml`, `analysis.yml`, `auto_algo.yml`, `manual_algo.yml`,
`execution.yml`, `telegram.yml`. Each file defines a private
`_avc_<domain>:` variable holding only that domain's subtree; `_compose.yml`
recombines them with Jinja's `combine(recursive=True)` into the one
`apexvoid_trading_bot_config` variable `roles/deploy_image/tasks/main.yml`
still renders verbatim to `config/trading-bot.yml` on the host.

**This is a pure reorganization, not a behavior change.** Ansible loads
every `.yml`/`.yaml`/`.json` file under a `group_vars/<group>/` directory
recursively, so this subdirectory is picked up the same way the flat files
next to it (`vars.yml`, `vault.yml`, ...) always have been — verified with
`ansible-inventory --host apexvoid` before and after the split, resolving
to a byte-for-byte-equal (deep-equal) `apexvoid_trading_bot_config` value.
No task, template, or rendered file changed.

## Why split now, and why this shape

apexvoid-trading-bot's own config went through the same problem (one
900-line `trading-bot.yml`) and solved it with per-domain category files —
see its `docs/configuration-v3-migration-audit.md`. This mirrors that
split using **this file's still-old, pre-cutover key names**
(`delivery:`, not `telegram:`; `actionability`/`risk`/`strategies` at the
old top level, not nested under `auto_algo:`) because that's still the
literal shape `config/trading-bot.yml` needs on the host today. A few old
top-level keys straddle more than one new domain and are deliberately
split across fragments — `contract:` across `runtime.yml` (account, mode),
`transport.yml` (streams, versions), and `instruments.yml` (the
`instrument.canonical_symbol`/`symbols` duplicate, flagged upstream as
"superseded by instruments.yml"); the old flat `runtime:` key across
`runtime.yml` (`profile`, ansible-only) and `auto_algo.yml` (`auto_trade`,
`scanner`) — `combine(recursive=True)` deep-merges these back together, so
nothing is lost or reordered semantically.

`database.yml` and `journal.yml` have no fragment: this variable carries
no non-secret Postgres topology or journaling content today.

## When apexvoid-trading-bot's Stage C7 lands

Once apexvoid-trading-bot switches the live host to
`APEXVOID_CONFIG_FILE` + a real V3 root file (Stage C7, not done — see
that repo's `docs/configuration.md`), each of these fragments becomes the
natural place to also adopt the *new* key names/shape 1:1 with its
matching `config/<domain>.yml`, and `_compose.yml`'s job changes from
"render one legacy blob" to "render or template each domain file
separately, or drive `APEXVOID_CONFIG_FILE` directly." That's future work,
not part of this split.
