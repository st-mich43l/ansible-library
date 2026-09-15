# ansible-library

[![Ansible](https://img.shields.io/badge/Ansible-control_repo-1A1918?style=flat-square&logo=ansible&logoColor=white)](https://www.ansible.com/)
[![Ansible Vault](https://img.shields.io/badge/Secrets-Ansible_Vault-EE0000?style=flat-square&logo=ansible&logoColor=white)](https://docs.ansible.com/ansible/latest/vault_guide/index.html)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/github/license/st-mich43l/ansible-library?style=flat-square)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/st-mich43l/ansible-library?style=flat-square)](https://github.com/st-mich43l/ansible-library/commits/master)
[![Repo Size](https://img.shields.io/github/repo-size/st-mich43l/ansible-library?style=flat-square)](https://github.com/st-mich43l/ansible-library)

Central **deployment control repo** for `st-mich43l` services — the single source
of truth for *where* and *how* each service deploys. Non-secret config lives in
`inventory/group_vars/all/` (clear text); secrets live in an **Ansible Vault**
(encrypted, safe to commit). GitHub Actions (via
[`action-library`](https://github.com/st-mich43l/action-library)) runs these
playbooks, so a service repo needs only **one** secret: the vault password.

> Restructured from an Ansible *collection* into a control repo: roles moved to
> `roles/`, plus `inventory/`, `group_vars/`, `playbooks/`.

## Layout

```
ansible.cfg                 # inventory path, roles_path, host_key_checking off
inventory/
  hosts.yml                 # the fleet (apexvoid VPS: host, port, users)
  group_vars/all/
    vars.yml                 # ← centralized variables (deploy_user, deploy_root, projects table)
    apexvoid_trading_bot_config.yml      # cleartext trading-bot CONFIG_FILE body
    apexvoid_trading_bot_bootstrap_env.yml  # process/bootstrap ENV only (no trading policy)
    vault.yml                # ← secrets, ansible-vault encrypted (SSH key, API keys)
roles/
  init_env/                 # provision a host (deploy user, docker group, key, deploy root)
  deploy_compose/           # rsync a service's deploy folder + docker compose up (mode: rsync)
  deploy_image/             # render compose from a pushed image tag + pull + up (mode: image)
  deploy_opensips/ deb_src_update/ swarm_cluster/   # pre-existing / special-purpose roles
playbooks/
  init-env.yml               # one-time host provisioning (run as root)
  deploy.yml                 # deploy one project - picks deploy_compose or deploy_image
                              # per that project's `mode` in vars.yml (default: rsync)
docs/                        # per-project deploy notes (e.g. manifest-authority cutovers)
artifacts/                   # point-in-time exported snapshots (e.g. env cutover records)
requirements.yml             # ansible.posix
```

`deploy.yml`'s role choice: each entry in the `projects:` table (`vars.yml`) sets
`mode: rsync` (default, → `deploy_compose`: ship the service's deploy folder,
build/compose on the host) or `mode: image` (→ `deploy_image`: render the compose
template with a CI-built/pushed image tag, `docker compose pull`, then up).
`routing` runs `rsync` mode; `apexvoid-occult`, `apexvoid-trading`,
`apexvoid-ledger`, and `apexvoid-trading-bot` all run `image` mode.

## Where variables live

Everything a deploy needs is defined **once** in
[`inventory/group_vars/all/vars.yml`](inventory/group_vars/all/vars.yml):

- fleet defaults: `deploy_user`, `deploy_root`, `deploy_key_path`, `deploy_public_key`
- a `projects:` table — add a service there and it becomes deployable.

Secrets (the deploy SSH private key, and later per-service API keys/tokens) live
in [`inventory/group_vars/all/vault.yml`](inventory/group_vars/all/vault.yml),
referenced through `vault_*` indirection vars. Edit with:

```bash
ansible-vault edit inventory/group_vars/all/vault.yml
```

### apexvoid-trading-bot

- **Vault** (`vault_apexvoid_trading_bot_env`): secrets and ops IDs only
  (Telegram tokens, Postgres password/`DATABASE_URL`, cTrader OAuth, channel/owner IDs).
- **Cleartext** `apexvoid_trading_bot_config.yml`: structured `trading-bot.yml`
  (profile, instruments, strategies, analysis, risk, …) — sole public trading policy.
- **Cleartext** `apexvoid_trading_bot_bootstrap_env.yml`: process/bootstrap ENV only
  (`CTRADER_HOST`/`PORT`, Redis, log paths, `CTRADER_CONFIGURATION_SOURCE=manifest`,
  `CTRADER_MANIFEST_PARITY_MODE=off`). No duplicated `AUTO_TRADE_*` trading policy.
- Deploy (`deploy_image`) renders `config/trading-bot.yml` + `secrets/trading-bot.env`
  on the host from `apexvoid_trading_bot_config.yml`/`..._bootstrap_env.yml` above,
  mounts them via the slim compose template, and `--force-recreate`s only when
  those checksums change.
- **This render is a full mirror, not a sync from the service repo.**
  `apexvoid_trading_bot_config.yml` here is a hand-maintained *copy* of
  `apexvoid-trading-bot`'s own `config/trading-bot.yml` — the deploy never reads
  that service-repo file. Editing `config/trading-bot.yml` in the service repo
  changes nothing on the host until this copy is updated to match in the same
  change. A drift here fails closed (Pydantic validates the deployed file on
  container start), so a missed sync surfaces as `config-compiler` exiting 1 and
  `trading-bot`/`ctrader-engine` never starting — not a silent policy mismatch,
  but still a full outage until someone notices and re-syncs this file.
- **Cutover note:** rollback requires redeploying the previous image **and** the
  previous Ansible inventory revision (legacy trading ENV). Flipping only
  `CTRADER_CONFIGURATION_SOURCE=environment` is insufficient after this change.

## Usage

Install the collection dependency once:

```bash
ansible-galaxy collection install -r requirements.yml
```

**Provision a fresh host** (run as root; deploy user not yet created):

```bash
ansible-playbook playbooks/init-env.yml -e provision_key=~/.ssh/id_rsa
```

**Deploy a project** (locally, pointing at a service checkout):

```bash
ansible-playbook playbooks/deploy.yml \
  -e project=routing -e src_base=/path/to/routing \
  --ask-vault-pass
```

`deploy.yml` stages the vaulted SSH key on the control node, then (rsync-mode
projects) ships the project's deploy folder to the host and runs `docker
compose`. The rsync excludes (`.env`, `certbot`, ...) are preserved on the host
by `--delete`. Image-mode projects (`deploy_image`) also require
`-e image_tag=<tag>` — CI always supplies this (the just-pushed short SHA); a
local run needs it passed explicitly.

## CI integration

Both reusable workflows live in
[`action-library`](https://github.com/st-mich43l/action-library) and end the
same way: checkout the service repo + this repo, install Ansible, run
`deploy.yml`. Pick the one matching the project's `mode`:

- **`deploy-ansible.yml`** — rsync-mode projects (e.g. `routing`): deploy only,
  no image build.
- **`build-push-deploy.yml`** — image-mode projects (e.g.
  `apexvoid-trading-bot`, which builds/pushes two images): builds each image,
  pushes `<tag>` + `latest`, then deploys with `-e image_tag=<short-sha>`.

The service repo only sets the secret `ANSIBLE_VAULT_PASSWORD` (image-mode
projects also need `DOCKERHUB_TOKEN` for the push):

```yaml
# rsync mode (routing)
jobs:
  deploy:
    uses: st-mich43l/action-library/.github/workflows/deploy-ansible.yml@master
    with:
      project: routing
    secrets:
      vault-password: ${{ secrets.ANSIBLE_VAULT_PASSWORD }}
```

```yaml
# image mode (apexvoid-trading-bot: builds + pushes 2 images, then deploys)
jobs:
  release:
    uses: st-mich43l/action-library/.github/workflows/build-push-deploy.yml@master
    with:
      project: apexvoid-trading-bot
      registry-namespace: mich43l
      images: >-
        [
          {"name": "apexvoid-trading-bot", "context": "algo-bot", "dockerfile": "algo-bot/Dockerfile"},
          {"name": "apexvoid-ctrader-engine", "context": "ctrader-engine", "dockerfile": "ctrader-engine/Dockerfile"}
        ]
    secrets:
      registry-token: ${{ secrets.DOCKERHUB_TOKEN }}
      vault-password: ${{ secrets.ANSIBLE_VAULT_PASSWORD }}
```

Because the vault is encrypted, this repo can be **public** and CI needs no read
token — both reusable workflows only need `project` (+ `images`/
`registry-namespace` for image mode) and the `vault-password` secret.

## Security

- `vault.yml` is committed **only while encrypted** (AES256). `.gitignore` allows
  it as an explicit exception; never commit a decrypted vault.
- Vault password files (`.vault_pass`, `vault_pass.txt`) are git-ignored — the
  password belongs in the GitHub secret `ANSIBLE_VAULT_PASSWORD`, nowhere in git.
