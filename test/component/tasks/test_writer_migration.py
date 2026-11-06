"""First-release writer storage rejects pre-release migration without losing evidence."""

from contextlib import closing
import sqlite3
import unittest

from openmachine.chat import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.chat.tasks import database
from test.support.tasks import TaskFixture, write_legacy_checkpoint


class WriterMigrationTests(TaskFixture, unittest.TestCase):
    def old_schema(self, version):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE writer_reservations")
            connection.execute("UPDATE store SET schema=?", (version,))
            connection.commit()

    def test_fresh_current_schema_and_idempotent_managed_open(self):
        state = tasks.migrate(self.state)
        self.assertEqual((state["kind"], state["schema"], state["active"]),
                         ("sqlite", database.SCHEMA_VERSION, True))
        self.assertEqual(tasks.migrate(self.state), state)
        with database.connect(self.state) as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(writer_reservations)")]
        self.assertEqual(columns, ["task_id", "root", "device", "inode"])

    def test_prior_sqlite_versions_fail_closed_without_rewriting_task_or_receipt(self):
        for version in (1, 2):
            with self.subTest(schema=version):
                state = self.root / f"old-{version}"
                config = self.request
                tasks.create(state, config["task_id"], config["update_id"], config["title"],
                             config["anchors"], config["origin"], config["contract"], config["stages"])
                with closing(sqlite3.connect(state / "tasks.db")) as connection:
                    previous = tuple(connection.execute("SELECT current,sha256 FROM tasks").fetchone())
                    connection.execute("DROP TABLE writer_reservations")
                    connection.execute("UPDATE store SET schema=?", (version,))
                    connection.commit()
                inode = (state / "tasks.db").stat().st_ino
                self.fault("task_migration_required", tasks.migrate, state)
                self.assertEqual((state / "tasks.db").stat().st_ino, inode)
                with closing(sqlite3.connect(state / "tasks.db")) as connection:
                    self.assertEqual(connection.execute("SELECT schema FROM store").fetchone()[0], version)
                    self.assertEqual(tuple(connection.execute("SELECT current,sha256 FROM tasks").fetchone()), previous)
                    self.assertIsNone(connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='writer_reservations'").fetchone())

    def test_legacy_file_store_is_preserved_not_implicitly_imported(self):
        write_legacy_checkpoint(self.state, "old-note", "Pre-release developer work.\n")
        root = self.state / "tasks"
        original = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.fault("task_migration_required", tasks.migrate, self.state)
        self.assertFalse((self.state / "tasks.db").exists())
        self.assertEqual(original, {str(path.relative_to(root)): path.read_bytes()
                                    for path in root.rglob("*") if path.is_file()})

    def test_restore_fence_and_existing_reservation_are_not_reset(self):
        self.accept_plan()
        args = self.write_arguments()
        self.app.write_workspace_file(**args)
        before = self.task_bytes()
        self.assertEqual(tasks.migrate(self.state)["schema"], 3)
        self.assertEqual(self.task_bytes(), before)
        with database.connect(self.state) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM writer_reservations").fetchone()[0], 1)
        with database.connect(self.state, write=True) as connection:
            connection.execute("UPDATE store SET restore_required=1")
        self.fault("task_restore_unreconciled", tasks.migrate, self.state)
        self.assertEqual(self.task_bytes(), before)

    def test_unsupported_schema_must_not_be_repaired_or_reinitialized(self):
        self.create()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE store SET schema=99")
            connection.commit()
        original = self.path.read_bytes()
        self.fault("task_migration_required", tasks.migrate, self.state)
        self.assertEqual(self.path.read_bytes(), original)
