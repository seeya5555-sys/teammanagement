"""2026-11-10 기능 불변 리팩터 등가성 + 자동화 큐 안전장치 테스트.

· automation_enqueue_once / automation_reuse_or_enqueue: 기존 "조회 후 INSERT" 와 같은 결과
· 무인 적재(ext enqueue)는 verify 만, stage2 는 SOA 선박/그룹만, running 6시간 초과는 failed(재큐잉 없음)
· _att_disk_index: 카드별 스캐너 3종과 같은 인덱스
"""
import os
import tempfile
import unittest

import app as appmod
import helpers_shared as HS
import routes_calendar_dock as R
import routes_dock_submit as RD
from source_bundle import shared_ns  # noqa: E402

H = {"X-API-Key": "secret"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg_db = appmod.app.config["DATABASE"]
        db = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ("secret",))
        self.client = appmod.app.test_client()
        self.old_csrf = appmod.app.config.get("CSRF_PROTECT")
        appmod.app.config["CSRF_PROTECT"] = False     # CSRF 검사 자체는 tests/test_csrf.py 가 본다
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "admin"

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg_db
        appmod.app.config["CSRF_PROTECT"] = self.old_csrf
        self.tmp.cleanup()

    def rows(self, where="1=1"):
        with appmod.app.app_context():
            return [dict(r) for r in appmod.query(
                "SELECT run_id,task,mode,status,requested_by,params,summary FROM automation_run WHERE %s ORDER BY id" % where)]

    def seed(self, run_id, task, status, started="datetime('now','localtime')"):
        with appmod.app.app_context():
            appmod.execute(
                "INSERT INTO automation_run (run_id,task,mode,status,started_at) VALUES (?,?,?,?,%s)" % started,
                (run_id, task, "live", status))


class EnqueueHelperTests(Base):
    def test_enqueue_once_blocks_duplicate(self):
        with appmod.app.app_context():
            a = shared_ns.automation_enqueue_once("jeonja", "verify", "u", '{"k":1}')
            b = shared_ns.automation_enqueue_once("jeonja", "verify", "u")
            other = shared_ns.automation_enqueue_once("aor_submit", "live", "u")
        self.assertRegex(a, r"^[0-9a-f]{12}$")
        self.assertIsNone(b)
        self.assertIsNotNone(other)
        got = self.rows("task='jeonja'")
        self.assertEqual(1, len(got))
        self.assertEqual(("verify", "queued", "u", '{"k":1}'),
                         (got[0]["mode"], got[0]["status"], got[0]["requested_by"], got[0]["params"]))

    def test_enqueue_once_running_blocks_only_when_asked(self):
        self.seed("run000000001", "aor_submit", "running")
        with appmod.app.app_context():
            self.assertIsNone(shared_ns.automation_enqueue_once("aor_submit", "live", "u"))
            fresh = shared_ns.automation_enqueue_once("aor_submit", "live", "u", None, ("queued",))
            self.assertIsNotNone(fresh)
            self.assertIsNone(shared_ns.automation_enqueue_once("aor_submit", "live", "u", None, ("queued",)))

    def test_enqueue_once_rejects_unknown_states(self):
        with appmod.app.app_context():
            with self.assertRaises(KeyError):
                shared_ns.automation_enqueue_once("jeonja", "verify", "u", None, ("done",))

    def test_reuse_or_enqueue_matches_old_contract(self):
        with appmod.app.app_context():
            a = shared_ns.automation_reuse_or_enqueue("reqgen_save", "live", "u", ("queued",))
            b = shared_ns.automation_reuse_or_enqueue("reqgen_save", "live", "u", ("queued",))
            self.assertEqual(a, b)                      # queued 재사용
            appmod.execute("UPDATE automation_run SET status='running' WHERE run_id=?", (a,))
            c = shared_ns.automation_reuse_or_enqueue("reqgen_save", "live", "u", ("queued",))
            self.assertNotEqual(a, c)                   # running 은 재사용 안 함(fresh)
            d = shared_ns.automation_reuse_or_enqueue("reqgen_save", "live", "u", ("queued", "running"))
            self.assertEqual(c, d)                      # 최신(id DESC) 재사용
        self.assertEqual(2, len(self.rows("task='reqgen_save'")))
        self.assertTrue(all(r["mode"] == "live" and r["requested_by"] == "u" for r in self.rows("task='reqgen_save'")))


