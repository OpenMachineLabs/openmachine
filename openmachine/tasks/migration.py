"""Explicit legacy cutover and SQLite backup under the original Task lock."""

import hashlib
import os
from pathlib import Path
import sqlite3
import uuid

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory
from . import database as db, records, store
from .index import MAX_INDEX, private_path, read_bytes, read_checkpoint, read_index, read_structured, structured_metadata


def _manifest(root: Path) -> list[tuple[str, str, int]]:
    result = []
    for path in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if path.is_dir() and not path.is_symlink():
            private_path(path, directory=True)
            continue
        raw = read_bytes(path, MAX_INDEX)
        result.append((path.relative_to(root).as_posix(), hashlib.sha256(raw).hexdigest(), len(raw)))
    return result


def _source(root: Path) -> tuple[dict, list]:
    index = read_index(root)
    for identity, item in index["tasks"].items():
        if item.get("owner") == "structured":
            record, raw = read_structured(root, identity)
            if item != structured_metadata(record, raw):
                raise Fault("task_index_incomplete", "Legacy metadata differs from its original Task authority.")
        else:
            read_checkpoint(root, index, identity)
    return index, _manifest(root)


def _verify_import(connection, root: Path, manifest: list) -> None:
    retained = [tuple(row) for row in connection.execute("SELECT path,sha256,size FROM imports ORDER BY path")]
    if retained != manifest:
        raise Fault("task_migration_incomplete", "The imported source manifest differs from the retained original bytes.")
    index = read_index(root)
    if connection.execute("SELECT count(*) FROM tasks").fetchone()[0] != len(index["tasks"]):
        raise Fault("task_migration_incomplete", "The imported Task count is incomplete.")
    for identity, item in sorted(index["tasks"].items()):
        row = store._row(connection, identity)
        current = db.decode(row["current"], row["sha256"])
        if item.get("owner") == "structured":
            current, _ = store._current(connection, row)
            original, _ = read_structured(root, identity)
            operations = [store._operation(op) for op in connection.execute("SELECT * FROM operations WHERE task_id=? ORDER BY ordinal", (identity,))]
            history = [db.decode(event["payload"], event["sha256"]) for event in connection.execute("SELECT payload,sha256 FROM task_events WHERE task_id=? ORDER BY seq", (identity,))]
            witnesses = store._witnesses(connection, identity, operations)
            if (current != store._core(original) or operations != original["operations"] or history != original["history"]
                    or {w["operation_id"]: w for w in witnesses} != {w["operation_id"]: w for w in original.get("effect_scopes", [])}
                    or row["operation_count"] != len(operations) or row["history_count"] != len(history)):
                raise Fault("task_migration_incomplete", "Imported Task facts do not match the original retained state.")
        else:
            metadata, raw = read_checkpoint(root, index, identity)
            if any(current.get(key) != value for key, value in metadata.items()) or current["body"].encode() != raw:
                raise Fault("task_migration_incomplete", "Imported Markdown differs from its original checkpoint.")


