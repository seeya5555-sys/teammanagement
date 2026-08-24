"""Calendar service extraction contract tests."""

import os
import tempfile
import unittest

import app as appmod
import calendar_service


class CalendarServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        database = os.path.join(self.tmp.name, "calendar.db")
        appmod.DATABASE = database
        appmod.app.config["DATABASE"] = database
        with appmod.app.app_context():
            appmod.init_db(False)
        self.context = appmod.app.app_context()
        self.context.push()
        appmod.execute(
            "INSERT OR IGNORE INTO supervisors (id, name) VALUES (2,'Sup 2'),(3,'Sup 3')"
        )

    def tearDown(self):
        self.context.pop()
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        self.tmp.cleanup()

    def test_crud_normalization_and_empty_update_are_preserved(self):
        event_id = calendar_service.create_event({
            "title": "Engine call",
            "start_date": "2026-08-25",
            "color": "NOT-A-COLOR",
            "completed": True,
        }, "admin", {"blue", "red"})

        event = calendar_service.get_event(event_id)
        self.assertEqual("blue", event["color"])
        self.assertEqual(1, event["all_day"])
        self.assertEqual(1, event["completed"])
        self.assertEqual("manual", event["source_type"])
        self.assertEqual("admin", event["created_by"])
        self.assertFalse(calendar_service.update_event(event_id, {}, {"blue", "red"}))
        self.assertTrue(calendar_service.update_event(
            event_id, {"location": "", "completed": False}, {"blue", "red"},
        ))
        event = calendar_service.get_event(event_id)
        self.assertIsNone(event["location"])
        self.assertEqual(0, event["completed"])

        calendar_service.delete_event(event_id)
        self.assertIsNone(calendar_service.get_event(event_id))

    def test_overlap_scope_order_and_source_lookup_are_preserved(self):
        public_id = calendar_service.create_event({
            "title": "Public multi-day", "start_date": "2026-08-20",
            "end_date": "2026-08-27", "source_type": "issue", "source_id": 44,
        }, "admin", {"blue"})
        calendar_service.create_event({
            "title": "Supervisor 2", "start_date": "2026-08-25", "supervisor_id": 2,
        }, "admin", {"blue"})
        calendar_service.create_event({
            "title": "Supervisor 3", "start_date": "2026-08-25", "supervisor_id": 3,
        }, "admin", {"blue"})

        rows = calendar_service.list_events("2026-08-25", "2026-08-25", "2")
        self.assertEqual(["Public multi-day", "Supervisor 2"], [r["title"] for r in rows])
        self.assertEqual(public_id, calendar_service.find_event("issue", 44)["id"])
        self.assertIsNone(calendar_service.find_event("", 44))

    def test_invalid_scope_and_empty_required_fields_are_rejected(self):
        with self.assertRaisesRegex(
            calendar_service.CalendarInputError,
            "supervisor_id 는 정수 또는 all 이어야 합니다",
        ):
            calendar_service.list_events(supervisor_id="not-a-number")

        for payload, message in (
            ({"title": 0, "start_date": "2026-08-25"}, "title 이 필요합니다"),
            ({"title": "Event", "start_date": []}, "start_date 가 필요합니다"),
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                calendar_service.CalendarInputError, message
            ):
                calendar_service.validate_event_payload(payload, creating=True)

        # POST historically treats an explicit empty color like omission and
        # applies blue; only PUT's empty color used to create SQL NULL.
        calendar_service.validate_event_payload({
            "title": "Whitespace stays compatible",
            "start_date": " ",
            "color": "",
        }, creating=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
