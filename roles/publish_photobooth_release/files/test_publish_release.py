"""Tests for publish_release.py. Temporary directories and fixture bytes only; nothing is published.

    python3 -m pytest roles/publish_photobooth_release/files -q
"""
import hashlib
import json
import multiprocessing
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
    out = pr.publish(store, up, sha)
    assert out["status"] == "published" and out["latest"] == "0.3.0"
    m = manifest(store)
    assert [r["version"] for r in m["releases"]] == ["0.3.0", "0.2.1"]
    assert m["latest"]["stable"] == "0.3.0"


def test_preview_release_is_listed_but_never_becomes_the_stable_download(store):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    up, sha = make_upload(store, "0.3.0-rc.1", payload=b"MZ rc", channel="preview")
    pr.publish(store, up, sha)
    m = manifest(store)
    assert m["latest"]["stable"] == "0.2.0"
    assert [r["version"] for r in m["releases"]] == ["0.3.0-rc.1", "0.2.0"]


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
    # the manifest never referenced the release; the promoted directory and the journal remain
    assert manifest(store)["releases"] == []
    assert os.path.isdir(os.path.join(store.releases, "v0.2.0"))
    assert os.path.exists(store.journal_path)
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


def _publish_in_process(root, up, sha, q):
    try:
        q.put(pr.publish(pr.Store(root), up, sha)["status"])
    except pr.PublishError as e:
        q.put(f"error:{e.code}")
    except BaseException as e:  # noqa: BLE001 - report, never hang the parent
        q.put(f"crash:{type(e).__name__}")


def test_concurrent_publications_are_serialised(store):
    ups = [make_upload(store, "0.2.0", name=f"c{i}") for i in range(4)]
    q = multiprocessing.Queue()
    procs = [multiprocessing.Process(target=_publish_in_process, args=(store.root, up, sha, q)) for up, sha in ups]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        if p.is_alive():
            p.kill()
    results = []
    for _ in procs:  # a crashed worker must fail the test, not hang it
        try:
            results.append(q.get(timeout=20))
        except Exception:
            results.append("crashed")
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
    assert out == {"status": "latest-changed", "from": "0.3.0", "to": "0.2.1"}
    m = manifest(store)
    assert m["latest"]["stable"] == "0.2.1" and len(m["releases"]) == 3
    for r in m["releases"]:
        assert os.path.isfile(os.path.join(store.public, r["assets"][0]["path"]))


def test_restore_previous_manifest(store):
    up, sha = make_upload(store, "0.2.0")
    pr.publish(store, up, sha)
    up, sha = make_upload(store, "0.3.0", payload=b"MZ 0.3.0")
    pr.publish(store, up, sha)
    out = pr.restore(store, None)
    assert out["status"] == "restored" and out["latest"] == "0.2.0"
    assert [r["version"] for r in manifest(store)["releases"]] == ["0.2.0"]


def test_withdraw_removes_from_manifest_and_public_storage_without_deleting(store):
    for v in ("0.2.0", "0.3.0"):
        up, sha = make_upload(store, v, payload=f"MZ {v}".encode())
        pr.publish(store, up, sha)
    out = pr.withdraw(store, "0.3.0", remove_files=True)
    assert out["latest"] == "0.2.0"
    assert not os.path.exists(os.path.join(store.releases, "v0.3.0"))
    assert os.path.isdir(out["filesMovedTo"]) and out["filesMovedTo"].startswith(store.withdrawn)
    assert [r["version"] for r in manifest(store)["releases"]] == ["0.2.0"]


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
