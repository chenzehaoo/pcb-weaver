"""Revision files and an append-only application event ledger."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def canonical(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _resolve_existing_ancestor(path: Path) -> Path:
    """Resolve aliases strictly, appending only the not-yet-created tail.

    Windows non-strict realpath can retain a device prefix if parent creation
    changes ERROR_PATH_NOT_FOUND to ERROR_FILE_NOT_FOUND during its call. A
    strictly resolved existing ancestor has no such missing-path fallback.
    """
    current, missing = path, []
    while True:
        try:
            resolved = current.resolve(strict=True)
        except FileNotFoundError:
            # Do not mistake a dangling link for an ordinary missing component.
            # A concurrently created entry gets one strict retry, never a
            # lexical fallback; permission errors and unresolved links propagate.
            try:
                current.lstat()
            except FileNotFoundError:
                if current.parent == current:
                    raise
                missing.append(current.name)
                current = current.parent
                continue
            resolved = current.resolve(strict=True)
        return resolved.joinpath(*reversed(missing))


class Store:
    def __init__(self, root: Path):
        self.root = root.expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, parent TEXT,
                    created TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
                    created TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "ledger.sqlite3", timeout=20)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def identifier(value: str):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
            raise ValueError("Use a 1-64 character project/id containing letters, digits, _ or -")
        return value

    def project_dir(self, project):
        path = self.root / "projects" / self.identifier(project)
        if not _resolve_existing_ancestor(path).is_relative_to(self.root):
            raise ValueError("Project path escapes workspace")
        return path

    def revision_dir(self, project, revision):
        path = self.project_dir(project) / "revisions" / self.identifier(revision)
        if not _resolve_existing_ancestor(path).is_relative_to(self.root):
            raise ValueError("Revision path escapes workspace")
        return path

    @contextmanager
    def lock(self, project):
        directory = self.project_dir(project)
        directory.mkdir(parents=True, exist_ok=True)
        # OS byte-range locks are released even if a worker crashes.
        handle = (directory / ".lock").open("a+b")
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            yield
        except OSError as exc:
            if not locked:
                raise RuntimeError("Another operation is using this project; retry after it finishes") from exc
            raise
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def event(self, project, kind, payload):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT hash FROM events WHERE project=? ORDER BY seq DESC LIMIT 1", (project,)).fetchone()
            previous = row[0] if row else "0" * 64
            created = now()
            encoded = canonical(payload)
            checksum = hashlib.sha256(canonical([project, created, kind, encoded, previous]).encode()).hexdigest()
            db.execute("INSERT INTO events(project,created,kind,payload,previous_hash,hash) VALUES(?,?,?,?,?,?)",
                       (project, created, kind, encoded, previous, checksum))

    def add_revision(self, data):
        with self.connect() as db:
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (data["id"], data["project"], data.get("parent"), data["created"], canonical(data)))
        self.event(data["project"], "revision_created", data)

    def revision(self, project, revision):
        self.identifier(project)
        self.identifier(revision)
        with self.connect() as db:
            row = db.execute("SELECT payload FROM revisions WHERE project=? AND id=?", (project, revision)).fetchone()
        if not row:
            raise ValueError("Unknown project revision")
        return json.loads(row[0])

    def list_revisions(self, project):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM revisions WHERE project=? ORDER BY created", (project,))]

    def history(self, project):
        with self.connect() as db:
            rows = db.execute("SELECT seq,created,kind,payload,previous_hash,hash FROM events WHERE project=? ORDER BY seq", (project,)).fetchall()
        previous = "0" * 64
        events = []
        for seq, created, kind, payload, link, checksum in rows:
            actual = hashlib.sha256(canonical([project, created, kind, payload, link]).encode()).hexdigest()
            if link != previous or checksum != actual:
                raise ValueError("Event ledger integrity check failed")
            previous = checksum
            events.append(dict(seq=seq, created=created, kind=kind, payload=json.loads(payload), hash=checksum))
        return {"project": project, "integrity": "verified", "events": events,
                "scope": "Local hash chain detects accidental edits; not a signed or externally anchored audit log"}
