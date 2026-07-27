"""GET /api/ext/aor/reingest-statuses — prep 엔진 skip 판정용 읽기전용 목록.

배경(2026-07-27): prep 이 skip 목록을 `/api/aor/drafts?status=all` 에서 가져갔는데 그 라우트는
`@admin_required`(세션쿠키 전용)라 X-API-Key 로는 항상 401 이었고, 클라이언트가 비-200 을
`set()` 으로 삼켜 skip 최적화가 처음부터 죽어 있었다. 이 엔드포인트가 그 대체다.
"""
import ast
import os
import re
import sqlite3
import sys
import tempfile
import unittest

# 직접 실행(`python3 tests/test_aor_statuses.py`)도 되게 한다 — 안 되면 아래 구조 가드
# (main guard 뒤 클래스 검사)가 검증할 대상이 없어 무의미해진다(올마이트 R11).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod

ACTIVE_LOCKING = "/api/ext/aor/approved"   # 대조군: 조회하면서 락 거는 기존 엔드포인트
URL = "/api/ext/aor/reingest-statuses"


class AORStatusesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.app.config["DATABASE"]
        self.old_database_constant = appmod.DATABASE
        test_db = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = test_db
        appmod.app.config["DATABASE"] = test_db
        # 첨부 preview 부수효과를 검사하려면 실제 instance/aor_pdfs 를 건드리면 안 된다 — 임시로 격리.
        self.old_pdf_dir = appmod.AOR_PDF_DIR
        appmod.AOR_PDF_DIR = os.path.join(self.tmp.name, "aor_pdfs")
        os.makedirs(appmod.AOR_PDF_DIR, exist_ok=True)
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod._ensure_api_table()
            appmod.execute(
                "INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)",
                ("secret",),
            )

    def tearDown(self):
        appmod.app.config["DATABASE"] = self.old_db
        appmod.DATABASE = self.old_database_constant
        appmod.AOR_PDF_DIR = self.old_pdf_dir
        self.tmp.cleanup()

    # ---- helpers ----
    def _insert(self, aor_cd, status, **cols):
        keys = ["aor_cd", "status"] + list(cols)
        vals = [aor_cd, status] + list(cols.values())
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            cur = db.execute(
                f"INSERT INTO aor_draft ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                vals,
            )
            db.commit()
            return cur.lastrowid

    def _get(self, key="secret"):
        headers = {"X-API-Key": key} if key is not None else {}
        with appmod.app.test_client() as client:
            return client.get(URL, headers=headers)

    def _statuses(self):
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            return sorted(db.execute("SELECT id, status FROM aor_draft").fetchall())

    # ---- auth ----
    def test_requires_api_key(self):
        self._insert("ATGRCA2607220003", "approved")
        self.assertEqual(401, self._get(key=None).status_code)
        self.assertEqual(401, self._get(key="wrong").status_code)
        self.assertEqual(200, self._get().status_code)

    # ---- payload 최소화 ----
    def test_returns_only_aor_cd_and_status(self):
        self._insert("ATGRCA2607220003", "approved", vsl_nm="ATLANTIC GREEN", amt=1411,
                     proposed_comment="민감한 결재 코멘트", raw_row='{"secret":1}')
        body = self._get().get_json()
        self.assertEqual(1, body["count"])
        self.assertEqual([{"aor_cd": "ATGRCA2607220003", "status": "approved"}], body["drafts"])
        # 금액·코멘트·원본행이 절대 새어나가지 않아야 한다
        self.assertNotIn("1411", str(body))
        self.assertNotIn("민감한", str(body))
        self.assertNotIn("secret", str(body))

    # ---- 읽기전용 ----
    def test_does_not_mutate_status(self):
        self._insert("ATGRCA2607220003", "approved")
        self._insert("BGBBCA2607230002", "pending")
        before = self._statuses()
        self.assertEqual(200, self._get().status_code)
        self.assertEqual(before, self._statuses())

    def test_unlike_approved_endpoint_it_does_not_lock(self):
        """대조: /api/ext/aor/approved 는 같은 행을 submitting 으로 락한다."""
        self._insert("ATGRCA2607220003", "approved")
        self.assertEqual(200, self._get().status_code)
        self.assertEqual(["approved"], [s for _, s in self._statuses()])
        with appmod.app.test_client() as client:
            client.get(ACTIVE_LOCKING, headers={"X-API-Key": "secret"})
        self.assertEqual(["submitting"], [s for _, s in self._statuses()])

    # ---- aor_cd 당 1행(최신) ----
    def test_one_row_per_aor_cd_latest_wins(self):
        """종료행(rejected)은 같은 aor_cd 로 남을 수 있다 — 그 잔재가 skip 판정을 오염시키면 안 된다.

        active 상태군에는 partial unique index 가 걸려 있어 활성행은 원래 1개뿐이다.
        """
        self._insert("ATGRCA2607220003", "rejected")
        self._insert("ATGRCA2607220003", "pending")
        body = self._get().get_json()
        self.assertEqual(1, body["count"])
        self.assertEqual([{"aor_cd": "ATGRCA2607220003", "status": "pending"}], body["drafts"])

    def test_stale_approved_history_does_not_mask_new_pending(self):
        """옛 approved 가 종료 처리되고(duplicate) 새 pending 이 생긴 경우 pending 이 보여야 한다."""
        self._insert("ATGRCA2607220003", "duplicate")
        self._insert("ATGRCA2607220003", "pending")
        drafts = self._get().get_json()["drafts"]
        self.assertEqual([{"aor_cd": "ATGRCA2607220003", "status": "pending"}], drafts)

    # ---- 필터링하지 않음(판정은 클라이언트) ----
    def test_returns_all_statuses_including_pending_and_hold(self):
        for cd, st in [("A2607220001", "pending"), ("B2607220002", "hold"),
                       ("C2607220003", "approved"), ("D2607220004", "submitting"),
                       ("E2607220005", "submitted"), ("F2607220006", "rejecting"),
                       ("G2607220007", "failed")]:
            self._insert(cd, st)
        got = {d["aor_cd"]: d["status"] for d in self._get().get_json()["drafts"]}
        self.assertEqual(
            {"A2607220001": "pending", "B2607220002": "hold", "C2607220003": "approved",
             "D2607220004": "submitting", "E2607220005": "submitted",
             "F2607220006": "rejecting", "G2607220007": "failed"},
            got,
        )

    def _index_predicate(self):
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            row = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                             ("uq_aor_draft_active_cd",)).fetchone()
        self.assertIsNotNone(row, "partial unique index 가 아예 없음 — skip 안전성 근거가 무너짐")
        m = re.search(r"WHERE\s+status\s+IN\s*\((.*?)\)", row[0], re.S | re.I)
        self.assertIsNotNone(m, row[0])
        # ⛔ `strip("'\"")` 로 뭉개면 안 된다(올마이트 R10) — 그건 우리가 고친 바로 그 버그라,
        #    테스트가 같은 실수를 하면 predicate 커버리지 검사가 거짓 PASS 한다.
        #    프로덕션과 **같은 해독기**로 읽는다.
        vals = set()
        for tok in m.group(1).split(","):
            v = appmod._sql_literal_value(tok)
            self.assertIsNotNone(v, f"predicate 항목 해독 실패: {tok!r} / {row[0]}")
            vals.add(v)
        return vals

    def test_index_predicate_covers_every_noop_status_and_pending_hold(self):
        """false-skip 불가능성의 **근거 자체**를 검사한다(올마이트 R5 blocker).

        skip 이 안전한 이유는 "skip 상태 행이 최신이면 그게 유일한 active 행"이기 때문이고,
        그건 index predicate 가 skip 상태 전부 + pending/hold 를 **모두** 포함할 때만 성립한다.
        누가 predicate 에서 상태를 하나 빼면 그 순간 false-skip 이 가능해지므로 여기서 깨뜨린다.
        """
        pred = self._index_predicate()
        missing = (set(appmod.AOR_REINGEST_NOOP_STATUSES) | {"pending", "hold"}) - pred
        self.assertEqual(set(), missing,
                         f"index predicate 에서 빠진 상태={sorted(missing)} — false-skip 가능해짐")

    def test_no_noop_status_can_coexist_with_pending_or_hold(self):
        """skip 상태 × {pending, hold} **전 조합**이 DB 레벨에서 금지되는지 확인.

        이게 성립해야 "MAX(id) 가 skip 상태 = 유일 active 행" 이라는 추론이 참이 된다.
        (기존엔 approved+pending 한 조합만 봤음 — 올마이트 R5)
        """
        for i, noop in enumerate(appmod.AOR_REINGEST_NOOP_STATUSES):
            for k, other in enumerate(("pending", "hold")):
                for order in (0, 1):
                    cd = f"X{i}{k}{order}607220001"
                    first, second = (noop, other) if order == 0 else (other, noop)
                    self._insert(cd, first)
                    with self.assertRaises(sqlite3.IntegrityError,
                                           msg=f"{first} + {second} 가 공존 가능함"):
                        self._insert(cd, second)

    def test_all_noop_status_pairs_are_mutually_exclusive(self):
        """skip 상태끼리도 같은 aor_cd 로 둘 다 active 일 수 없어야 한다."""
        noops = list(appmod.AOR_REINGEST_NOOP_STATUSES)
        for i, a in enumerate(noops):
            for j, b in enumerate(noops):
                if i >= j:
                    continue
                cd = f"Y{i}{j}607220001"
                self._insert(cd, a)
                with self.assertRaises(sqlite3.IntegrityError, msg=f"{a} + {b} 공존"):
                    self._insert(cd, b)

    def test_partial_unique_index_forbids_two_active_rows(self):
        """R1-1 반박의 근거를 테스트로 고정.

        `uq_aor_draft_active_cd` 가 active 상태군에 걸려 있어 approved + pending 공존이
        DB 레벨에서 불가능하다. 이게 깨지면 skip 오탐이 실제로 가능해지므로 즉시 실패시킨다.
        """
        self._insert("ATGRCA2607220003", "approved")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert("ATGRCA2607220003", "pending")
        # 종료행은 공존 가능해야 한다(그래서 MAX(id) 가 필요)
        self._insert("ATGRCA2607220003", "rejected")

    def test_ingest_canonicalizes_aor_cd_so_variants_cannot_split(self):
        """대소문자·공백 변형이 별개 active 행으로 갈라지면 skip 증명이 깨진다(올마이트 R6).

        unique index 는 exact match 라 DB 스스로는 변형을 막지 못한다. 막아주는 건
        **API 경계의 `strip().upper()`** 이므로, 그 canonicalization 을 여기서 고정한다.
        """
        self._insert("ATGRCA2607220003", "approved")
        for variant in ("  atgrca2607220003  ", "AtGrCa2607220003", "atgrca2607220003\t"):
            with appmod.app.test_client() as client:
                r = client.post("/api/ext/aor/drafts",
                                json={"aor_cd": variant, "proposed_comment": "변형 적재"},
                                headers={"X-API-Key": "secret"})
            self.assertEqual(200, r.status_code, variant)
            self.assertTrue(r.get_json().get("dedup"),
                            f"변형 {variant!r} 이 별개 행으로 새로 들어감 — skip 증명 붕괴")
        # 여전히 행은 1개여야 한다
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM aor_draft").fetchone()[0])
        # 조회 결과도 정규형 1건
        self.assertEqual([{"aor_cd": "ATGRCA2607220003", "status": "approved"}],
                         self._get().get_json()["drafts"])

    def test_terminal_row_with_higher_id_only_causes_extra_work_not_false_skip(self):
        """MAX(id) 가 "최신 생성"이라 생기는 위험의 **방향**을 고정한다.

        active 행보다 나중에 만들어진 종료행이 있으면 MAX(id) 는 종료행을 집는다 →
        skip 안 함 → 불필요 재처리(낭비). 반대 방향(재처리 필요한데 skip)은 나오지 않는다.
        """
        self._insert("ATGRCA2607220003", "approved")   # active, 낮은 id
        self._insert("ATGRCA2607220003", "duplicate")  # terminal, 높은 id
        drafts = self._get().get_json()["drafts"]
        self.assertEqual([{"aor_cd": "ATGRCA2607220003", "status": "duplicate"}], drafts)

    def test_status_changed_in_place_is_reflected(self):
        """상태가 새 행이 아니라 in-place UPDATE 로 바뀌는 경로도 즉시 반영되어야 한다."""
        did = self._insert("ATGRCA2607220003", "approved")
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("UPDATE aor_draft SET status='failed' WHERE id=?", (did,))
            db.commit()
        self.assertEqual([{"aor_cd": "ATGRCA2607220003", "status": "failed"}],
                         self._get().get_json()["drafts"])

    def _full_row(self, cd):
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.row_factory = sqlite3.Row
            r = db.execute("SELECT * FROM aor_draft WHERE aor_cd=?", (cd,)).fetchone()
            return dict(r) if r else None

    def _snapshot(self):
        """테이블 **전체** + preview 디렉터리 전체를 찍는다.

        단일 행만 비교하면 "새 행이 추가됐다"·"엉뚱한 preview 가 생겼다"를 놓친다(올마이트 R5).
        """
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("SELECT * FROM aor_draft ORDER BY id")]
        files = {}
        for name in sorted(os.listdir(appmod.AOR_PDF_DIR)):
            with open(os.path.join(appmod.AOR_PDF_DIR, name), "rb") as fh:
                files[name] = fh.read()
        return rows, files

    def _reingest(self, cd):
        # attach_files 를 실제로 실어보낸다 — 첨부 입력이 없는 요청으로는 "첨부 부수효과 없음"이
        # 증명되지 않는다는 지적(올마이트 R4) 반영.
        with appmod.app.test_client() as client:
            return client.post(
                "/api/ext/aor/drafts",
                json={"aor_cd": cd, "proposed_comment": "덮어쓰기 시도", "vsl_nm": "다른배",
                      "amt": 99999, "subj": "다른건", "raw_row": {"x": 1},
                      "attach_files": [{"FILE_NM": "새견적서.pdf", "FILE_EXT": "pdf"}]},
                headers={"X-API-Key": "secret"})

    def test_noop_statuses_are_really_noop(self):
        """`AOR_REINGEST_NOOP_STATUSES` 는 **선언**이고, 이 테스트가 그 선언의 **증명**이다.

        상수를 복제하지 않고 서버 상수 자체를 순회한다 — 상수에 상태를 추가했는데 실제로 no-op 이
        아니면 여기서 깨진다. 클라이언트는 이 상수를 응답으로 받아 자기 skip 집합을 좁히므로,
        이 테스트가 cross-repo drift 의 실질 가드다.
        """
        self.assertTrue(appmod.AOR_REINGEST_NOOP_STATUSES, "no-op 상태 목록이 비어 있음")
        for i, st in enumerate(appmod.AOR_REINGEST_NOOP_STATUSES):
            cd = f"Z26072200{i:02d}"
            did = self._insert(cd, st, proposed_comment="원본", vsl_nm="원래배", amt=100)
            pdf = appmod._aor_pdf_path(did, 0)
            os.makedirs(os.path.dirname(pdf), exist_ok=True)
            with open(pdf, "wb") as fh:
                fh.write(b"%PDF-1.4 original")
            before = self._snapshot()

            r = self._reingest(cd)

            self.assertEqual(200, r.status_code, st)
            self.assertTrue(r.get_json().get("dedup"), f"{st} 가 dedup 되지 않음")
            self.assertNotIn("updated", r.get_json(), f"{st} 가 갱신됨")
            after = self._snapshot()
            # 전 컬럼 + 행 추가 여부 + preview 파일 추가/삭제/변경까지 통째로 비교
            self.assertEqual(before[0], after[0], f"{st}: aor_draft 테이블이 변경됨")
            self.assertEqual(before[1], after[1], f"{st}: 첨부 preview 디렉터리가 변경됨")

    def test_noop_list_excludes_pending_and_hold(self):
        """pending 은 갱신 대상, hold 는 러너가 첨부를 재업로드하므로 no-op 이 아니다."""
        self.assertNotIn("pending", appmod.AOR_REINGEST_NOOP_STATUSES)
        self.assertNotIn("hold", appmod.AOR_REINGEST_NOOP_STATUSES)

        # pending 은 실제로 갱신 + 기존 preview 삭제되어야 한다(그래서 skip 하면 안 됨)
        did = self._insert("Z2607220099", "pending", proposed_comment="원본")
        pdf = appmod._aor_pdf_path(did, 0)
        os.makedirs(os.path.dirname(pdf), exist_ok=True)
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 original")
        r = self._reingest("Z2607220099")
        self.assertTrue(r.get_json().get("updated"))
        self.assertEqual("덮어쓰기 시도", self._full_row("Z2607220099")["proposed_comment"])
        self.assertFalse(os.path.exists(pdf), "pending 재적재 시 stale preview 가 지워져야 함")

    def test_response_advertises_noop_statuses(self):
        body = self._get().get_json()
        self.assertEqual(list(appmod.AOR_REINGEST_NOOP_STATUSES), body["noop_statuses"])

    def test_response_advertises_terminal_statuses(self):
        """러너가 실제로 skip 할 수 있는 absorbing 부분집합도 서버가 정본으로 내려준다."""
        body = self._get().get_json()
        self.assertEqual(list(appmod.AOR_REINGEST_TERMINAL_STATUSES), body["terminal_statuses"])

    def test_terminal_statuses_omitted_whenever_noop_is(self):
        """둘은 항상 같이 나가거나 같이 빠진다.

        terminal 만 남으면 skip 이 켜진 채로 근거가 반쪽이 되고, noop 만 남으면 구버전
        클라가 no-op 전체를 skip 하던 옛 동작으로 조용히 되돌아간다.
        """
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX uq_aor_draft_active_cd")
            db.commit()
        body = self._get().get_json()
        self.assertNotIn("noop_statuses", body)
        self.assertNotIn("terminal_statuses", body)

    # ---- 불변식 자가검증 (올마이트 R7) ----
    def test_stale_index_predicate_disables_skip_instead_of_risking_false_skip(self):
        """predicate 가 skip 상태를 못 덮으면 `noop_statuses` 를 빼서 클라 skip 을 꺼야 한다.

        `CREATE UNIQUE INDEX IF NOT EXISTS` 라 소스 predicate 를 바꿔도 기존 index 가 남는다.
        그 상황을 재현: index 를 좁은 predicate 로 재생성하면 응답에서 목록이 빠져야 한다.
        """
        self._insert("ATGRCA2607220003", "approved")
        self.assertIn("noop_statuses", self._get().get_json())   # 정상 상태 확인

        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX uq_aor_draft_active_cd")
            # 'approved' 를 뺀 구버전 predicate — 이러면 approved + pending 공존이 가능해진다
            db.execute("CREATE UNIQUE INDEX uq_aor_draft_active_cd ON aor_draft(aor_cd) "
                       "WHERE status IN ('pending','hold')")
            db.commit()

        body = self._get().get_json()
        self.assertNotIn("noop_statuses", body,
                         "predicate 가 깨졌는데 여전히 skip 근거를 내보냄 — false-skip 위험")
        self.assertEqual(200, self._get().status_code)   # 조회 자체는 계속 동작(fail-open)

    def test_missing_index_disables_skip(self):
        self._insert("ATGRCA2607220003", "approved")
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX uq_aor_draft_active_cd")
            db.commit()
        self.assertNotIn("noop_statuses", self._get().get_json())

    def test_canonical_key_collision_is_excluded_from_skip(self):
        """legacy 변형행으로 active 행이 갈라져 있으면 그 key 는 아예 목록에서 뺀다.

        unique index 는 exact match 라 'ABC' 와 'abc' 를 둘 다 허용한다. 그대로 두면
        approved 행이 pending 행을 가려 **false-skip** 이 난다 — 그래서 제외해 재처리시킨다.
        """
        self._insert("ATGRCA2607220003", "approved")
        self._insert("atgrca2607220003", "pending")   # 변형 — index 가 못 막음
        drafts = self._get().get_json()["drafts"]
        self.assertEqual([], drafts,
                         f"충돌 key 가 목록에 남음 — false-skip 가능: {drafts}")

    def test_single_non_canonical_active_row_is_excluded(self):
        """충돌이 없어도 **단독 비정규 행** 하나로 false-skip 이 난다(올마이트 R8 blocker).

        DB 에 'atgrca...' approved 만 있으면 충돌검사(COUNT>1)에 안 걸린다. 그런데 클라는
        'ATGRCA...' 로 정규화해 skip 하고, 정작 ingest 는 'ATGRCA...' 로 exact-match 조회하므로
        dedup 이 안 돼 **새 행을 INSERT** 한다 = no-op 이 아님. 그래서 비정규 행은 통째로 뺀다.
        """
        self._insert("atgrca2607220003", "approved")
        self.assertEqual([], self._get().get_json()["drafts"])

        # 실제로 no-op 이 아님을 증명: 정규형으로 재적재하면 dedup 이 아니라 신규 생성(201)
        with appmod.app.test_client() as client:
            r = client.post("/api/ext/aor/drafts",
                            json={"aor_cd": "ATGRCA2607220003", "proposed_comment": "신규"},
                            headers={"X-API-Key": "secret"})
        self.assertEqual(201, r.status_code,
                         "비정규 행이 dedup 을 막지 못하는데도 skip 됐다면 카드 유실")

    def test_non_canonical_row_does_not_hide_canonical_sibling(self):
        """같은 canonical key 의 정규 행이 따로 있어도, 변형 행이 있으면 그 key 는 제외한다."""
        self._insert("ATGRCA2607220003", "approved")
        self._insert(" atgrca2607220003 ", "pending")
        self.assertEqual([], self._get().get_json()["drafts"])

    def test_index_check_rejects_extra_predicate_condition(self):
        """`status IN (...) AND ...` 는 일부 active 행만 보호하므로 통과시키면 안 된다."""
        self._insert("ATGRCA2607220003", "approved")
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX uq_aor_draft_active_cd")
            db.execute(
                "CREATE UNIQUE INDEX uq_aor_draft_active_cd ON aor_draft(aor_cd) "
                "WHERE status IN ('pending','hold','approved','submitting','submitted',"
                "'rejecting','reject_submitting') AND amt IS NOT NULL")
            db.commit()
        self.assertNotIn("noop_statuses", self._get().get_json())

    def test_index_check_rejects_non_unique_index(self):
        self._insert("ATGRCA2607220003", "approved")
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX uq_aor_draft_active_cd")
            db.execute(
                "CREATE INDEX uq_aor_draft_active_cd ON aor_draft(aor_cd) "
                "WHERE status IN ('pending','hold','approved','submitting','submitted',"
                "'rejecting','reject_submitting')")
            db.commit()
        self.assertNotIn("noop_statuses", self._get().get_json())

    def test_index_check_rejects_wrong_column(self):
        self._insert("ATGRCA2607220003", "approved")
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX uq_aor_draft_active_cd")
            db.execute(
                "CREATE UNIQUE INDEX uq_aor_draft_active_cd ON aor_draft(id) "
                "WHERE status IN ('pending','hold','approved','submitting','submitted',"
                "'rejecting','reject_submitting')")
            db.commit()
        self.assertNotIn("noop_statuses", self._get().get_json())

    def _replace_index(self, predicate_items, unique=True, table="aor_draft", col="aor_cd"):
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX IF EXISTS uq_aor_draft_active_cd")
            db.execute("CREATE %s INDEX uq_aor_draft_active_cd ON %s(%s) "
                       "WHERE status IN (%s)"
                       % ("UNIQUE" if unique else "", table, col, predicate_items))
            db.commit()

    def test_index_check_rejects_escaped_quote_literal(self):
        """`'''approved'''` 는 값이 `'approved'` — approved 를 안 덮는다(올마이트 R9 blocker).

        따옴표를 `strip()` 으로 뭉개면 `approved` 로 보여 통과해버린다 = false-positive.
        """
        self._insert("ATGRCA2607220003", "approved")
        self._replace_index(
            "'pending','hold','''approved''','submitting','submitted',"
            "'rejecting','reject_submitting'")
        self.assertNotIn("noop_statuses", self._get().get_json())

    def test_index_check_rejects_identifier_instead_of_literal(self):
        """`"approved"` 는 SQL 상 문자열이 아니라 identifier — 리터럴로 인정하면 안 된다."""
        self._insert("ATGRCA2607220003", "approved")
        # sqlite 는 매칭 컬럼이 없으면 `"approved"` 를 문자열로 관대하게 해석하지만,
        # 우리 검사기는 그 관용에 기대면 안 된다(다른 스키마·다른 엔진에서 의미가 바뀜).
        self._replace_index(
            '\'pending\',\'hold\',"approved",\'submitting\',\'submitted\','
            "'rejecting','reject_submitting'")
        self.assertNotIn("noop_statuses", self._get().get_json())

    def test_index_check_rejects_literal_containing_comma(self):
        """콤마 분리로는 못 나누는 리터럴이 있으면 해독 실패로 보고 거부해야 한다."""
        self._insert("ATGRCA2607220003", "approved")
        self._replace_index(
            "'pending','hold','appro,ved','submitting','submitted',"
            "'rejecting','reject_submitting'")
        self.assertNotIn("noop_statuses", self._get().get_json())

    def test_sql_literal_decoder_semantics(self):
        """해독기 자체 단위검사 — 여기서 틀리면 위 통합검사들이 우연히 맞는 것뿐이다."""
        self.assertEqual("approved", appmod._sql_literal_value("'approved'"))
        self.assertEqual("approved", appmod._sql_literal_value("  'approved' "))
        self.assertEqual("'approved'", appmod._sql_literal_value("'''approved'''"))
        self.assertEqual("it's", appmod._sql_literal_value("'it''s'"))
        self.assertEqual("", appmod._sql_literal_value("''"))
        for bad in ('"approved"', "approved", "'approved", "approved'",
                    "'", "", "x'6162'", "1", "'a'b'"):
            self.assertIsNone(appmod._sql_literal_value(bad), bad)

    def test_index_check_accepts_the_real_index(self):
        """가드가 너무 빡빡해 정상 index 까지 막으면 최적화가 영영 안 돈다 — 양성 확인.

        음성 케이스만 있으면 "항상 False" 인 가드도 전부 통과한다.
        """
        with appmod.app.app_context():
            self.assertTrue(appmod._aor_index_predicate_covers_noop())

    # (같은 이름의 escaped-quote 테스트가 위 424 줄에 있다. 중복 정의는 Python 이 뒤엣것으로
    #  덮어써서 앞 테스트가 조용히 사라지므로 하나만 남긴다 — 올마이트 R10.)

    def test_collision_on_unrelated_key_does_not_hide_others(self):
        self._insert("ATGRCA2607220003", "approved")
        self._insert("atgrca2607220003", "pending")
        self._insert("BGBBCA2607230002", "approved")
        got = {d["aor_cd"] for d in self._get().get_json()["drafts"]}
        self.assertEqual({"BGBBCA2607230002"}, got)

    # ---- 스냅샷 보장 실패 시 skip 비활성 (올마이트 R10·R11) ----
    def test_existing_transaction_disables_skip(self):
        """이미 transaction 중이면 스냅샷 출처를 모른다 → 보수적으로 noop_statuses 생략.

        ⚠️ 실제 HTTP 경로에서 이 분기는 **도달 불가**다(2026-07-27 실측): `api_key_required`
        → `_check_api_key` → `_get_api_key` → `_ensure_api_table` 이 `execute()`(=`commit()`)
        를 때리므로 뷰 진입 시점엔 항상 autocommit 상태다. 그래서 여기선 인증을 우회해
        분기 자체만 검증한다 — 도달 불가라도 방어는 남겨둔다(인증 경로가 바뀔 수 있음).
        """
        self._insert("ATGRCA2607220003", "approved")
        real_check = appmod._check_api_key
        appmod._check_api_key = lambda: True
        try:
            with appmod.app.test_request_context(headers={"X-API-Key": "secret"}):
                db = appmod.get_db()
                db.execute("BEGIN")
                self.assertTrue(db.in_transaction, "선행 조건이 안 잡혔으면 테스트가 무의미")
                try:
                    body = appmod.api_ext_aor_reingest_statuses().get_json()
                finally:
                    db.rollback()
        finally:
            appmod._check_api_key = real_check
        self.assertNotIn("noop_statuses", body)
        # 목록 자체는 정상 반환 — 최적화만 끄고 기능은 유지
        self.assertEqual(1, body["count"])

    def test_auth_path_always_leaves_autocommit(self):
        """위 분기가 '도달 불가'라는 근거를 회귀로 고정한다.

        인증 경로가 나중에 commit 을 안 하도록 바뀌면 이 테스트가 깨지고,
        그때는 in_transaction 분기가 실제 경로가 되므로 재검토해야 한다.
        """
        with appmod.app.test_request_context(headers={"X-API-Key": "secret"}):
            db = appmod.get_db()
            db.execute("BEGIN")
            self.assertTrue(db.in_transaction)
            self.assertTrue(appmod._check_api_key())
            self.assertFalse(db.in_transaction,
                             "인증 경로가 더는 commit 하지 않음 — in_transaction 분기 재검토 필요")

    def test_begin_failure_disables_skip_instead_of_500(self):
        """BEGIN 이 실패해도 500 이 아니라 'skip off' 로 수렴해야 한다."""
        self._insert("ATGRCA2607220003", "approved")
        real_get_db = appmod.get_db

        class _NoBegin:
            def __init__(self, db):
                self._db = db
            in_transaction = False
            def execute(self, sql, *a, **k):
                if sql.strip().upper().startswith("BEGIN"):
                    raise sqlite3.OperationalError("boom")
                return self._db.execute(sql, *a, **k)
            def __getattr__(self, n):
                return getattr(self._db, n)

        appmod.get_db = lambda: _NoBegin(real_get_db())
        try:
            r = self._get()
        finally:
            appmod.get_db = real_get_db
        self.assertEqual(200, r.status_code)
        self.assertNotIn("noop_statuses", r.get_json())

    def test_rollback_failure_does_not_turn_a_good_response_into_500(self):
        self._insert("ATGRCA2607220003", "approved")
        real_get_db = appmod.get_db

        class _BadRollback:
            def __init__(self, db):
                self._db = db
            in_transaction = False
            def rollback(self):
                raise sqlite3.OperationalError("boom")
            def __getattr__(self, n):
                return getattr(self._db, n)

        appmod.get_db = lambda: _BadRollback(real_get_db())
        try:
            r = self._get()
        finally:
            appmod.get_db = real_get_db
        self.assertEqual(200, r.status_code)
        self.assertIn("noop_statuses", r.get_json())

    def test_empty_table(self):
        body = self._get().get_json()
        self.assertEqual(0, body["count"])
        self.assertEqual([], body["drafts"])


