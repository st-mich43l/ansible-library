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

    def init(self) -> bool:
        for d, mode in ((self.root, 0o755), (self.public, 0o755), (self.releases, 0o755), (self.staging, 0o700),
                        (self.state, 0o700), (self.backups, 0o700), (self.withdrawn, 0o700)):
            os.makedirs(d, exist_ok=True)
            os.chmod(d, mode)
        if os.path.exists(self.manifest_path):
            return False
        atomic_write_json(self.manifest_path, empty_manifest())
        return True

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
        atomic_write_json(self.manifest_path, m)

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


def publish(store: Store, staging_dir: str, expected_sha: str, now: str | None = None,
            reserve_bytes: int = 1 << 30) -> dict:
    staging_dir = os.path.abspath(staging_dir)
    if os.path.commonpath([staging_dir, store.staging]) != store.staging or staging_dir == store.staging:
        fail(2, "the upload must be a directory inside the staging area")
    if not SHA256.match(expected_sha or ""):
        fail(2, "the trusted CI checksum must be 64 lower-case hex characters")
    meta_path = os.path.join(staging_dir, "release.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            release = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(2, f"release metadata unreadable: {e}")
    errs = validate_release(release, require_published=False)
    if errs:
        fail(2, "invalid release metadata: " + "; ".join(errs))
    if "publishedAt" in release:
        fail(2, "publishedAt is set by the publisher, not by the build")
    v = release["version"]
    asset = release["assets"][0]
    if len(release["assets"]) != 1:
        fail(2, "exactly one Windows x64 executable per release is supported")
    if asset["sha256"] != expected_sha:
        fail(4, "the release metadata checksum differs from the trusted CI checksum")
    files_dir = os.path.join(staging_dir, "files")
    check_release_dir(files_dir, release)
    if disk_free_bytes(store.root) < reserve_bytes:
        fail(4, f"less than the configured reserve of {reserve_bytes // (1 << 20)} MB is free")

    with store.lock():
        recover(store)
        manifest = store.load_manifest()
        target = os.path.join(store.releases, release_dir_name(v))
        existing = next((r for r in manifest["releases"] if r["version"] == v), None)
        if existing is not None:
            if existing["assets"][0]["sha256"] == expected_sha:
                shutil.rmtree(staging_dir, ignore_errors=True)
                return {"status": "unchanged", "version": v, "message": f"{v} is already published with this executable"}
            fail(3, f"version {v} is already published with a different executable (sha256 {existing['assets'][0]['sha256']}); "
                    "publish a new version instead")
        if os.path.exists(target):
            # left over from an interrupted run: reuse only if it is byte-identical
            try:
                check_release_dir(target, release)
            except PublishError:
                fail(3, f"{target} exists with different contents; it was not published; investigate before retrying")
            shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            os.chmod(files_dir, 0o755)
            for n in os.listdir(files_dir):
                os.chmod(os.path.join(files_dir, n), 0o644)
            atomic_write_json(store.journal_path, {"version": v, "step": "promoting", "sha256": expected_sha}, mode=0o600)
            os.rename(files_dir, target)  # same filesystem: atomic
            fsync_dir(store.releases)
            shutil.rmtree(staging_dir, ignore_errors=True)
        check_release_dir(target, release)

        record = dict(release)
        record["publishedAt"] = now or utc_now()
        new = json.loads(json.dumps(manifest))
        new["releases"].append(record)
        new["releases"].sort(key=lambda r: parse_semver(r["version"]), reverse=True)
        current = new["latest"]["stable"]
        if record["channel"] == "stable" and (current is None or parse_semver(v) > parse_semver(current)):
            new["latest"]["stable"] = v  # an older version never replaces a newer stable release
        new["updatedAt"] = record["publishedAt"]
        atomic_write_json(store.journal_path, {"version": v, "step": "manifest", "sha256": expected_sha}, mode=0o600)
        store.write_manifest(new)  # the final publication step
        os.unlink(store.journal_path)
        return {"status": "published", "version": v, "latest": new["latest"]["stable"], "path": f"releases/{release_dir_name(v)}/{asset['filename']}",
                "size": asset["size"], "sha256": expected_sha}


def recover(store: Store) -> None:
    """Called under the lock. A journal means a previous run stopped between promotion and the manifest
    update. The manifest was never changed in that window, so it still points only at complete releases;
    the promoted directory stays (byte-identical retries reuse it) and the journal is cleared."""
    if os.path.exists(store.journal_path):
        os.unlink(store.journal_path)


# ------------------------------------------------------------------ operator commands
def set_latest(store: Store, version: str) -> dict:
    parse_semver(version)
    with store.lock():
        m = store.load_manifest()
        r = next((x for x in m["releases"] if x["version"] == version), None)
        if r is None or r["channel"] != "stable":
            fail(2, f"{version} is not a published stable release")
        previous = m["latest"]["stable"]
        m["latest"]["stable"] = version
        m["updatedAt"] = utc_now()
        store.write_manifest(m)
        return {"status": "latest-changed", "from": previous, "to": version}


def withdraw(store: Store, version: str, remove_files: bool) -> dict:
    parse_semver(version)
    with store.lock():
        m = store.load_manifest()
        if not any(r["version"] == version for r in m["releases"]):
            fail(2, f"{version} is not in the manifest")
        m["releases"] = [r for r in m["releases"] if r["version"] != version]
        if m["latest"]["stable"] == version:
            stable = [r["version"] for r in m["releases"] if r["channel"] == "stable"]
            m["latest"]["stable"] = max(stable, key=parse_semver) if stable else None
        m["updatedAt"] = utc_now()
        store.write_manifest(m)
        moved = None
        if remove_files:
            src = os.path.join(store.releases, release_dir_name(version))
            if os.path.isdir(src):
                moved = os.path.join(store.withdrawn, f"{release_dir_name(version)}-{time.strftime('%Y%m%dT%H%M%S')}")
                os.rename(src, moved)
        return {"status": "withdrawn", "version": version, "latest": m["latest"]["stable"], "filesMovedTo": moved}


def restore(store: Store, backup: str | None) -> dict:
    with store.lock():
        backups = sorted(f for f in os.listdir(store.backups) if f.startswith("index-") and f.endswith(".json"))
        if not backups:
            fail(5, "no previous manifest to restore")
        name = backup or backups[-1]
        if name not in backups:
            fail(2, f"unknown backup {name}; available: {backups[-5:]}")
        with open(os.path.join(store.backups, name), encoding="utf-8") as f:
            m = json.load(f)
        errs = validate_manifest(m)
        if errs:
            fail(5, "backup is invalid: " + "; ".join(errs))
        for r in m["releases"]:
            if not os.path.isfile(os.path.join(store.public, r["assets"][0]["path"])):
                fail(5, f"backup references {r['version']}, whose files are not in public storage")
        store.write_manifest(m)
        return {"status": "restored", "from": name, "latest": m["latest"]["stable"]}


def status(store: Store) -> dict:
    m = store.load_manifest()
    return {"latest": m["latest"]["stable"], "releases": [r["version"] for r in m["releases"]],
            "publicBytes": dir_size(store.public), "stagingBytes": dir_size(store.staging), "freeBytes": disk_free_bytes(store.root),
            "pendingJournal": os.path.exists(store.journal_path)}


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
    p = sub.add_parser("set-latest")
    p.add_argument("version")
    p = sub.add_parser("withdraw")
    p.add_argument("version")
    p.add_argument("--remove-files", action="store_true")
    p = sub.add_parser("restore")
    p.add_argument("--backup")
    sub.add_parser("status")
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
            out = publish(store, a.staging_dir, a.expected_sha256, reserve_bytes=a.reserve_mb << 20)
        elif a.cmd == "set-latest":
            out = set_latest(store, a.version)
        elif a.cmd == "withdraw":
            out = withdraw(store, a.version, a.remove_files)
        elif a.cmd == "restore":
            out = restore(store, a.backup)
        elif a.cmd == "status":
            out = status(store)
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
