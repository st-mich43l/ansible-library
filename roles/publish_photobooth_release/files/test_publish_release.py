"""Tests for publish_release.py. Temporary directories and fixture bytes only; nothing is published.

    python3 -m pytest roles/publish_photobooth_release/files -q
"""
import hashlib
import json
import concurrent.futures
import os
import shutil
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import publish_release as pr  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def make_upload(store, version, payload=b"MZ fixture executable", channel="stable", extra=None, name="up"):
    """Stage an upload exactly as the playbook does: staging/<unique>/{release.json, files/...}."""
    up = os.path.join(store.staging, f"{name}-{time.time_ns()}")
    files = os.path.join(up, "files")
    os.makedirs(files)
    exe = pr.exe_filename(version)
    with open(os.path.join(files, exe), "wb") as f:
        f.write(payload)
    sha = hashlib.sha256(payload).hexdigest()
    with open(os.path.join(files, "SHA256SUMS"), "w") as f:
        f.write(f"{sha}  {exe}\n")
    for n in ("RELEASE-NOTES.md", "THIRD-PARTY-NOTICES.txt"):
        with open(os.path.join(files, n), "w") as f:
            f.write(f"{n} for {version}\n")
    for n, content in (extra or {}).items():
        with open(os.path.join(files, n), "w") as f:
            f.write(content)
    rel = f"releases/v{version}"
    meta = {
        "version": version, "tag": f"v{version}", "name": "ApexVoid Photobooth", "channel": channel, "commit": COMMIT,
        "notesPath": f"{rel}/RELEASE-NOTES.md", "checksumsPath": f"{rel}/SHA256SUMS", "noticesPath": f"{rel}/THIRD-PARTY-NOTICES.txt",
        "assets": [{"platform": "windows", "architecture": "x64", "format": "exe", "filename": exe, "path": f"{rel}/{exe}",
                    "size": len(payload), "sha256": sha, "signed": False, "includesCanonSdk": False}],
    }
    with open(os.path.join(up, "release.json"), "w") as f:
        json.dump(meta, f)
    return up, sha


@pytest.fixture
def store(tmp_path):
    s = pr.Store(str(tmp_path / "apexvoid-photobooth"))
    assert s.init() is True
    return s


def manifest(store):
    with open(store.manifest_path) as f:
        return json.load(f)


# ---------------------------------------------------------------- initialisation
def test_empty_repository_initialisation_is_valid_and_never_overwritten(store):
    m = manifest(store)
    assert m["schemaVersion"] == 1 and m["latest"] == {"stable": None} and m["releases"] == []
    assert pr.validate_manifest(m) == []
    assert oct(os.stat(store.staging).st_mode & 0o777) == "0o700"
    assert oct(os.stat(store.state).st_mode & 0o777) == "0o700"
    assert oct(os.stat(store.public).st_mode & 0o777) == "0o755"
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    assert store.init() is False  # routine provisioning
    assert manifest(store)["latest"]["stable"] == "0.2.0"


# ---------------------------------------------------------------- publish paths
def test_valid_publication(store):
    up, sha = make_upload(store, "0.2.0")
    out = pr.publish(store, up, sha, now="2026-11-25T13:00:00Z")
    assert out["status"] == "published" and out["latest"] == "0.2.0"
    m = manifest(store)
    assert pr.validate_manifest(m) == []
    r = m["releases"][0]
    assert r["publishedAt"] == "2026-11-25T13:00:00Z" and r["assets"][0]["sha256"] == sha
    exe = os.path.join(store.public, r["assets"][0]["path"])
    assert os.path.isfile(exe) and pr.sha256_file(exe) == sha
    assert oct(os.stat(exe).st_mode & 0o777) == "0o644"
    assert not os.path.exists(up), "the staging upload is cleaned"
    assert not os.path.exists(store.journal_path)


def test_identical_retry_is_a_successful_no_op(store):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    before = manifest(store)
    up2, _ = make_upload(store, "0.2.0")
    out = pr.publish(store, up2, sha)
    assert out["status"] == "unchanged"
    assert manifest(store) == before


