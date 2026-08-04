# ansible-library

[![Ansible](https://img.shields.io/badge/Ansible-control_repo-1A1918?style=flat-square&logo=ansible&logoColor=white)](https://www.ansible.com/)
[![Ansible Vault](https://img.shields.io/badge/Secrets-Ansible_Vault-EE0000?style=flat-square&logo=ansible&logoColor=white)](https://docs.ansible.com/ansible/latest/vault_guide/index.html)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/github/license/mach1el/ansible-library?style=flat-square)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/mach1el/ansible-library?style=flat-square)](https://github.com/mach1el/ansible-library/commits/master)
[![Repo Size](https://img.shields.io/github/repo-size/mach1el/ansible-library?style=flat-square)](https://github.com/mach1el/ansible-library)

Central **deployment control repo** for `mach1el` services — the single source of
truth for *where* and *how* each service deploys. Non-secret config lives in
`group_vars` (clear text); secrets live in an **Ansible Vault** (encrypted, safe
to commit). GitHub Actions (via [`action-library`](https://github.com/mach1el/action-library))
runs these playbooks, so a service repo needs only **one** secret: the vault
password.

> Restructured from an Ansible *collection* into a control repo: roles moved to
> `roles/`, plus `inventory/`, `group_vars/`, `playbooks/`.

## Layout

```
ansible.cfg              # inventory path, roles_path, host_key_checking off
inventory/hosts.yml      # the fleet (apexvoid VPS: host, port, users)
group_vars/all/
  vars.yml               # ← centralized variables (deploy_user, deploy_root, projects table)
  apexvoid_trading_bot_config.yml      # cleartext trading-bot CONFIG_FILE body
  apexvoid_trading_bot_shared_env.yml  # cleartext shared / cTrader ENV
  vault.yml              # ← secrets, ansible-vault encrypted (SSH key, API keys)
roles/
  init_env/              # provision a host (deploy user, docker group, key, deploy root)
  deploy_compose/        # rsync a service's deploy folder + docker compose up
  deb_src_update/ swarm_cluster/   # pre-existing roles
playbooks/
  init-env.yml           # one-time host provisioning (run as root)
  deploy.yml             # deploy one project
requirements.yml         # ansible.posix
```

## Where variables live

Everything a deploy needs is defined **once** in
[`group_vars/all/vars.yml`](group_vars/all/vars.yml):

- fleet defaults: `deploy_user`, `deploy_root`, `deploy_key_path`, `deploy_public_key`
- a `projects:` table — add a service there and it becomes deployable.

Secrets (the deploy SSH private key, and later per-service API keys/tokens) live
in [`group_vars/all/vault.yml`](group_vars/all/vault.yml), referenced through
`vault_*` indirection vars. Edit with:

```bash
ansible-vault edit group_vars/all/vault.yml
```

### apexvoid-trading-bot

- **Vault** (`vault_apexvoid_trading_bot_env`): secrets and ops IDs only
  (Telegram tokens, Postgres password/`DATABASE_URL`, cTrader OAuth, channel/owner IDs).
- **Cleartext** `apexvoid_trading_bot_config.yml`: structured `trading-bot.yml`
  (instruments, strategies, analysis, …).
- **Cleartext** `apexvoid_trading_bot_shared_env.yml`: non-secret ENV shared with
  cTrader and bootstrap flags (`AUTO_TRADE_*`, lookbacks already moved into YAML
  instruments when possible).
- Deploy renders `config/trading-bot.yml` + `secrets/trading-bot.env`, mounts them
  via the slim compose template, and `--force-recreate`s only when those
  checksums change.

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

`deploy.yml` stages the vaulted SSH key on the control node, then rsyncs the
project's deploy folder to the host and runs `docker compose`. The rsync excludes
(`.env`, `certbot`, ...) are preserved on the host by `--delete`.

## CI integration

`action-library`'s `deploy-ansible.yml` reusable workflow checks out the service
repo + this repo, installs Ansible, and runs `deploy.yml`. The service repo only
sets the secret `ANSIBLE_VAULT_PASSWORD`:

```yaml
jobs:
  deploy:
    uses: mach1el/action-library/.github/workflows/deploy-ansible.yml@master
    with:
      project: routing
    secrets:
      vault-password: ${{ secrets.ANSIBLE_VAULT_PASSWORD }}
```

Because the vault is encrypted, this repo can be **public** and CI needs no read
token. Keep it private and pass `ansible-library-token` instead if you prefer.

## Security

- `vault.yml` is committed **only while encrypted** (AES256). `.gitignore` allows
  it as an explicit exception; never commit a decrypted vault.
- Vault password files (`.vault_pass`, `vault_pass.txt`) are git-ignored — the
  password belongs in the GitHub secret `ANSIBLE_VAULT_PASSWORD`, nowhere in git.
