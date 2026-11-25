# ApexVoid Photobooth — release distribution

Official Windows builds of ApexVoid Photobooth are stored on the apexvoid VPS and served by the website container
at `https://photobooth.apexvoid.net/downloads/`. The website reads `/downloads/index.json` at runtime, so a new
release appears without rebuilding or redeploying the site.

```text
master CI (validated exe) ──► playbooks/publish-photobooth-release.yml ──► VPS storage
                                                                          │  (read-only bind mount)
browser ◄── routing (TLS) ◄── apexvoid-photobooth-webpage (nginx /downloads/) ◄┘
```

This repository is the single source of truth for the storage paths, permissions, publisher and publish playbook.

## Storage contract

All variables are flat, in [`inventory/group_vars/all/vars.yml`](../inventory/group_vars/all/vars.yml):

| Variable                                    | Value                                          |
| ------------------------------------------- | ---------------------------------------------- |
| `photobooth_release_root`                   | `/home/apexvoid/artifacts/apexvoid-photobooth` |
| `photobooth_release_public_dir`             | `<root>/public` (mounted into the website)     |
| `photobooth_release_staging_dir`            | `<root>/staging` (private, 0700)               |
| `photobooth_release_state_dir`              | `<root>/state` (private, 0700)                 |
| `photobooth_release_public_origin`          | `https://photobooth.apexvoid.net`              |
| `photobooth_release_public_prefix`          | `/downloads`                                   |
| `photobooth_release_owner` / `_group`       | `deploy_user` (`apexvoid`)                     |
| `photobooth_release_directory_mode`         | `0755` (public directories)                    |
| `photobooth_release_file_mode`              | `0644` (public files)                          |
| `photobooth_release_private_directory_mode` | `0700`                                         |
| `photobooth_release_schema_version`         | `1`                                            |
| `photobooth_release_minimum_free_space_mb`  | `1024` (kept free after an upload)             |
| `photobooth_release_retention_enabled`      | `false` — nothing is ever pruned automatically |

```text
<root>/
  public/                       0755  → website container /srv/photobooth-downloads (read-only)
    index.json                  0644  the manifest
    releases/v<version>/        0755  immutable once published
      ApexVoidPhotobooth-v<version>-windows-x64.exe
      SHA256SUMS  RELEASE-NOTES.md  THIRD-PARTY-NOTICES.txt  [LICENSE.txt  SOURCE.md]
  staging/                      0700  uploads in progress (same filesystem → atomic rename)
  state/                        0700
    bin/publish_release.py            installed by the publish role
    lock  journal.json                publishing lock and unfinished-transaction journal
    manifests/index-<time>.json       every previous manifest (rollback source)
    withdrawn/                        releases taken out of public storage
```

`/home/apexvoid` itself is 0700. That is fine: Docker bind-mounts `public/` as root, and nginx in the container (uid
101) only needs read access to `public/` and below. Staging and state are never mounted.

Provisioning is the `photobooth_release_storage` role. It runs automatically before every website deploy (the
`provision_role` of the `apexvoid-photobooth-webpage` project, so the bind-mount source exists before Compose starts)
and before every publication. It creates missing directories, fixes modes, and writes an **empty** manifest only when
none exists. It never overwrites a manifest and never deletes a release.

## URL contract

| Path                                                               | Served as                                     |
| ------------------------------------------------------------------ | --------------------------------------------- |
| `/downloads/index.json`                                            | `application/json`, `Cache-Control: no-cache` |
| `/downloads/releases/v<ver>/ApexVoidPhotobooth-v<ver>-windows-x64.exe` | attachment, immutable cache, Range support |
| `/downloads/releases/v<ver>/{SHA256SUMS,RELEASE-NOTES.md,THIRD-PARTY-NOTICES.txt,LICENSE.txt,SOURCE.md}` | text, immutable |

Anything else under `/downloads/` is a real 404 (no directory listing, no fallback to the site). Only GET and HEAD.
The website's nginx configuration (branch `webpage`, `deploy/nginx.conf`) implements this allowlist.

## Manifest (schema version 1)

```json
{
  "schemaVersion": 1,
  "updatedAt": "2027-01-20T09:00:00Z",
  "latest": { "stable": "0.2.1" },
  "releases": [
    {
      "version": "0.2.1",
      "tag": "v0.2.1",
      "name": "ApexVoid Photobooth",
      "channel": "stable",
      "publishedAt": "2027-01-20T09:00:00Z",
      "commit": "<40 hex>",
      "notesPath": "releases/v0.2.1/RELEASE-NOTES.md",
      "checksumsPath": "releases/v0.2.1/SHA256SUMS",
      "noticesPath": "releases/v0.2.1/THIRD-PARTY-NOTICES.txt",
      "assets": [
        {
          "platform": "windows",
          "architecture": "x64",
          "format": "exe",
          "filename": "ApexVoidPhotobooth-v0.2.1-windows-x64.exe",
          "path": "releases/v0.2.1/ApexVoidPhotobooth-v0.2.1-windows-x64.exe",
          "size": 1000003,
          "sha256": "<64 lowercase hex>",
          "signed": false,
          "includesCanonSdk": false
        }
      ]
    }
  ]
}
```

