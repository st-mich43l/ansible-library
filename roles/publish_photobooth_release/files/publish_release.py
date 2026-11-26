#!/usr/bin/env python3
"""ApexVoid Photobooth release publisher (runs on the VPS; Python 3 standard library only).

Storage layout under --root (one filesystem, so every promotion is an atomic rename):

    public/index.json                 the manifest the website reads (schemaVersion 1)
    public/releases/v<version>/...    immutable release directories (served under /downloads/)
    staging/<unique>/                 uploads in progress (private, 0700)
    state/lock                        publishing lock (flock)
    state/journal.json                an unfinished transaction, for safe recovery
    state/manifests/                  every previous manifest (rollback source)
    state/withdrawn/                  releases taken out of public storage (never deleted here)

Commands (all mutating commands take the lock and print one JSON result line):

    init        create the layout and an empty manifest if none exists (never overwrites)
    publish     promote an uploaded staging directory into public storage and the manifest
    set-latest  make an already published stable version the current one (rollback / promotion)
    withdraw    remove a release from the manifest and move its files out of public storage
    restore     put back a previous manifest (from state/manifests/)
    status      print the manifest summary and storage usage
    validate    validate a manifest file (used by tests and operators)

Exit codes: 0 ok (including an identical re-publish), 2 invalid input, 3 version conflict,
4 storage problem (space, lock, integrity), 5 manifest problem.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

SCHEMA_VERSION = 1
PRODUCT_NAME = "ApexVoid Photobooth"

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

PLATFORMS = {"windows": {"x64"}}
FORMATS = {"exe"}
CHANNELS = {"stable", "preview"}
# Files a release directory may contain. Anything else is refused.
REQUIRED_FILES = {"SHA256SUMS", "RELEASE-NOTES.md", "THIRD-PARTY-NOTICES.txt"}
OPTIONAL_FILES = {"LICENSE.txt", "SOURCE.md"}


class PublishError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def fail(code: int, message: str) -> None:
    raise PublishError(code, message)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ versions
def parse_semver(v: str) -> tuple:
    m = SEMVER.match(v)
    if not m:
        fail(2, f"not a semantic version: {v!r}")
    major, minor, patch, pre = m.groups()
    pre_key: tuple = ()
    if pre is not None:
        pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    # a release (no pre-release part) sorts after every pre-release of the same version
    return (int(major), int(minor), int(patch), 1 if pre is None else 0, pre_key)


def is_prerelease(v: str) -> bool:
    return "-" in v


def exe_filename(version: str) -> str:
    return f"ApexVoidPhotobooth-v{version}-windows-x64.exe"


def release_dir_name(version: str) -> str:
    return f"v{version}"


# ------------------------------------------------------------------ validation
def _rel_path_ok(path: Any, version: str) -> bool:
    """A manifest path is relative to the public download prefix and stays inside its release directory."""
    if not isinstance(path, str) or not path:
        return False
    if path.startswith(("/", "\\")) or "://" in path or path.startswith("//") or "\\" in path:
        return False
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "releases" or parts[1] != release_dir_name(version):
        return False
    return bool(SAFE_NAME.match(parts[2])) and parts[2] not in {".", ".."}


def validate_release(r: Any, *, require_published: bool = True) -> list[str]:
    errs: list[str] = []
    if not isinstance(r, dict):
        return ["release is not an object"]
    v = r.get("version")
    if not isinstance(v, str) or not SEMVER.match(v):
        return [f"invalid version {v!r}"]
    if r.get("tag") != f"v{v}":
        errs.append(f"{v}: tag must be v{v}")
    if r.get("name") != PRODUCT_NAME:
        errs.append(f"{v}: name must be {PRODUCT_NAME!r}")
    ch = r.get("channel")
    if ch not in CHANNELS:
        errs.append(f"{v}: channel must be one of {sorted(CHANNELS)}")
    elif (ch == "stable") == is_prerelease(v):
        errs.append(f"{v}: stable releases have no pre-release part; preview releases must have one")
    if require_published and not (isinstance(r.get("publishedAt"), str) and TIMESTAMP.match(r["publishedAt"])):
        errs.append(f"{v}: publishedAt must be an ISO-8601 UTC timestamp")
    if not (isinstance(r.get("commit"), str) and COMMIT.match(r["commit"])):
        errs.append(f"{v}: commit must be a full 40-character lower-case SHA")
    for key in ("notesPath", "checksumsPath", "noticesPath"):
        if not _rel_path_ok(r.get(key), v):
            errs.append(f"{v}: {key} must be releases/v{v}/<file>")
    assets = r.get("assets")
    if not isinstance(assets, list) or not assets:
        errs.append(f"{v}: assets must be a non-empty list")
        return errs
    for a in assets:
        if not isinstance(a, dict):
            errs.append(f"{v}: asset is not an object")
            continue
        plat, arch, fmt = a.get("platform"), a.get("architecture"), a.get("format")
        if plat not in PLATFORMS or arch not in PLATFORMS.get(plat, set()):
            errs.append(f"{v}: unsupported platform/architecture {plat}/{arch}")
        if fmt not in FORMATS:
            errs.append(f"{v}: unsupported format {fmt!r}")
        fn = a.get("filename")
        if fn != exe_filename(v):
            errs.append(f"{v}: asset filename must be {exe_filename(v)}")
        if a.get("path") != f"releases/{release_dir_name(v)}/{fn}" or not _rel_path_ok(a.get("path"), v):
            errs.append(f"{v}: asset path must be releases/v{v}/{fn}")
        size = a.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            errs.append(f"{v}: asset size must be a positive integer")
        if not (isinstance(a.get("sha256"), str) and SHA256.match(a["sha256"])):
            errs.append(f"{v}: asset sha256 must be 64 lower-case hex characters")
        for flag in ("signed", "includesCanonSdk"):
            if not isinstance(a.get(flag), bool):
                errs.append(f"{v}: asset {flag} must be true or false")
    return errs


def validate_manifest(m: Any) -> list[str]:
    if not isinstance(m, dict):
        return ["manifest is not an object"]
    errs: list[str] = []
    if m.get("schemaVersion") != SCHEMA_VERSION:
        return [f"unsupported schemaVersion {m.get('schemaVersion')!r} (expected {SCHEMA_VERSION})"]
    if not (isinstance(m.get("updatedAt"), str) and TIMESTAMP.match(m["updatedAt"])):
        errs.append("updatedAt must be an ISO-8601 UTC timestamp")
    latest = m.get("latest")
    if not isinstance(latest, dict) or "stable" not in latest:
        errs.append("latest.stable is required (null when there is no stable release)")
        latest = {"stable": None}
    releases = m.get("releases")
    if not isinstance(releases, list):
        return errs + ["releases must be a list"]
    seen: set[str] = set()
    for r in releases:
        errs += validate_release(r)
        v = r.get("version") if isinstance(r, dict) else None
        if isinstance(v, str):
            if v in seen:
                errs.append(f"duplicate version {v}")
            seen.add(v)
    stable = latest.get("stable")
    if stable is not None:
        match = [r for r in releases if isinstance(r, dict) and r.get("version") == stable]
        if not match or match[0].get("channel") != "stable":
            errs.append(f"latest.stable {stable!r} is not a published stable release")
    elif any(isinstance(r, dict) and r.get("channel") == "stable" for r in releases):
        # null is allowed only when nothing stable is published
        errs.append("latest.stable is null although stable releases are published")
    return errs


def empty_manifest(now: str | None = None) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "updatedAt": now or utc_now(), "latest": {"stable": None}, "releases": []}


# ------------------------------------------------------------------ filesystem helpers
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: str, data: dict, mode: int = 0o644) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(d)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class Store:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.public = os.path.join(self.root, "public")
        self.releases = os.path.join(self.public, "releases")
        self.staging = os.path.join(self.root, "staging")
        self.state = os.path.join(self.root, "state")
        self.manifest_path = os.path.join(self.public, "index.json")
        self.journal_path = os.path.join(self.state, "journal.json")
        self.backups = os.path.join(self.state, "manifests")
        self.withdrawn = os.path.join(self.state, "withdrawn")
        self.ledger_path = os.path.join(self.state, "ledger.json")

    def init(self) -> bool:
        for d, mode in ((self.root, 0o755), (self.public, 0o755), (self.releases, 0o755), (self.staging, 0o700),
                        (self.state, 0o700), (self.backups, 0o700), (self.withdrawn, 0o700)):
            os.makedirs(d, exist_ok=True)
            os.chmod(d, mode)
        created = not os.path.exists(self.manifest_path)
        if created:
            atomic_write_json(self.manifest_path, empty_manifest())
        if not os.path.exists(self.ledger_path):
            atomic_write_json(self.ledger_path, {"schemaVersion": 1, "published": {}, "expired": {}}, mode=0o600)
        return created

    def load_manifest(self) -> dict:
        try:
            with open(self.manifest_path, encoding="utf-8") as f:
                m = json.load(f)
        except FileNotFoundError:
            fail(5, "no manifest: run init first")
        except json.JSONDecodeError as e:
            fail(5, f"manifest is not valid JSON: {e}")
        errs = validate_manifest(m)
        if errs:
            fail(5, "current manifest is invalid: " + "; ".join(errs))
        return m

    def write_manifest(self, m: dict) -> None:
        errs = validate_manifest(m)
        if errs:
            fail(5, "refusing to write an invalid manifest: " + "; ".join(errs))
        if os.path.exists(self.manifest_path):
            backup = os.path.join(self.backups, f"index-{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns()}.json")
            shutil.copy2(self.manifest_path, backup)
            os.chmod(backup, 0o600)
            backups = sorted(f for f in os.listdir(self.backups) if f.startswith("index-") and f.endswith(".json"))
            for old in backups[:-20]:
                os.unlink(os.path.join(self.backups, old))
        atomic_write_json(self.manifest_path, m)

    def load_ledger(self) -> dict:
        try:
            with open(self.ledger_path, encoding="utf-8") as f:
                ledger = json.load(f)
        except FileNotFoundError:
            ledger = {"schemaVersion": 1, "published": {}, "expired": {}}
        except (OSError, json.JSONDecodeError) as e:
            fail(5, f"publication ledger is unreadable: {e}")
        if not isinstance(ledger, dict) or ledger.get("schemaVersion") != 1 or not isinstance(ledger.get("published"), dict) or not isinstance(ledger.get("expired"), dict):
            fail(5, "publication ledger has an invalid schema")
        return ledger

    @contextlib.contextmanager
    def lock(self, timeout: float = 120.0) -> Iterator[None]:
        os.makedirs(self.state, exist_ok=True)
        fd = os.open(os.path.join(self.state, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        fail(4, "another publication holds the lock; try again later")
                    time.sleep(0.2)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def disk_free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def dir_size(path: str) -> int:
    total = 0
    for base, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(base, f)).st_size
    return total


# ------------------------------------------------------------------ publish
def check_release_dir(path: str, release: dict) -> None:
    """The directory holds exactly the allowlisted regular files, with matching checksums."""
    v = release["version"]
    if not os.path.isdir(path) or os.path.islink(path):
        fail(2, f"release directory missing: {path}")
    names = set(os.listdir(path))
    allowed = REQUIRED_FILES | OPTIONAL_FILES | {exe_filename(v)}
    extra = names - allowed
    if extra:
        fail(2, f"files not allowed in a release: {sorted(extra)}")
    missing = (REQUIRED_FILES | {exe_filename(v)}) - names
    if missing:
        fail(2, f"release is missing {sorted(missing)}")
    for n in names:
        p = os.path.join(path, n)
        if os.path.islink(p) or not os.path.isfile(p):
            fail(2, f"{n} is not a regular file")
    asset = release["assets"][0]
    exe = os.path.join(path, exe_filename(v))
    if os.path.getsize(exe) != asset["size"]:
        fail(4, f"executable size {os.path.getsize(exe)} differs from the release metadata {asset['size']}")
    actual = sha256_file(exe)
    if actual != asset["sha256"]:
        fail(4, f"executable SHA-256 {actual} differs from the release metadata {asset['sha256']}")
    with open(os.path.join(path, "SHA256SUMS"), encoding="utf-8") as f:
        sums = {}
        for line in f:
            line = line.strip()
            if line:
                parts = line.split()
                if len(parts) != 2:
                    fail(2, "SHA256SUMS has a malformed line")
                sums[parts[1].lstrip("*")] = parts[0]
    if sums.get(exe_filename(v)) != actual:
        fail(4, "SHA256SUMS does not list the executable with its actual checksum")


# ------------------------------------------------------------------ retention and transaction-safe publication
def plan_retention(manifest: dict, candidate: dict | None = None, archive_keep: int = 4) -> dict:
    """Pure deterministic planner. The authoritative current pointer survives an intentional rollback."""
    errs = validate_manifest(manifest)
    if errs:
        fail(5, "cannot plan retention from invalid manifest: " + "; ".join(errs))
    if not isinstance(archive_keep, int) or isinstance(archive_keep, bool) or archive_keep < 0:
        fail(2, "archive_keep must be a non-negative integer")
    all_releases = [copy.deepcopy(r) for r in manifest["releases"]]
    releases = [r for r in all_releases if r["channel"] == "stable"]
    candidate_version = None
    if candidate is not None:
        errs = validate_release(candidate)
        if errs:
            fail(2, "invalid candidate for retention planning: " + "; ".join(errs))
        if candidate["channel"] != "stable":
            fail(2, "only stable releases may be published in the public catalog")
        candidate_version = candidate["version"]
        releases = [r for r in releases if r["version"] != candidate_version]
        releases.append(copy.deepcopy(candidate))
    current = candidate_version or manifest["latest"]["stable"]
    if current and not any(r["version"] == current for r in releases):
        fail(5, f"current version {current} is missing from retention candidates")
    ordered = sorted((r for r in releases if r["version"] != current), key=lambda r: parse_semver(r["version"]), reverse=True)
    kept_archives = ordered[:archive_keep]
    retained = ([next(r for r in releases if r["version"] == current)] if current else []) + kept_archives
    kept_versions = {r["version"] for r in retained}
    expired = sorted((r for r in all_releases if r["channel"] != "stable" or r["version"] not in kept_versions),
                     key=lambda r: parse_semver(r["version"]))
    planned = copy.deepcopy(manifest)
    planned["releases"] = sorted(retained, key=lambda r: parse_semver(r["version"]), reverse=True)
    planned["latest"]["stable"] = current
    if candidate is not None:
        planned["updatedAt"] = candidate["publishedAt"]
    return {"current": current, "retained": planned["releases"], "archives": kept_archives, "expired": expired,
            "cleanupPaths": [f"public/releases/{release_dir_name(r['version'])}" for r in expired],
            "manifestChanged": planned != manifest, "manifest": planned}


def verify_public_release(origin: str, public_prefix: str, manifest: dict, version: str, timeout: int = 30) -> None:
    """Fetch and validate the public manifest, then stream/hash the published executable."""
    base = origin.rstrip("/") + "/" + public_prefix.strip("/")
    try:
        req = urllib.request.Request(base + "/index.json", headers={"Cache-Control": "no-cache", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            public_manifest = json.loads(response.read().decode("utf-8"))
        errs = validate_manifest(public_manifest)
        if errs or public_manifest != manifest:
            fail(4, "public manifest is invalid or differs from the transaction manifest")
        release = next(r for r in manifest["releases"] if r["version"] == version)
        asset = release["assets"][0]
        req = urllib.request.Request(base + "/" + asset["path"])
        digest, size = hashlib.sha256(), 0
        with urllib.request.urlopen(req, timeout=timeout) as response:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
        if size != asset["size"] or digest.hexdigest() != asset["sha256"]:
            fail(4, "public executable size or SHA-256 differs from the manifest")
    except PublishError:
        raise
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as e:
        fail(4, f"public release verification failed: {e}")


def _same_manifest(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True, separators=(",", ":")) == json.dumps(b, sort_keys=True, separators=(",", ":"))


def _write_ledger(store: Store, ledger: dict) -> None:
    atomic_write_json(store.ledger_path, ledger, mode=0o600)


def safe_delete_expired(store: Store, version: str, release: dict, current_manifest: dict, txid: str) -> int:
    """Delete exactly one validated, transaction-authorized release directory."""
    try:
        with open(store.journal_path, encoding="utf-8") as f:
            journal = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(4, f"no valid committed transaction authorizes deletion: {e}")
    if journal.get("id") != txid or journal.get("phase") not in {"verified", "cleanup-pending", "complete"}:
        fail(4, "transaction is not committed for retention cleanup")
    if not any(isinstance(r, dict) and r.get("version") == version for r in journal.get("expired", [])):
        fail(4, f"transaction does not mark {version} for expiration")
    if not SEMVER.match(version) or release.get("version") != version:
        fail(4, "refusing to delete an invalid version")
    if any(r["version"] == version for r in current_manifest["releases"]) or current_manifest["latest"]["stable"] == version:
        fail(4, f"refusing to delete manifest-referenced version {version}")
    root = os.path.realpath(store.releases)
    target = os.path.join(store.releases, release_dir_name(version))
    if os.path.islink(store.releases) or os.path.islink(target):
        fail(4, f"refusing to delete symbolic link for {version}")
    if os.path.realpath(os.path.dirname(target)) != root or os.path.basename(target) != release_dir_name(version):
        fail(4, f"release path escaped storage root for {version}")
    if not os.path.lexists(target):
        return 0
    try:
        check_release_dir(target, release)
    except PublishError:
        # A previous rmtree may have stopped part-way through. Retry only the exact
        # transaction-authorized path, and only if every survivor is an allowlisted regular file.
        if journal.get("phase") != "cleanup-pending":
            raise
        allowed = REQUIRED_FILES | OPTIONAL_FILES | {exe_filename(version)}
        names = os.listdir(target)
        if set(names) - allowed:
            fail(4, f"partial cleanup contains unexpected files for {version}")
        for name in names:
            path = os.path.join(target, name)
            if os.path.islink(path) or not os.path.isfile(path):
                fail(4, f"partial cleanup contains a non-regular file for {version}")
        exe = os.path.join(target, exe_filename(version))
        if os.path.exists(exe) and (os.path.getsize(exe) != release["assets"][0]["size"] or
                                    sha256_file(exe) != release["assets"][0]["sha256"]):
            fail(4, f"partial cleanup executable identity changed for {version}")
    size = dir_size(target)
    shutil.rmtree(target)
    fsync_dir(store.releases)
    return size


def _finish_cleanup(store: Store, tx: dict) -> dict:
    manifest = store.load_manifest()
    if not _same_manifest(manifest, tx["newManifest"]):
        fail(5, "pending transaction no longer matches the public manifest")
    ledger, removed = store.load_ledger(), int(tx.get("removedBytes", 0))
    for retained in tx.get("retainedRecords", []):
        version = retained["version"]
        if version not in ledger["expired"]:
            ledger["published"][version] = {"version": version, "publishedAt": retained["publishedAt"],
                "sourceCommit": retained["commit"], "sha256": retained["assets"][0]["sha256"], "status": "retained"}
    new_record = tx.get("newRecord")
    if isinstance(new_record, dict):
        ledger["published"][new_record["version"]] = {"version": new_record["version"], "publishedAt": new_record["publishedAt"],
            "sourceCommit": new_record["commit"], "sha256": new_record["assets"][0]["sha256"], "status": "retained"}
        _write_ledger(store, ledger)
    for expired in tx.get("expired", []):
        version = expired["version"]
        try:
            removed += safe_delete_expired(store, version, expired, manifest, tx["id"])
        except OSError as e:
            tx.update(phase="cleanup-pending", removedBytes=removed, lastError=str(e))
            atomic_write_json(store.journal_path, tx, mode=0o600)
            fail(4, f"publication succeeded; cleanup pending for v{version}: {e}")
        entry = {"version": version, "publishedAt": expired["publishedAt"], "sourceCommit": expired["commit"],
                 "sha256": expired["assets"][0]["sha256"], "expiredAt": tx["committedAt"], "status": "expired"}
        ledger["published"][version] = entry
        ledger["expired"][version] = copy.deepcopy(entry)
        tx["removedBytes"] = removed
        _write_ledger(store, ledger)
        atomic_write_json(store.journal_path, tx, mode=0o600)
    tx.update(phase="complete", removedBytes=removed)
    atomic_write_json(store.journal_path, tx, mode=0o600)
    os.unlink(store.journal_path)
    fsync_dir(store.state)
    return {"removedBytes": removed, "expired": [r["version"] for r in tx.get("expired", [])]}


def recover(store: Store) -> None:
    if not os.path.exists(store.journal_path):
        return
    try:
        with open(store.journal_path, encoding="utf-8") as f:
            tx = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(5, f"transaction journal is unreadable: {e}")
    if not isinstance(tx, dict) or tx.get("schemaVersion") != 1 or not tx.get("id"):
        fail(5, "transaction journal has an invalid schema")
    manifest = store.load_manifest()
    if tx.get("phase") in {"verified", "cleanup-pending", "complete"}:
        _finish_cleanup(store, tx)
        return
    old, new = tx.get("previousManifest"), tx.get("newManifest")
    if not isinstance(old, dict) or not isinstance(new, dict) or validate_manifest(old) or validate_manifest(new):
        fail(5, "transaction journal does not contain valid compare-and-swap manifests")
    if _same_manifest(manifest, new):
        atomic_write_json(store.manifest_path, old)
    elif not _same_manifest(manifest, old):
        fail(5, "transaction manifest changed independently; refusing unsafe recovery")
    if tx.get("kind") == "publish" and isinstance(tx.get("newRecord"), dict):
        candidate = tx["newRecord"]
        target = os.path.join(store.releases, release_dir_name(candidate["version"]))
        if os.path.lexists(target):
            check_release_dir(target, candidate)
            if not any(r["version"] == candidate["version"] for r in old["releases"]):
                shutil.rmtree(target)
                fsync_dir(store.releases)
    os.unlink(store.journal_path)
    fsync_dir(store.state)


def publish(store: Store, staging_dir: str, expected_sha: str, now: str | None = None,
            reserve_bytes: int = 1 << 30, archive_keep: int = 4, retention_enabled: bool = True,
            verify_origin: str = "", public_prefix: str = "/downloads", max_versions: int | None = None) -> dict:
    staging_dir = os.path.abspath(staging_dir)
    if os.path.commonpath([staging_dir, store.staging]) != store.staging or staging_dir == store.staging:
        fail(2, "the upload must be a directory inside the staging area")
    if not SHA256.match(expected_sha or ""):
        fail(2, "the trusted CI checksum must be 64 lower-case hex characters")
    try:
        with open(os.path.join(staging_dir, "release.json"), encoding="utf-8") as f:
            release = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(2, f"release metadata unreadable: {e}")
    errs = validate_release(release, require_published=False)
    if errs or "publishedAt" in release:
        fail(2, "invalid release metadata or publisher-controlled publishedAt")
    v, asset = release["version"], release["assets"][0]
    if release["channel"] != "stable":
        fail(2, "preview releases are not accepted into the public release catalog")
    if len(release["assets"]) != 1 or asset["sha256"] != expected_sha:
        fail(4, "release asset count or trusted checksum is invalid")
    files_dir = os.path.join(staging_dir, "files")
    check_release_dir(files_dir, release)
    if disk_free_bytes(store.root) < reserve_bytes:
        fail(4, f"less than the configured reserve of {reserve_bytes // (1 << 20)} MB is free")
    if not isinstance(archive_keep, int) or archive_keep < 0:
        fail(2, "archive_keep must be a non-negative integer")
    if max_versions is not None and max_versions != archive_keep + 1:
        fail(2, "max_versions must equal archive_keep + 1")

    with store.lock():
        recover(store)
        manifest, ledger = store.load_manifest(), store.load_ledger()
        if v in ledger["expired"]:
            fail(3, f"v{v} has expired and cannot be republished (original sha256 {ledger['expired'][v].get('sha256')})")
        existing = next((r for r in manifest["releases"] if r["version"] == v), None)
        if existing:
            if existing["assets"][0]["sha256"] == expected_sha:
                shutil.rmtree(staging_dir, ignore_errors=True)
                return {"status": "unchanged", "version": v, "message": f"{v} is already published with this executable"}
            fail(3, f"version {v} is already published with a different executable; publish a new version instead")
        if v in ledger["published"]:
            fail(3, f"v{v} has an immutable publication identity and cannot be republished after withdrawal")
        current = manifest["latest"]["stable"]
        if current and parse_semver(v) <= parse_semver(current):
            fail(3, f"out-of-order publication v{v} cannot replace or follow current v{current}")
        record = copy.deepcopy(release)
        record["publishedAt"] = now or utc_now()
        planned = plan_retention(manifest, record, archive_keep)
        new_manifest = planned["manifest"]
        if not retention_enabled:
            new_manifest = copy.deepcopy(manifest)
            new_manifest["releases"].append(record)
            new_manifest["releases"].sort(key=lambda r: parse_semver(r["version"]), reverse=True)
            new_manifest["latest"]["stable"] = v
            new_manifest["updatedAt"] = record["publishedAt"]
        target = os.path.join(store.releases, release_dir_name(v))
        if os.path.lexists(target):
            fail(3, f"{target} exists outside the current manifest; investigate before retrying")
        tx = {"schemaVersion": 1, "id": f"publish-{time.time_ns()}", "kind": "publish", "phase": "prepared",
              "version": v, "previousManifest": manifest, "newManifest": new_manifest,
              "newRecord": record, "retainedRecords": new_manifest["releases"],
              "expired": planned["expired"] if retention_enabled else [], "committedAt": record["publishedAt"]}
        atomic_write_json(store.journal_path, tx, mode=0o600)
        os.chmod(files_dir, 0o755)
        for name in os.listdir(files_dir):
            os.chmod(os.path.join(files_dir, name), 0o644)
        os.rename(files_dir, target)
        fsync_dir(store.releases)
        check_release_dir(target, record)
        tx["phase"] = "promoted"
        atomic_write_json(store.journal_path, tx, mode=0o600)
        try:
            store.write_manifest(new_manifest)
            tx["phase"] = "manifest-written"
            atomic_write_json(store.journal_path, tx, mode=0o600)
            if verify_origin:
                verify_public_release(verify_origin, public_prefix, new_manifest, v)
            tx["phase"] = "verified"
            atomic_write_json(store.journal_path, tx, mode=0o600)
        except BaseException:
            recover(store)
            raise
        ledger["published"][v] = {"version": v, "publishedAt": record["publishedAt"], "sourceCommit": record["commit"],
                                  "sha256": expected_sha, "status": "retained"}
        _write_ledger(store, ledger)
        cleanup = _finish_cleanup(store, tx) if retention_enabled else {"removedBytes": 0, "expired": []}
        if not retention_enabled:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(store.journal_path)
        shutil.rmtree(staging_dir, ignore_errors=True)
        return {"status": "published", "version": v, "latest": new_manifest["latest"]["stable"],
                "path": f"releases/{release_dir_name(v)}/{asset['filename']}", "size": asset["size"],
                "sha256": expected_sha, "retention": {"kept": [r["version"] for r in new_manifest["releases"]],
                "expired": cleanup["expired"], "removedBytes": cleanup["removedBytes"]}}


# ------------------------------------------------------------------ operator commands
def retention_report(store: Store, archive_keep: int = 4) -> dict:
    """Read-only planner with conservative filesystem accounting; never creates directories or files."""
    manifest = store.load_manifest()
    plan = plan_retention(manifest, archive_keep=archive_keep)
    known = {release_dir_name(r["version"]): r for r in manifest["releases"]}
    expired = {r["version"]: r for r in plan["expired"]}
    disk_entries = set(os.listdir(store.releases)) if os.path.isdir(store.releases) else set()
    unrecognized = sorted(name for name in disk_entries if name not in known and name not in {release_dir_name(v) for v in expired})
    violations, reclaimable, retained_versions = [], 0, [r["version"] for r in plan["retained"]]
    for r in plan["retained"]:
        path = os.path.join(store.releases, release_dir_name(r["version"]))
        try:
            check_release_dir(path, r)
        except PublishError as e:
            violations.append(str(e))
    for version, r in expired.items():
        path = os.path.join(store.releases, release_dir_name(version))
        if not os.path.lexists(path):
            continue
        try:
            check_release_dir(path, r)
            reclaimable += dir_size(path)
        except PublishError as e:
            violations.append(str(e))
    for name in disk_entries:
        if name not in known:
            unrecognized.append(name)
    ledger = store.load_ledger()
    return {"current": plan["current"], "existingReleaseCount": len(manifest["releases"]), "retained": retained_versions,
            "archives": [r["version"] for r in plan["archives"]], "remove": [r["version"] for r in plan["expired"]],
            "cleanupPaths": plan["cleanupPaths"], "reclaimableBytes": reclaimable,
            "unrecognizedDirectories": sorted(set(unrecognized)), "policyViolations": violations,
            "retentionLimit": archive_keep + 1, "expiredPublicationCount": len(ledger["expired"]),
            "pendingCleanup": os.path.exists(store.journal_path), "manifestChanged": plan["manifestChanged"]}


def prune(store: Store, archive_keep: int = 4, apply: bool = False, verify_origin: str = "",
          public_prefix: str = "/downloads") -> dict:
    if not apply:
        return {"status": "dry-run", **retention_report(store, archive_keep)}
    with store.lock():
        recover(store)
        manifest = store.load_manifest()
        report = retention_report(store, archive_keep)
        if report["policyViolations"]:
            fail(4, "retention refused because release integrity checks failed: " + "; ".join(report["policyViolations"]))
        plan = plan_retention(manifest, archive_keep=archive_keep)
        for r in plan["retained"]:
            check_release_dir(os.path.join(store.releases, release_dir_name(r["version"])), r)
        ledger = store.load_ledger()
        for r in plan["retained"]:
            identity = {"version": r["version"], "publishedAt": r["publishedAt"], "sourceCommit": r["commit"],
                        "sha256": r["assets"][0]["sha256"], "status": "retained"}
            prior = ledger["published"].get(r["version"])
            if prior and prior.get("sha256") != identity["sha256"]:
                fail(5, f"ledger identity mismatch for retained v{r['version']}")
            ledger["published"][r["version"]] = prior or identity
        _write_ledger(store, ledger)
        if not plan["manifestChanged"] and not plan["expired"]:
            return {"status": "unchanged", **report}
        tx = {"schemaVersion": 1, "id": f"prune-{time.time_ns()}", "kind": "prune", "phase": "prepared",
              "previousManifest": manifest, "newManifest": plan["manifest"], "retainedRecords": plan["retained"],
              "expired": plan["expired"], "committedAt": utc_now()}
        atomic_write_json(store.journal_path, tx, mode=0o600)
        try:
            store.write_manifest(plan["manifest"])
            tx["phase"] = "manifest-written"
            atomic_write_json(store.journal_path, tx, mode=0o600)
            if verify_origin and plan["current"]:
                verify_public_release(verify_origin, public_prefix, plan["manifest"], plan["current"])
            tx["phase"] = "verified"
            atomic_write_json(store.journal_path, tx, mode=0o600)
        except BaseException:
            recover(store)
            raise
        result = _finish_cleanup(store, tx)
        return {"status": "pruned", "current": plan["current"], "retained": [r["version"] for r in plan["retained"]],
                **result, "publicBytes": dir_size(store.public), "freeBytes": disk_free_bytes(store.root)}


def set_latest(store: Store, version: str) -> dict:
    parse_semver(version)
    with store.lock():
        recover(store)
        m = store.load_manifest()
        r = next((x for x in m["releases"] if x["version"] == version), None)
        if r is None or r["channel"] != "stable":
            fail(2, f"{version} is not a retained stable release; expired releases cannot be restored from metadata")
        check_release_dir(os.path.join(store.releases, release_dir_name(version)), r)
        previous = m["latest"]["stable"]
        m["latest"]["stable"] = version
        m["updatedAt"] = utc_now()
        planned = plan_retention(m, archive_keep=4)
        m = planned["manifest"]
        store.write_manifest(m)
        return {"status": "latest-changed", "from": previous, "to": version}


def withdraw(store: Store, version: str, remove_files: bool) -> dict:
    parse_semver(version)
    with store.lock():
        recover(store)
        m = store.load_manifest()
        if m["latest"]["stable"] == version:
            fail(4, "set another current stable release before withdrawing this version")
        if not any(r["version"] == version for r in m["releases"]):
            fail(2, f"{version} is not in the manifest")
        withdrawn_record = next(r for r in m["releases"] if r["version"] == version)
        m["releases"] = [r for r in m["releases"] if r["version"] != version]
        if m["latest"]["stable"] == version:
            stable = [r["version"] for r in m["releases"] if r["channel"] == "stable"]
            m["latest"]["stable"] = max(stable, key=parse_semver) if stable else None
        m["updatedAt"] = utc_now()
        store.write_manifest(m)
        ledger = store.load_ledger()
        ledger["published"].setdefault(version, {"version": version, "publishedAt": withdrawn_record["publishedAt"],
            "sourceCommit": withdrawn_record["commit"], "sha256": withdrawn_record["assets"][0]["sha256"]})
        ledger["published"][version]["status"] = "withdrawn"
        ledger["published"][version]["withdrawnAt"] = utc_now()
        _write_ledger(store, ledger)
        moved = None
        if remove_files:
            src = os.path.join(store.releases, release_dir_name(version))
            if os.path.isdir(src):
                moved = os.path.join(store.withdrawn, f"{release_dir_name(version)}-{time.strftime('%Y%m%dT%H%M%S')}")
                os.rename(src, moved)
        return {"status": "withdrawn", "version": version, "latest": m["latest"]["stable"], "filesMovedTo": moved}


def restore(store: Store, backup: str | None) -> dict:
    with store.lock():
        recover(store)
        if not backup:
            fail(2, "restore requires an explicit --backup name")
        backups = sorted(f for f in os.listdir(store.backups) if f.startswith("index-") and f.endswith(".json"))
        if not backups:
            fail(5, "no previous manifest to restore")
        name = backup
        if name not in backups:
            fail(2, f"unknown backup {name}; available: {backups[-5:]}")
        with open(os.path.join(store.backups, name), encoding="utf-8") as f:
            m = json.load(f)
        errs = validate_manifest(m)
        if errs:
            fail(5, "backup is invalid: " + "; ".join(errs))
        for r in m["releases"]:
            check_release_dir(os.path.join(store.releases, release_dir_name(r["version"])), r)
        plan = plan_retention(m, archive_keep=4)
        m = plan["manifest"]
        store.write_manifest(m)
        return {"status": "restored", "from": name, "latest": m["latest"]["stable"]}


def status(store: Store) -> dict:
    m = store.load_manifest()
    plan = plan_retention(m)
    ledger = store.load_ledger()
    pending_cleanup = False
    if os.path.exists(store.journal_path):
        try:
            with open(store.journal_path, encoding="utf-8") as f:
                pending_cleanup = json.load(f).get("phase") in {"verified", "cleanup-pending"}
        except (OSError, json.JSONDecodeError):
            fail(5, "transaction journal is unreadable")
    return {"latest": m["latest"]["stable"], "current": m["latest"]["stable"],
            "releases": [r["version"] for r in m["releases"]], "publicVersionCount": len(m["releases"]),
            "archives": [r["version"] for r in plan["archives"]],
            "retentionLimit": 5, "expiredPublicationCount": len(ledger["expired"]),
            "publicBytes": dir_size(store.public), "stagingBytes": dir_size(store.staging), "freeBytes": disk_free_bytes(store.root),
            "pendingJournal": os.path.exists(store.journal_path), "pendingCleanup": pending_cleanup}


def clean_staging(store: Store, older_than_s: int = 6 * 3600) -> int:
    n = 0
    for name in os.listdir(store.staging):
        p = os.path.join(store.staging, name)
        if os.path.isdir(p) and time.time() - os.path.getmtime(p) > older_than_s:
            shutil.rmtree(p, ignore_errors=True)
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("publish")
    p.add_argument("--staging-dir", required=True)
    p.add_argument("--expected-sha256", required=True)
    p.add_argument("--reserve-mb", type=int, default=1024)
    p.add_argument("--archive-keep", type=int, default=4)
    p.add_argument("--max-versions", type=int, default=5)
    p.add_argument("--retention-enabled", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--verify-origin", default="")
    p.add_argument("--public-prefix", default="/downloads")
    p = sub.add_parser("set-latest")
    p.add_argument("version")
    p = sub.add_parser("withdraw")
    p.add_argument("version")
    p.add_argument("--remove-files", action="store_true")
    p = sub.add_parser("restore")
    p.add_argument("--backup")
    sub.add_parser("status")
    p = sub.add_parser("retention-plan")
    p.add_argument("--archive-keep", type=int, default=4)
    p = sub.add_parser("prune")
    p.add_argument("--archive-keep", type=int, default=4)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--verify-origin", default="")
    p.add_argument("--public-prefix", default="/downloads")
    p = sub.add_parser("clean-staging")
    p.add_argument("--older-than-hours", type=int, default=6)
    p = sub.add_parser("validate")
    p.add_argument("manifest")
    a = ap.parse_args(argv)
    store = Store(a.root)
    try:
        if a.cmd == "init":
            out = {"status": "created" if store.init() else "exists"}
        elif a.cmd == "publish":
            out = publish(store, a.staging_dir, a.expected_sha256, reserve_bytes=a.reserve_mb << 20,
                          archive_keep=a.archive_keep, retention_enabled=a.retention_enabled,
                          verify_origin=a.verify_origin, public_prefix=a.public_prefix, max_versions=a.max_versions)
        elif a.cmd == "set-latest":
            out = set_latest(store, a.version)
        elif a.cmd == "withdraw":
            out = withdraw(store, a.version, a.remove_files)
        elif a.cmd == "restore":
            out = restore(store, a.backup)
        elif a.cmd == "status":
            out = status(store)
        elif a.cmd == "retention-plan":
            out = retention_report(store, a.archive_keep)
        elif a.cmd == "prune":
            out = prune(store, a.archive_keep, a.apply, a.verify_origin, a.public_prefix)
        elif a.cmd == "clean-staging":
            out = {"status": "cleaned", "removed": clean_staging(store, a.older_than_hours * 3600)}
        else:
            with open(a.manifest, encoding="utf-8") as f:
                errs = validate_manifest(json.load(f))
            out = {"status": "valid" if not errs else "invalid", "errors": errs}
            if errs:
                print(json.dumps(out))
                return 5
    except PublishError as e:
        print(json.dumps({"status": "error", "code": e.code, "error": str(e)}))
        return e.code
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
