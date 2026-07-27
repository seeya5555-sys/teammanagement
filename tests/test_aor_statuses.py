"""GET /api/ext/aor/reingest-statuses — prep 엔진 skip 판정용 읽기전용 목록.

배경(2026-07-27): prep 이 skip 목록을 `/api/aor/drafts?status=all` 에서 가져갔는데 그 라우트는
`@admin_required`(세션쿠키 전용)라 X-API-Key 로는 항상 401 이었고, 클라이언트가 비-200 을
`set()` 으로 삼켜 skip 최적화가 처음부터 죽어 있었다. 이 엔드포인트가 그 대체다.
"""
import os
import re
import sqlite3
import tempfile
import unittest

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
        return {s.strip().strip("'\"") for s in m.group(1).split(",")}

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

    def test_index_check_accepts_the_real_index(self):
        """가드가 너무 빡빡해 정상 index 까지 막으면 최적화가 영영 안 돈다 — 양성 확인.

        음성 케이스만 있으면 "항상 False" 인 가드도 전부 통과한다.
        """
        with appmod.app.app_context():
            self.assertTrue(appmod._aor_index_predicate_covers_noop())

    def test_index_check_rejects_escaped_quote_literal(self):
        """`'''approved'''`는 SQL상 따옴표가 포함된 다른 값이다.

        단순 quote strip이면 이를 approved로 오인하여 false-skip이 난다. parser는
        SQL literal의 실제 값을 해독하고 noop_statuses를 생략해야 한다.
        """
        self._insert("ATGRCA2607220003", "approved")
        with sqlite3.connect(appmod.app.config["DATABASE"]) as db:
            db.execute("DROP INDEX uq_aor_draft_active_cd")
            db.execute(
                "CREATE UNIQUE INDEX uq_aor_draft_active_cd ON aor_draft(aor_cd) "
                "WHERE status IN ('pending','hold', '''approved''','submitting','submitted',"
                "'rejecting','reject_submitting')"
            )
            db.commit()
        self.assertNotIn("noop_statuses", self._get().get_json(),
                         "escaped quote literal을 approved로 오인하면 false-skip 위험")

    def test_collision_on_unrelated_key_does_not_hide_others(self):
        self._insert("ATGRCA2607220003", "approved")
        self._insert("atgrca2607220003", "pending")
        self._insert("BGBBCA2607230002", "approved")
        got = {d["aor_cd"] for d in self._get().get_json()["drafts"]}
        self.assertEqual({"BGBBCA2607230002"}, got)

    def test_empty_table(self):
        body = self._get().get_json()
        self.assertEqual(0, body["count"])
        self.assertEqual([], body["drafts"])


if __name__ == "__main__":
    unittest.main()
