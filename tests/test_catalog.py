import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from pcb_weaver import catalog
from pcb_weaver.storage import Store, digest, write_json


@pytest.fixture
def engine(tmp_path):
    store = Store(tmp_path / "workspace")
    def verified(project, revision):
        folder = store.revision_dir(project, revision)
        folder.mkdir(parents=True, exist_ok=True)
        return {"digest": "digest-" + revision}, folder
    return SimpleNamespace(store=store, _verified=verified)


def record(engine, revision="r-a", run="v-one", created="2026-09-07T00:00:00Z", **changes):
    data, folder = engine._verified("demo", revision)
    directory = folder / "verification" / run
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / "drc.json"
    artifact.write_text('{"errors":0}', encoding="utf-8")
    result = {"verification_id": run, "revision": revision, "revision_digest": data["digest"],
              "status": "passed", "created": created, "evidence_hashes": {"drc.json": digest(artifact)}, **changes}
    write_json(directory / "result.json", result)
    engine.store.event("demo", "verification_completed", {"verification_id": run, "revision": revision,
                       "status": result["status"], "sha256": digest(directory / "result.json")})
    return directory


def test_recorded_verification_is_bound_to_revision(engine):
    original = record(engine)
    assert catalog.verification(engine, "demo", "r-a")["status"] == "passed"
    _, target = engine._verified("demo", "r-b")
    shutil.copytree(original, target / "verification" / original.name)
    assert catalog.verification(engine, "demo", "r-b")["status"] == "invalid_evidence"


def test_verification_digest_and_run_directory_are_bound(engine):
    path = record(engine, revision_digest="another-digest")
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"
    path = record(engine)
    path.rename(path.with_name("v-copied"))
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"


@pytest.mark.parametrize("name", ["../secret", "../../outside", "/outside", "C:/outside", "C:outside", "\\\\server\\share", "file:stream"])
def test_even_recorded_external_evidence_paths_are_rejected(engine, name):
    record(engine, evidence_hashes={name: "fake-sha"})
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"


def test_missing_or_corrupted_latest_result_cannot_fall_back_to_pass(engine):
    record(engine)
    latest = record(engine, run="v-two", created="2026-09-07T01:00:00Z", status="blocked")
    (latest / "result.json").unlink()
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"
    (latest / "result.json").write_text("not json", encoding="utf-8")
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"


def test_modified_evidence_bytes_are_rejected(engine):
    directory = record(engine)
    (directory / "drc.json").write_text('{"errors":20}', encoding="utf-8")
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"


def test_ledger_order_wins_over_file_mtime(engine):
    record(engine)
    record(engine, run="v-two", created="2026-09-07T01:00:00Z", status="blocked")
    assert catalog.verification(engine, "demo", "r-a")["status"] == "blocked"


def test_invalid_chronology_cannot_make_report_choose_old_pass(engine):
    record(engine)
    record(engine, run="v-two", created="2026-09-06T01:00:00Z", status="blocked")
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"


def test_unrecorded_result_does_not_count_as_verification(engine):
    _, folder = engine._verified("demo", "r-a")
    write_json(folder / "verification" / "v-forged" / "result.json", {"status": "passed"})
    assert catalog.verification(engine, "demo", "r-a")["status"] == "invalid_evidence"


def test_renamed_plan_cannot_masquerade_as_recorded_plan(engine):
    data, folder = engine._verified("demo", "r-a")
    path = folder / "plans" / "p-renamed.json"
    write_json(path, {"plan_id": "p-original", "revision_digest": data["digest"]})
    engine.store.event("demo", "layout_planned", {"revision": "r-a", "plan_id": "p-original", "sha256": digest(path)})
    with pytest.raises(ValueError, match="Plan evidence"):
        catalog.plans(engine, "demo", "r-a")


def test_artifact_path_rejects_traversal(engine):
    with pytest.raises(ValueError, match="escapes"):
        catalog.artifact_path(engine.store.root, engine.store.root / ".." / "secret")


def test_artifact_path_rejects_internal_and_external_directory_aliases(engine, tmp_path):
    # Windows directory junctions do not require symlink privileges.
    import os
    import subprocess
    for index, target in enumerate([engine.store.root / "real", tmp_path / "outside"]):
        target.mkdir()
        link = engine.store.root / f"alias-{index}"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
        try:
            with pytest.raises(ValueError, match="link or junction"):
                catalog.artifact_path(engine.store.root, link / "result.json")
        finally:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()


def test_download_returns_the_same_bytes_that_were_verified(engine, monkeypatch):
    path = engine.store.project_dir("demo") / "releases" / "release-one.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"recorded")
    engine.store.event("demo", "release_created", {"release_id": "release-one", "sha256": digest(path)})
    original = Path.read_bytes
    def replace_after_read(self):
        raw = original(self)
        if self == path:
            self.write_bytes(b"replacement")
        return raw
    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    assert catalog.archive_bytes(engine, "demo", "release-one") == b"recorded"
