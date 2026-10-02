"""조선소 견적 remark 대기열 — 업로드가 job 을 만들고, 데쿠 결과는 사람 손대지 않은 auto 행만 덮는 계약.

잠그는 것:
  ① 업로드(규칙 성공) → 금액 즉시 저장 + pending job 1건(재업로드 시 이전 pending 은 superseded).
  ② 결과 반영은 업로드 때 임시 remark 그대로인 auto 행만 — 그 사이 사람이 고친 remark/manual 행 보존.
  ③ General/Paint 고정 형식 강제, 길이 제한.
  ④ error 보고 3회 → failed, 임시 remark 유지. 완료 job 재전송은 무변경.
"""
import io
import os
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns
from openpyxl import Workbook

H = {"X-API-Key": "secret"}


def quote_xlsx():
    wb = Workbook(); cv = wb.active; cv.title = 'Cover'
    cv.append(['1) Total repair period:', None, None, 30])
    ws = wb.create_sheet('Quotation')
    hdr = [None] * 21; hdr[0], hdr[2], hdr[16], hdr[19] = 'Item No.', 'Work Description', "Q'ty", 'Net   Total'
    ws.append(hdr)
    for itm, desc, amt in [(1, 'General Service', 0), (1.1, 'Fire', 10), (2, 'HULL PAINTINGS', 0), (2.1, 'Hull', 20),
                           (4, 'deck', 0), (4.1, 'Deck Pipe', 30), (5.1, 'Aux. Boiler', 40), (6.1, 'Electric Motor', 5)]:
        r = [None] * 21; r[0], r[2], r[19] = itm, desc, amt
        if amt: r[16] = 1
        ws.append(r)
    for lab, v in (('Normal Total Price/USD', 105), ('Final discount', 0.2), ('Total Price after dicount/USD Net', 84)):
        r = [None] * 21; r[16] = lab; r[19] = v; ws.append(r)
    b = io.BytesIO(); wb.save(b); b.seek(0); return b


class YardRemarkJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.app.config["DATABASE"]; self.old_c = appmod.DATABASE
        self.old_csrf = appmod.app.config.get("CSRF_PROTECT")
        db = os.path.join(self.tmp.name, "t.db")
        appmod.DATABASE = db; appmod.app.config["DATABASE"] = db; appmod.app.config["CSRF_PROTECT"] = False
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)", ("secret",))
            self.uid = appmod.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active) VALUES(?,?,?,?,1)",
                ("yard", appmod.generate_password_hash("pw"), "Y", "admin"))
        self.c = appmod.app.test_client()
        with self.c.session_transaction() as s:
            s["user_id"] = self.uid

    def tearDown(self):
        appmod.app.config["DATABASE"] = self.old_db; appmod.DATABASE = self.old_c
        appmod.app.config["CSRF_PROTECT"] = self.old_csrf
        self.tmp.cleanup()

    def upload(self):
        r = self.c.post('/api/dock_yard/upload', data={'vsl_nm': 'SAP', 'file': (quote_xlsx(), 'q.xlsx')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)); return r.get_json()

    def rows(self):
        with appmod.app.app_context():
            return {r['category']: dict(r) for r in appmod.query("SELECT * FROM dock_yard WHERE vsl_nm='SAP'")}

    def jobs(self):
        return self.c.get('/api/ext/dock_yard/remark_jobs', headers=H).get_json()['jobs']

    def test_flow(self):
        j = self.upload()
        self.assertEqual(j['source'], 'rule'); self.assertTrue(j['remark_job'])
        self.assertEqual(self.rows()['Engine']['amount'], 40)
        jobs = self.jobs(); self.assertEqual(len(jobs), 1)
        self.assertIn('Deck Pipe', jobs[0]['quote_text']); self.assertEqual(jobs[0]['amounts']['Deck'], 30)
        # 재업로드 → 이전 pending superseded, 새 job 1건
        j2 = self.upload(); jobs = self.jobs()
        self.assertEqual([x['id'] for x in jobs], [j2['remark_job']])
        # 사람이 Steel remark 수정(금액 무관 remark 만) → 데쿠 결과로 덮이면 안 됨
        sid = self.rows()['Steel']['id']
        self.c.patch(f'/api/dock_yard/{sid}', json={'remark': '형 메모'})
        res = self.c.post(f"/api/ext/dock_yard/remark_jobs/{j2['remark_job']}", headers=H, json={'remarks': {
            'Deck': 'Deck pipe renewal etc.', 'Steel': 'AI steel', 'General': '엉뚱한 형식',
            'Engine': 'x' * 999, 'Paint': '', 'Electric': '', 'Discount': ''}}).get_json()
        rows = self.rows()
        self.assertEqual(rows['Deck']['remark'], 'Deck pipe renewal etc.')
        self.assertEqual(rows['Steel']['remark'], '형 메모')
        self.assertEqual(rows['General']['remark'], '입거 예상일정 : 일, 상가일정 : ')   # 형식 위반 → 스켈레톤
        self.assertEqual(len(rows['Engine']['remark']), 300)
        self.assertGreaterEqual(res['kept_human'], 1)
        self.assertEqual(self.jobs(), [])
        again = self.c.post(f"/api/ext/dock_yard/remark_jobs/{j2['remark_job']}", headers=H,
                            json={'remarks': {'Deck': 'late'}}).get_json()
        self.assertEqual(again['applied'], 0); self.assertEqual(self.rows()['Deck']['remark'], 'Deck pipe renewal etc.')

    def test_errors_fail_closed(self):
        j = self.upload(); before = self.rows()['Deck']['remark']
        for _ in range(3):
            self.c.post(f"/api/ext/dock_yard/remark_jobs/{j['remark_job']}", headers=H, json={'error': 'llm down'})
        self.assertEqual(self.jobs(), []); self.assertEqual(self.rows()['Deck']['remark'], before)

    def test_incomplete_or_stale_rejected(self):
        j = self.upload(); before = self.rows()['Deck']['remark']
        r = self.c.post(f"/api/ext/dock_yard/remark_jobs/{j['remark_job']}", headers=H,
                        json={'remarks': {'Deck': 'only deck', 'Engine': 5}})
        self.assertEqual(r.status_code, 422)                       # 누락·비문자열 → 미반영·재시도 대상
        self.assertEqual(self.rows()['Deck']['remark'], before)
        self.assertEqual(len(self.jobs()), 1)
        old = j['remark_job']; self.upload()                       # 재업로드 → 옛 job superseded
        full = {c: 'stale' for c in ['General', 'Paint', 'Steel', 'Deck', 'Engine', 'Electric', 'Discount']}
        r = self.c.post(f"/api/ext/dock_yard/remark_jobs/{old}", headers=H, json={'remarks': full}).get_json()
        self.assertEqual(r['status'], 'superseded'); self.assertNotEqual(self.rows()['Deck']['remark'], 'stale')

    def test_requires_key(self):
        self.assertIn(self.c.get('/api/ext/dock_yard/remark_jobs').status_code, (401, 403))


if __name__ == '__main__':
    unittest.main()
