import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from flask import Flask, g, session

import drydock_integration as integration


class DockRosterVesselTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.trmt_path = os.path.join(self.temp.name, "trmt.db")
        self.dock_path = os.path.join(self.temp.name, "fleet.db")

        trmt = sqlite3.connect(self.trmt_path)
        trmt.executescript("""
            CREATE TABLE vessels (
                id INTEGER PRIMARY KEY, name TEXT, vessel_type TEXT, imo TEXT,
                class_society TEXT, active INTEGER
            );
            CREATE TABLE supervisor_vessels (supervisor_id INTEGER, vessel_id INTEGER);
            CREATE TABLE dock_reports (
                id INTEGER PRIMARY KEY, vessel_id INTEGER, gross_tonnage TEXT,
                dead_weight TEXT, updated_at TEXT
            );
            INSERT INTO vessels VALUES(1,'MY VESSEL','VLCC','9123456','BV',1);
            INSERT INTO vessels VALUES(2,'OTHER VESSEL','CNTR','9234567','DNV',1);
            INSERT INTO vessels VALUES(3,'INACTIVE','VLCC','9345678','KR',0);
            INSERT INTO supervisor_vessels VALUES(7,1);
            INSERT INTO supervisor_vessels VALUES(8,2);
            INSERT INTO supervisor_vessels VALUES(7,3);
            INSERT INTO dock_reports VALUES(1,1,'154379','299533','2026-01-01');
        """)
        trmt.commit()
        trmt.close()

        dock = sqlite3.connect(self.dock_path)
        dock.executescript("""
            CREATE TABLE vessels (
                id TEXT PRIMARY KEY, name TEXT, type TEXT, imo TEXT, shipyard TEXT,
                class_society TEXT, berthing_date TEXT, dock_in TEXT, dock_out TEXT,
                departure_date TEXT, duration INTEGER, grt TEXT, dc_rate REAL DEFAULT 0
            );
        """)
        dock.commit()
        dock.close()

        self.dd_app = Flask("dock_roster_test")
        self.dd_app.secret_key = "test"
        self.trmt_app = Flask("trmt_roster_test")
        self.trmt_app.config["DATABASE"] = self.trmt_path

        def get_db():
            if "dock_db" not in g:
                g.dock_db = sqlite3.connect(self.dock_path)
                g.dock_db.row_factory = sqlite3.Row
            return g.dock_db

        def one(sql, value):
            return get_db().execute(sql, (value,)).fetchone()

        def to_vessel(record):
            return {
                "id": record["id"], "name": record["name"], "type": record["type"],
                "imo": record["imo"], "shipyard": record["shipyard"],
                "classSociety": record["class_society"], "grt": record["grt"],
            }

        self.dd = SimpleNamespace(get_db=get_db, row=one, to_vessel=to_vessel)
        integration._install_roster_vessels_endpoints(self.dd, self.trmt_app, self.dd_app)

        @self.dd_app.teardown_appcontext
        def close_db(_error=None):
            db = g.pop("dock_db", None)
            if db is not None:
                db.close()

        self.client = self.dd_app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self, supervisor_id=7):
        with self.client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["role"] = "admin"
            if supervisor_id is not None:
                sess["supervisor_id"] = supervisor_id

    def test_list_is_assigned_active_roster_with_saved_particulars(self):
        self.login()
        response = self.client.get(integration.ROSTER_VESSELS_PATH)
        self.assertEqual(200, response.status_code)
        self.assertEqual([{
            "id": 1, "name": "MY VESSEL", "type": "VLCC", "imo": "9123456",
            "classSociety": "BV", "grtDwt": "GRT 154379 / DWT 299533",
            "grossTonnage": "154379", "deadWeight": "299533",
        }], response.get_json())

    def test_create_rejects_other_roster_and_ignores_spoofed_particulars(self):
        self.login()
        denied = self.client.post(integration.ROSTER_VESSELS_PATH, json={"trmtVesselId": 2})
        self.assertEqual(403, denied.status_code)

        created = self.client.post(integration.ROSTER_VESSELS_PATH, json={
            "trmtVesselId": 1, "name": "SPOOF", "type": "OTHER", "imo": "0",
            "classSociety": "ABS", "shipyard": "YARD", "dockIn": "2026-08-25",
        })
        self.assertEqual(201, created.status_code)
        body = created.get_json()
        self.assertEqual("MY VESSEL", body["name"])
        self.assertEqual("VLCC", body["type"])
        self.assertEqual("9123456", body["imo"])
        self.assertEqual("BV", body["classSociety"])
        self.assertEqual("GRT 154379 / DWT 299533", body["grt"])
        self.assertEqual("YARD", body["shipyard"])

    def test_missing_supervisor_has_no_roster(self):
        self.login(supervisor_id=None)
        self.assertEqual([], self.client.get(integration.ROSTER_VESSELS_PATH).get_json())


if __name__ == "__main__":
    unittest.main()