def test_version_conflict_is_rejected_and_nothing_changes(store):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    before = manifest(store)
    up2, sha2 = make_upload(store, "0.2.0", payload=b"MZ different build")
    with pytest.raises(pr.PublishError) as e:
        pr.publish(store, up2, sha2)
    assert e.value.code == 3 and "already published with a different executable" in str(e.value)
    assert manifest(store) == before
    exe = os.path.join(store.public, before["releases"][0]["assets"][0]["path"])
    assert pr.sha256_file(exe) == sha, "a published version is never overwritten"


def test_checksum_must_match_the_trusted_ci_checksum(store):
    up, sha = make_upload(store, "0.2.0")
    with pytest.raises(pr.PublishError) as e:
        pr.publish(store, up, "f" * 64)
    assert e.value.code == 4
    assert manifest(store)["releases"] == []


def test_corrupted_upload_fails_integrity_validation(store):
    up, sha = make_upload(store, "0.2.0")
    with open(os.path.join(up, "files", pr.exe_filename("0.2.0")), "ab") as f:
        f.write(b"tampered")
    with pytest.raises(pr.PublishError) as e:
        pr.publish(store, up, sha)
    assert e.value.code == 4
    assert manifest(store)["releases"] == []
    assert not os.path.exists(os.path.join(store.releases, "v0.2.0"))


def test_interrupted_upload_missing_file_is_refused(store):
    up, sha = make_upload(store, "0.2.0")
    os.unlink(os.path.join(up, "files", "THIRD-PARTY-NOTICES.txt"))
    with pytest.raises(pr.PublishError) as e:
        pr.publish(store, up, sha)
    assert e.value.code == 2 and "missing" in str(e.value)
    assert manifest(store)["releases"] == []


def test_unexpected_files_and_symlinks_are_refused(store):
    up, sha = make_upload(store, "0.2.0", extra={"debug.log": "secret"})
    with pytest.raises(pr.PublishError):
        pr.publish(store, up, sha)
    up, sha = make_upload(store, "0.2.0")
    os.unlink(os.path.join(up, "files", "RELEASE-NOTES.md"))
    os.symlink("/etc/passwd", os.path.join(up, "files", "RELEASE-NOTES.md"))
    with pytest.raises(pr.PublishError):
        pr.publish(store, up, sha)
    assert manifest(store)["releases"] == []


def test_upload_outside_staging_is_refused(store, tmp_path):
    up, sha = make_upload(store, "0.2.0")
    outside = tmp_path / "elsewhere"
    shutil.move(up, outside)
    with pytest.raises(pr.PublishError) as e:
        pr.publish(store, str(outside), sha)
    assert e.value.code == 2


def test_out_of_order_publication_never_demotes_a_newer_stable(store):
    up, sha = make_upload(store, "0.3.0", payload=b"MZ 0.3.0")
    pr.publish(store, up, sha)
    up, sha = make_upload(store, "0.2.1", payload=b"MZ 0.2.1")
    with pytest.raises(pr.PublishError, match="out-of-order"):
        pr.publish(store, up, sha)
    m = manifest(store)
    assert [r["version"] for r in m["releases"]] == ["0.3.0"]
    assert m["latest"]["stable"] == "0.3.0"


def test_rollback_pointer_does_not_allow_a_stale_job_to_displace_newer_archive(store):
    for version in ("0.2.0", "0.2.1", "0.3.0"):
        up, sha = make_upload(store, version, payload=version.encode())
        pr.publish(store, up, sha)
    pr.set_latest(store, "0.2.1")
    up, sha = make_upload(store, "0.2.2", payload=b"stale build")
    with pytest.raises(pr.PublishError, match="highest published stable v0.3.0"):
        pr.publish(store, up, sha)
    assert manifest(store)["latest"]["stable"] == "0.2.1"


def test_preview_release_is_rejected_from_public_catalog(store):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    up, sha = make_upload(store, "0.3.0-rc.1", payload=b"MZ rc", channel="preview")
    with pytest.raises(pr.PublishError, match="preview releases"):
        pr.publish(store, up, sha)
    assert [r["version"] for r in manifest(store)["releases"]] == ["0.2.0"]


def test_insufficient_disk_space_is_refused_before_anything_changes(store):
    up, sha = make_upload(store, "0.2.0")
    with pytest.raises(pr.PublishError) as e:
        pr.publish(store, up, sha, reserve_bytes=1 << 62)
    assert e.value.code == 4 and "reserve" in str(e.value)
    assert manifest(store)["releases"] == []


