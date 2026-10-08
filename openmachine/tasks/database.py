"""Private SQLite ownership, transactions and durability for the Task owner."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import uuid

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from . import records
from .index import private_path

SCHEMA_VERSION = 1
MIN_ADMISSION_FREE = 16 * 1024 * 1024
INITIALIZING = b"openmachine-task-initializing-v1\n"
_active = ContextVar("task_transaction", default=None)
SCHEMA = """
CREATE TABLE store (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema INTEGER NOT NULL,
 identity TEXT NOT NULL, active INTEGER NOT NULL, generation INTEGER NOT NULL,
 archive TEXT, import_digest TEXT, restore_required INTEGER NOT NULL DEFAULT 0) STRICT;
CREATE TABLE tasks (task_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('structured','legacy')),
 revision INTEGER NOT NULL, change_seq INTEGER NOT NULL, current TEXT NOT NULL, sha256 TEXT NOT NULL,
 title_key TEXT NOT NULL, anchors TEXT NOT NULL, operation_count INTEGER NOT NULL DEFAULT 0,
 history_count INTEGER NOT NULL DEFAULT 0, scope_count INTEGER NOT NULL DEFAULT 0,
 stage_counts TEXT NOT NULL DEFAULT '{}') STRICT;
CREATE INDEX task_changes ON tasks(change_seq);
CREATE INDEX task_titles ON tasks(title_key);
CREATE TABLE operations (task_id TEXT NOT NULL REFERENCES tasks(task_id), id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, stage_id TEXT NOT NULL, plan_revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
 kind TEXT NOT NULL, machine TEXT, target_identity TEXT, repository TEXT, effect_key TEXT NOT NULL,
 admitted_at_ms INTEGER NOT NULL, observed_at_ms INTEGER, pending INTEGER NOT NULL,
 unresolved INTEGER NOT NULL, outcome TEXT, payload TEXT NOT NULL, sha256 TEXT NOT NULL, locator_id TEXT,
 PRIMARY KEY(task_id,id), UNIQUE(task_id,ordinal)) STRICT;
CREATE INDEX operation_ids ON operations(id);
CREATE INDEX operation_keys ON operations(effect_key);
CREATE INDEX operation_pending ON operations(task_id,unresolved,ordinal);
CREATE INDEX operation_repository ON operations(repository,unresolved);
CREATE INDEX operation_target ON operations(machine,target_identity);
CREATE INDEX operation_locator ON operations(task_id,machine,locator_id);
CREATE INDEX operation_stage ON operations(task_id,stage_id,ordinal DESC);
CREATE INDEX operation_admission ON operations(task_id,epoch,admitted_at_ms);
CREATE INDEX operation_progress ON operations(task_id,admitted_at_ms DESC);
CREATE INDEX operation_observation ON operations(task_id,observed_at_ms DESC);
CREATE TABLE task_events (task_id TEXT NOT NULL REFERENCES tasks(task_id), seq INTEGER NOT NULL,
 kind TEXT NOT NULL, at_ms INTEGER NOT NULL, stage_id TEXT, operation_id TEXT,
 payload TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(task_id,seq)) STRICT;
CREATE TABLE task_updates (task_id TEXT NOT NULL REFERENCES tasks(task_id), id TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, PRIMARY KEY(task_id,id)) STRICT;
CREATE TABLE stage_decisions (task_id TEXT NOT NULL REFERENCES tasks(task_id) DEFERRABLE INITIALLY DEFERRED,
 stage_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY(task_id,stage_id,version)) STRICT;
CREATE INDEX event_facts ON task_events(task_id,kind,stage_id,at_ms);
CREATE TABLE operation_scopes (task_id TEXT NOT NULL, operation_id TEXT NOT NULL,
 scope TEXT NOT NULL, witness TEXT, sha256 TEXT NOT NULL, PRIMARY KEY(task_id,operation_id),
 FOREIGN KEY(task_id,operation_id) REFERENCES operations(task_id,id)) STRICT;
