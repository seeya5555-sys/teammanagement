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

    def test_leave_allowance_and_quarter_day_counting(self):
        calendar_service.set_leave_allowance(2026, 2, 15.0, "admin", 2.25)
        for leave_type, date in (
            ("annual", "2026-01-10"), ("half", "2026-02-10"),
            ("quarter", "2026-03-10"), ("annual", "2027-01-10"),
        ):
            calendar_service.create_event({
                "title": leave_type, "start_date": date,
                "supervisor_id": 2, "leave_type": leave_type,
            }, "admin", {"blue"})

        summary = calendar_service.leave_summary(2026, 2)
        self.assertEqual(15.0, summary["allowance"])
        self.assertEqual(1.75, summary["calendar_used"])
        self.assertEqual(2.25, summary["manual_used"])
        self.assertEqual(4.0, summary["used"])
        self.assertEqual(11.0, summary["remaining"])
        self.assertEqual({"annual": 1, "half": 1, "quarter": 1}, summary["counts"])

    def test_init_db_migrates_legacy_leave_allowance_table(self):
        appmod.execute("DROP TABLE calendar_leave_allowances")
        appmod.execute("""
            CREATE TABLE calendar_leave_allowances (
                supervisor_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                days REAL NOT NULL,
                updated_by TEXT,
                updated_at TEXT,
                PRIMARY KEY (supervisor_id, year)
            )
        """)
        appmod.execute("""
            INSERT INTO calendar_leave_allowances
                (supervisor_id, year, days, updated_by)
            VALUES (2, 2026, 17, 'legacy')
        """)

        appmod.init_db(False)

        columns = {
            row["name"] for row in appmod.query(
                "PRAGMA table_info(calendar_leave_allowances)"
            )
        }
        self.assertIn("manual_used", columns)
        summary = calendar_service.leave_summary(2026, 2)
        self.assertEqual(17, summary["allowance"])
        self.assertEqual(0, summary["manual_used"])

    def test_leave_validation_rejects_invalid_type_range_and_multi_day(self):
        for payload, message in (
            ({"leave_type": "hour", "supervisor_id": 2}, "leave_type"),
            ({"leave_type": "annual"}, "담당 감독"),
            ({"leave_type": "annual", "supervisor_id": 2,
              "start_date": "2026-01-01", "end_date": "2026-01-02"}, "하루 단위"),
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                calendar_service.CalendarInputError, message
            ):
                calendar_service.validate_event_payload(payload)
        with self.assertRaisesRegex(calendar_service.CalendarInputError, "0.25일 단위"):
            calendar_service.set_leave_allowance(2026, 2, 10.1, "admin")

    def test_leave_summary_http_contract_and_scope(self):
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            sess.update(user_id=1, username="sup2", role="user", supervisor_id=2)
        token = client.get("/api/csrf-token").get_json()["token"]
        saved = client.put("/api/cal/leave-summary", json={
            "year": 2026, "supervisor_id": 2, "days": 12.25,
            "manual_used": 3.5,
        }, headers={"X-CSRF-Token": token})
        self.assertEqual(200, saved.status_code)
        self.assertEqual(12.25, saved.get_json()["allowance"])
        self.assertEqual(3.5, saved.get_json()["manual_used"])
        self.assertEqual(3.5, saved.get_json()["used"])
        legacy_saved = client.put("/api/cal/leave-summary", json={
            "year": 2026, "supervisor_id": 2, "days": 13,
        }, headers={"X-CSRF-Token": token})
        self.assertEqual(3.5, legacy_saved.get_json()["manual_used"])
        saved = legacy_saved
        loaded = client.get("/api/cal/leave-summary?year=2026&supervisor_id=2")
        self.assertEqual(saved.get_json(), loaded.get_json())
        self.assertEqual(403, client.get(
            "/api/cal/leave-summary?year=2026&supervisor_id=3"
        ).status_code)

        event_id = calendar_service.create_event({
            "title": "Annual", "start_date": "2026-06-01",
            "supervisor_id": 2, "leave_type": "annual",
        }, "sup2", {"blue"})
        bypass = client.put(f"/api/cal/events/{event_id}", json={
            "end_date": "2026-06-02",
        }, headers={"X-CSRF-Token": token})
        self.assertEqual(400, bypass.status_code)
        self.assertIn("하루 단위", bypass.get_json()["error"])

        with self.assertRaisesRegex(calendar_service.CalendarInputError, "존재하지 않는"):
            calendar_service.set_leave_allowance(2026, 999, 12, "sup2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