def test_interrupted_between_promotion_and_manifest_update_recovers_on_retry(store, monkeypatch):
    up, sha = make_upload(store, "0.2.0")
    real = store.write_manifest

    def crash(_m):
        raise KeyboardInterrupt("power loss")

    monkeypatch.setattr(store, "write_manifest", crash)
    with pytest.raises(KeyboardInterrupt):
        pr.publish(store, up, sha)
    # The publisher restores the exact previous manifest and removes only its orphan promotion.
    assert manifest(store)["releases"] == []
    assert not os.path.exists(os.path.join(store.releases, "v0.2.0"))
    assert not os.path.exists(store.journal_path)
    monkeypatch.setattr(store, "write_manifest", real)
    up2, _ = make_upload(store, "0.2.0")
    out = pr.publish(store, up2, sha)
    assert out["status"] == "published"
    assert manifest(store)["latest"]["stable"] == "0.2.0"
    assert not os.path.exists(store.journal_path)


def test_interrupted_manifest_write_leaves_the_previous_manifest_intact(store, monkeypatch):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    before = manifest(store)

    def crash_replace(src, dst):
        raise OSError("disk went away during rename")

    up2, sha2 = make_upload(store, "0.3.0", payload=b"MZ 0.3.0")
    monkeypatch.setattr(pr.os, "replace", crash_replace)
    with pytest.raises(OSError):
        pr.publish(store, up2, sha2)
    monkeypatch.undo()
    assert manifest(store) == before
    assert not [f for f in os.listdir(store.public) if f.startswith(".tmp-")], "no temporary manifest left visible"


def test_a_leftover_directory_with_different_contents_is_never_published(store):
    os.makedirs(os.path.join(store.releases, "v0.2.0"))
    with open(os.path.join(store.releases, "v0.2.0", pr.exe_filename("0.2.0")), "wb") as f:
        f.write(b"something else")
    up, sha = make_upload(store, "0.2.0")
    with pytest.raises(pr.PublishError) as e:
        pr.publish(store, up, sha)
    assert e.value.code == 3
    assert manifest(store)["releases"] == []


def _publish_in_thread(root, up, sha):
    try:
        return pr.publish(pr.Store(root), up, sha)["status"]
    except pr.PublishError as e:
        return f"error:{e.code}"
    except BaseException as e:  # noqa: BLE001 - report, never hang the parent
        return f"crash:{type(e).__name__}"


def test_concurrent_publications_are_serialised(store):
    ups = [make_upload(store, "0.2.0", name=f"c{i}") for i in range(4)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_publish_in_thread, store.root, up, sha) for up, sha in ups]
        results = [f.result(timeout=30) for f in futures]
    results.sort()
    assert results == ["published", "unchanged", "unchanged", "unchanged"]
    m = manifest(store)
    assert len(m["releases"]) == 1 and pr.validate_manifest(m) == []


def test_previous_releases_survive_and_rollback_to_a_previous_stable(store):
    for v in ("0.2.0", "0.2.1", "0.3.0"):
        up, sha = make_upload(store, v, payload=f"MZ {v}".encode())
        pr.publish(store, up, sha)
    assert manifest(store)["latest"]["stable"] == "0.3.0"
    out = pr.set_latest(store, "0.2.1")
    assert out["status"] == "latest-changed" and out["from"] == "0.3.0" and out["to"] == "0.2.1"
    m = manifest(store)
    assert m["latest"]["stable"] == "0.2.1" and len(m["releases"]) == 3
    for r in m["releases"]:
        assert os.path.isfile(os.path.join(store.public, r["assets"][0]["path"]))


def test_restore_previous_manifest_pointer_keeps_current_archived_versions(store):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    up, sha = make_upload(store, "0.3.0", payload=b"MZ 0.3.0")
    pr.publish(store, up, sha)
    backup = sorted(f for f in os.listdir(store.backups) if f.startswith("index-") and f.endswith(".json"))[-1]
    out = pr.restore(store, backup)
    assert out["status"] == "restored" and out["latest"] == "0.2.0"
    assert [r["version"] for r in manifest(store)["releases"]] == ["0.3.0", "0.2.0"]