class EnqueueRaceTests(Base):
    """조회와 적재 사이에 다른 요청이 끼어든 경우(적재 거부)를 강제로 만들어 분기를 확인."""

    def setUp(self):
        super().setUp()
        self._hs = HS.automation_enqueue_once
        self._rd = RD.automation_enqueue_once

    def tearDown(self):
        HS.automation_enqueue_once = self._hs
        RD.automation_enqueue_once = self._rd
        super().tearDown()

    def test_reuse_picks_up_competitor_run(self):
        calls = []

        def refused(task, mode, user, params=None, block_states=("queued", "running")):
            calls.append(task)
            if len(calls) == 1:      # 경쟁 요청이 먼저 넣은 상황
                appmod.execute("INSERT INTO automation_run (run_id,task,mode,status) VALUES ('competitor01',?,'live','queued')", (task,))
            return None
        HS.automation_enqueue_once = refused
        with appmod.app.app_context():
            self.assertEqual("competitor01", HS.automation_reuse_or_enqueue("aor_submit", "live", "u"))
        self.assertEqual(1, len(self.rows()))

    def test_reuse_never_inserts_unconditionally(self):
        HS.automation_enqueue_once = lambda *a, **k: None
        with appmod.app.app_context():
            self.assertIsNone(HS.automation_reuse_or_enqueue("aor_submit", "live", "u", ("queued",)))
        self.assertEqual([], self.rows())

    def test_hub_and_ext_race_responses(self):
        RD.automation_enqueue_once = lambda *a, **k: None
        with appmod.app.app_context():
            t = next(k for k in shared_ns.automation_tasks() if not k.startswith("soa_"))
        r = self.client.post("/api/automation/run", json={"task": t, "mode": "verify"})
        self.assertEqual(409, r.status_code)
        r = self.client.post("/api/ext/automation/enqueue", json={"task": t}, headers=H)
        self.assertEqual((200, True), (r.status_code, r.get_json().get("skipped")))
        self.assertEqual([], self.rows())

    def test_enqueue_respects_explicit_transaction_flag_like_execute(self):
        """execute 와 같은 계약: 호출부가 묶음 transaction 플래그를 켠 동안에는 중간 commit 을 내지 않는다."""
        from flask import g
        with appmod.app.app_context():
            g._reqgen_result_transaction = True
            rid = HS.automation_enqueue_once("jeonja", "verify", "u")
            self.assertIsNotNone(rid)
            appmod.get_db().rollback()
            g._reqgen_result_transaction = False
        self.assertEqual([], self.rows())
        with appmod.app.app_context():       # 플래그 없으면 기존처럼 즉시 commit
            HS.automation_enqueue_once("jeonja", "verify", "u")
            appmod.get_db().rollback()
        self.assertEqual(1, len(self.rows()))


