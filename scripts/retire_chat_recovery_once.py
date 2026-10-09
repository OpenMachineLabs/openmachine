"""One-time OFFLINE Task data conversion. Not part of the installed runtime.

This command only stages a verified SQLite copy. It never replaces the source,
changes service selection, schedules work, or makes a restored copy writable.
An operator must separately stop admission, reconcile active work, retain the
source, and perform a guarded service cutover.
"""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from openmachine.tasks import database, records, store

OPAQUE_TABLES = (
    "store", "operations", "task_events", "task_updates",
    "stage_decisions", "operation_scopes", "imports",
)


def _private_file(path: Path) -> Path:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ValueError("Source must be an owned, private, single-link regular file")
    return path.resolve(strict=True)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _rows(connection: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _retire(raw: str, sha256: str, identity: str, revision: int) -> tuple[str, str, bool]:
    source = raw.encode("utf-8")
    value = json.loads(raw, object_pairs_hook=records._pairs)
    if hashlib.sha256(source).hexdigest() != sha256 or _canonical(value) != source:
        raise ValueError("Task authority has an invalid canonical hash")
    if value.get("task_id") != identity or value.get("revision") != revision:
        raise ValueError("Task identity/revision differs from its indexed row")
    contract = value.get("contract")
    if type(contract) is not dict:
        raise ValueError("Unknown Task contract")
    policy = "recovery_policy" in contract
    registration = "chat_registration" in value
    if policy != registration:
        raise ValueError("Half-retired recovery metadata must not be guessed")
    if not policy:
        return raw, sha256, False
    if (type(value.get("format")) is not int or value["format"] not in (3, 4)
            or contract["recovery_policy"] not in ("disabled", "task_chat")
            or type(contract["recovery_policy"]) is not str
            or (value["chat_registration"] is not None
                and (type(value["chat_registration"]) is not str
                     or re.fullmatch(r"[0-9a-f]{32}", value["chat_registration"]) is None))):
        raise ValueError("Unrecognized retired Task metadata must remain untouched")
    del contract["recovery_policy"]
    del value["chat_registration"]
    converted = _canonical(value)
    return converted.decode("utf-8"), hashlib.sha256(converted).hexdigest(), True


def stage(source_db: Path, destination_db: Path) -> dict:
    """Produce a validated private copy, leaving original bytes and files untouched."""
    database.sqlite_runtime()
    source_db = _private_file(source_db)
    destination_db = destination_db.absolute()
    parent = destination_db.parent
    parent_info = parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
            or parent_info.st_mode & 0o077 or destination_db == source_db
            or os.path.lexists(destination_db)):
        raise ValueError("Choose an absent destination inside an owned private directory")
    fd, temporary_name = tempfile.mkstemp(prefix=".retire-task-", suffix=".db", dir=parent)
    os.close(fd)
    temporary = Path(temporary_name)
    published = False
    try:
        with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as candidate:
                original.backup(candidate)
                candidate.row_factory = sqlite3.Row
                if candidate.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Source Task snapshot failed SQLite integrity validation")
                owner = candidate.execute(
                    "SELECT schema,identity,active,generation,restore_required FROM store WHERE singleton=1"
                ).fetchone()
                if (owner is None or owner["schema"] != 1 or owner["active"] != 1
                        or owner["restore_required"] != 0):
                    raise ValueError("Unknown/inactive/unreconciled Task store cannot be migrated")
                original_opaque = {table: _rows(candidate, table) for table in OPAQUE_TABLES}
                original_tasks = {row["task_id"]: tuple(row) for row in
                                  candidate.execute("SELECT * FROM tasks ORDER BY task_id")}
                converted_count = 0
                candidate.execute("BEGIN IMMEDIATE")
                for row in candidate.execute(
                        "SELECT task_id,kind,revision,current,sha256 FROM tasks ORDER BY task_id"
                ).fetchall():
                    if row["kind"] != "structured":
                        # Existing Markdown checkpoints are not JSON Task authority.
                        continue
                    payload, digest, changed = _retire(
                        row["current"], row["sha256"], row["task_id"], row["revision"])
                    if changed:
                        candidate.execute("UPDATE tasks SET current=?,sha256=? WHERE task_id=?",
                                          (payload, digest, row["task_id"]))
                        converted_count += 1
                candidate.commit()

                if candidate.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Converted SQLite copy failed integrity validation")
                if candidate.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Converted SQLite copy has broken foreign keys")
                if any(_rows(candidate, table) != before
                       for table, before in original_opaque.items()):
                    raise ValueError("Conversion changed operation, event or store evidence")
                updated_tasks = {row["task_id"]: tuple(row) for row in
                                 candidate.execute("SELECT * FROM tasks ORDER BY task_id")}
                if original_tasks.keys() != updated_tasks.keys():
                    raise ValueError("Conversion altered Task identities")
                task_columns = [row["name"] for row in candidate.execute("PRAGMA table_info(tasks)")]
                changed_columns = {task_columns.index("current"), task_columns.index("sha256")}
                for identity, prior in original_tasks.items():
                    latest = updated_tasks[identity]
                    if any(a != b for index, (a, b) in enumerate(zip(prior, latest))
                           if index not in changed_columns):
                        raise ValueError("Conversion changed retained Task indexes")
                    row = store._row(candidate, identity)
                    if row["kind"] == "structured":
                        store._current(candidate, row)  # Strict format reader, including decision references.
                        store._view(candidate, identity)  # All indexed operation/Task facts.
                if tuple(candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) != (0, 0, 0):
                    raise ValueError("Staged copy did not checkpoint cleanly")
                # Preserve the WAL header required by the read-only runtime.
                if candidate.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    raise ValueError("Staged copy lost the required WAL journal mode")
                result = {
                    "status": "staged_not_applied",
                    "structured_tasks": sum(candidate.execute(
                        "SELECT count(*) FROM tasks WHERE kind='structured'").fetchone()),
                    "converted_tasks": converted_count,
                    "store_identity": owner["identity"],
                    "store_generation": owner["generation"],
                }
        handle = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
        os.link(temporary, destination_db, follow_symlinks=False)
        published = True
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return result
    finally:
        temporary.unlink(missing_ok=True)
        if not published:
            # Never remove or change the source, even when validation fails.
            assert source_db.exists()


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage, do not install, a one-time Task authority conversion")
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    args = parser.parse_args()
    result = stage(args.source_db, args.output_db)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
