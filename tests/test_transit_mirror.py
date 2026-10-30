import os
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
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


def fake_get(path, timeout=None):
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
        def g(path, timeout=None):
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
    # ── 통항 모니터링 / transit_today ─────────────────────────────
    def _bundle(self):
        return {
            "daily": {"date": "20261030", "notes": ["메모1", {"nested": 1}, "  "],
                      "rows": [{"vessel": "PERU PROSPERITY", "bound": "INBOUND", "mgr": "CSM",
                                "consent": {"crew": 23, "signs": 23, "files": 2, "verdict": "이상 없음",
                                            "reviewed": True, "names": ["secret crew"]},
                                "cps": [{"key": "z", "lt": "30/11:45LT", "real": True, "t": "x"},
                                        {"key": "a", "lt": "30/17:45LT", "real": False}]}]},
            "prepare": {"basis": "기준 30/06:00LT", "basisKst": "2026-10-30 11:00 KST", "stamp": "20261030-0600",
                        "rows": []},
            "registry": {"20261030": [{"vessel": "MALTA PROSPERITY", "key": "MALTAPROSPERITY",
                                       "bound": "E.BOUND", "mgr": "FLEET",
                                       "cps": {"a": {"iso": "20261030", "hhmm": "1145", "lt": "30/11:45LT",
                                                     "real": True, "_from": "등록"}}}],
                         "20261031": [{"vessel": "X", "key": "X"}]},
            "record": {"rows": [{"vessel": "SPAIN B", "startIso": "20261030", "done": True, "bound": "E.BOUND",
                                 "status": "완료", "cps": [{"key": "z", "iso": "20261030", "hhmm": "0130",
                                                           "lt": "30 / 01:30", "real": True}]}]},
            "consent": {"byVessel": {"MALTAPROSPERITY": {"crew": 16, "signs": 15, "verdict": "보완 필요",
                                                         "reviewed": True, "issueText": "secret"}}},
        }

    def _mon_get(self, path, bundle=None, fail=False):
        b = bundle or self._bundle()

        def g(p, timeout=None):
            if fail:
                raise OSError("down")
            if p.startswith("/api/transit-plan/today"):
                return b
            if p.startswith("/api/transit-plan/dates"):
                return {"dates": ["20261029", "20261030", "bad"]}
            if p == "/api/vessels":
                return VESSELS
            return {}
        with mock.patch.object(tm, "_get_json", side_effect=g):
            return self.client.get(path)

    def test_monitor_auth_required(self):
        self.assertEqual(401, self.client.get("/api/transit-mirror/monitor").status_code)

    def test_monitor_bad_date(self):
        self.login()
        for bad in ("2026-10-30", "20261340", "abc", "2026103"):
            r = self.client.get("/api/transit-mirror/monitor?date=" + bad)
            self.assertEqual(400, r.status_code, bad)

    def test_monitor_upstream_down_no_500(self):
        self.login()
        r = self._mon_get("/api/transit-mirror/monitor?date=20261030", fail=True)
        self.assertEqual(200, r.status_code)
        d = r.get_json()
        self.assertFalse(d["ok"])
        self.assertEqual([], d["rows"])

    def test_monitor_internal_error_no_500(self):
        self.login()
        with mock.patch.object(tm, "build_monitor", side_effect=RuntimeError("x")):
            r = self._mon_get("/api/transit-mirror/monitor?date=20261030")
        self.assertEqual(200, r.status_code)
        self.assertFalse(r.get_json()["ok"])

    def test_monitor_shape_all_vessels_and_whitelist(self):
        self.login(sup=False)   # 전체 선박 뷰 — 배정 무관
        d = self._mon_get("/api/transit-mirror/monitor?date=20261030").get_json()
        self.assertTrue(d["ok"])
        names = {r["key"]: r for r in d["rows"]}
        self.assertEqual({"PERUPROSPERITY", "MALTAPROSPERITY", "SPAINB"}, set(names))
        peru, malta, spain = names["PERUPROSPERITY"], names["MALTAPROSPERITY"], names["SPAINB"]
        self.assertEqual("INBOUND", peru["bound"])
        self.assertEqual("OUTBOUND", malta["bound"])
        self.assertTrue(malta["registered"])
        self.assertFalse(peru["registered"])
        self.assertEqual("30/11:45", peru["cps"]["z"]["lt"])
        self.assertTrue(peru["cps"]["z"]["real"])
        # 걸프 LT 11:45 = 07:45Z
        self.assertEqual(datetime(2026, 10, 30, 7, 45, tzinfo=timezone.utc).timestamp(), peru["cps"]["z"]["utc"])
        self.assertEqual(datetime(2026, 10, 30, 7, 45, tzinfo=timezone.utc).timestamp(), malta["cps"]["a"]["utc"])
        self.assertNotIn("_from", malta["cps"]["a"])
        self.assertEqual(set(peru["consent"]), {"crew", "signs", "files", "issues", "reviewed", "reviewedAt",
                                                "verdict", "sameDate", "dateUsed"})
        self.assertNotIn("issueText", malta["consent"])
        self.assertEqual(16, malta["consent"]["crew"])
        self.assertTrue(spain["rec"]["done"])
        self.assertEqual(["메모1"], d["memos"])
        self.assertEqual("기준 30/06:00LT", d["basis"]["basis"])
        dates = [x["date"] for x in d["dates"]]
        self.assertIn("20261029", dates)
        self.assertIn("20261031", dates)
        self.assertNotIn("bad", dates)
        tab = [x for x in d["dates"] if x["date"] == "20261030"][0]
        self.assertEqual(3, tab["count"])
        self.assertIn("transiting", peru)
        self.assertEqual("SVMS", peru["report"]["source"])
        self.assertNotIn("names", str(d))
        self.assertNotIn("secret", str(d))

    def test_monitor_nested_values_never_returned(self):
        self.login()
        b = self._bundle()
        b["daily"]["rows"][0]["mgr"] = {"x": 1}
        b["daily"]["rows"][0]["cps"][0]["lt"] = {"x": 1}
        b["prepare"]["basis"] = ["x"]
        d = self._mon_get("/api/transit-mirror/monitor?date=20261030", bundle=b).get_json()
        peru = [r for r in d["rows"] if r["key"] == "PERUPROSPERITY"][0]
        self.assertEqual("", peru["mgr"])
        self.assertEqual("", d["basis"]["basis"])

    def test_transit_today_keys(self):
        b = self._bundle()
        keys = tm.transit_today_keys({"today": b, "registry": b["registry"]}, TODAY)
        self.assertEqual({"PERUPROSPERITY", "MALTAPROSPERITY"}, keys)
        self.assertEqual(set(), tm.transit_today_keys({"today": b}, date(2026, 11, 2)))

    def test_transit_today_flag_in_mirror_api(self):
        self.login()
        b = self._bundle()

        def g(p, timeout=None):
            return {"/api/vessels": VESSELS, "/api/transit-plan/today": b,
                    "/api/transit-plan/registry": {"registry": b["registry"]}}[p]
        with mock.patch.object(tm, "_get_json", side_effect=g), mock.patch.object(tm, "datetime") as dt:
            dt.now.return_value.date.return_value = TODAY
            dt.now.return_value.strftime.return_value = "20261030"
            dt.fromtimestamp.side_effect = datetime.fromtimestamp
            d = self.client.get("/api/transit-mirror").get_json()
        flags = {v["key"]: v["transit_today"] for v in d["vessels"]}
        self.assertEqual({"PERUPROSPERITY": True, "MALTAPROSPERITY": True}, flags)

    # ── 동시성: single-flight · 세대 토큰 · 잠금 밖 I/O ──────────────
    def test_single_background_refresh_under_concurrency(self):
        import threading as th
        calls = {"n": 0}
        lk = th.Lock()
        gate = th.Event()

        def g(p, timeout=None):
            with lk:
                calls["n"] += 1
            gate.wait(2)
            return fake_get(p)
        with mock.patch.object(tm, "_get_json", side_effect=g):
            ts = [th.Thread(target=tm.cached_transit_today_keys) for _ in range(20)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            time.sleep(0.2)
            gate.set()
            deadline = time.time() + 3
            while tm._warm["running"] and time.time() < deadline:
                time.sleep(0.02)
        self.assertFalse(tm._warm["running"])
        self.assertEqual(3, calls["n"])   # 전체 번들 1회분(vessels+today+registry)만

    def test_concurrent_get_upstream_single_flight(self):
        import threading as th
        calls = {"n": 0}
        lk = th.Lock()

        def g(p, timeout=None):
            with lk:
                calls["n"] += 1
            time.sleep(0.2)
            return fake_get(p)
        res = []
        with mock.patch.object(tm, "_get_json", side_effect=g):
            ts = [th.Thread(target=lambda: res.append(tm.get_upstream())) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(3, calls["n"])
        self.assertTrue(all(r[0] is not None for r in res))

    def test_reset_during_inflight_does_not_repopulate(self):
        import threading as th
        started, release = th.Event(), th.Event()

        def g(p, timeout=None):
            started.set()
            release.wait(2)
            return fake_get(p)
        out = {}
        with mock.patch.object(tm, "_get_json", side_effect=g):
            t = th.Thread(target=lambda: out.setdefault("r", tm.get_upstream()))
            t.start()
            self.assertTrue(started.wait(2))
            tm.reset_cache()
            release.set()
            t.join(3)
        self.assertIsNone(tm._cache["data"])
        self.assertEqual(0.0, tm._cache["at"])

    def test_cache_read_not_blocked_by_slow_monitor_fetch(self):
        import threading as th
        tm._cache["data"] = {"vessels": VESSELS, "today": {}, "registry": {}}
        tm._cache["at"] = time.time()
        started = th.Event()

        def slow(p, timeout=None):
            started.set()
            time.sleep(2)
            return {}
        with mock.patch.object(tm, "_get_json", side_effect=slow):
            t = th.Thread(target=lambda: tm.get_monitor_bundle("20261030"))
            t.start()
            self.assertTrue(started.wait(2))
            t0 = time.monotonic()
            vs = tm.get_vessels_for_fleet()
            keys = tm.cached_transit_today_keys()
            data, stale, _ = tm.get_upstream()
            el = time.monotonic() - t0
            t.join(5)
        self.assertLess(el, 0.3)
        self.assertEqual(VESSELS, vs)
        self.assertEqual(set(), keys)
        self.assertFalse(stale)


if __name__ == "__main__":
    unittest.main()
