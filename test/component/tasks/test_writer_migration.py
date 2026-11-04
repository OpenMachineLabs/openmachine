"""One-time writer backfill preserves canonical history and refuses guesses."""

from contextlib import closing
import sqlite3
import unittest
import uuid

from openmachine import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.service.checks import resolve_task_writers
from openmachine.tasks import database
from test.support.tasks import TaskFixture, task_contract


class WriterMigrationTests(TaskFixture, unittest.TestCase):
    def old_schema(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE writer_reservations")
            connection.execute("UPDATE store SET schema=2")
            connection.commit()

    def history(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return {table:connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                    for table in ("tasks","operations","task_events","task_updates")}

    def migrate(self):
        return tasks.migrate(self.state, resolve_writers=lambda current, operations:
            resolve_task_writers(self.state,self.config,current,operations))

    def test_unique_owner_backfill_preserves_all_history_bytes(self):
        self.accept_plan()
        args=self.write_arguments()
        self.app.write_workspace_file(**args)
        receipt=(self.state / "workspace-changes" / args["change_id"] / "record.json").read_bytes()
        before=self.history()
        self.old_schema()
        self.assertEqual(self.migrate()["schema"],database.SCHEMA_VERSION)
        self.assertEqual(self.history(),before)
        self.assertEqual((self.state / "workspace-changes" / args["change_id"] / "record.json").read_bytes(),receipt)
        with database.connect(self.state) as connection:
            rows=connection.execute("SELECT task_id,root,state FROM writer_reservations").fetchall()
        self.assertEqual([tuple(row) for row in rows],[("structured-one",str(self.work),"owned")])
        self.assertEqual(self.migrate()["schema"],database.SCHEMA_VERSION)

    def test_configuration_remap_does_not_erase_verified_historical_owner(self):
        self.accept_plan()
        self.app.write_workspace_file(**self.write_arguments())
        remapped = self.root / "remapped-root"
        remapped.mkdir()
        self.config.workspaces["project"] = self.config.workspaces["project"].model_copy(
            update={"root": str(remapped)})
        self.old_schema()
        self.assertEqual(self.migrate()["schema"], database.SCHEMA_VERSION)
        with database.connect(self.state) as connection:
            rows = connection.execute("SELECT task_id,root,state FROM writer_reservations").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("structured-one", str(self.work), "owned")])
        with self.assertRaises(Fault) as caught:
            self.app.write_workspace_file("desktop", str(self.work), "foreign.txt", "bad", "", "")
        self.assertEqual(caught.exception.code, "task_resource_conflict")
        self.assertFalse((self.work / "foreign.txt").exists())
        self.assertEqual(self.app.write_workspace_file(
            "desktop", str(remapped), "independent.txt", "free", "", "")["status"], "written")

    def test_historical_two_owners_are_contested_and_unrelated_root_is_free(self):
        self.accept_plan()
        self.app.write_workspace_file(**self.write_arguments())
        # Construct the released writer's valid historical shape: schema 2
        # admitted sequential writers to one target without a durable owner.
        with database.connect(self.state,write=True) as connection:
            connection.execute("DELETE FROM writer_reservations")
        request=task_contract(self.context,"historical-two")
        request["contract"]["tools"]=self.request["contract"]["tools"]
        created=self.app.create_task(**request)["record"]
        record=self.app.control_task(task_id="historical-two",expected_revision=created["revision"],expected_plan_revision=1,
            expected_epoch=0,update_id=uuid.uuid4().hex,transition={"action":"claim","handoff_id":created["handoff"]["id"]})["record"]
        args=self.write_arguments(path="second.txt")
        args["task_context"].update(task_id="historical-two",task_revision=record["revision"],continuation_epoch=record["control"]["epoch"])
        self.app.write_workspace_file(**args)
        before=self.history()
        self.old_schema()
        self.migrate()
        self.assertEqual(self.history(),before)
        with self.assertRaises(Fault) as caught:
            self.app.write_workspace_file("desktop","project","blocked.txt","bad","","")
        self.assertEqual(caught.exception.code,"task_resource_conflict")
        self.assertEqual(caught.exception.evidence["reason"],"historical_contested")
        self.assertEqual(caught.exception.evidence["blocking_task_ids"],["historical-two","structured-one"])
        self.assertFalse((self.work / "blocked.txt").exists())
        other=self.root / "unrelated"
        other.mkdir()
        self.assertEqual(self.app.write_workspace_file("desktop",str(other),"free.txt","free","","")["status"],"written")

    def test_old_schema_activation_reads_settled_history(self):
        self.accept_plan()
        self.app.write_workspace_file(**self.write_arguments())
        self.old_schema()
        self.assertEqual(list(tasks.activation_blockers(self.state)), [])

    def test_old_schema_activation_still_blocks_unknown_original(self):
        from unittest.mock import patch
        self.accept_plan()
        args = self.write_arguments()
        with patch.object(self.app.machines, "write_workspace_file",
                          side_effect=Fault("fixture_unknown", "Lost original result")):
            self.fault("fixture_unknown", self.app.write_workspace_file, **args)
        self.old_schema()
        blocked = list(tasks.activation_blockers(self.state))
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["id"], args["change_id"])

    def test_replaced_historical_root_rejects_cutover_without_rewriting_history(self):
        self.accept_plan()
        self.app.write_workspace_file(**self.write_arguments())
        self.old_schema()
        before=self.history()
        self.work.rename(self.root / "original")
        self.work.mkdir()
        self.fault("task_migration_incompatible",self.migrate)
        self.assertEqual(self.history(),before)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT schema FROM store").fetchone()[0],2)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='writer_reservations'").fetchone())
