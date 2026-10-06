import sqlite3

import pytest

from pcb_weaver.storage import Store


def test_connection_commits_and_closes(tmp_path):
    store = Store(tmp_path)
    with store.connect() as connection:
        connection.execute("CREATE TABLE probe (value INTEGER)")
        connection.execute("INSERT INTO probe VALUES (7)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with store.connect() as reopened:
        assert reopened.execute("SELECT value FROM probe").fetchall() == [(7,)]


def test_connection_rolls_back_and_closes_on_error(tmp_path):
    store = Store(tmp_path)
    with store.connect() as connection:
        connection.execute("CREATE TABLE probe (value INTEGER)")
    with pytest.raises(RuntimeError, match="abort"):
        with store.connect() as failed:
            failed.execute("INSERT INTO probe VALUES (9)")
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
    with store.connect() as reopened:
        assert reopened.execute("SELECT * FROM probe").fetchall() == []


def test_connections_close_if_setup_fails(tmp_path, monkeypatch):
    store = Store(tmp_path)

    class BrokenSetup(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            if args[0] == "PRAGMA journal_mode=WAL":
                raise sqlite3.OperationalError("injected setup failure")
            return super().execute(*args, **kwargs)

    connection = sqlite3.connect(tmp_path / "setup.sqlite3", factory=BrokenSetup)
    monkeypatch.setattr("pcb_weaver.storage.sqlite3.connect", lambda *a, **kw: connection)
    with pytest.raises(sqlite3.OperationalError):
        with store.connect():
            pytest.fail("Setup should fail before yielding")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_ledger_calls_leave_no_windows_file_locks(tmp_path):
    store = Store(tmp_path)
    store.add_revision({"id": "r-test", "project": "demo", "created": "2026-09-07"})
    store.event("demo", "test", {"ok": True})
    assert store.revision("demo", "r-test")["id"] == "r-test"
    assert len(store.list_revisions("demo")) == 1
    assert store.history("demo")["integrity"] == "verified"
    # Keep Store alive: cleanup must not depend on garbage collection.
    for path in tmp_path.iterdir():
        path.unlink()
    assert not list(store.root.iterdir())