CREATE TABLE imports (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL) STRICT;
"""


def sqlite_runtime() -> dict:
    version = sqlite3.sqlite_version_info
    supported = (version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0)
                 or (3, 44, 6) <= version < (3, 45, 0))
    if not supported:
        raise Fault("task_sqlite_unsupported", "Use a packaged SQLite engine with the WAL-reset fix (3.51.3 or a verified patched release).")
    with closing(sqlite3.connect(":memory:")) as connection:
        source = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
    return {"version": sqlite3.sqlite_version, "version_info": list(version), "source_id": source, "supported": True}


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initializing(state: Path, *, durable: bool = False) -> bool:
    """Read the original lock marker, including its one proven publication alias."""
    private_path(state, directory=True)
    private_path(state / "locks", directory=True)
    path, prepared = state / "locks/tasks.lock", state / "locks/.tasks.lock.prepare"
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077
            or before.st_size > len(INITIALIZING) or before.st_nlink not in (1, 2)):
        raise Fault("corrupt_tasks", "The original Task lock has invalid initialization evidence.")
    if before.st_nlink == 2:
        try:
            alias = prepared.lstat()
        except FileNotFoundError as error:
            raise Fault("corrupt_tasks", "The Task lock has an unrecognized publication alias.") from error
        if (alias.st_dev, alias.st_ino) != (before.st_dev, before.st_ino):
            raise Fault("corrupt_tasks", "An unrelated link cannot stand in for Task lock preparation.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_nlink) != (before.st_dev, before.st_ino, before.st_nlink):
            raise Fault("corrupt_tasks", "The original Task lock changed while reading initialization evidence.")
        marker = stream.read(len(INITIALIZING) + 1)
        if durable:
            # A previous process may have cleared the marker but lost its fsync acknowledgement.
            os.fsync(stream.fileno())
    if marker not in (b"", INITIALIZING) or before.st_nlink == 2 and marker != INITIALIZING:
        raise Fault("corrupt_tasks", "The Task initialization marker is incomplete or unknown.")
    if marker and (os.path.lexists(state / "tasks") or any(state.glob("tasks-legacy-*"))):
        raise Fault("corrupt_tasks", "Fresh Task initialization cannot replace retained legacy authority.")
    return bool(marker)


@contextmanager
def initialization_lock(state: Path):
    """Publish a marked lock once, then retain that same inode through initialization."""
    lock, prepared = state / "locks/tasks.lock", state / "locks/.tasks.lock.prepare"
    published = os.path.lexists(lock)
    if published:
        initializing(state)
    elif (os.path.lexists(state / "tasks.db") or os.path.lexists(state / "tasks")
            or any(state.glob("tasks-legacy-*"))):
        raise Fault("corrupt_tasks", "Retained Task authority lost its original lock; never create another inode.")
    locks = private_directory(state / "locks")
    if not published and os.path.lexists(prepared):
        private_path(prepared)
    with lease(lock if published else prepared, create=not published) as stream:
        if os.path.lexists(lock):
            info = lock.lstat()
            held = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
                raise Fault("task_conflict", "Another initializer published the original Task lock; retry its identity.")
            initializing(state)
        else:
            if published or (os.path.lexists(state / "tasks.db") or os.path.lexists(state / "tasks")
                             or any(state.glob("tasks-legacy-*"))):
                raise Fault("corrupt_tasks", "The original Task lock or authority changed during initialization.")
            info = private_path(prepared)
            held = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
                raise Fault("corrupt_tasks", "The private Task lock preparation changed identity.")
            if os.write(stream.fileno(), INITIALIZING) != len(INITIALIZING):
                raise Fault("task_write_unverified", "The private Task initialization marker was not fully written.")
            os.ftruncate(stream.fileno(), len(INITIALIZING))
            os.fsync(stream.fileno())
            os.link(prepared, lock, follow_symlinks=False)
            sync_directory(locks)
        if lock.lstat().st_nlink == 2:
            initializing(state)
            prepared.unlink()
            sync_directory(locks)
        private_path(lock)
        if os.path.lexists(state / "tasks.db"):
            finish_initialization(state)
        elif not initializing(state) and not os.path.lexists(state / "tasks"):
            raise Fault("corrupt_tasks", "The established Task store disappeared; never initialize empty history.")
        yield


def finish_initialization(state: Path) -> None:
    """Under the original exclusive lock, fence fresh recovery before any Task write."""
    if not initializing(state):
        return
    with closing(_open(state, write=False)) as connection:
        owner = metadata(connection)
        if (owner["generation"] or owner["archive"] is not None or owner["restore_required"]
                or connection.execute("SELECT 1 FROM tasks UNION ALL SELECT 1 FROM imports LIMIT 1").fetchone()):
            raise Fault("corrupt_tasks", "Initialization recovery requires an empty, unpublished Task authority.")
    sync_directory(state)
    sync_directory(state / "locks")
    fd = os.open(state / "locks/tasks.lock", os.O_WRONLY | os.O_NOFOLLOW)
    try:
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)


def _paths(state: Path) -> None:
    private_path(state, directory=True)
    private_path(state / "locks", directory=True)
    private_path(state / "locks/tasks.lock")
    private_path(state / "tasks.db")
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            private_path(state / ("tasks.db" + suffix))
        except FileNotFoundError:
            pass


def _open(state: Path, *, write: bool) -> sqlite3.Connection:
    sqlite_runtime()
    _paths(state)
    if write and initializing(state, durable=True):
        raise Fault("task_activation_pending", "Finish original Task initialization before admitting any write.")
    before = (state / "tasks.db").stat()
    connection = sqlite3.connect((state / "tasks.db").as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, isolation_level=None, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        if sys.platform == "darwin":
            connection.execute("PRAGMA fullfsync=ON")
            connection.execute("PRAGMA checkpoint_fullfsync=ON")
        expected = {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1, "busy_timeout": 5000}
        if sys.platform == "darwin":
            expected.update(fullfsync=1, checkpoint_fullfsync=1)
        if any(connection.execute("PRAGMA " + key).fetchone()[0] != value for key, value in expected.items()):
            raise Fault("task_storage_unsupported", "The Task database did not retain required transaction and durability settings.")
        after = (state / "tasks.db").stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise Fault("corrupt_tasks", "The Task database identity changed while opening it.")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        _paths(state)
        return connection
    except BaseException:
        connection.close()
        raise


def metadata(connection: sqlite3.Connection) -> dict:
    row = connection.execute("SELECT * FROM store WHERE singleton=1").fetchone()
    if row is None or row["schema"] != SCHEMA_VERSION:
        raise Fault("unsupported_task_format", "The authoritative Task database schema is missing or unsupported.")
    value = dict(row)
    if (len(value["identity"]) != 32 or any(c not in "0123456789abcdef" for c in value["identity"])
            or value["active"] not in (0, 1) or not 0 <= value["generation"] <= records.MAX_INTEGER
            or value["restore_required"] not in (0, 1)):
        raise Fault("corrupt_tasks", "Task store ownership metadata is invalid; preserve its original database.")
    archive, digest = value["archive"], value["import_digest"]
    if ((archive is None) != (digest is None) or archive is not None and (
            not isinstance(archive, str) or re.fullmatch(r"tasks-legacy-[0-9a-f]{32}", archive) is None
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None)):
        raise Fault("corrupt_tasks", "Task archive identity and import digest must bind one private retained source.")
    return value


def create_database(state: Path, *, active: bool, archive: str | None = None,
                    import_digest: str | None = None, generation: int = 0) -> None:
    """Called under the original exclusive Task lock; never replaces a database."""
    sqlite_runtime()
    prepared = state / (".tasks-" + uuid.uuid4().hex + ".db")
    fd = os.open(prepared, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    connection = sqlite3.connect(prepared, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        if sys.platform == "darwin":
            connection.execute("PRAGMA fullfsync=ON")
            connection.execute("PRAGMA checkpoint_fullfsync=ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute("INSERT INTO store VALUES (1,?,?,?,?,?,?,0)",
            (SCHEMA_VERSION, uuid.uuid4().hex, int(active), generation, archive, import_digest))
        commit(connection)
        if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() != (0, 0, 0):
            raise Fault("task_write_unverified", "The private Task schema checkpoint was not acknowledged.")
    finally:
        connection.close()
    private_path(prepared)
    fd = os.open(prepared, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.path.lexists(state / "tasks.db"):
        raise Fault("task_conflict", "Task storage appeared during initialization; preserve its original identity.")
    os.rename(prepared, state / "tasks.db")
    sync_directory(state)
    finish_initialization(state)
    _paths(state)


def commit(connection: sqlite3.Connection) -> None:
    """A successful return is the sole database admission acknowledgement."""
    connection.commit()


def _fault(error: sqlite3.Error, write: bool) -> Fault:
    code = getattr(error, "sqlite_errorcode", 0) & 255
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return Fault("task_store_busy", "Another Task transaction owns the database; preserve the original operation ID.")
    if code == sqlite3.SQLITE_FULL:
        return Fault("task_storage_full", "Task storage is full; preserve original receipts and do not dispatch uncommitted work.")
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_SCHEMA):
        return Fault("corrupt_tasks", "The authoritative Task database is unreadable; never replace it with empty history.")
    return Fault("task_write_unverified" if write else "corrupt_tasks",
                 "Task storage did not acknowledge the transaction; retain original identities and evidence.")


@contextmanager
def connect(state: Path, *, write: bool = False, initialize: bool = False, maintenance: bool = False):
    """Short shared maintenance lease; SQLite owns normal read/write concurrency."""
    if not state.is_absolute():
        raise Fault("invalid_state", "Use an absolute private state directory.")
    retained = _active.get()
    if retained is not None and retained[0] == state:
        if write and not retained[2]:
            raise Fault("task_conflict", "A read-only Task snapshot cannot become a write transaction.")
        yield retained[1]
        return
    if not state.exists():
        if not initialize:
            yield None
            return
        private_directory(state)
    private_path(state, directory=True)
    lock_present = os.path.lexists(state / "locks/tasks.lock")
    pending = lock_present and initializing(state)
    if not os.path.lexists(state / "tasks.db"):
        if os.path.lexists(state / "tasks"):
            if write:
                raise Fault("task_migration_required", "Activate the reviewed Task database migration before new task writes.")
            yield None
            return
        if lock_present and not pending or any(state.glob("tasks-legacy-*")):
            raise Fault("corrupt_tasks", "The existing Task store disappeared; never initialize over retained ownership.")
        if not initialize:
            yield None
            return
    if initialize and (pending or not os.path.lexists(state / "tasks.db")):
        with initialization_lock(state):
            if os.path.lexists(state / "tasks"):
                raise Fault("task_conflict", "Task storage appeared during initialization; read its original identity.")
            if not os.path.lexists(state / "tasks.db"):
                create_database(state, active=True)
    connection = None
    try:
        _paths(state)
        with lease(state / "locks/tasks.lock", shared=True, create=False):
            connection = _open(state, write=write)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            owner = metadata(connection)
            if owner["active"] and os.path.lexists(state / "tasks"):
                raise Fault("corrupt_tasks", "SQLite and a legacy Task directory both claim authority.")
            if not maintenance and not owner["active"]:
                raise Fault("task_activation_pending", "Task storage activation is incomplete; continue the original managed migration.")
            if write and owner["restore_required"] and not maintenance:
                raise Fault("task_restore_unreconciled", "Account for effects after the backup watermark before enabling restored task writes.")
            token = _active.set((state, connection, write))
            try:
                yield connection
                if write:
                    commit(connection)
                else:
                    connection.rollback()
            finally:
                _active.reset(token)
    except sqlite3.Error as error:
        raise _fault(error, write) from error
    except OSError as error:
        raise Fault("corrupt_tasks", "The original Task database or stable lock is missing or unsafe.") from error
    finally:
        if connection is not None:
            connection.close()


def bump(connection: sqlite3.Connection) -> int:
    row = connection.execute("UPDATE store SET generation=generation+1 WHERE generation<? RETURNING generation",
                             (records.MAX_INTEGER,)).fetchone()
    if row is None:
        raise Fault("task_capacity", "Task generation exhausted its exact integer representation.")
    return row[0]


def decode(payload: str, sha256: str) -> dict:
    try:
        raw = payload.encode("utf-8")
        value = json.loads(payload, object_pairs_hook=records._pairs)
        if hashlib.sha256(raw).hexdigest() != sha256 or records.canonical(value) != raw:
            raise ValueError("canonical content differs")
        return value
    except (ValueError, TypeError, UnicodeError) as error:
        raise Fault("corrupt_tasks", "A Task row differs from its canonical evidence; do not skip it.") from error


def encoded(value: dict) -> tuple[str, str]:
    raw = records.canonical(value)
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def admission_space(state: Path) -> None:
    # This is a backpressure threshold, not a promise of reserved disk blocks.
    _paths(state)
    if shutil.disk_usage(state).free < MIN_ADMISSION_FREE:
        raise Fault("task_storage_full", "Low free disk space is reserved for original outcomes and stopping.")


def storage_state(state: Path) -> dict:
    if not os.path.lexists(state / "tasks.db"):
        if not os.path.lexists(state / "tasks"):
            lock_present = os.path.lexists(state / "locks/tasks.lock")
            pending = lock_present and initializing(state)
            if lock_present and not pending or any(state.glob("tasks-legacy-*")):
                raise Fault("corrupt_tasks", "Retained Task ownership has no authoritative store.")
            return {"kind": "absent", "schema": None, "identity": None, "generation": None,
                    "active": False, "fenced": pending}
        from .index import read_index
        private_path(state, directory=True)
        private_path(state / "tasks", directory=True)
        private_path(state / "locks", directory=True)
        private_path(state / "locks/tasks.lock")
        with lease(state / "locks/tasks.lock", shared=True, create=False):
            index = read_index(state / "tasks")
        return {"kind": "legacy", "schema": None, "identity": None, "generation": index["generation"],
                "active": False, "fenced": False}
    with connect(state, maintenance=True) as connection:
        owner = metadata(connection)
        legacy = os.path.lexists(state / "tasks")
        fenced = not legacy
        if owner["archive"] and fenced:
            private_path(state / owner["archive"], directory=True)
        return {"kind": "legacy" if legacy else "sqlite", "schema": owner["schema"],
                "identity": owner["identity"], "generation": owner["generation"],
                "active": bool(owner["active"]), "fenced": fenced,
                "restore_required": bool(owner["restore_required"])}
