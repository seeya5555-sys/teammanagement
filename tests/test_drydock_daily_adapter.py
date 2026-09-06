import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import drydock_integration as integration


SOURCE_TABLES = integration.DAILY_EVENT_SOURCES


def make_db(path):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, vessel_id TEXT, number TEXT,
            section TEXT, category TEXT, description TEXT, vendor TEXT,
            remarks TEXT, updated_at TEXT);
        CREATE TABLE discussions (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            date TEXT, time_of_day TEXT, item TEXT, description TEXT,
            actions TEXT, priority TEXT, updated_at TEXT);
        CREATE TABLE class_items (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            finding TEXT, description TEXT, actions TEXT, responsible TEXT,
            priority TEXT, updated_at TEXT);
        CREATE TABLE steel_repair (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            description TEXT, location TEXT, priority TEXT, status TEXT,
            start_date TEXT, completion_date TEXT, remark TEXT, last_updated TEXT);
        CREATE TABLE pipe_repair (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            description TEXT, system_line TEXT, position_tank TEXT, priority TEXT,
            status TEXT, start_date TEXT, completion_date TEXT, remark TEXT,
            last_updated TEXT);
        CREATE TABLE outfitting (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            description TEXT, location TEXT, priority TEXT, status TEXT,
            start_date TEXT, completion_date TEXT, remark TEXT, last_updated TEXT);
        CREATE TABLE wbt_cot (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            tank_name TEXT, manhole_status TEXT, open_date TEXT, close_date TEXT,
            bottom_plug_open TEXT, bottom_plug_close TEXT, remark TEXT,
            updated_at TEXT);
        CREATE TABLE portable_fan (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            location TEXT, qty TEXT, start_date TEXT, stop_date TEXT,
            remark TEXT, updated_at TEXT);
        CREATE TABLE staging (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            location TEXT, staging_area TEXT, qty TEXT, remark TEXT,
            updated_at TEXT);
        CREATE TABLE gas_free (id INTEGER PRIMARY KEY, vessel_id TEXT, no TEXT,
            tank TEXT, certificate TEXT, date TEXT, remark TEXT, updated_at TEXT);
        """
    )
    day = "2026-08-20"
    db.execute("INSERT INTO jobs VALUES (1,'v_DM17','J-1','STEEL','Shipyard','Hull work','',?,?)",
               (json.dumps([{"date": day, "progress": "50%", "important": True}], ensure_ascii=False),
                "2026-08-20T08:00:00+09:00"))
    db.execute("INSERT INTO discussions VALUES (1,'v_DM17','D-1',?,?,?,?,?,?,?)",
               (day, "Morning", "Vendor delivery", "Arrived",
                json.dumps([{"date": day, "progress": "Received", "important": False}]),
                "Normal", "2026-08-20T09:00:00+09:00"))
    db.execute("INSERT INTO class_items VALUES (1,'v_DM17','C-1',?,?,?,?,?,?)",
               ("Class finding", "Details",
                json.dumps([{"date": day, "progress": "Survey booked", "important": True}]),
                "Shipyard", "Critical", "2026-08-20T10:00:00+09:00"))
    updated = "2026-08-20T11:00:00+09:00"
    db.execute("INSERT INTO steel_repair VALUES (1,'v_DM17','S-1','Steel plate','Tank','Urgent','Open',?,?,?,?)",
               (day, None, "remark", updated))
    db.execute("INSERT INTO pipe_repair VALUES (1,'v_DM17','P-1','Pipe spool','Ballast','WBT','Normal','Open',?,?,?,?)",
               (day, None, "remark", updated))
    db.execute("INSERT INTO outfitting VALUES (1,'v_DM17','O-1','Expansion joint','FR 65','Normal','Open',?,?,?,?)",
               (day, None, "remark", updated))
    db.execute("INSERT INTO wbt_cot VALUES (1,'v_DM17','W-1','Tank 1','Open',?,?,?,?,?,?)",
               (day, None, None, None, "remark", updated))
    db.execute("INSERT INTO portable_fan VALUES (1,'v_DM17','F-1','Tank 1','2',?,?,?,?)",
               (day, None, "remark", updated))
    db.execute("INSERT INTO staging VALUES (1,'v_DM17','G-1','FR 10','Area','1','remark',?)",
               (updated,))
    db.execute("INSERT INTO gas_free VALUES (1,'v_DM17','G-1','Tank 1','Yes',?,?,?)",
               (day, "remark", updated))
    # An event from another project and one from another day must not leak.
    db.execute("INSERT INTO jobs VALUES (2,'v_DM18','J-2','GENERAL','Shipyard','Other','',?,?)",
               (json.dumps([{"date": "2026-08-20", "progress": "leak"}]), updated))
    db.execute("INSERT INTO jobs VALUES (3,'v_DM17','J-3','GENERAL','Shipyard','Old','',?,?)",
               (json.dumps([{"date": "2026-08-19", "progress": "old"}]), updated))
    db.commit()
    db.close()


class DockDailyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "fleet.db")
        make_db(self.db_path)
        self.dd_app = Flask("fake_dock_manager")
        self.dd_app.secret_key = "dock-secret"
        self.dd_app.config["DATABASE"] = self.db_path
        self.dd = SimpleNamespace(
            app=self.dd_app,
            DATABASE=self.db_path,
            get_db=lambda: sqlite3.connect(self.db_path),
        )
        @self.dd_app.route("/api/vessels", methods=["POST"])
        def legacy_create_vessel():
            return {"legacy": True}, 201
        @self.dd_app.route("/api/test-write", methods=["POST"])
        def test_write():
            return {"ok": True}, 200
        self.trmt_app = Flask("fake_trmt")
        self.trmt_app.secret_key = "trmt-secret"
        self.trmt_db_path = os.path.join(self.temp.name, "trmt.db")
        trmt_db = sqlite3.connect(self.trmt_db_path)
        trmt_db.execute("CREATE TABLE users(username TEXT PRIMARY KEY, role TEXT, active INTEGER, app_scope TEXT)")
        trmt_db.execute("INSERT INTO users VALUES('admin','admin',1,'business')")
        trmt_db.execute("INSERT INTO users VALUES('viewer','viewer',1,'business')")
        trmt_db.commit(); trmt_db.close()
        self.trmt_app.config['DATABASE'] = self.trmt_db_path
        with patch("helpers_shared._check_api_key", return_value=True):
            integration.apply(self.dd, self.trmt_app)
        self.client = self.dd_app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_get_returns_all_sources_with_filters_identity_hash_and_timezone(self):
        with patch("helpers_shared._check_api_key", return_value=True):
            response = self.client.get(
                "/api/integration/daily-events?project_ids=v_DM17&date=2026-08-20",
                headers={"X-API-Key": "key"},
            )
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload["complete"])
        self.assertEqual(list(SOURCE_TABLES), payload["complete_sources"])
        self.assertEqual({"v_DM17"}, {event["source_project_id"] for event in payload["events"]})
        self.assertEqual({
            "jobs", "discussions", "class_items", "steel_repair", "pipe_repair",
            "outfitting", "wbt_cot", "portable_fan", "staging", "gas_free",
        }, {event["source_table"] for event in payload["events"]})
        for event in payload["events"]:
            self.assertEqual("2026-08-20", event["date"])
            self.assertRegex(event["source_subkey"], r"[A-Za-z_]+:[^:]+:.+")
            self.assertRegex(event["source_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(event["source_updated_at"], r"(?:Z|[+-]\d\d:\d\d)$")
            self.assertEqual(event["source_hash"], integration._canonical_hash(event))
        self.assertTrue(any(event["kind"] == "job_remark" for event in payload["events"]))
        self.assertTrue(all(
            event["source_subkey"].count(":") >= 4
            for event in payload["events"] if event["kind"] == "job_remark"
        ))
        self.assertTrue(any(event["kind"] == "discussion_action" for event in payload["events"]))
        self.assertTrue(any(event["suggested_section"] == "survey" for event in payload["events"]))

    def test_repeated_calls_are_deterministic_and_do_not_write_database(self):
        probe = sqlite3.connect(self.db_path)
        before = probe.execute("PRAGMA data_version").fetchone()[0]
        probe.close()
        with patch("helpers_shared._check_api_key", return_value=True):
            first = self.client.get("/api/integration/daily-events?project_ids=v_DM17&date=2026-08-20").get_data()
            second = self.client.get("/api/integration/daily-events?project_ids=v_DM17&date=2026-08-20").get_data()
        probe = sqlite3.connect(self.db_path)
        after = probe.execute("PRAGMA data_version").fetchone()[0]
        probe.close()
        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_api_key_bypass_is_exact_get_endpoint_only(self):
        with patch("helpers_shared._check_api_key", return_value=True):
            self.assertEqual(200, self.client.get("/api/integration/daily-events?project_ids=v_DM17&date=2026-08-20").status_code)
            self.assertEqual(401, self.client.get("/api/integration/daily-events/?project_ids=v_DM17&date=2026-08-20").status_code)
            self.assertEqual(401, self.client.post("/api/integration/daily-events?project_ids=v_DM17&date=2026-08-20").status_code)
            self.assertEqual(401, self.client.get("/api/other", headers={"X-API-Key": "key"}).status_code)
        with patch("helpers_shared._check_api_key", return_value=False):
            self.assertEqual(401, self.client.get("/api/integration/daily-events?project_ids=v_DM17&date=2026-08-20").status_code)

    def test_invalid_query_and_missing_source_fail_closed(self):
        with patch("helpers_shared._check_api_key", return_value=True):
            self.assertEqual(400, self.client.get("/api/integration/daily-events?project_ids=v_DM17&date=2026-8-20").status_code)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE gas_free")
        conn.commit()
        conn.close()
        with patch("helpers_shared._check_api_key", return_value=True):
            response = self.client.get("/api/integration/daily-events?project_ids=v_DM17&date=2026-08-20")
        self.assertEqual(503, response.status_code)
        self.assertFalse(response.get_json()["complete"])

    def test_admin_gate_blocks_legacy_freeform_create_and_viewer_direct_post(self):
        with self.client.session_transaction() as sess:
            sess.update({"username": "admin", "role": "admin", "supervisor_id": 7})
        legacy = self.client.post("/api/vessels", json={"name": "FREEFORM"})
        self.assertEqual(403, legacy.status_code)
        same_origin = self.client.post("/api/vessels", json={"name": "FREEFORM"},
                                       headers={"Origin": "https://vslmanager.duckdns.org"})
        self.assertEqual(410, same_origin.status_code)
        self.assertEqual(403, self.client.post('/api/test-write', json={}).status_code)
        self.assertEqual(200, self.client.post('/api/test-write', json={},
                         headers={'Origin': 'https://vslmanager.duckdns.org'}).status_code)
        self.assertEqual(403, self.client.post('/api/test-write', json={},
                         headers={'Origin': 'https://attacker.invalid'}).status_code)
        cross = self.client.post("/api/vessels", json={"name": "FREEFORM"},
                                 headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(403, cross.status_code)
        for bad in ('null', 'https://vslmanager.duckdns.org.attacker.invalid',
                    'http://vslmanager.duckdns.org', 'https://user@vslmanager.duckdns.org'):
            response = self.client.post("/api/vessels", json={"name": "FREEFORM"},
                                        headers={"Origin": bad})
            self.assertEqual(403, response.status_code, bad)

        with self.client.session_transaction() as sess:
            sess.update({"username": "viewer", "role": "viewer", "supervisor_id": 7})
        direct = self.client.post(integration.ROSTER_VESSELS_PATH, json={"trmtVesselId": 1})
        self.assertEqual(401, direct.status_code)


if __name__ == "__main__":
    unittest.main()
