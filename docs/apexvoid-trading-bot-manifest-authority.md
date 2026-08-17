# Manifest authority inventory cutover (PR4B)

Depends on **apexvoid-trading-bot PR4A** (`refactor/config-manifest-authority-cutover`)
and a published image that supports `CTRADER_CONFIGURATION_SOURCE=manifest`
without legacy trading ENV.

## Ownership

| Layer | File | Contents |
| --- | --- | --- |
| Structured config | `inventory/group_vars/all/apexvoid_trading_bot_config.yml` | Public trading policy (`runtime.profile`, instruments, risk, …) |
| Bootstrap ENV | `inventory/group_vars/all/apexvoid_trading_bot_bootstrap_env.yml` | Process/bootstrap only + `source=manifest` / `parity=off` |
| Vault | `inventory/group_vars/all/vault.yml` | Secrets only (**unchanged** in this PR) |

## Production settings

```text
CTRADER_CONFIGURATION_SOURCE=manifest
CTRADER_MANIFEST_PARITY_MODE=off
runtime.profile=demo_eval
live instruments=XAU, EURUSD, GBPJPY
```

`parity=off` means ENV-versus-manifest comparison is disabled because duplicated
trading ENV is gone. Manifest schema/fingerprint/XAU validation stays enforced
in the application.

## Audit

`artifacts/apexvoid-trading-bot-env-cutover.json` lists removed vs retained keys
(no secret values).

## Deploy sequence

1. Merge + publish application PR4A images.
2. Merge this Ansible PR.
3. Deploy image tag + inventory together.
4. Do not deploy either half independently.

## Rollback

Redeploy the previous known-good image **and** the previous Ansible inventory
revision that still contains full legacy trading ENV. Do not flip only
`CTRADER_CONFIGURATION_SOURCE=environment` after this cutover.
