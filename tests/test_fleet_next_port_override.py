import json
import os
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns


class FleetNextPortOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.app.config["DATABASE"]
        self.old_secret_key = appmod.app.config["SECRET_KEY"]
        self.old_locode_files = shared_ns.FLEET_LOCODE_FILES
        self.old_locode_name_files = shared_ns.FLEET_LOCODE_NAME_FILES
        self.old_country_map_files = shared_ns.FLEET_COUNTRY_MAP_FILES
        self.old_label_files = shared_ns.FLEET_LOCODE_LABEL_FILES
        self.old_catalog_cache = shared_ns._fleet_port_catalog_cache
        self.old_fleet_map_file = shared_ns.FLEET_MAP_FILE
        appmod.app.config["DATABASE"] = os.path.join(self.tmp.name, "test.db")
        # Production entry points call init_runtime() before serving.  This test
        # swaps only the DB, so provide an explicit test key instead of relying
        # on the now-forbidden import-time random key.
        appmod.app.config["SECRET_KEY"] = b"fleet-next-port-test-key"
        shared_ns.FLEET_MAP_FILE = os.path.join(self.tmp.name, "fleet_map.json")
        self.ctx = appmod.app.app_context()
        self.ctx.push()

    def tearDown(self):
        appmod.close_db()
        self.ctx.pop()
        appmod.app.config["DATABASE"] = self.old_db
        appmod.app.config["SECRET_KEY"] = self.old_secret_key
        shared_ns.FLEET_LOCODE_FILES = self.old_locode_files
        shared_ns.FLEET_LOCODE_NAME_FILES = self.old_locode_name_files
        shared_ns.FLEET_COUNTRY_MAP_FILES = self.old_country_map_files
        shared_ns.FLEET_LOCODE_LABEL_FILES = self.old_label_files
        shared_ns._fleet_port_catalog_cache = self.old_catalog_cache
        shared_ns.FLEET_MAP_FILE = self.old_fleet_map_file
        self.tmp.cleanup()

    def test_explicit_dzalg_code_resolves_to_algiers_not_ohio(self):
        vessel = {
            "name": "ATLANTIC GENEVA",
            "lat": 1.0,
            "lng": 2.0,
            "next_port": {"name": "ALGER (ALGIERS), ALGERIA", "cd": "DZALG", "xy": [40.7, -83.8333]},
            "route_legs": [[[1.0, 2.0], [40.7, -83.8333]]],
        }
        shared_ns._fleet_apply_code_first_next_port(vessel)
        self.assertEqual(vessel["next_port"]["name"], "ALGER (ALGIERS), ALGERIA")
        self.assertEqual(vessel["next_port"]["xy"], [36.75, 3.05])
        self.assertEqual(vessel["route_legs"], [[[1.0, 2.0], [36.75, 3.05]]])

    def test_code_extraction_skips_malformed_candidate(self):
        vessel = {"next_port": {"cd": "not a code", "code": "DZALG"}}
        self.assertEqual(shared_ns._fleet_extract_next_port_code(vessel), "DZALG")

    def test_code_first_builds_direct_route_when_route_legs_empty(self):
        vessel = {
            "name": "Ship A",
            "lat": 1.0,
            "lng": 2.0,
            "next_port": {"name": "Algiers", "cd": "DZALG", "xy": [40.7, -83.8333]},
            "route_legs": [],
        }
        shared_ns._fleet_apply_code_first_next_port(vessel)
        self.assertEqual(vessel["route_legs"], [[[1.0, 2.0], [36.75, 3.05]]])

    def test_code_first_preserves_route_detail_and_replaces_terminal_point(self):
        vessel = {
            "name": "Ship A",
            "lat": 1.0,
            "lng": 2.0,
            "next_port": {"name": "Algiers", "cd": "DZALG", "xy": [40.7, -83.8333]},
            "route_legs": [[[1.0, 2.0], [4.0, 5.0], [40.7, -83.8333]]],
        }
        shared_ns._fleet_apply_code_first_next_port(vessel)
        self.assertEqual(vessel["route_legs"], [[[1.0, 2.0], [4.0, 5.0], [36.75, 3.05]]])

    def test_esalg_and_algeciras_manual_resolution_use_packaged_catalog(self):
        missing = os.path.join(self.tmp.name, "missing")
        pkg = shared_ns.FLEET_MAP_PACKAGED_DIR
        shared_ns.FLEET_LOCODE_FILES = (os.path.join(pkg, "locode.json"), os.path.join(missing, "locode.json"))
        shared_ns.FLEET_LOCODE_NAME_FILES = (os.path.join(pkg, "locode_name.json"), os.path.join(missing, "locode_name.json"))
        shared_ns.FLEET_COUNTRY_MAP_FILES = (os.path.join(pkg, "country_map.json"), os.path.join(missing, "country_map.json"))
        shared_ns.FLEET_LOCODE_LABEL_FILES = (os.path.join(pkg, "locode_labels.json"),)
        shared_ns._fleet_port_catalog_cache = None
        by_code, err = shared_ns._fleet_resolve_port_input("ESALG")
        self.assertIsNone(err)
        self.assertEqual(by_code, {"label": "Algeciras", "code": "ESALG", "xy": [36.1275, -5.4533]})
        by_name, err = shared_ns._fleet_resolve_port_input("Algeciras")
        self.assertIsNone(err)
        self.assertEqual(by_name["label"], "Algeciras")
        self.assertEqual(by_name["xy"], [36.1275, -5.4533])

    def test_country_qualified_unknown_country_rejected(self):
        resolved, err = shared_ns._fleet_resolve_port_input("Busan, Neverland")
        self.assertIsNone(resolved)
        self.assertEqual(err, "unknown country")

    def test_catalog_rejects_non_finite_and_out_of_bounds_coordinates(self):
        locode_file = os.path.join(self.tmp.name, "locode.json")
        name_file = os.path.join(self.tmp.name, "locode_name.json")
        country_file = os.path.join(self.tmp.name, "country_map.json")
        label_file = os.path.join(self.tmp.name, "labels.json")
        with open(locode_file, "w", encoding="utf-8") as f:
            json.dump({"XXNAN": [float("nan"), 1], "XXINF": [float("inf"), 1], "XXBAD": [91, 1],
                       "XXOK": [1, 2]}, f)
        with open(name_file, "w", encoding="utf-8") as f:
            json.dump({"by": {"XX|NANPORT": [float("nan"), 1], "XX|BADPORT": [1, 181],
                              "XX|OKPORT": [3, 4]}, "glob": {}}, f)
        with open(country_file, "w", encoding="utf-8") as f:
            json.dump({"XCOUNTRY": "XX"}, f)
        with open(label_file, "w", encoding="utf-8") as f:
            json.dump({"XXOK": "Ok Port"}, f)
        shared_ns.FLEET_LOCODE_FILES = (locode_file,)
        shared_ns.FLEET_LOCODE_NAME_FILES = (name_file,)
        shared_ns.FLEET_COUNTRY_MAP_FILES = (country_file,)
        shared_ns.FLEET_LOCODE_LABEL_FILES = (label_file,)
        shared_ns._fleet_port_catalog_cache = None
        cat = shared_ns._fleet_port_catalog()
        self.assertNotIn("XXNAN", cat["locodes"])
        self.assertNotIn("XXINF", cat["locodes"])
        self.assertNotIn("XXBAD", cat["locodes"])
        self.assertEqual(cat["locodes"]["XXOK"], [1.0, 2.0])
        self.assertNotIn("NANPORT", cat["by_name"])
        self.assertNotIn("BADPORT", cat["by_name"])
        self.assertIn("OKPORT", cat["by_name"])

    def test_same_normalized_auto_identity_keeps_override(self):
        vessel = {
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Algiers", "cd": "DZALG", "xy": [36.75, 3.05]},
        }
        auto_id = shared_ns._fleet_auto_next_port_identity(vessel)
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, auto_id),
        )
        shared_ns._fleet_apply_manual_next_port_overrides([vessel])
        self.assertTrue(vessel["next_port"]["manual"])
        self.assertEqual(vessel["next_port"]["name"], "Busan")
        self.assertEqual(vessel["route_legs"], [[[10.0, 20.0], [35.1333, 129.05]]])

    def test_manual_override_preserves_route_detail_and_replaces_terminal_point(self):
        vessel = {
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Algiers", "cd": "DZALG", "xy": [36.75, 3.05]},
            "route_legs": [[[10.0, 20.0], [15.0, 25.0], [36.75, 3.05]]],
        }
        auto_id = shared_ns._fleet_auto_next_port_identity(vessel)
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, auto_id),
        )
        shared_ns._fleet_apply_manual_next_port_overrides([vessel])
        self.assertEqual(vessel["route_legs"], [[[10.0, 20.0], [15.0, 25.0], [35.1333, 129.05]]])

    def test_get_style_manual_apply_does_not_create_override_table(self):
        vessel = {
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Algiers", "cd": "DZALG", "xy": [36.75, 3.05]},
        }
        shared_ns._fleet_apply_manual_next_port_overrides([vessel], ensure_schema=False)
        row = appmod.query("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_next_port_override'",
                           one=True)
        self.assertIsNone(row)

    def test_changed_auto_identity_ignores_override_and_keeps_db_row(self):
        vessel = {
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Yokohama", "cd": "JPYOK", "xy": [35.45, 139.65]},
        }
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, "CODE:DZALG"),
        )
        shared_ns._fleet_apply_manual_next_port_overrides([vessel])
        self.assertNotIn("manual", vessel["next_port"])
        row = appmod.query("SELECT vessel_key FROM fleet_next_port_override WHERE vessel_key=?", ("ship a",), one=True)
        self.assertIsNotNone(row)

    def test_missing_auto_identity_ignores_override_and_keeps_db_row(self):
        vessel = {
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {},
        }
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, "CODE:DZALG"),
        )
        shared_ns._fleet_apply_manual_next_port_overrides([vessel])
        self.assertNotIn("manual", vessel["next_port"])
        row = appmod.query("SELECT vessel_key FROM fleet_next_port_override WHERE vessel_key=?", ("ship a",), one=True)
        self.assertIsNotNone(row)

    def test_push_invalidation_is_one_way_and_does_not_reactivate(self):
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, "CODE:DZALG"),
        )
        invalidated = shared_ns._fleet_invalidate_next_port_overrides_from_push([{
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Yokohama", "cd": "JPYOK", "xy": [35.45, 139.65]},
        }])
        self.assertEqual(invalidated, 1)
        row = appmod.query("SELECT active, inactivated_reason FROM fleet_next_port_override WHERE vessel_key=?",
                           ("ship a",), one=True)
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["inactivated_reason"], "auto identity changed")

        invalidated = shared_ns._fleet_invalidate_next_port_overrides_from_push([{
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Algiers", "cd": "DZALG", "xy": [36.75, 3.05]},
        }])
        self.assertEqual(invalidated, 0)
        vessel = {
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Algiers", "cd": "DZALG", "xy": [36.75, 3.05]},
        }
        shared_ns._fleet_apply_manual_next_port_overrides([vessel])
        self.assertNotIn("manual", vessel["next_port"])

    def test_external_push_route_invalidates_active_override(self):
        shared_ns._ensure_api_table()
        appmod.execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)", ("secret",))
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, "CODE:DZALG"),
        )
        payload = {
            "generated_at": "2026-07-14 10:00",
            "fleet": [{
                "name": "Ship A",
                "lat": 10.0,
                "lng": 20.0,
                "next_port": {"name": "Yokohama", "cd": "JPYOK", "xy": [35.45, 139.65]},
            }],
        }
        res = appmod.app.test_client().post(
            "/api/ext/fleet-map/push",
            json=payload,
            headers={"X-API-Key": "secret"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["next_port_overrides_invalidated"], 1)
        row = appmod.query("SELECT active FROM fleet_next_port_override WHERE vessel_key=?", ("ship a",), one=True)
        self.assertEqual(row["active"], 0)

    def test_push_missing_auto_identity_inactivates_override(self):
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, "CODE:DZALG"),
        )
        invalidated = shared_ns._fleet_invalidate_next_port_overrides_from_push([{
            "name": "Ship A",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {},
        }])
        self.assertEqual(invalidated, 1)
        row = appmod.query("SELECT active, inactivated_reason FROM fleet_next_port_override WHERE vessel_key=?",
                           ("ship a",), one=True)
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["inactivated_reason"], "auto identity missing")

    def test_partial_push_does_not_inactivate_absent_vessel_override(self):
        shared_ns._ensure_fleet_next_port_override_table()
        appmod.execute(
            "INSERT INTO fleet_next_port_override "
            "(vessel_key, vessel_name, manual_label, manual_lat, manual_lng, auto_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ship a", "Ship A", "Busan", 35.1333, 129.05, "CODE:DZALG"),
        )
        invalidated = shared_ns._fleet_invalidate_next_port_overrides_from_push([{
            "name": "Ship B",
            "lat": 10.0,
            "lng": 20.0,
            "next_port": {"name": "Yokohama", "cd": "JPYOK", "xy": [35.45, 139.65]},
        }])
        self.assertEqual(invalidated, 0)
        row = appmod.query("SELECT active, inactivated_at FROM fleet_next_port_override WHERE vessel_key=?",
                           ("ship a",), one=True)
        self.assertEqual(row["active"], 1)
        self.assertIsNone(row["inactivated_at"])

    def test_invalid_manual_input_is_rejected(self):
        resolved, err = shared_ns._fleet_resolve_port_input("Definitely Not A Real Port")
        self.assertIsNone(resolved)
        self.assertIsNotNone(err)

    def test_unassigned_logged_in_user_cannot_write_manual_override(self):
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 99
            sess["username"] = "unassigned"
            sess["role"] = "user"
        # 계정은 실제로 있어야 한다 — `login_required` 가 요청마다 users 를 재확인하므로,
        # 없는 uid 면 담당 배정 검사(403)에 닿기 전에 401 로 끊겨 이 테스트의 의도가 사라진다.
        appmod.execute(
            "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, "
            "password_hash TEXT, display_name TEXT, supervisor_id INTEGER, "
            "role TEXT DEFAULT 'member', active INTEGER NOT NULL DEFAULT 1)")
        appmod.execute(
            "INSERT OR IGNORE INTO users (id,username,password_hash,role,active) "
            "VALUES (99,'unassigned','x','member',1)")
        res = client.post("/api/fleet-map/next-port-override", json={})
        self.assertEqual(res.status_code, 403)
        res = client.delete("/api/fleet-map/next-port-override", json={})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
