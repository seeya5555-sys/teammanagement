import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import app as appmod
import transit_mirror as tm

TODAY = date(2026, 10, 30)

VESSELS = [
    {"name": "PERU PROSPERITY", "latitude": 26.5, "longitude": 56.3, "positionSource": "SVMS",
     "positionLastUpdate": "2026-10-30 09:00", "positionTs": 1793000000, "speed": "12.1",
     "category": "통항중", "condition": "Sailing", "presentLocation": "Hormuz", "nextPort": "AEFJR",
     "eta": "2026-10-31 10:00", "etd": "", "hormuzExit": "10월 30일", "transit48h": "Y", "transitPlan": "PLAN",
     "crewCount": "23", "hsfo": "3657.02", "bonusAmount": "1000", "charterer": "BAHRI",
     "chartererConsent": "secret", "crewConsentAg": "설득 중", "id": 1},
    {"name": "Malta-Prosperity", "latitude": 25.0, "longitude": 55.0, "positionSource": "EMAIL",
     "category": "AG INSIDE"},
    {"name": "OTHER SHIP", "latitude": 1, "longitude": 2, "category": "BBC"},
]
TODAY_PLAN = {"consent": {"byVessel": {"PERUPROSPERITY": {
    "verdict": "보완 필요", "reviewed": True, "reviewedAt": "x", "crew": 23, "signs": 23, "files": 2,
    "issues": 1, "dateUsed": "2026-10-30", "sameDate": True}}}}
REGISTRY = {"ok": True, "registry": {
    "20261029": [{"key": "PERUPROSPERITY", "bound": "W.BOUND", "cps": {"z": {"lt": "old", "z": "", "real": True}}}],
    "20261030": [{"key": "PERUPROSPERITY", "bound": "W.BOUND", "mgr": "SYNERGY",
                  "cps": {"a": {"lt": "30/16:15LT", "z": "(30/12:15Z)", "real": False, "_from": "x"}}},
                 {"key": "OTHERSHIP", "bound": "E.BOUND", "cps": {}}],
}}


def fake_get(path):
    return {"/api/vessels": VESSELS, "/api/transit-plan/today": TODAY_PLAN,
            "/api/transit-plan/registry": REGISTRY}[path]


