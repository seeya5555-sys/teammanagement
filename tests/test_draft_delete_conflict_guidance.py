"""단건 draft 삭제 409 안내문구 계약 — "존재하지 않는 조치"를 시키지 않는다.

배경(2026-08-15, 페이블 사후검증 non-blocking #3)
------------------------------------------------
`02b7308` 이 단건 삭제를 조건부 DELETE 로 바꾸면서 409 본문에 **"먼저 취소(pending
복귀)하세요"** 한 줄을 고정으로 넣었다. 그런데 실제로 되돌릴 수 있는 경로는 표마다 다르다.

  · `fundreq`/`invoice` : `/reset` 이 `approved`·`rejecting` → `pending` 을 지원한다.
  · `aor`               : **reset 이 아예 없다.** `unhold` 는 `hold`→`pending` 전용인데
                          `hold` 는 애초에 삭제 가능 상태라 409 에 오지 않는다.
  · `submitting` / `reject_submitting` : 러너가 SVMS 를 건드리는 중이라 어느 표에서도
                          되돌릴 수단이 없다.

즉 구 문구는 aor 의 4개 보호상태 전부와, fundreq/invoice 의 in-flight 2개 상태에서
사용자에게 **없는 버튼을 누르라고** 안내하고 있었다. 기능 자체(fail-closed)는 옳았고
문구만 틀렸지만, 돈경로에서 "취소하면 지울 수 있다"는 오안내는 사용자가 러너 완료를
기다리는 대신 헛수고를 반복하게 만든다.

두 층으로 본다
  ① 런타임: 표 × 보호상태 전 조합에서 409 와 status 를 실제로 확인하고, 안내문구가
     그 상태에서 **실행 가능한** 조치만 말하는지 본다.
  ② 정적: `_DRAFT_RESETTABLE` 이 실제 `/reset` 라우트의 SQL allowlist 와 일치하는지.
     나중에 aor 에 reset 이 생기거나 reset 의 허용 상태가 바뀌면 문구가 조용히 stale
     해지는데, 런타임 테스트만으로는 "문구가 보수적으로 틀린" 그 드리프트를 못 잡는다.
"""
import ast
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod
import routes_calendar_dock as rcd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 러너가 집어가기 전/집어간 뒤 — 단건 삭제가 막혀야 하는 상태 전부.
PROTECTED = ("approved", "rejecting", "submitting", "reject_submitting")
INFLIGHT = ("submitting", "reject_submitting")

# (표, 삭제 URL 템플릿, 최소 INSERT)
TABLES = {
    "aor_draft": (
        "/api/aor/drafts/{did}",
        "INSERT INTO aor_draft(aor_cd, status) VALUES(?,?)",
    ),
    "fundreq_draft": (
        "/api/fundreq/drafts/{did}",
        "INSERT INTO fundreq_draft(opex_cd, status) VALUES(?,?)",
    ),
    "invoice_draft": (
        "/api/invoice/drafts/{did}",
        "INSERT INTO invoice_draft(inv_cd, status, raw_card) VALUES(?,?,'{}')",
    ),
}


class DraftDeleteConflictGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        self.old_testing = appmod.app.config.get("TESTING")
        db = os.path.join(self.tmp.name, "conflict.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="guide-admin", display_name="Guide Admin",
                           role="admin", supervisor_id=None)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        appmod.app.config["TESTING"] = self.old_testing
        self.tmp.cleanup()

    def _insert(self, table, status):
        sql = TABLES[table][1]
        with appmod.app.app_context():
            if table == "invoice_draft":
                return appmod.execute(sql, (f"KEY-{status}-{os.urandom(3).hex()}", status))
            return appmod.execute(sql, (f"KEY-{status}-{os.urandom(3).hex()}", status))

    # ── ① 런타임 ────────────────────────────────────────────────
    def test_protected_states_are_refused_with_actionable_guidance(self):
        for table, (url_tpl, _) in TABLES.items():
            for status in PROTECTED:
                with self.subTest(table=table, status=status):
                    did = self._insert(table, status)
                    response = self.client.delete(url_tpl.format(did=did))
                    self.assertEqual(409, response.status_code)
                    body = response.get_json()
                    self.assertEqual(status, body["status"])
                    message = body["error"]

                    resettable = status in rcd._DRAFT_RESETTABLE.get(table, ())
                    if resettable:
                        self.assertIn("결정 취소", message)
                    else:
                        # 되돌릴 수단이 없는 상태에 취소를 시키면 안 된다.
                        self.assertNotIn("취소", message)
                        self.assertIn("삭제하세요", message)
                    if status in INFLIGHT:
                        self.assertIn("실행 중", message)

                    # 문구가 어떻든 행은 살아 있어야 한다(fail-closed 회귀 방지).
                    with appmod.app.app_context():
                        row = appmod.query(f"SELECT status FROM {table} WHERE id=?",
                                           (did,), one=True)
                    self.assertIsNotNone(row)
                    self.assertEqual(status, row["status"])

    def test_aor_is_never_told_to_cancel_because_it_has_no_reset(self):
        # aor 는 표 자체가 reset 을 안 가진다 — 매핑에 실수로 들어오면 즉시 깨진다.
        self.assertNotIn("aor_draft", rcd._DRAFT_RESETTABLE)
        rules = {str(r) for r in appmod.app.url_map.iter_rules()}
        self.assertNotIn("/api/aor/drafts/<int:did>/reset", rules)

    def test_deletable_states_still_delete(self):
        # 안내문구 분기를 넣다가 삭제 자체를 막아버리는 회귀를 잡는다.
        for table, (url_tpl, _) in TABLES.items():
            for status in ("pending", "hold", "submitted", "rejected", "failed", "reject_failed"):
                with self.subTest(table=table, status=status):
                    did = self._insert(table, status)
                    response = self.client.delete(url_tpl.format(did=did))
                    self.assertEqual(200, response.status_code)

    def test_missing_row_is_404_not_409(self):
        for table, (url_tpl, _) in TABLES.items():
            with self.subTest(table=table):
                response = self.client.delete(url_tpl.format(did=999_999))
                self.assertEqual(404, response.status_code)

    # ── ② 정적: 매핑이 실제 reset 라우트와 일치하는가 ──────────────
    def test_resettable_map_matches_reset_endpoint_sql(self):
        """`_DRAFT_RESETTABLE` 은 `/reset` 의 status allowlist 에서 파생된 사실이어야 한다.

        reset 의 허용 상태가 바뀌었는데 안내문구 매핑이 그대로면, 사용자는 되지도 않는
        [결정 취소]를 안내받거나(과대) 되는데도 못 한다고 안내받는다(과소).
        """
        source = open(os.path.join(ROOT, "routes_calendar_dock.py"), encoding="utf-8").read()
        tree = ast.parse(source)
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.endswith("_reset"):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or getattr(call.func, "id", None) != "execute_rc":
                    continue
                sql = call.args[0]
                if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                    continue
                match = re.search(r"UPDATE\s+(\w+)\s+SET\s+status='pending'", sql.value)
                allow = re.search(r"status\s+IN\s*\(([^)]*)\)", sql.value)
                if match and allow:
                    found[match.group(1)] = tuple(
                        sorted(s.strip().strip("'") for s in allow.group(1).split(","))
                    )
        self.assertTrue(found, "reset 라우트의 UPDATE 를 하나도 못 찾았다 — 파서가 stale 하다")
        declared = {t: tuple(sorted(v)) for t, v in rcd._DRAFT_RESETTABLE.items()}
        self.assertEqual(found, declared)

    def test_inflight_states_are_exactly_the_undeletable_runner_locks(self):
        # 삭제 허용 목록 + 보호 목록이 상태 전집합을 덮는지 — 새 상태가 생기면
        # 어느 분기에도 안 걸려 "러너 실행 대기" 라는 틀린 기본 문구가 나간다.
        deletable = {s.strip().strip("'") for s in rcd._DRAFT_DELETABLE_SQL.split(",")}
        self.assertEqual(set(), deletable & set(PROTECTED))
        self.assertEqual(set(INFLIGHT), set(rcd._DRAFT_INFLIGHT_STATUSES))


if __name__ == "__main__":
    unittest.main()