def migrate(state: Path) -> dict:
    """Only managed maintenance calls this; imports never observe or dispatch tools."""
    db.sqlite_runtime()
    if not state.is_absolute():
        raise Fault("invalid_state", "Use an absolute private state directory.")
    private_directory(state)
    legacy = state / "tasks"
    with db.initialization_lock(state):
        if not os.path.lexists(state / "tasks.db"):
            if os.path.lexists(legacy):
                private_path(legacy, directory=True)
                index, manifest = _source(legacy)
                archive = "tasks-legacy-" + uuid.uuid4().hex
                db.create_database(state, active=False, archive=archive,
                    import_digest=records.digest(manifest), generation=index["generation"])
            else:
                db.create_database(state, active=False)
        connection = db._open(state, write=True)
        try:
            owner = db.metadata(connection)
            if owner["restore_required"]:
                raise Fault("task_restore_unreconciled", "Migration cannot grant restored history new write authority.")
            if owner["active"]:
                if os.path.lexists(legacy):
                    raise Fault("corrupt_tasks", "Two Task stores claim authority.")
            elif owner["archive"] is None:
                if os.path.lexists(legacy):
                    raise Fault("corrupt_tasks", "Fresh database preparation cannot consume unrelated legacy history.")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE store SET active=1 WHERE singleton=1")
                db.commit(connection)
                db.sync_directory(state)
            else:
                archive = state / owner["archive"]
                source = legacy if os.path.lexists(legacy) else archive
                private_path(source, directory=True)
                index, manifest = _source(source)
                digest = records.digest(manifest)
                if digest != owner["import_digest"]:
                    if source != legacy or os.path.lexists(archive):
                        raise Fault("task_migration_conflict", "Fenced original Task bytes changed; preserve both stores.")
                    # Before the fence, the original controller may have resumed
                    # after a failed activation. Rebuild only its derived import.
                    connection.execute("BEGIN IMMEDIATE")
                    for table in ("operation_scopes", "task_events", "task_updates", "operations", "stage_decisions", "tasks", "imports"):
                        connection.execute(f"DELETE FROM {table}")
                    connection.execute("UPDATE store SET import_digest=?,generation=? WHERE singleton=1", (digest, index["generation"]))
                    db.commit(connection)
                if not connection.execute("SELECT 1 FROM imports LIMIT 1").fetchone():
                    if source != legacy or connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
                        raise Fault("task_migration_incomplete", "Migration lost its original complete import evidence.")
                    connection.execute("BEGIN IMMEDIATE")
                    for sequence, (identity, item) in enumerate(sorted(index["tasks"].items()), 1):
                        if item.get("owner") == "structured":
                            record, _ = read_structured(source, identity)
                            store._insert_record(connection, record, sequence=sequence)
                        else:
                            metadata, raw = read_checkpoint(source, index, identity)
                            store._put_legacy(connection, identity, {**metadata, "body": raw.decode(), "content_redacted": False}, sequence=sequence)
                    connection.executemany("INSERT INTO imports VALUES (?,?,?)", manifest)
                    db.commit(connection)
                _verify_import(connection, source, manifest)
                if _manifest(source) != manifest:
                    raise Fault("task_migration_conflict", "Legacy Task bytes changed during import verification.")
                if source == legacy:
                    if os.path.lexists(archive):
                        raise Fault("task_migration_conflict", "The private archive destination already exists.")
                    os.rename(legacy, archive)
                    db.sync_directory(state)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE store SET active=1 WHERE singleton=1")
                db.commit(connection)
                db.sync_directory(state)
        except sqlite3.Error as error:
            raise db._fault(error, True) from error
        finally:
            connection.close()
    return db.storage_state(state)


def backup(state: Path, destination: Path) -> dict:
    """Create one consistent, private backup; copied history has no new-write authority."""
    if not destination.is_absolute() or os.path.lexists(destination):
        raise Fault("task_backup_conflict", "Choose an absent absolute backup destination.")
    private_path(destination.parent, directory=True)
    with db.connect(state) as source:
        if source is None:
            raise Fault("tasks_uninitialized", "There is no Task database to back up.")
        owner = db.metadata(source)
        prepared = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
        fd = os.open(prepared, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        target = sqlite3.connect(prepared)
        try:
            source.backup(target)
            target.execute("PRAGMA synchronous=FULL")
            if os.sys.platform == "darwin":
                target.execute("PRAGMA fullfsync=ON")
            target.execute("UPDATE store SET restore_required=1 WHERE singleton=1")
            target.commit()
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            target.close()
        # Publish only after copied authority has been explicitly fenced. A
        # failed preparation remains private evidence, never a usable backup.
        fd = os.open(prepared, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(prepared, destination, follow_symlinks=False)
        prepared.unlink()
        db.sync_directory(destination.parent)
    return {"identity": owner["identity"], "generation": owner["generation"], "path": str(destination),
            "restore_required": True}
