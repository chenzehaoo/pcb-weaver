"""Offline snapshot tests; fixture bytes are not native Altium documents."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("altium_project_snapshot", ROOT / "scripts/altium_project_snapshot.py")
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def _project(names=None):
    names = names or [f"doc{i}.SchDoc" for i in range(1, 7)]
    return ("[Design]\nVersion=1.0\n" + "".join(
        f"[Document{i}]\nDocumentPath={name}\n" for i, name in enumerate(names, 1))).encode()


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    project = source / "test.PrjPcb"
    project.write_bytes(_project())
    for name in snapshot.document_paths(project.read_bytes()):
        (source / name).write_bytes(b"offline fixture " + name.encode())
    return project, tmp_path / "snapshots"


def test_snapshot_preserves_all_bytes_and_hashes_without_overwrite(inputs):
    project, output = inputs
    before = {path: path.read_bytes() for path in project.parent.iterdir()}
    first = snapshot.snapshot_project(project, output)
    second = snapshot.snapshot_project(project, output)
    assert first["status"] == second["status"] == "completed"
    assert first["snapshot_directory"] != second["snapshot_directory"]
    folder = Path(first["snapshot_directory"])
    assert uuid.UUID(folder.name).hex == folder.name
    assert first["file_count"] == len(first["files"]) == 7 and first["document_count"] == 6
    assert first["source_unchanged"] and not first["compile_verified"] and not first["native_drc_pass"]
    assert json.loads((folder / "manifest.json").read_bytes()) == first
    for row in first["files"]:
        assert Path(row["copy"]).read_bytes() == before[Path(row["source"])]
        assert row["source_sha256"] == row["source_reread_sha256"] == row["copy_sha256"]
    assert before == {path: path.read_bytes() for path in project.parent.iterdir()}


@pytest.mark.parametrize("name", ["C:/escape.SchDoc", "C:escape.SchDoc", "/escape", "\\\\host\\share\\doc",
    "../escape", "..\\escape", "sub/doc", "sub\\doc", ".", "..", "", "doc:stream", "NUL", "COM1.SchDoc",
    "bad?.SchDoc", "trailing.", "doc2.SchDoc", "DOC2.SCHDOC", "test.PrjPcb", "manifest.json"])
def test_unsafe_document_paths_fail_before_creation(inputs, name):
    project, output = inputs
    project.write_bytes(_project([name, *[f"doc{i}.SchDoc" for i in range(2, 7)]]))
    result = snapshot.snapshot_project(project, output)
    assert result["status"] == "failed" and result["errors"] and not result["source_unchanged"]
    assert not output.exists()


@pytest.mark.parametrize("suffix", ["[Document7]\nDocumentPath=extra\n", "[Document1]\nDocumentPath=duplicate\n",
    "[Other]\nDocumentPath=hidden\n", "[DEFAULT]\nDocumentPath=inherited\n"])
def test_ambiguous_project_sections_rejected(inputs, suffix):
    project, output = inputs
    project.write_bytes(project.read_bytes() + suffix.encode())
    assert snapshot.snapshot_project(project, output)["status"] == "failed"
    assert not output.exists()


@pytest.mark.parametrize("fault", ["missing", "empty", "missing_key", "duplicate_key"])
def test_missing_and_malformed_dependencies(inputs, fault):
    project, output = inputs
    doc = project.parent / "doc1.SchDoc"
    if fault == "missing":
        doc.unlink()
    elif fault == "empty":
        doc.write_bytes(b"")
    elif fault == "missing_key":
        project.write_bytes(project.read_bytes().replace(b"DocumentPath=doc1.SchDoc", b"Other=value"))
    else:
        project.write_bytes(project.read_bytes().replace(b"DocumentPath=doc1.SchDoc", b"DocumentPath=doc1.SchDoc\nDocumentPath=doc1.SchDoc"))
    assert snapshot.snapshot_project(project, output)["status"] == "failed"
    assert not output.exists()


@pytest.mark.parametrize("fault", ["source", "copy", "write"])
def test_changes_and_copy_failure_retain_failed_evidence(inputs, monkeypatch, fault):
    project, output = inputs
    real = snapshot._write_new
    def changed(path, raw):
        if path.name == "doc6.SchDoc" and fault == "write":
            raise OSError("injected copy failure")
        real(path, raw)
        if path.name == "doc6.SchDoc":
            target = project if fault == "source" else path.parent / project.name
            target.write_bytes(b"changed")
    monkeypatch.setattr(snapshot, "_write_new", changed)
    result = snapshot.snapshot_project(project, output)
    assert result["status"] == "failed" and not result["source_unchanged"]
    folder = Path(result["snapshot_directory"])
    assert json.loads((folder / "manifest.json").read_bytes())["status"] == "failed"
    assert (folder / project.name).exists()


def test_existing_uuid_directory_is_never_overwritten(inputs, monkeypatch):
    project, output = inputs
    identifier = uuid.uuid4()
    folder = output / identifier.hex
    folder.mkdir(parents=True)
    sentinel = folder / "manifest.json"
    sentinel.write_bytes(b"existing")
    monkeypatch.setattr(snapshot.uuid, "uuid4", lambda: identifier)
    assert snapshot.snapshot_project(project, output)["status"] == "failed"
    assert sentinel.read_bytes() == b"existing" and list(folder.iterdir()) == [sentinel]


def test_destination_inside_source_rejected(inputs):
    project, _ = inputs
    output = project.parent / "snapshots"
    assert snapshot.snapshot_project(project, output)["status"] == "failed"
    assert not output.exists()


@pytest.mark.parametrize("location", ["source", "output"])
def test_directory_links_rejected_without_following(inputs, tmp_path, location):
    project, output = inputs
    link = tmp_path / "linked"
    target = project.parent if location == "source" else tmp_path / "target"
    if location == "output":
        target.mkdir()
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        result = snapshot.snapshot_project(link / project.name if location == "source" else project,
                                           link / "snapshots" if location == "output" else output)
        assert result["status"] == "failed" and "reparse" in result["errors"][0]
        if location == "output":
            assert not list(target.iterdir())
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_real_project_has_exactly_six_local_document_paths():
    names = snapshot.document_paths(snapshot.SOURCE_PROJECT.read_bytes())
    assert names == ["Connector_WiFi.SchDoc", "WiFi.PcbDoc", "WiFi.PCBDwf", "WiFi_miniPCIe.BomDoc",
                     "WiFi_panel.PcbDoc", "WiFi_panel.PCBDwf"]
