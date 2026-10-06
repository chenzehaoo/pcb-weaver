"""Bounded Windows path creation races and containment regression checks."""

import json
import os
from pathlib import Path
import threading
import subprocess
import time

import pytest

from pcb_weaver.storage import Store


def race_first_missing_parent(monkeypatch, target, create_parent, operation):
    import ntpath

    original = ntpath._getfinalpathname
    entered, created = threading.Event(), threading.Event()
    errors, winerrors = [], []
    fired = False

    def worker():
        try:
            if not entered.wait(5):
                raise TimeoutError("Native missing-parent call was not reached")
            create_parent()
        except BaseException as error:
            errors.append(error)
        finally:
            created.set()

    def native_with_barrier(path):
        nonlocal fired
        try:
            return original(path)
        except OSError as error:
            if ntpath.normcase(str(path)) == ntpath.normcase(str(target)):
                winerrors.append(error.winerror)
                if not fired:
                    fired = True
                    entered.set()
                    if not created.wait(5):
                        raise TimeoutError("Parent creation did not finish")
            raise

    thread = threading.Thread(target=worker, daemon=True)
    monkeypatch.setattr(ntpath, "_getfinalpathname", native_with_barrier)
    thread.start()
    try:
        result = operation()
    finally:
        entered.set()
        thread.join(timeout=6)
        assert not thread.is_alive(), "Bounded path-race worker failed to stop"
    assert not errors, errors
    return result, winerrors


@pytest.mark.skipif(os.name != "nt", reason="Exercises the native Windows resolver")
def test_native_nonstrict_resolve_parent_creation_prefix_race(tmp_path, monkeypatch):
    root = tmp_path.resolve(strict=True)
    target = root / "projects/survives"
    resolved, winerrors = race_first_missing_parent(
        monkeypatch, target, lambda: (root / "projects/platform-worker").mkdir(parents=True), target.resolve)
    print(json.dumps({"requested": str(target), "resolved": str(resolved),
                      "winerrors": winerrors, "relative_to_root": resolved.is_relative_to(root)}))
    # Newer Python versions may normalize both spellings; they must still identify
    # the same missing child. The affected runtime exhibits errors 3 then 2.
    assert str(resolved).removeprefix("\\\\?\\").casefold() == str(target).casefold()
    assert target.parent.is_dir() and not target.exists()
    if not resolved.is_relative_to(root):
        assert winerrors[0] == 3 and winerrors[-1] == 2


@pytest.mark.skipif(os.name != "nt", reason="Exercises the native Windows resolver")
def test_store_project_dir_during_first_worker_parent_creation(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    target = store.root / "projects/survives"
    actual, _ = race_first_missing_parent(
        monkeypatch, target, lambda: (store.root / "projects/platform-worker").mkdir(parents=True),
        lambda: store.project_dir("survives"))
    assert actual == target


@pytest.mark.skipif(os.name != "nt", reason="Exercises the native Windows resolver")
def test_store_revision_dir_during_parent_creation(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    project = store.project_dir("survives")
    project.mkdir(parents=True)
    target = project / "revisions/r-new"
    actual, _ = race_first_missing_parent(
        monkeypatch, target, lambda: (project / "revisions/r-worker").mkdir(parents=True),
        lambda: store.revision_dir("survives", "r-new"))
    assert actual == target


@pytest.mark.skipif(os.name != "nt", reason="Bounded native Windows creation-race stress")
def test_twenty_first_project_creation_races(tmp_path, monkeypatch):
    started = time.perf_counter()
    for index in range(20):
        assert time.perf_counter() - started < 15, "Bounded stress budget exceeded"
        store = Store(tmp_path / f"data-{index}")
        target = store.root / "projects/survives"
        with monkeypatch.context() as patch:
            actual, _ = race_first_missing_parent(
                patch, target, lambda: (store.root / "projects/platform-worker").mkdir(parents=True),
                lambda: store.project_dir("survives"))
        assert actual == target
    print(json.dumps({"first_creation_races": 20, "seconds": time.perf_counter() - started}))


def directory_alias(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)],
                       check=True, capture_output=True, timeout=5)
    else:
        path.symlink_to(target, target_is_directory=True)


def remove_alias(path):
    if os.name == "nt":
        path.rmdir()
    else:
        path.unlink()


@pytest.mark.parametrize("level", ["projects", "project", "revisions", "revision"])
def test_external_directory_alias_cannot_pass_containment(tmp_path, level):
    store = Store(tmp_path / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    paths = {"projects": store.root / "projects", "project": store.root / "projects/escape",
             "revisions": store.root / "projects/escape/revisions",
             "revision": store.root / "projects/escape/revisions/r-a"}
    alias = paths[level]
    directory_alias(alias, outside)
    try:
        with pytest.raises(ValueError, match="escapes workspace"):
            if level in {"projects", "project"}:
                store.project_dir("escape")
            else:
                store.revision_dir("escape", "r-a")
    finally:
        remove_alias(alias)


def test_dangling_external_alias_cannot_be_treated_as_missing_directory(tmp_path):
    store = Store(tmp_path / "data")
    alias = store.root / "projects/escape"
    directory_alias(alias, tmp_path / "missing-external-target")
    try:
        with pytest.raises((ValueError, OSError)):
            store.project_dir("escape")
        with pytest.raises((ValueError, OSError)):
            store.revision_dir("escape", "r-a")
    finally:
        remove_alias(alias)


def test_internal_alias_preserves_existing_containment_semantics(tmp_path):
    store = Store(tmp_path / "data")
    target = store.root / "real"
    target.mkdir()
    alias = store.root / "projects/inside"
    directory_alias(alias, target)
    try:
        assert store.project_dir("inside") == alias
        assert store.revision_dir("inside", "r-a") == alias / "revisions/r-a"
    finally:
        remove_alias(alias)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows creation race with an escaping junction")
def test_concurrent_parent_creation_cannot_redirect_outside_workspace(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = store.root / "projects"
    target = alias / "survives"
    try:
        with pytest.raises(ValueError, match="escapes workspace"):
            race_first_missing_parent(monkeypatch, target, lambda: directory_alias(alias, outside),
                                      lambda: store.project_dir("survives"))
    finally:
        if alias.is_junction():
            remove_alias(alias)


def test_permission_error_is_not_a_lexical_containment_fallback(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    target = store.root / "projects/blocked"
    original = Path.resolve

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("injected access denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", denied)
    with pytest.raises(PermissionError, match="access denial"):
        store.project_dir("blocked")


@pytest.mark.parametrize("identifier", ["../escape", "C:escape", "a/b", "a\\b", ""])
def test_identifiers_still_reject_path_syntax(tmp_path, identifier):
    store = Store(tmp_path / "data")
    with pytest.raises(ValueError):
        store.project_dir(identifier)
    with pytest.raises(ValueError):
        store.revision_dir("demo", identifier)


def test_new_root_is_canonical_and_no_project_is_created_eagerly(tmp_path):
    store = Store(tmp_path / "new/parent/data")
    assert store.root == store.root.resolve(strict=True)
    assert store.root.is_dir()
    assert not (store.root / "projects").exists()