def test_withdraw_removes_from_manifest_and_public_storage_without_deleting(store):
    for v in ("0.2.0", "0.3.0"):
        up, sha = make_upload(store, v, payload=f"MZ {v}".encode())
        pr.publish(store, up, sha)
    pr.set_latest(store, "0.2.0")
    out = pr.withdraw(store, "0.3.0", remove_files=True)
    assert out["latest"] == "0.2.0"
    assert not os.path.exists(os.path.join(store.releases, "v0.3.0"))
    assert os.path.isdir(out["filesMovedTo"]) and out["filesMovedTo"].startswith(store.withdrawn)
    assert [r["version"] for r in manifest(store)["releases"]] == ["0.2.0"]


def test_sixth_release_expires_oldest_and_preserves_five(store):
    for patch in range(2, 8):
        version = f"0.2.{patch}"
        up, sha = make_upload(store, version, payload=version.encode())
        result = pr.publish(store, up, sha)
    assert [r["version"] for r in manifest(store)["releases"]] == ["0.2.7", "0.2.6", "0.2.5", "0.2.4", "0.2.3"]
    assert result["retention"]["expired"] == ["0.2.2"]
    assert not os.path.exists(os.path.join(store.releases, "v0.2.2"))
    assert store.load_ledger()["expired"]["0.2.2"]["status"] == "expired"


@pytest.mark.parametrize(("count", "archive_count"), [(0, 0), (1, 0), (4, 3), (5, 4)])
def test_retention_plan_boundary_counts(store, count, archive_count):
    for patch in range(1, count + 1):
        version = f"0.2.{patch}"
        up, sha = make_upload(store, version, payload=version.encode())
        pr.publish(store, up, sha)
    plan = pr.plan_retention(manifest(store), archive_keep=4)
    assert len(plan["retained"]) == count
    assert len(plan["archives"]) == archive_count


def test_symlinked_expiration_is_reported_and_never_removed(store, tmp_path):
    for patch in range(1, 7):
        version = f"0.2.{patch}"
        up, sha = make_upload(store, version, payload=version.encode())
        pr.publish(store, up, sha, retention_enabled=False)
    target = os.path.join(store.releases, "v0.2.1")
    shutil.rmtree(target)
    outside = tmp_path / "outside-release"
    outside.mkdir()
    (outside / "important.txt").write_text("preserve")
    os.symlink(outside, target)
    before = manifest(store)
    with pytest.raises(pr.PublishError, match="integrity checks failed"):
        pr.prune(store, apply=True)
    assert manifest(store) == before
    assert os.path.islink(target) and (outside / "important.txt").read_text() == "preserve"


def test_ten_release_migration_dry_run_is_read_only_then_prunes_only_expired(store):
    for minor in range(1, 11):
        version = f"0.{minor}.0"
        up, sha = make_upload(store, version, payload=version.encode())
        pr.publish(store, up, sha, retention_enabled=False)
    before = manifest(store)
    dry = pr.prune(store, apply=False)
    assert dry["status"] == "dry-run" and dry["existingReleaseCount"] == 10
    assert dry["retained"] == ["0.10.0", "0.9.0", "0.8.0", "0.7.0", "0.6.0"]
    assert dry["remove"] == ["0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0"]
    assert manifest(store) == before and all(os.path.isdir(os.path.join(store.releases, f"v0.{n}.0")) for n in range(1, 11))
    applied = pr.prune(store, apply=True)
    assert applied["status"] == "pruned" and len(manifest(store)["releases"]) == 5
    assert not any(os.path.exists(os.path.join(store.releases, f"v0.{n}.0")) for n in range(1, 6))
    assert all(os.path.isdir(os.path.join(store.releases, f"v0.{n}.0")) for n in range(6, 11))


def test_expired_release_cannot_be_republished_even_with_different_contents(store):
    for patch in range(2, 8):
        version = f"0.2.{patch}"
        up, sha = make_upload(store, version, payload=version.encode())
        pr.publish(store, up, sha)
    up, sha = make_upload(store, "0.2.2", payload=b"different expired executable")
    with pytest.raises(pr.PublishError, match="expired and cannot be republished"):
        pr.publish(store, up, sha)


