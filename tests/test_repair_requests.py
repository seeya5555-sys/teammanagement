import os
import tempfile
import unittest

import app as appmod


class RepairRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'test.db')
        self.old = appmod.app.config['DATABASE']; self.old_global = appmod.DATABASE
        appmod.DATABASE = self.db; appmod.app.config['DATABASE'] = self.db
        appmod.app.config.update(TESTING=True, SECRET_KEY='test')
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod.execute("INSERT INTO vessels(name,vsl_cd,active) VALUES('TEST VESSEL','TSTV',1)")
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('automation_enabled','1')")
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','test-ext-key')")
        self.c = appmod.app.test_client()
        self.ext = {'X-API-Key': 'test-ext-key'}
        with self.c.session_transaction() as s:
            s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'

    def tearDown(self):
        appmod.app.config['DATABASE'] = self.old; appmod.DATABASE = self.old_global
        self.tmp.cleanup()

    def body(self, dock=False, cid='idem-1'):
        return dict(client_request_id=cid, vessel_id=1, subject='M/E repair', category='M/E',
                    equipment='MAIN ENGINE', maker='', type_nm='', app_voy='001E',
                    app_port_cd='KRPUS', app_dt='2026-08-14', cause='Leak found',
                    inspection='Crew inspected', detail='Renew gasket', stock='vendor',
                    reason_cd='P', dept_cd='E', dock_yn=dock, urgent_yn=False, critical_yn=False)

    def test_general_create_idempotent_and_approve_result(self):
        r = self.c.post('/api/repair-requests', json=self.body())
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        rid = r.get_json()['id']
        again = self.c.post('/api/repair-requests', json=self.body())
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()['id'], rid)
        with appmod.app.app_context():
            rr = appmod.query('SELECT * FROM repair_request WHERE id=?', (rid,), one=True)
            dp = appmod.query('SELECT * FROM dock_procure WHERE id=?', (rr['dock_rid'],), one=True)
            vessel = appmod.query('SELECT * FROM dock_procure_vessel WHERE vsl_nm=?', ('TEST VESSEL',), one=True)
            self.assertEqual(rr['dock_yn'], 'N'); self.assertEqual(dp['req_no'], f'RR{rid}')
            self.assertEqual(vessel['vsl_cd'], 'TSTV')
        a = self.c.post(f'/api/repair-requests/{rid}/approve', json={})
        self.assertEqual(a.status_code, 200, a.get_data(as_text=True))

    def test_init_backfills_preexisting_repair_vessel(self):
        with appmod.app.app_context():
            appmod.execute("INSERT INTO repair_request(vessel_id,vsl_cd,vsl_nm,subject,category,equipment,"
                           "app_voy,app_port_cd,app_dt,cause,inspection,detail,stock,reason_cd,dept_cd) "
                           "VALUES(1,'TSTV','LEGACY VESSEL','legacy','M/E','M/E','1','KRPUS','20260814',"
                           "'c','i','d','vendor','P','E')")
            appmod.init_db(drop=False)
            vessel = appmod.query('SELECT * FROM dock_procure_vessel WHERE vsl_nm=?',
                                  ('LEGACY VESSEL',), one=True)
            self.assertEqual(vessel['vsl_cd'], 'TSTV')

    def test_list_exposes_dock_rid_and_existing_vessel_code_wins(self):
        with appmod.app.app_context():
            appmod.execute("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd) VALUES('TEST VESSEL','KEEP')")
        row = self.c.post('/api/repair-requests', json=self.body(False, 'link-contract')).get_json()
        listed = self.c.get('/api/repair-requests').get_json()['requests'][0]
        self.assertEqual(listed['dock_rid'], row['dock_rid'])
        with appmod.app.app_context():
            vessel = appmod.query('SELECT vsl_cd FROM dock_procure_vessel WHERE vsl_nm=?',
                                  ('TEST VESSEL',), one=True)
            self.assertEqual(vessel['vsl_cd'], 'KEEP')

    def test_procurement_surfaces_keep_repair_rows_out_of_dock(self):
        repair = self.c.post('/api/repair-requests', json=self.body(False, 'scope-split')).get_json()
        with appmod.app.app_context():
            appmod.execute("INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,subject) "
                           "VALUES('TEST VESSEL','TSTV','P1','P','Dock paint')")

        dock = self.c.get('/api/dock_procure/lines?vsl_nm=TEST+VESSEL').get_json()
        self.assertEqual([row['req_no'] for row in dock['lines']], ['P1'])
        self.assertEqual(dock['scope'], 'dock')

        scoped = self.c.get(f"/api/dock_procure/lines?scope=repair&repair_id={repair['id']}")
        self.assertEqual(scoped.status_code, 200, scoped.get_data(as_text=True))
        payload = scoped.get_json()
        self.assertEqual([row['id'] for row in payload['lines']], [repair['dock_rid']])
        self.assertEqual(payload['repair'], {'id': repair['id'], 'dock_yn': 'N'})
        self.assertEqual(len(payload['vessels']), 1)

        page = self.c.get(f"/repair-requests?procure_id={repair['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('수리 발주', page.get_data(as_text=True))

    def test_dock_reserves_r_and_saved_is_immutable(self):
        one = self.c.post('/api/repair-requests', json=self.body(True, 'dock-1')).get_json()
        two = self.c.post('/api/repair-requests', json=self.body(True, 'dock-2')).get_json()
        with appmod.app.app_context():
            tags = [r['req_no'] for r in appmod.query(
                'SELECT d.req_no FROM repair_request r JOIN dock_procure d ON d.id=r.dock_rid ORDER BY r.id')]
            self.assertEqual(tags, ['R1', 'R2'])
            appmod.execute("UPDATE repair_request SET status='saving' WHERE id=?", (one['id'],))
        key = appmod.app.config.get('API_KEY')
        headers = {'X-API-Key': key} if key else {}
        # direct function contract is covered without depending on deployment API-key config
        with appmod.app.app_context():
            appmod.execute("UPDATE repair_request SET status='saved',rep_cd='TSTVME1' WHERE id=?", (one['id'],))
        p = self.c.patch(f"/api/repair-requests/{one['id']}", json={**self.body(True), 'version': 1})
        self.assertEqual(p.status_code, 409)

    def test_dock_scope_cannot_change_after_create(self):
        row = self.c.post('/api/repair-requests', json=self.body(False, 'scope-1')).get_json()
        p = self.c.patch(f"/api/repair-requests/{row['id']}",
                         json={**self.body(True), 'version': row['version']})
        self.assertEqual(p.status_code, 409)
        self.assertIn('Dock 여부', p.get_json()['error'])

    def test_claim_result_lifecycle_is_atomic(self):
        row = self.c.post('/api/repair-requests', json=self.body(False, 'life-1')).get_json()
        self.assertEqual(self.c.post(f"/api/repair-requests/{row['id']}/approve", json={}).status_code, 200)
        claim = self.c.get('/api/ext/repair-requests/approved', headers=self.ext)
        self.assertEqual(claim.status_code, 200)
        self.assertEqual(claim.get_json()['requests'][0]['id'], row['id'])
        self.assertEqual(self.c.get('/api/ext/repair-requests/approved', headers=self.ext).get_json()['count'], 0)
        bad = self.c.post(f"/api/ext/repair-requests/{row['id']}/result",
                          headers=self.ext, json={'ok': True, 'result': 'missing key'})
        self.assertEqual(bad.status_code, 400)
        done = self.c.post(f"/api/ext/repair-requests/{row['id']}/result", headers=self.ext,
                           json={'ok': True, 'rep_cd': 'TSTVME99', 'result': 'verified'})
        self.assertTrue(done.get_json()['applied'])
        with appmod.app.app_context():
            rr = appmod.query('SELECT * FROM repair_request WHERE id=?', (row['id'],), one=True)
            dp = appmod.query('SELECT * FROM dock_procure WHERE id=?', (rr['dock_rid'],), one=True)
            self.assertEqual((rr['status'], rr['rep_cd']), ('saved', 'TSTVME99'))
            self.assertEqual((dp['svms_req_no'], dp['stg_quote']), ('TSTVME99', 1))

    def test_human_recovery_requires_explicit_confirmation(self):
        row = self.c.post('/api/repair-requests', json=self.body(False, 'recover-1')).get_json()
        self.c.post(f"/api/repair-requests/{row['id']}/approve", json={})
        self.c.get('/api/ext/repair-requests/approved', headers=self.ext)
        denied = self.c.post(f"/api/repair-requests/{row['id']}/resolve",
                             json={'action': 'release'})
        self.assertEqual(denied.status_code, 400)
        released = self.c.post(f"/api/repair-requests/{row['id']}/resolve",
                               json={'action': 'release', 'confirmation': 'SVMS미생성확인'})
        self.assertEqual(released.get_json()['status'], 'failed')
        self.assertEqual(self.c.post(f"/api/repair-requests/{row['id']}/approve", json={}).status_code, 200)
        self.c.get('/api/ext/repair-requests/approved', headers=self.ext)
        saved = self.c.post(f"/api/repair-requests/{row['id']}/resolve",
                            json={'action': 'mark_saved', 'rep_cd': 'TSTVME77',
                                  'confirmation': 'SVMS확인'})
        self.assertEqual((saved.status_code, saved.get_json()['status']), (200, 'saved'))

    def test_dock_deletes_refuse_repair_owned_line(self):
        """Dock 쪽 삭제 경로는 수리신청서 소유 라인을 409 로 거부해야 한다.
        FK(dock_rid REFERENCES dock_procure) 가 막아 주기는 하지만, 막히는 방식이
        IntegrityError → raw HTML 500 이면 사유가 안 남고 선박은 영영 안 지워진다."""
        repair = self.c.post('/api/repair-requests', json=self.body(False, 'del-guard')).get_json()
        with appmod.app.app_context():
            appmod.execute("INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,subject) "
                           "VALUES('TEST VESSEL','TSTV','P9','P','Dock paint')")

        line = self.c.delete(f"/api/dock_procure/{repair['dock_rid']}")
        self.assertEqual(line.status_code, 409, line.get_data(as_text=True))
        self.assertIn('수리신청서', line.get_json()['error'])

        vessel = self.c.delete('/api/dock_procure/vessel', json={'vsl_nm': 'TEST VESSEL'})
        self.assertEqual(vessel.status_code, 409, vessel.get_data(as_text=True))
        with appmod.app.app_context():           # 부분삭제 0 — 순수 라인까지 살아 있어야 한다
            self.assertEqual(appmod.query("SELECT COUNT(*) c FROM dock_procure WHERE vsl_nm=?",
                                          ('TEST VESSEL',), one=True)['c'], 2)
            self.assertTrue(appmod.query("SELECT vsl_nm FROM dock_procure_vessel WHERE vsl_nm=?",
                                         ('TEST VESSEL',), one=True))

        # 신청서를 지우면 연결이 풀리고 두 경로가 다시 열린다.
        self.assertEqual(self.c.delete(f"/api/repair-requests/{repair['id']}").status_code, 200)
        reopened = self.c.delete('/api/dock_procure/vessel', json={'vsl_nm': 'TEST VESSEL'})
        self.assertEqual((reopened.status_code, reopened.get_json()['deleted_lines']), (200, 1))


    def test_replay_is_reported_so_clients_cannot_lose_edits(self):
        """같은 client_request_id 재전송은 201 이 아니라 200 + replayed=True 여야 한다.
        응답만 유실된 재시도에서 클라이언트가 '저장됨'으로 오인하면 사용자가 고친 내용이
        조용히 사라진다(iOS 는 status code 를 못 보므로 본문 플래그가 유일한 신호)."""
        first = self.c.post('/api/repair-requests', json=self.body(False, 'replay-1'))
        self.assertEqual((first.status_code, first.get_json()['replayed']), (201, False))
        again = self.c.post('/api/repair-requests', json={**self.body(False, 'replay-1'),
                                                         'subject': '형이 고친 제목'})
        self.assertEqual((again.status_code, again.get_json()['replayed']), (200, True))
        self.assertEqual(again.get_json()['id'], first.get_json()['id'])
        with appmod.app.app_context():          # 재전송 body 는 반영되지 않아야 한다(idempotent)
            self.assertEqual(appmod.query('SELECT subject FROM repair_request WHERE id=?',
                                          (first.get_json()['id'],), one=True)['subject'], 'M/E repair')

    def test_delete_race_past_precheck_still_returns_409_not_500(self):
        """pre-check 통과 후 연결이 생기는 race(TOCTOU) 는 FK 가 막는다 — 그 IntegrityError 가
        raw HTML 500 으로 새지 않고 409 로 나와야 한다. pre-check 를 무력화해 except 분기를
        직접 태우는 negative control(가드가 없으면 이 테스트는 500 으로 실패한다)."""
        from unittest.mock import patch
        import routes_dock_submit as rds
        repair = self.c.post('/api/repair-requests', json=self.body(False, 'race-1')).get_json()

        orig = rds.query                        # 패치 전에 원본을 잡는다(안 잡으면 무한재귀)

        def blind(sql, *a, **kw):               # 수리연결 pre-check 만 못 보게 만든다
            if 'FROM repair_request WHERE dock_rid' in sql:
                return None
            return orig(sql, *a, **kw)

        with patch.object(rds, 'query', side_effect=blind):
            line = self.c.delete(f"/api/dock_procure/{repair['dock_rid']}")
        self.assertEqual(line.status_code, 409, line.get_data(as_text=True))
        with appmod.app.app_context():          # 롤백 확인 — 라인과 신청서 둘 다 살아 있어야 한다
            self.assertTrue(appmod.query('SELECT id FROM dock_procure WHERE id=?',
                                         (repair['dock_rid'],), one=True))
            self.assertTrue(appmod.query('SELECT id FROM repair_request WHERE id=?',
                                         (repair['id'],), one=True))


    def test_repair_reuses_existing_dock_vessel_spelling(self):
        """같은 SVMS 코드의 Dock 엔트리가 있으면 그 표기를 재사용해야 한다.
        `vessels.name` 표기를 그대로 넣으면 같은 배가 두 장 카드로 갈린다(실사고: 'Belgium B'/'BELGIUM B')."""
        with appmod.app.app_context():
            appmod.execute("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd,vtype) "
                           "VALUES('TEST VESSEL DOCK','TSTV','VLCC')")
        row = self.c.post('/api/repair-requests', json=self.body(False, 'canon-1')).get_json()
        with appmod.app.app_context():
            rr = appmod.query('SELECT vsl_nm FROM repair_request WHERE id=?', (row['id'],), one=True)
            dp = appmod.query('SELECT vsl_nm FROM dock_procure WHERE id=?', (row['dock_rid'],), one=True)
            self.assertEqual((rr['vsl_nm'], dp['vsl_nm']), ('TEST VESSEL DOCK', 'TEST VESSEL DOCK'))
            self.assertIsNone(appmod.query("SELECT vsl_nm FROM dock_procure_vessel WHERE vsl_nm='TEST VESSEL'",
                                           one=True))

    def test_repair_only_vessel_is_hidden_from_dock_tab_until_dock_lines_exist(self):
        """일반수리 shim 엔트리는 Dock 발주현황 목록에 뜨면 안 된다 — 삭제도 안 되는 '0/0' 유령 카드가 된다.
        실제 Dock 라인이 붙으면 자동으로 다시 보여야 한다(숨김이 영구 은폐가 되면 안 됨)."""
        self.c.post('/api/repair-requests', json=self.body(False, 'shim-1'))
        with appmod.app.app_context():
            self.assertEqual(appmod.query("SELECT origin FROM dock_procure_vessel WHERE vsl_nm=?",
                                          ('TEST VESSEL',), one=True)['origin'], 'repair')
        listed = self.c.get('/api/dock_procure/lines').get_json()
        self.assertNotIn('TEST VESSEL', [v['vsl_nm'] for v in listed['vessels']])

        with appmod.app.app_context():           # 실제 Dock 라인 등장 → 다시 노출
            appmod.execute("INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,subject) "
                           "VALUES('TEST VESSEL','TSTV','P3','P','Dock paint')")
        again = self.c.get('/api/dock_procure/lines').get_json()
        self.assertIn('TEST VESSEL', [v['vsl_nm'] for v in again['vessels']])

    def test_boot_never_auto_merges_split_vessel_rows(self):
        """이미 갈라진 중복 행은 **자동 병합하지 않는다**(vsl_nm 은 11개 테이블이 문자열로 참조하는
        그룹 키라 이름 재매핑이 파괴적). 기동 backfill 은 태깅만 하고 데이터는 그대로 두며,
        화면 문제는 Dock 탭 필터로 해소된다."""
        row = self.c.post('/api/repair-requests', json=self.body(False, 'nomerge-1')).get_json()
        with appmod.app.app_context():
            appmod.execute("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd,vtype,shipyard) "
                           "VALUES('TEST VESSEL UP','TSTV','VLCC','YARD')")
            appmod.init_db(drop=False)
            self.assertTrue(appmod.query("SELECT vsl_nm FROM dock_procure_vessel WHERE vsl_nm=?",
                                         ('TEST VESSEL',), one=True))       # shim 행 보존
            self.assertEqual(appmod.query('SELECT vsl_nm FROM repair_request WHERE id=?',
                                          (row['id'],), one=True)['vsl_nm'], 'TEST VESSEL')
            self.assertEqual(appmod.query('SELECT vsl_nm FROM dock_procure WHERE id=?',
                                          (row['dock_rid'],), one=True)['vsl_nm'], 'TEST VESSEL')
        listed = self.c.get('/api/dock_procure/lines').get_json()            # 화면에서만 사라진다
        names = [v['vsl_nm'] for v in listed['vessels']]
        self.assertNotIn('TEST VESSEL', names)
        self.assertIn('TEST VESSEL UP', names)

    def test_boot_tagging_spares_vessels_with_any_dock_evidence(self):
        """negative control — Dock 근거(라인·dock_yard·dk_cd·조선소 벤더·메타)가 하나라도 있으면
        `origin='repair'` 로 태깅하면 안 된다. 오태깅하면 진짜 입거선박이 Dock 탭에서 사라진다."""
        cases = {
            'EV LINE': "INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,subject) "
                       "VALUES('EV LINE','TSTV','P7','P','Dock paint')",
            'EV YARD': "INSERT INTO dock_yard(vsl_nm,vsl_cd,category,amount) "
                       "VALUES('EV YARD','TSTV','General',1)",
            'EV DKCD': "UPDATE dock_procure_vessel SET dk_cd='D1' WHERE vsl_nm='EV DKCD'",
            'EV VNDR': "UPDATE dock_procure_vessel SET shipyard_vndr_cd='V1' WHERE vsl_nm='EV VNDR'",
            'EV META': "UPDATE dock_procure_vessel SET survey='2ND SPECIAL SURVEY' WHERE vsl_nm='EV META'",
        }
        with appmod.app.app_context():
            # positive control — 근거가 하나도 없는 shim 은 반드시 태깅돼야 한다. 이게 없으면
            # 태깅 UPDATE 가 통째로 죽어도(init_db 는 try/except 로 조용히 넘어간다) 아래
            # negative 단정이 전부 공허하게 통과한다.
            cases['EV NONE'] = "SELECT 1"
            for name, sql in cases.items():
                # 각 선박에 수리신청서 이력을 만들어 태깅 후보로 올린 뒤, Dock 근거 1종만 부여한다.
                appmod.execute("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd) VALUES(?,'TSTV')", (name,))
                appmod.execute("INSERT INTO repair_request(vessel_id,vsl_cd,vsl_nm,subject,category,equipment,"
                               "app_voy,app_port_cd,app_dt,cause,inspection,detail,stock,reason_cd,dept_cd) "
                               "VALUES(1,'TSTV',?,'s','M/E','M/E','1','KRPUS','20260815','c','i','d',"
                               "'vendor','P','E')", (name,))
                appmod.execute(sql)
            # 컬럼을 지워 backfill 미실행 상태를 재현한다(태깅은 `origin` 이 없을 때만 1회 돈다).
            appmod.execute("ALTER TABLE dock_procure_vessel DROP COLUMN origin")
            appmod.init_db(drop=False)
            self.assertIn('origin', [r[1] for r in appmod.query(
                "PRAGMA table_info(dock_procure_vessel)")])   # backfill 이 실제로 돌았음을 확인
            self.assertEqual(appmod.query("SELECT origin FROM dock_procure_vessel WHERE vsl_nm=?",
                                          ('EV NONE',), one=True)['origin'], 'repair')
            for name in cases:
                if name == 'EV NONE':
                    continue
                self.assertIsNone(appmod.query("SELECT origin FROM dock_procure_vessel WHERE vsl_nm=?",
                                               (name,), one=True)['origin'], name)


if __name__ == '__main__': unittest.main()