class TransitMirrorTests(unittest.TestCase):
    def setUp(self):
        tm.reset_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        self.old_key = appmod.app.config["SECRET_KEY"]
        db = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["SECRET_KEY"] = b"transit-mirror-test"
        with appmod.app.app_context():
            appmod.init_db(False)
            sid = appmod.execute("INSERT INTO supervisors(name) VALUES('테스트감독')")
            v1 = appmod.execute("INSERT INTO vessels(name) VALUES('PERU PROSPERITY')")
            v2 = appmod.execute("INSERT INTO vessels(name) VALUES('MALTA PROSPERITY')")
            appmod.execute("INSERT INTO vessels(name) VALUES('OTHER SHIP')")
            appmod.execute("INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)", (sid, v1))
            appmod.execute("INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)", (sid, v2))
            uid = appmod.execute(
                "INSERT INTO users(username,password_hash,supervisor_id,role) VALUES('tm','x',?,'member')", (sid,))
        self.sid, self.uid = sid, uid
        self.client = appmod.app.test_client()

    def tearDown(self):
        tm.reset_cache()
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        appmod.app.config["SECRET_KEY"] = self.old_key
        self.tmp.cleanup()

    def login(self, sup=True):
        with self.client.session_transaction() as s:
            s["user_id"] = self.uid
            s["role"] = "member"
            if sup:
                s["supervisor_id"] = self.sid

    def test_auth_required(self):
        r = self.client.get("/api/transit-mirror")
        self.assertEqual(401, r.status_code)

    def test_assignment_filter_and_whitelist(self):
        self.login()
        with mock.patch.object(tm, "_get_json", side_effect=fake_get), \
                mock.patch.object(tm, "datetime") as dt:
            dt.now.return_value.date.return_value = TODAY
            dt.fromtimestamp.side_effect = __import__("datetime").datetime.fromtimestamp
            r = self.client.get("/api/transit-mirror")
        self.assertEqual(200, r.status_code)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["stale"])
        names = [v["key"] for v in d["vessels"]]
        self.assertEqual(["MALTAPROSPERITY", "PERUPROSPERITY"], names)
        peru = d["vessels"][1]
        for banned in ("crewCount", "hsfo", "bonusAmount", "charterer", "chartererConsent", "crewConsentAg", "id"):
            self.assertNotIn(banned, peru)
        self.assertEqual("통항중", peru["category"])
        self.assertEqual("SVMS", peru["positionSource"])
        self.assertEqual(set(peru["consent"]), set(tm.CONSENT_FIELDS))
        self.assertEqual("보완 필요", peru["consent"]["verdict"])
        self.assertNotIn("crew", peru["consent"])
        self.assertEqual(1, len(peru["checkpoints"]))  # 어제 항목 제외
        cp = peru["checkpoints"][0]
        self.assertEqual("20261030", cp["date"])
        self.assertNotIn("mgr", cp)
        self.assertEqual({"lt": "30/16:15LT", "z": "(30/12:15Z)", "real": False}, cp["cps"]["a"])
        self.assertIsNone(d["vessels"][0]["consent"])

    def test_no_supervisor_link_returns_empty(self):
        self.login(sup=False)
        with mock.patch.object(tm, "_get_json", side_effect=fake_get):
            d = self.client.get("/api/transit-mirror").get_json()
        self.assertEqual([], d["vessels"])

    def test_upstream_down_no_cache_returns_empty_not_500(self):
        self.login()
        with mock.patch.object(tm, "_get_json", side_effect=OSError("down")):
            r = self.client.get("/api/transit-mirror")
        self.assertEqual(200, r.status_code)
        d = r.get_json()
        self.assertFalse(d["ok"])
        self.assertTrue(d["stale"])
        self.assertEqual([], d["vessels"])

    def test_upstream_down_returns_stale_cache(self):
        self.login()
        with mock.patch.object(tm, "_get_json", side_effect=fake_get):
            self.assertEqual(2, len(self.client.get("/api/transit-mirror").get_json()["vessels"]))
        tm._cache["at"] -= tm.TTL_SEC + 1  # TTL 만료
        with mock.patch.object(tm, "_get_json", side_effect=OSError("down")):
            d = self.client.get("/api/transit-mirror").get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["stale"])
        self.assertEqual(2, len(d["vessels"]))

    def test_cache_ttl_avoids_refetch(self):
        with mock.patch.object(tm, "_get_json", side_effect=fake_get) as g:
            tm.get_upstream(now=1000.0)
            tm.get_upstream(now=1000.0 + tm.TTL_SEC - 1)
        self.assertEqual(3, g.call_count)

    def test_base_url_env(self):
        with mock.patch.dict(os.environ, {"TRANSIT_MIRROR_URL": "http://x.test:1/"}):
            self.assertEqual("http://x.test:1", tm.base_url())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("http://158.180.94.133:5000", tm.base_url())

    def test_norm_name(self):
        self.assertEqual("MALTAPROSPERITY", tm.norm_name(" Malta-Prosperity "))


    def _fetch(self, vessels, today=None, registry=None):
        def g(path):
            return {"/api/vessels": vessels, "/api/transit-plan/today": today or TODAY_PLAN,
                    "/api/transit-plan/registry": registry or REGISTRY}[path]
        with mock.patch.object(tm, "_get_json", side_effect=g):
            r = self.client.get("/api/transit-mirror")
        self.assertEqual(200, r.status_code)
        return r.get_json()

    def _assert_no_nested(self, obj, top=True):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("consent", "checkpoints", "cps") or (top and k == "vessels"):
                    self._assert_no_nested(v, top=False)
                elif isinstance(v, dict) and all(isinstance(x, (str, int, float, bool, type(None))) for x in v.values()):
                    continue  # cps 항목(lt/z/real)
                else:
                    self.assertNotIsInstance(v, (dict, list), k)
        elif isinstance(obj, list):
            for x in obj:
                self._assert_no_nested(x, top=False)

    def test_nested_values_dropped(self):
        self.login()
        v = dict(VESSELS[0], transitPlan={"secret": "crew"}, speed=[1, 2], condition={"a": 1})
        today = {"consent": {"byVessel": {"PERUPROSPERITY": {"verdict": {"crew": 23}, "reviewed": [1]}}}}
        reg = {"registry": {"20261030": [{"key": "PERUPROSPERITY", "bound": {"x": 1},
                                          "cps": {"a": {"lt": {"x": 1}, "z": ["y"], "real": {"r": 1}}}}]}}
        with mock.patch.object(tm, "datetime") as dt:
            dt.now.return_value.date.return_value = TODAY
            dt.fromtimestamp.side_effect = __import__("datetime").datetime.fromtimestamp
            d = self._fetch([v], today, reg)
        peru = d["vessels"][0]
        self.assertIsNone(peru["transitPlan"])
        self.assertIsNone(peru["speed"])
        self.assertIsNone(peru["condition"])
        self.assertIsNone(peru["consent"]["verdict"])
        self.assertIsNone(peru["consent"]["reviewed"])
        self.assertEqual("", peru["checkpoints"][0]["bound"])
        self.assertEqual({"lt": None, "z": None, "real": None}, peru["checkpoints"][0]["cps"]["a"])
        self._assert_no_nested(d)

    def test_upstream_name_collision_fail_closed(self):
        self.login()
        d = self._fetch([VESSELS[0], dict(VESSELS[0], name="Peru-Prosperity"), VESSELS[1]])
        self.assertEqual(["MALTAPROSPERITY"], [v["key"] for v in d["vessels"]])

    def test_trmt_name_collision_fail_closed(self):
        self.login()
        with appmod.app.app_context():
            appmod.execute("INSERT INTO vessels(name) VALUES('MALTA-PROSPERITY')")  # 미배정이지만 충돌
        d = self._fetch(VESSELS)
        self.assertEqual(["PERUPROSPERITY"], [v["key"] for v in d["vessels"]])

    def test_db_error_returns_200_empty(self):
        self.login()
        with mock.patch.object(tm, "query", side_effect=RuntimeError("db boom")):
            d = self._fetch(VESSELS)
        self.assertFalse(d["ok"])
        self.assertEqual([], d["vessels"])

    def test_xss_string_passes_as_data(self):
        self.login()
        evil = '<img src=x onerror="alert(1)">&\''
        d = self._fetch([dict(VESSELS[0], presentLocation=evil)])
        self.assertEqual(evil, d["vessels"][0]["presentLocation"])

if __name__ == "__main__":
    unittest.main()