class AbsorbingStatusInvariantTests(unittest.TestCase):
    """`AOR_REINGEST_TERMINAL_STATUSES` 가 정말 absorbing 인지 **두 겹**으로 강제한다.

    러너의 skip 안전성은 전적으로 이 불변식에 기댄다(올마이트 R16):
      "skip 대상 상태에서는 어떤 전이로도 빠져나갈 수 없다."
    깨지면 skip 된 건이 나중에 재적재가 필요해지는데, 그때는 이미 SVMS 조회창(오늘-120d)
    밖이라 입력조차 안 와서 **영구 누락**이 된다.

    1차 = DB trigger(`trg_aor_draft_absorbing`). 어떤 경로로 오든 DB 가 UPDATE 를 거부한다.
    2차 = 아래 소스 스캔. trigger 가 있어도 "왜 이런 코드가 생겼나" 를 개발 시점에 잡는다.
    ⚠️ 소스 스캔만으로는 부족하다는 걸 전제로 짰다(올마이트 R17: f-string·소문자·OR·
       다른 모듈·마이그레이션·수동 SQL). 그래서 정적으로 판단 불가능한 형태는 **통과가 아니라
       실패**로 처리한다.
    """

    #: repo 전체를 훑는다 — app.py 밖(마이그레이션·스크립트)에도 전이가 있을 수 있다.
    # 'tests' 제외 — 여기 SQL 은 불변식을 **깨는지 확인하려고 일부러 쓰는 fixture** 라
    # (예: absorbing 이탈이 거부되는지 보는 UPDATE) 스캔 대상이 되면 항상 위반으로 잡힌다.
    # 운영 경로가 아니므로 제외해도 방어가 약해지지 않는다.
    _SKIP_DIRS = {'.git', 'venv', 'node_modules', '__pycache__', 'static', '.state', 'tests'}
    _UPD_RE = re.compile(r'UPDATE\s+aor_draft\s+SET\b(?:(?!;).)*?\bstatus\s*=', re.S | re.I)
    _STATUS_LIT_RE = re.compile(r"status\s*(?:=\s*'([^']*)'|IN\s*\(([^)]*)\))", re.I)
    #: 순수 리터럴처럼 보여도 `"... status='%s'" % x` / `.format()` 로 나중에 값이 꽂히면
    #: 정적 판단은 거짓이 된다. 이런 흔적이 보이면 분석불가로 떨군다(올마이트 R18 test gap).
    _TEMPLATED_RE = re.compile(r"%[sdr(]|\{\w*\}")

    def _py_files(self):
        root = os.path.dirname(os.path.abspath(appmod.__file__))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self._SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(('.py', '.sql')):
                    yield os.path.join(dirpath, fn)

    def _scan(self):
        """(파일, 줄번호, SQL, 정적분석가능여부) 목록."""
        found = []
        for path in self._py_files():
            try:
                src = open(path, encoding='utf-8').read()
            except (OSError, UnicodeDecodeError):
                continue
            if not self._UPD_RE.search(src):
                continue
            if not path.endswith('.py'):
                found.append((path, 0, ' '.join(src.split()), False))
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                found.append((path, 0, '<parse 실패>', False))
                continue
            for n in ast.walk(tree):
                # 정적으로 읽을 수 있는 순수 문자열
                if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                   and self._UPD_RE.search(n.value):
                    txt = ' '.join(n.value.split())
                    found.append((path, n.lineno, txt,
                                  not self._TEMPLATED_RE.search(txt)))
                # f-string / % / .format() 으로 조립한 SQL 은 정적 판단 불가 → 실패 처리
                elif isinstance(n, ast.JoinedStr):
                    txt = ''.join(v.value for v in n.values
                                  if isinstance(v, ast.Constant) and isinstance(v.value, str))
                    if re.search(r'UPDATE\s+aor_draft', txt, re.I):
                        found.append((path, n.lineno, ' '.join(txt.split()), False))
        return found

    #: 정적 스캔을 통과시킬 **정당한 예외**. 근거 없이 늘리지 말 것.
    #:   key = SQL 안의 고유 문자열, value = 왜 absorbing 불변식을 안 깨는지.
    _JUSTIFIED = {
        "UPDATE aor_draft SET WHERE id=?":
            "prep ingest 갱신문. SET 절이 f-string 이라 정적 분석이 불가하지만, 조립 재료인 "
            "`cols` 가 소스 리터럴 dict 이고 거기에 status 키가 없다 — 즉 status 를 아예 "
            "건드리지 않는다. 그 사실은 test_ingest_update_never_sets_status 가 AST 로 못박는다.",
        "UPDATE aor_draft AS loser":
            "init_db 중복정리. 같은 canonical aor_cd 의 winner 가 active 로 남을 때만 loser 를 "
            "'duplicate' 로 강등한다. loser 가 terminal(rank 최대)이면 winner 도 같은 terminal 이라 "
            "**그 aor_cd 는 계속 terminal 로 보인다** — skip 집합의 key 는 행이 아니라 aor_cd 이므로 "
            "false-skip 이 생기지 않는다. trigger 도 같은 조건(NOT EXISTS 절)으로 이 경우만 허용한다.",
    }

    def test_justified_exceptions_are_not_stale(self):
        """allowlist 에만 남고 코드에서 사라진 예외가 있으면 방어가 느슨해진 채로 방치된다."""
        blob = ' '.join(sql for _, _, sql, _ in self._scan())
        for marker in self._JUSTIFIED:
            self.assertIn(marker, blob, f"allowlist 항목이 소스에 없음(stale): {marker}")

    def test_ingest_update_never_sets_status(self):
        """allowlist 근거 검증 — 그 f-string 이 조립하는 컬럼 집합에 status 가 없어야 한다.

        allowlist 는 "안전하다고 주장"만 할 뿐이라 주장 자체를 기계로 확인한다.
        `cols` 에 status 가 추가되면(또는 **kwargs 로 불투명해지면) 여기서 깨진다.
        """
        src = open(os.path.abspath(appmod.__file__), encoding='utf-8').read()
        tree = ast.parse(src)
        checked = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            joined = [n for n in ast.walk(fn) if isinstance(n, ast.JoinedStr) and any(
                isinstance(v, ast.Constant) and isinstance(v.value, str)
                and 'UPDATE aor_draft SET' in v.value for v in n.values)]
            if not joined:
                continue
            dicts = [n.value for n in ast.walk(fn)
                     if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'cols' for t in n.targets)
                     and isinstance(n.value, ast.Call)
                     and isinstance(n.value.func, ast.Name) and n.value.func.id == 'dict']
            self.assertTrue(dicts, f"{fn.name}: `cols = dict(...)` 리터럴을 못 찾음 — "
                                   f"allowlist 근거가 성립하는지 확인 불가")
            for call in dicts:
                names = [kw.arg for kw in call.keywords]
                self.assertNotIn(None, names, f"{fn.name}: cols 에 **kwargs — 불투명")
                self.assertNotIn('status', names, f"{fn.name}: cols 가 status 를 씀 — "
                                                  f"allowlist 근거 무효")
                self.assertFalse(call.args, f"{fn.name}: cols 에 positional arg — 불투명")
            # 초기값만 보면 `cols['status']=...` / `cols.update(...)` 로 나중에 넣는 걸 놓친다
            # (올마이트 R18 test gap). 리터럴 dict 이후의 어떤 변형도 허용하지 않는다.
            for n in ast.walk(fn):
                if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                   and n.value.id == 'cols' and isinstance(n.ctx, (ast.Store, ast.Del)):
                    self.fail(f"{fn.name}: cols 를 subscript 로 변형 — allowlist 근거 무효")
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                   and n.value.id == 'cols':
                    self.assertIn(n.attr, ('values', 'keys', 'items'),
                                  f"{fn.name}: cols.{n.attr}() 호출 — 변형 가능성, 근거 무효")
                # alias(`c = cols`) 로 우회하는 것도 막는다
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Name) \
                   and n.value.id == 'cols':
                    self.fail(f"{fn.name}: cols 를 다른 이름에 대입 — 추적 불가, 근거 무효")
            checked += 1
        self.assertEqual(1, checked, "대상 f-string UPDATE 가 1개가 아님 — 스캔 전제 변경됨")

    def test_scan_actually_finds_the_known_updates(self):
        """스캔이 죽으면 아래 가드가 전부 거짓 PASS 가 된다 — 실제 개수로 고정한다."""
        hits = self._scan()
        self.assertGreaterEqual(
            len(hits), 10,
            f"UPDATE 스캔이 예상보다 적게 잡음({len(hits)}) — 정규식이 SQL 형태 변화를 놓쳤을 수 있음")

    def test_terminal_is_subset_of_noop(self):
        self.assertTrue(set(appmod.AOR_REINGEST_TERMINAL_STATUSES)
                        <= set(appmod.AOR_REINGEST_NOOP_STATUSES),
                        "absorbing 인데 no-op 이 아닌 상태가 있음 — 계약 모순")

    @classmethod
    def _violation(cls, sql, analyzable, term):
        """이 UPDATE 문이 absorbing 이탈을 **일으키지 않는다고 정적으로 확신**할 수 있나.

        확신 못 하면(=동적 조립, WHERE 없음, OR 혼입, 리터럴 제한 없음) 위반으로 본다 —
        판단불가를 통과시키면 방어가 무의미해진다(올마이트 R17).
        반환: 위반 사유 문자열, 문제없으면 None.
        """
        if not analyzable:
            return "동적으로 조립(f-string/%/format)해 absorbing 이탈 여부를 정적 판단 불가"
        parts = re.split(r'\bWHERE\b', sql, flags=re.I)
        if len(parts) != 2:
            return "status 를 바꾸는데 WHERE 가 없거나 여러 개"
        cond = parts[1]
        # `WHERE status='pending' OR id=?` 처럼 OR 가 섞이면 status 제한이 무의미해진다.
        # (status IN (...) 안의 콤마는 OR 가 아니므로 IN 목록을 지운 뒤 판단)
        if re.search(r'(?i)\bOR\b', re.sub(r"IN\s*\([^)]*\)", "", cond, flags=re.I)):
            return "WHERE 에 OR 가 있어 status 제한이 보장되지 않음"
        srcs = set()
        for eq, inlist in cls._STATUS_LIT_RE.findall(cond):
            if eq:
                srcs.add(eq)
            else:
                srcs |= {x.strip().strip("'") for x in inlist.split(',') if x.strip()}
        if not srcs:
            return "WHERE 가 status 를 리터럴로 제한하지 않음(파라미터 바인딩 등)"
        bad = srcs & term
        if bad:
            return (f"absorbing 상태 {sorted(bad)} 에서 나가는 전이가 생겼음 — 러너 skip 이 "
                    f"영구 누락으로 바뀐다. AOR_REINGEST_TERMINAL_STATUSES 를 먼저 좁힐 것")
        return None

    def test_violation_judge_is_fail_closed(self):
        """판정 함수 자체를 합성 입력으로 검증 — 스캔이 조용히 물러지면 여기서 깨진다."""
        term = {'submitted'}
        bad_cases = [
            ("UPDATE aor_draft SET status='failed' WHERE status='submitted'", True),
            ("update aor_draft set status='failed' where status='submitted'", True),
            ("UPDATE aor_draft SET status='failed' WHERE status='pending' OR id=1", True),
            ("UPDATE aor_draft SET status='failed' WHERE id=?", True),
            ("UPDATE aor_draft SET status='failed'", True),
            # 동적 조립 계열은 analyzable=False 로 들어온다
            ("UPDATE aor_draft SET status='%s' WHERE status='pending'", False),
            ("UPDATE aor_draft SET status='{st}' WHERE status='pending'", False),
            ("UPDATE aor_draft SET status= WHERE id=?", False),
        ]
        for sql, analyzable in bad_cases:
            self.assertIsNotNone(self._violation(sql, analyzable, term),
                                 f"통과시키면 안 되는 SQL 을 통과시킴: {sql}")
        ok_cases = [
            "UPDATE aor_draft SET status='failed' WHERE status='submitting'",
            "UPDATE aor_draft SET status='rejected' WHERE status IN ('rejecting','hold')",
        ]
        for sql in ok_cases:
            self.assertIsNone(self._violation(sql, True, term), f"정상 SQL 인데 막음: {sql}")

    def test_templated_literal_is_not_treated_as_static(self):
        """`"... status='%s'" % x` 는 리터럴처럼 보여도 정적 판단이 불가하다."""
        self.assertTrue(self._TEMPLATED_RE.search("UPDATE aor_draft SET status='%s' WHERE id=1"))
        self.assertTrue(self._TEMPLATED_RE.search("UPDATE aor_draft SET status='{s}' WHERE id=1"))
        self.assertFalse(self._TEMPLATED_RE.search("UPDATE aor_draft SET status='failed' "
                                                   "WHERE status='submitting'"))

    def test_no_transition_leaves_a_terminal_status(self):
        term = set(appmod.AOR_REINGEST_TERMINAL_STATUSES)
        for path, lineno, sql, analyzable in self._scan():
            where = f"{os.path.relpath(path)}:{lineno}"
            # allowlist 는 **정적분석 가능한 .py 소스에만** 적용한다. .sql 파일은 파일 전체가
            # 한 hit 라 marker 하나로 통째 면제될 수 있다(올마이트 R18 test gap).
            if path.endswith('.py') and any(m in sql for m in self._JUSTIFIED):
                continue
            why = self._violation(sql, analyzable, term)
            self.assertIsNone(why, f"{where} — {why}: {sql[:140]}")