Rules (enforced by `publish_release.py validate_manifest` and mirrored by the website's runtime validator):

- `version` is SemVer without build metadata; `tag` is `v<version>`. Releases are unique and sorted newest first.
- `channel` is `stable` (no prerelease part) or `preview` (must have one).
- Every path is relative, inside `releases/v<version>/`, with a plain file name: no scheme, no `//`, no `..`.
- `size` is a positive integer; `sha256` is 64 lowercase hex; `commit` is 40 lowercase hex; times are UTC `…Z`.
- `latest.stable` names a published stable release, or is `null` only when none exists. Publishing an older version
  never moves it backwards; preview releases never become latest.
- `signed: false` means the executable has no Authenticode signature. Never set it to `true` without a real signature.

Shared fixtures (one valid manifest, the empty one and 20 invalid variants) live in
`roles/publish_photobooth_release/files/fixtures/`. The website copies them into its unit tests, so the publisher and
the site agree on the same contract.

## Publishing

The master branch of `apexvoid-photobooth` publishes: its `publish` job downloads the already validated CI artifact
(no rebuild), assembles `release.json` and the four files, and runs

```bash
ansible-playbook playbooks/publish-photobooth-release.yml \
  -e release_src=<dir with release.json and files/> -e release_version=0.2.1 -e release_sha256=<sha>
```

The playbook

1. stages the vaulted deploy key and pins the VPS host key (`deploy_target_known_hosts`, `StrictHostKeyChecking=yes`),
2. provisions storage, installs the publisher, removes stale staging directories (> 6 h),
3. checks free space (upload + `photobooth_release_minimum_free_space_mb`),
4. uploads into a fresh `staging/v<ver>.XXXXXXXX/`,
5. runs `publish_release.py publish`, which under the lock: verifies the allowlist, sizes, SHA-256 and `SHA256SUMS`;
   refuses a different artifact for an existing version (exit 3) and treats an identical one as `unchanged`; renames the
   staging directory into `public/releases/v<ver>/`; then backs up and atomically replaces `index.json` **last**,
6. checks through the public URL that the manifest lists the version and the executable answers HEAD 200 with the right
   size; if that fails after a new publication, restores the previous manifest,
7. always deletes the staging directory and the local key.

A crash between the rename and the manifest leaves an unlisted directory; the next attempt reuses it only if it is
byte-identical, otherwise it stops with a conflict for an operator.

## Operator procedures

Run on the VPS as `apexvoid`:

```bash
P="python3 /home/apexvoid/artifacts/apexvoid-photobooth/state/bin/publish_release.py --root /home/apexvoid/artifacts/apexvoid-photobooth"
```

| Task                         | Command                                                                     |
| ---------------------------- | --------------------------------------------------------------------------- |
| View releases and usage      | `$P status`                                                                 |
| Validate the live manifest   | `$P validate /home/apexvoid/artifacts/apexvoid-photobooth/public/index.json` |
| Verify a published binary    | `cd …/public/releases/v0.2.1 && sha256sum -c SHA256SUMS`                    |
| Verify from outside          | `curl -fsSL https://photobooth.apexvoid.net/downloads/releases/v0.2.1/SHA256SUMS`, download, `sha256sum -c` |
| Retry a failed publication   | GitHub → apexvoid-photobooth → Actions → failed run → **Re-run failed jobs** (reuses the artifact; identical retry is a no-op) |
| Make another stable current (rollback/promotion) | `$P set-latest 0.2.0`                                   |
| Withdraw a release           | `$P withdraw 0.2.1` (files move to `state/withdrawn/`; add `--remove-files` only to delete them) |
| Restore the previous manifest | `$P restore` (or `--backup <full path of a file in state/manifests/>`)                 |
| Remove stale uploads         | `$P clean-staging --older-than-hours 6`                                     |

Every mutating command takes the lock, keeps a backup of the manifest it replaces and prints one JSON line.
`restore` refuses a backup whose files are no longer in public storage. Exit codes: 0 ok, 2 invalid input,
3 version conflict, 4 storage/integrity, 5 manifest.

**Never** hand-edit `index.json`, reuse a version number for a different binary, or run `rsync --delete` against the
storage root. To replace a bad build, withdraw it and publish a new patch version.

### Backup and restore of the storage

The storage is plain files. Back up the whole root (it includes the manifest history):

```bash
tar -C /home/apexvoid/artifacts -czf apexvoid-photobooth-releases-$(date +%F).tar.gz apexvoid-photobooth
```

To restore on a new host: run the website deploy (provisions an empty layout), stop the publish workflow, extract the
archive over the root as `apexvoid`, run `$P validate …/public/index.json` and check one download from outside.

## Tests

```bash
python3 -m pytest roles/publish_photobooth_release/files -q -p no:asyncio   # publisher, 43 tests
ansible-playbook -i localhost, -c local tests/photobooth-release-local.yml -e test_root=/tmp/pbtest  # roles, no VPS
```

The local playbook provisions a throw-away root as the current user; add
`-e release_src=… -e release_version=… -e release_sha256=…` to run the whole publish role against it (public
verification is skipped). `.github/workflows/photobooth-release.yml` runs both on every change to these files.
