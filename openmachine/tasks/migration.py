"""Initialize current Task storage or refuse pre-release history; back up explicitly."""

import os
from pathlib import Path
import sqlite3
import uuid

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory
from . import database as db
from .index import private_path


def migrate(state: Path) -> dict:
    """Managed first-release initialization; never import or reset old Task authority."""
    db.sqlite_runtime()
    if not state.is_absolute():
        raise Fault("invalid_state", "Use an absolute private state directory.")
    private_directory(state)
    with db.initialization_lock(state):
        if os.path.lexists(state / "tasks") or any(state.glob("tasks-legacy-*")):
            raise Fault("task_migration_required",
                        "Pre-release Task history needs explicit developer review and reset; original bytes remain unchanged.")
        if not os.path.lexists(state / "tasks.db"):
            db.create_database(state, active=True)
        else:
            connection = db._open(state, write=True)
            try:
                schema = connection.execute("SELECT schema FROM store WHERE singleton=1").fetchone()
                if schema is None:
                    raise Fault("corrupt_tasks", "Task schema authority is missing; preserve the original store.")
                if schema[0] != db.SCHEMA_VERSION:
                    raise Fault("task_migration_required",
                                "An older pre-release Task schema cannot grant current writer authority; preserve and explicitly reset it.")
                owner = db.metadata(connection)
                if owner["archive"] is not None or owner["import_digest"] is not None:
                    raise Fault("task_migration_required", "Imported legacy Task authority cannot be silently activated.")
                if owner["restore_required"]:
                    raise Fault("task_restore_unreconciled", "Restore needs explicit original-state reconciliation.")
                if not owner["active"]:
                    raise Fault("task_activation_pending", "Inactive Task authority cannot be silently activated.")
                db.ensure_target_indexes(connection)  # Derived indexes, not a schema/data migration.
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