class QueueInvariantTests(Base):
    def task(self):
        with appmod.app.app_context():
            return next(k for k in shared_ns.automation_tasks() if not k.startswith("soa_"))

    def test_ext_enqueue_is_verify_only_and_dedups(self):
        t = self.task()
        for mode in ("live", "stage2", "reject_submit"):
            r = self.client.post("/api/ext/automation/enqueue", json={"task": t, "mode": mode}, headers=H)
            self.assertEqual(403, r.status_code, mode)
        self.assertEqual([], self.rows())
        r = self.client.post("/api/ext/automation/enqueue", json={"task": t}, headers=H)
        self.assertTrue(r.get_json().get("ok"))
        r2 = self.client.post("/api/ext/automation/enqueue", json={"task": t}, headers=H)
        self.assertEqual((200, True), (r2.status_code, r2.get_json().get("skipped")))
        got = self.rows()
        self.assertEqual(1, len(got))
        self.assertEqual(("verify", "queued", "scheduler"), (got[0]["mode"], got[0]["status"], got[0]["requested_by"]))

    def test_hub_run_duplicate_is_409_and_stage2_guard(self):
        t = self.task()
        r = self.client.post("/api/automation/run", json={"task": t, "mode": "verify"})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        r2 = self.client.post("/api/automation/run", json={"task": t, "mode": "verify"})
        self.assertEqual(409, r2.status_code)
        self.assertEqual(1, len(self.rows()))
        r3 = self.client.post("/api/automation/run", json={"task": t, "mode": "stage2"})
        self.assertEqual(400, r3.status_code)

    def test_claim_expires_stuck_running_without_requeue_and_keeps_single_running(self):
        self.seed("stuck0000001", "jeonja", "running", "datetime('now','localtime','-7 hours')")
        self.seed("queued000001", "aor_submit", "queued", "NULL")
        self.seed("queued000002", "aor_reject", "queued", "NULL")
        r = self.client.post("/api/ext/automation/claim", headers=H).get_json()
        self.assertEqual("queued000001", r["run"]["run_id"])
        st = {x["run_id"]: x for x in self.rows()}
        self.assertEqual("failed", st["stuck0000001"]["status"])
        self.assertIn("auto-expired", st["stuck0000001"]["summary"])
        self.assertEqual(3, len(st))                                   # 재큐잉 없음
        self.assertEqual("running", st["queued000001"]["status"])
        r2 = self.client.post("/api/ext/automation/claim", headers=H).get_json()
        self.assertEqual((None, True), (r2["run"], r2.get("busy")))
        self.assertEqual("queued", {x["run_id"]: x for x in self.rows()}["queued000002"]["status"])

    def test_claim_does_not_expire_recent_running(self):
        self.seed("live00000001", "jeonja", "running", "datetime('now','localtime','-5 hours')")
        r = self.client.post("/api/ext/automation/claim", headers=H).get_json()
        self.assertEqual((None, True), (r["run"], r.get("busy")))
        self.assertEqual("running", self.rows()[0]["status"])


class AttachmentIndexEquivalenceTests(unittest.TestCase):
    NAMES = ["1_0.pdf", "1_2.pdf", "1_10.pdf", "1_1.PDF", "1_x.pdf", "1_3.pdf.tmp", "1_.pdf", "1_4", "1.pdf",
             "2_0.png", "2_1.JPG", "2_2.jpeg", "2_3.pdf", "2_4.txt", "02_9.pdf", "12_0.pdf", "12_00.pdf",
             "12_7.pdf", "3_1_2.pdf", "_1.pdf", "x_1.pdf", "21_5.xlsx", "21_6.XLSX", "7.tmp", "9_0.pdf.part",
             "\u00b2_0.pdf", "\u0663_1.pdf", "5_\u0663.pdf", "5_1.pdf", "+6_1.pdf", " 6_2.pdf", "6_ 3.pdf"]

    def test_superscript_digit_name_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("4_\u00b2.pdf", "\u00b2_0.pdf", "4_1.pdf"):
                open(os.path.join(d, n), "wb").close()
            self.assertEqual({4: [1]}, R._att_disk_index(d, ("pdf",)))

    def test_index_matches_per_card_scanners(self):
        with tempfile.TemporaryDirectory() as d:
            for n in self.NAMES:
                open(os.path.join(d, n), "wb").close()
            old = (R.AOR_PDF_DIR, R.FUNDREQ_FILE_DIR, R.INVOICE_PDF_DIR)
            R.AOR_PDF_DIR = R.FUNDREQ_FILE_DIR = R.INVOICE_PDF_DIR = d
            try:
                aor = R._att_disk_index(d, ("pdf",), dedupe=False)
                inv = R._att_disk_index(d, ("pdf",))
                fr = R._att_disk_index(d, R._FUNDREQ_ATT_MIME, ci=True)
                for did in range(0, 30):
                    self.assertEqual(R._aor_pdf_indices(did), aor.get(did, []), did)      # aor 는 중복 보존
                    self.assertEqual(R._invoice_attachment_indices(did), inv.get(did, []), did)
                    self.assertEqual(R._fundreq_att_indices(did), fr.get(did, []), did)
                self.assertEqual([0, 2, 10], aor[1])
                self.assertEqual([0, 0, 7], aor[12])
                self.assertEqual([0, 7], inv[12])
            finally:
                R.AOR_PDF_DIR, R.FUNDREQ_FILE_DIR, R.INVOICE_PDF_DIR = old

    def test_missing_dir_is_empty(self):
        self.assertEqual({}, R._att_disk_index("/nonexistent/trmt-x", ("pdf",)))


if __name__ == "__main__":
    unittest.main()