class AbsorbingTriggerTests(unittest.TestCase):
    """1차 방어 = DB trigger. 실제로 UPDATE 가 거부되는지, 없으면 skip 이 꺼지는지."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = appmod.app.config["DATABASE"]
        self._orig_const = appmod.DATABASE
        self.path = os.path.join(self.tmp.name, "test.db")
        appmod.DATABASE = self.path
        appmod.app.config["DATABASE"] = self.path
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)",
                           ("secret",))

    def tearDown(self):
        appmod.app.config["DATABASE"] = self._orig
        appmod.DATABASE = self._orig_const
        self.tmp.cleanup()

    def _body(self):
        with appmod.app.test_client() as c:
            return c.get(URL, headers={"X-API-Key": "secret"}).get_json()

    def _seed(self, status):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO aor_draft (aor_cd, status) VALUES (?,?)", ("ATGRCA1", status))
            db.commit()

    def test_trigger_exists_after_init(self):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                             ('trg_aor_draft_absorbing',)).fetchone()
        self.assertIsNotNone(row, "init_db 가 absorbing trigger 를 안 만듦")

    def test_leaving_a_terminal_status_is_rejected_by_the_db(self):
        for term in appmod.AOR_REINGEST_TERMINAL_STATUSES:
            with self.subTest(status=term):
                with sqlite3.connect(self.path) as db:
                    db.execute("DELETE FROM aor_draft")
                    db.execute("INSERT INTO aor_draft (aor_cd, status) VALUES (?,?)",
                               ("ATGRCA1", term))
                    db.commit()
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute("UPDATE aor_draft SET status='pending' WHERE aor_cd='ATGRCA1'")

    def test_non_terminal_transitions_still_work(self):
        """가드가 정상 워크플로를 막으면 안 된다 — pending→approved→submitting→submitted."""
        self._seed('pending')
        with sqlite3.connect(self.path) as db:
            for a, b in [('pending', 'approved'), ('approved', 'submitting'),
                         ('submitting', 'submitted')]:
                db.execute("UPDATE aor_draft SET status=? WHERE status=?", (b, a))
            db.commit()
            self.assertEqual('submitted',
                             db.execute("SELECT status FROM aor_draft").fetchone()[0])

    def test_purge_delete_still_works(self):
        """유일하게 허용되는 이탈은 purge(DELETE) — trigger 는 UPDATE 만 막는다."""
        self._seed('submitted')
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM aor_draft WHERE status IN "
                       "('submitted','rejected','failed','reject_failed')")
            db.commit()
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM aor_draft").fetchone()[0])

    def test_duplicate_cleanup_is_not_blocked_by_the_trigger(self):
        """같은 aor_cd 의 submitted 2행이 있어도 init_db 중복정리가 ABORT 되면 안 된다.

        막히면 부팅(ExecStartPre 의 init_db)이 죽어 서비스가 아예 안 뜬다.
        """
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM aor_draft")
            # 중복 active 행은 partial unique index 가 막는다. 정리 대상은 애초에 그 인덱스가
            # 없던 레거시 DB 이므로 그 상태를 재현한다(init_db 가 다시 만든다).
            db.execute("DROP INDEX IF EXISTS uq_aor_draft_active_cd")
            db.execute("INSERT INTO aor_draft (id, aor_cd, status) VALUES (1,'ATGRCA1','submitted')")
            db.execute("INSERT INTO aor_draft (id, aor_cd, status) VALUES (2,'ATGRCA1','submitted')")
            db.commit()
        with appmod.app.app_context():
            appmod.init_db(drop=False)          # 여기서 IntegrityError 나면 실패
        with sqlite3.connect(self.path) as db:
            got = dict(db.execute("SELECT id, status FROM aor_draft").fetchall())
        self.assertEqual({1: 'duplicate', 2: 'submitted'}, got)

    def test_terminal_row_cannot_change_its_canonical_key(self):
        """status 를 안 건드려도 aor_cd 를 바꾸면 **원래 key 가 terminal 을 잃는다**(R18 blocker)."""
        self._seed('submitted')
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE aor_draft SET aor_cd='OTHERCD' WHERE aor_cd='ATGRCA1'")

    def test_canonicalizing_the_key_is_allowed(self):
        """`upper(trim())` 정규화는 canonical key 를 안 바꾸므로 막으면 안 된다.

        막히면 init_db 의 표기 정규화 UPDATE 가 ABORT 되어 부팅이 죽는다.
        """
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM aor_draft")
            db.execute("INSERT INTO aor_draft (id, aor_cd, status) "
                       "VALUES (1,' atgrca1 ','submitted')")
            db.commit()
            db.execute("UPDATE aor_draft SET aor_cd=upper(trim(aor_cd)) "
                       "WHERE aor_cd<>upper(trim(aor_cd))")
            db.commit()
            self.assertEqual(('ATGRCA1', 'submitted'),
                             db.execute("SELECT aor_cd, status FROM aor_draft").fetchone())

    def test_status_null_transition_is_rejected(self):
        """`NEW.status <> OLD.status` 는 NULL 에서 안 뜬다 — `IS NOT` + NOT NULL 둘 다 확인."""
        self._seed('submitted')
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE aor_draft SET status=NULL WHERE aor_cd='ATGRCA1'")
            self.assertEqual('submitted',
                             db.execute("SELECT status FROM aor_draft "
                                        "WHERE aor_cd='ATGRCA1'").fetchone()[0])

    def test_status_column_is_not_null(self):
        """방어의 한 겹인 NOT NULL 제약이 조용히 사라지지 않게 고정한다."""
        with sqlite3.connect(self.path) as db:
            cols = {r[1]: r for r in db.execute("PRAGMA table_info(aor_draft)")}
        self.assertEqual(1, cols['status'][3], "aor_draft.status 의 NOT NULL 이 사라짐")

    def test_stale_trigger_is_auto_replaced_on_boot(self):
        """정의가 바뀌면 부팅 때 자동 교체돼야 한다 — 안 그러면 skip 이 영구 비활성(R19)."""
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER IF EXISTS trg_aor_draft_absorbing")
            db.execute("CREATE TRIGGER trg_aor_draft_absorbing BEFORE UPDATE OF status "
                       "ON aor_draft FOR EACH ROW WHEN 0 BEGIN SELECT 1; END")
            db.commit()
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            self.assertTrue(appmod._aor_absorbing_trigger_ok(),
                            "옛 trigger 가 그대로 남음 — DROP/CREATE 교체가 안 됨")
        self.assertIn('terminal_statuses', self._body())

    def test_last_terminal_row_of_a_key_cannot_leave(self):
        """대표가 하나뿐이면 강등 불가 — 그 aor_cd 가 terminal 로 안 보이게 되기 때문."""
        self._seed('submitted')
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE aor_draft SET status='duplicate' WHERE aor_cd='ATGRCA1'")

    def test_missing_trigger_disables_skip_entirely(self):
        """trigger 가 없으면 근거가 없다 → noop/terminal 둘 다 빼서 클라 skip 을 끈다."""
        self._seed('submitted')
        self.assertIn('terminal_statuses', self._body())   # 정상 상태 확인
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER trg_aor_draft_absorbing")
            db.commit()
        body = self._body()
        self.assertNotIn('terminal_statuses', body)
        self.assertNotIn('noop_statuses', body,
                         "trigger 없이 noop 만 내보내면 옛 동작으로 되돌아감(올마이트 R17)")

    def test_stale_trigger_definition_disables_skip(self):
        """상수를 넓혀도 `IF NOT EXISTS` 라 기존 trigger 는 안 바뀐다 → 불일치면 꺼야 한다."""
        self._seed('submitted')
        orig = appmod.AOR_REINGEST_TERMINAL_STATUSES
        try:
            appmod.AOR_REINGEST_TERMINAL_STATUSES = orig + ('rejected',)
            self.assertNotIn('terminal_statuses', self._body())
        finally:
            appmod.AOR_REINGEST_TERMINAL_STATUSES = orig


class TestSuiteIntegrityTests(unittest.TestCase):
    """테스트 파일 자체의 결함을 잡는 메타 테스트.

    2026-07-27: 같은 이름의 테스트가 두 번 정의돼 앞엣것이 조용히 사라진 사고(올마이트 R10).
    Python 은 뒤 정의로 덮어쓰고 unittest 는 아무 경고도 안 하므로, PASS 개수만 보면 모른다.
    ⚠️ 이 클래스는 반드시 `if __name__ == "__main__"` **앞**에 있어야 한다 — 뒤에 두면
       직접 실행 시 `unittest.main()` 이 먼저 돌아 이 클래스가 아예 정의되지 않는다(올마이트 R11).
    """

    def test_no_duplicate_test_method_names(self):
        import ast
        src = open(__file__, encoding="utf-8").read()
        tree = ast.parse(src)
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            names = [f.name for f in cls.body
                     if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
            dups = sorted({n for n in names if names.count(n) > 1})
            self.assertEqual([], dups,
                             f"{cls.name} 에 중복 정의된 메서드={dups} — 앞 정의가 조용히 사라짐")

    def test_every_testcase_class_is_defined_before_main_guard(self):
        """직접 실행에서 조용히 빠지는 클래스가 없는지 — 위 사고의 일반형."""
        import ast
        tree = ast.parse(open(__file__, encoding="utf-8").read())
        guard = next((n.lineno for n in tree.body
                      if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)), None)
        self.assertIsNotNone(guard, "main guard 가 없음")
        late = [n.name for n in tree.body
                if isinstance(n, ast.ClassDef) and n.lineno > guard]
        self.assertEqual([], late, f"main guard 뒤에 정의된 클래스={late} — 직접 실행 시 누락됨")


if __name__ == "__main__":
    unittest.main()