def test_failed_public_verification_restores_exact_prior_manifest(store, monkeypatch):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    before = manifest(store)
    up, sha = make_upload(store, "0.3.0", payload=b"new candidate")

    def fail_verify(*_args, **_kwargs):
        raise pr.PublishError(4, "simulated public mismatch")

    monkeypatch.setattr(pr, "verify_public_release", fail_verify)
    with pytest.raises(pr.PublishError, match="simulated public mismatch"):
        pr.publish(store, up, sha, verify_origin="https://example.invalid")
    assert manifest(store) == before
    assert os.path.isdir(os.path.join(store.releases, "v0.2.0"))
    assert not os.path.exists(os.path.join(store.releases, "v0.3.0"))
    assert not os.path.exists(store.journal_path)


def test_interrupted_cleanup_is_pending_and_retried_safely(store, monkeypatch):
    for patch in range(2, 8):
        version = f"0.2.{patch}"
        up, sha = make_upload(store, version, payload=version.encode())
        pr.publish(store, up, sha)
    current = manifest(store)
    assert current["latest"]["stable"] == "0.2.7"
    # Build a legacy sixth release so prune has a committed cleanup operation.
    up, sha = make_upload(store, "0.2.8", payload=b"0.2.8")
    pr.publish(store, up, sha)
    assert not os.path.exists(store.journal_path)
    # The next cleanup failure must retain a durable transaction and report pending.
    original = pr.safe_delete_expired
    def denied(*_args, **_kwargs):
        raise OSError("simulated permission failure")
    # Seed an over-limit state using the supported non-pruning publisher mode.
    for patch in range(9, 13):
        version = f"0.2.{patch}"
        up, sha = make_upload(store, version, payload=version.encode())
        pr.publish(store, up, sha, retention_enabled=False)
    monkeypatch.setattr(pr, "safe_delete_expired", denied)
    with pytest.raises(pr.PublishError, match="cleanup pending"):
        pr.prune(store, apply=True)
    assert os.path.exists(store.journal_path)
    assert pr.status(store)["pendingCleanup"] is True
    monkeypatch.setattr(pr, "safe_delete_expired", original)
    with store.lock():
        pr.recover(store)
    assert not os.path.exists(store.journal_path)
    assert len(manifest(store)["releases"]) == 5


# ---------------------------------------------------------------- manifest contract
def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def test_shared_fixtures_valid():
    for name in ("manifest-empty.json", "manifest-valid.json"):
        assert pr.validate_manifest(load_fixture(name)) == [], name


@pytest.mark.parametrize("name", sorted(f for f in os.listdir(FIXTURES) if f.startswith("manifest-invalid-")))
def test_shared_fixtures_invalid(name):
    assert pr.validate_manifest(load_fixture(name)), f"{name} must be rejected"


def test_path_traversal_and_external_urls_are_rejected():
    base = load_fixture("manifest-valid.json")
    for bad in ("../../etc/passwd", "/releases/v0.2.0/x.exe", "https://evil.example/x.exe", "//evil.example/x.exe",
                "releases/v0.2.0/../../state/lock", "releases/v0.1.0/ApexVoidPhotobooth-v0.2.0-windows-x64.exe", "releases\\v0.2.0\\x.exe"):
        m = json.loads(json.dumps(base))
        m["releases"][0]["assets"][0]["path"] = bad
        assert pr.validate_manifest(m), bad


def test_semver_ordering():
    vs = ["0.2.0", "0.10.0", "0.2.0-rc.1", "0.2.0-rc.10", "0.2.0-rc.2", "1.0.0", "0.2.1"]
    assert sorted(vs, key=pr.parse_semver) == ["0.2.0-rc.1", "0.2.0-rc.2", "0.2.0-rc.10", "0.2.0", "0.2.1", "0.10.0", "1.0.0"]
    for bad in ("v0.2.0", "0.2", "01.2.3", "0.2.0+build", "latest"):
        with pytest.raises(pr.PublishError):
            pr.parse_semver(bad)


def test_cli_validate_and_exit_codes(tmp_path, capsys):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(load_fixture("manifest-valid.json")))
    assert pr.main(["--root", str(tmp_path / "r"), "validate", str(p)]) == 0
    p.write_text(json.dumps(load_fixture("manifest-invalid-duplicate.json")))
    assert pr.main(["--root", str(tmp_path / "r"), "validate", str(p)]) == 5
    assert pr.main(["--root", str(tmp_path / "r"), "init"]) == 0
    assert pr.main(["--root", str(tmp_path / "r"), "set-latest", "9.9.9"]) == 2
