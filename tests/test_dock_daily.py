"""Dock Daily Report contracts (web MVP).

These tests use the same temporary-DB pattern as the existing Flask tests and
avoid any external Dock Manager/SVMS service.
"""
import json
import io
import os
import re
import tempfile
import unittest
import zipfile

import app as appmod
import routes_dock_daily


class DockDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        self.old_upload_dir = routes_dock_daily.UPLOAD_DIR
        routes_dock_daily.UPLOAD_DIR = os.path.join(self.tmp.name, 'uploads')
        db = os.path.join(self.tmp.name, 'dock-daily.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            uid = appmod.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active) VALUES(?,?,?,?,1)",
                ('dockdaily-test', appmod.generate_password_hash('test'), 'Dock Daily', 'admin'))
            vessel = appmod.execute(
                "INSERT INTO vessels(name, vsl_cd, imo) VALUES(?,?,?)",
                ('DOCK DAILY TEST', 'D001', 'IMO-TEST'))
            self.uid, self.vessel = uid, vessel
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=self.uid, username='dockdaily-test', display_name='Dock Daily',
                     role='admin', permanent=False)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        routes_dock_daily.UPLOAD_DIR = self.old_upload_dir
        self.tmp.cleanup()

    def test_project_seeds_fixed_sections_and_report_is_unique(self):
        p = self.client.post('/api/dock-daily/projects', json={'vessel_id': self.vessel, 'title': 'Test DD'})
        self.assertEqual(201, p.status_code)
        pid = p.get_json()['id']
        with appmod.app.app_context():
            rows = appmod.query('SELECT section_key FROM dock_daily_section_def WHERE project_id=? ORDER BY sort_order', (pid,))
            self.assertEqual(['shipyard', 'survey', 'vendor', 'remark'], [r['section_key'] for r in rows])
        one = self.client.post(f'/api/dock-daily/projects/{pid}/reports/generate', json={'report_date': '2026-08-20'})
        two = self.client.post(f'/api/dock-daily/projects/{pid}/reports/generate', json={'report_date': '2026-08-20'})
        self.assertEqual(one.get_json()['id'], two.get_json()['id'])
        self.assertEqual(1, len(self.client.get(f'/api/dock-daily/projects/{pid}/reports').get_json()))

    def test_page_uses_trmt_shell_and_single_project_modal(self):
        page = self.client.get('/dock-daily')
        self.assertEqual(200, page.status_code)
        html = page.get_data(as_text=True)
        self.assertIn('class="topnav"', html)
        self.assertIn('id="dd-project-modal"', html)
        self.assertIn('id="ddp-vessel"', html)
        self.assertIn('필수 입력은 선박과 프로젝트명 2개입니다.', html)
        self.assertIn('#dd-project-form{display:flex;min-height:0;flex:1;flex-direction:column;overflow:hidden}', html)
        self.assertIn('#dd-project-form .modal-body{min-height:0;overflow-y:auto', html)
        self.assertIn('id="dd-preview-modal"', html)
        self.assertIn('id="dd-copy-all"', html)
        self.assertIn('id="dd-svms-push"', html)
        self.assertIn('id="dd-file-input"', html)
        self.assertNotIn('id="dd-add-block"', html)
        self.assertNotIn('{% block body %}', html)

        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'dock_daily.js'), encoding='utf-8') as f:
            script = f.read()
        self.assertNotIn("prompt('vessel_id", script)
        self.assertNotIn("confirm('이번 입거", script)
        self.assertIn("projectForm.onsubmit", script)
        self.assertIn("dd-section-edit", script)
        self.assertIn("ClipboardItem", script)
        # The subject block sits outside the server body <div>, so it carries its
        # own 11pt declaration or Outlook pastes that one line in its own font.
        self.assertIn('<p style="margin:0;line-height:1.5">&nbsp;</p></div>${v.html}', script)
        self.assertIn('font-size:11pt;line-height:1.5;color:#222"><p style="margin:0"><b>제목: ${esc(v.subject)}</b></p>',
                      script)
        # Cards auto-number on Enter and no longer show the meaningless
        # 수동 / 수동 수정 보호 pair on hand written blocks. The numbering logic
        # itself is exercised by tests/dock_daily_numbering.test.js.
        self.assertIn('js/dock_daily_numbering.js', html)
        self.assertLess(html.index('js/dock_daily_numbering.js'), html.index('js/dock_daily.js'))
        self.assertIn('window.DockDailyNumbering', script)
        self.assertIn("document.querySelectorAll('.dd-section-edit').forEach(bindItemNumbering)", script)
        self.assertIn('NUM.breakLine(ta.value,ta.selectionStart,ta.selectionEnd)', script)
        self.assertIn('oncompositionstart', script)
        self.assertIn('oncompositionend', script)
        self.assertIn('엔터를 누르면 1) 2) 번호가 붙습니다', script)
        self.assertNotIn("?'자동수집':'수동'", script)

        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js',
                               'dock_daily_numbering.js'), encoding='utf-8') as f:
            numbering = f.read()
        self.assertIn('function renumber(', numbering)
        self.assertIn('function breakLine(', numbering)

    def test_docx_attachment_upload_and_inline_preview(self):
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Attachment DD',
        }).get_json()
        report = self.client.post(
            f"/api/dock-daily/projects/{project['id']}/reports/generate",
            json={'report_date': '2026-08-20'},
        ).get_json()
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as zf:
            zf.writestr('word/document.xml',
                        '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Dock Word Preview</w:t></w:r></w:p></w:body></w:document>')
        uploaded = self.client.post(
            f"/api/dock-daily/reports/{report['id']}/attachments",
            data={'file': (io.BytesIO(raw.getvalue()), 'daily.docx')},
            content_type='multipart/form-data',
        )
        self.assertEqual(201, uploaded.status_code, uploaded.get_data(as_text=True))
        aid = uploaded.get_json()['id']
        preview = self.client.get(f'/api/dock-daily/attachments/{aid}/preview')
        self.assertEqual(200, preview.status_code)
        self.assertIn('Dock Word Preview', preview.get_data(as_text=True))
        self.assertEqual('nosniff', preview.headers['X-Content-Type-Options'])
        fetched = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        self.assertEqual('daily.docx', fetched['attachments'][0]['original_name'])

    def test_ooxml_preview_rejects_entity_payload(self):
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Unsafe Attachment DD',
        }).get_json()
        report = self.client.post(
            f"/api/dock-daily/projects/{project['id']}/reports/generate",
            json={'report_date': '2026-08-20'},
        ).get_json()
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as zf:
            zf.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY a "unsafe">]><x>&a;</x>')
        uploaded = self.client.post(
            f"/api/dock-daily/reports/{report['id']}/attachments",
            data={'file': (io.BytesIO(raw.getvalue()), 'unsafe.docx')},
            content_type='multipart/form-data',
        ).get_json()
        preview = self.client.get(f"/api/dock-daily/attachments/{uploaded['id']}/preview")
        self.assertEqual(200, preview.status_code)
        self.assertIn('미리보기로 변환하지 못했습니다', preview.get_data(as_text=True))
        self.assertNotIn('unsafe', preview.get_data(as_text=True))

    def test_report_includes_ios_itinerary_and_legacy_direct_dates(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel,
            'title': 'Test DD',
            'berthing_date': '2026-08-20',
            'dock_in_date': '2026-08-21',
            'dock_out_date': '2026-09-01',
            'departure_date': '2026-09-02',
        }).get_json()
        report = self.client.post(
            f"/api/dock-daily/projects/{p['id']}/reports/generate",
            json={'report_date': '2026-08-20'},
        ).get_json()

        self.assertEqual({
            'berthing': '2026-08-20',
            'dry_dock_in': '2026-08-21',
            'dry_dock_out': '2026-09-01',
            'departure': '2026-09-02',
        }, report['itinerary'])
        self.assertEqual('2026-08-20', report['berthing_date'])
        self.assertEqual('2026-08-21', report['dock_in_date'])
        self.assertEqual('2026-09-01', report['dock_out_date'])
        self.assertEqual('2026-09-02', report['departure_date'])

    def test_revision_conflict_and_final_lock(self):
        p = self.client.post('/api/dock-daily/projects', json={'vessel_id': self.vessel, 'title': 'Test DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate", json={'report_date': '2026-08-20'}).get_json()
        body = {'revision': r['revision'], 'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph', 'content': {'body': 'ok'}}]}
        updated = self.client.put(f"/api/dock-daily/reports/{r['id']}", json=body)
        self.assertEqual(200, updated.status_code)
        self.assertEqual(409, self.client.put(f"/api/dock-daily/reports/{r['id']}", json=body).status_code)
        final = updated.get_json()
        final['status'] = 'final'
        locked = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={'revision': final['revision'], 'status': 'final'})
        self.assertEqual(200, locked.status_code)
        self.assertEqual(409, self.client.put(f"/api/dock-daily/reports/{r['id']}", json=body).status_code)

    def test_runner_idempotency_and_partial_fail_closed(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Test DD', 'auto_generate': True,
            'active_from': '2026-08-01', 'active_to': '2026-08-31',
            'dock_manager_project_ids': ['v_DM17'],
        }).get_json()
        event = {'source_table': 'jobs', 'source_id': '1', 'source_subkey': 'job:1:remark:2026-08-20',
                 'date': '2026-08-20', 'source_updated_at': '2026-08-20T17:00:00+09:00',
                 'kind': 'job_remark', 'title': 'Job', 'body': 'Done', 'suggested_section': 'shipyard'}
        with appmod.app.app_context():
            key = 'dock-daily-test-key'
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", (key,))
        url = f"/api/ext/dock-daily/projects/{p['id']}/merge"
        bad = self.client.post(url, json={'report_date': '2026-08-20', 'events': [event], 'partial': True}, headers={'X-API-Key': key})
        self.assertEqual(409, bad.status_code)
        payload = {'report_date': '2026-08-20', 'events': [event], 'complete': True, 'partial': False}
        first = self.client.post(url, json=payload, headers={'X-API-Key': key})
        second = self.client.post(url, json=payload, headers={'X-API-Key': key})
        self.assertEqual(200, first.status_code); self.assertEqual(0, second.get_json()['applied'])
        removed = self.client.post(url, json={
            'report_date': '2026-08-20', 'events': [], 'complete': True, 'partial': False,
        }, headers={'X-API-Key': key})
        self.assertEqual(200, removed.status_code)
        self.assertEqual(1, removed.get_json()['missing'])
        with appmod.app.app_context():
            self.assertEqual(0, appmod.query('SELECT COUNT(*) n FROM dock_daily_block', one=True)['n'])
        projects = self.client.get('/api/ext/dock-daily/projects', headers={'X-API-Key': key})
        self.assertEqual(200, projects.status_code)
        self.assertEqual(['v_DM17'], projects.get_json()['projects'][0]['dock_manager_project_ids'])

    def test_only_special_sections_are_configurable(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'EGCS DD',
            'special_sections': [{'section_key': 'egcs', 'label': 'EGCS Retrofit', 'enabled': True}],
        }).get_json()
        changed = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={'sections': [
            {'section_key': 'shipyard', 'label': 'Changed', 'enabled': False},
            {'section_key': 'egcs', 'label': 'EGCS Special', 'enabled': False},
        ]}).get_json()
        sections = {section['section_key']: section for section in changed['sections']}
        self.assertEqual('Shipyard', sections['shipyard']['label'])
        self.assertEqual(1, sections['shipyard']['enabled'])
        self.assertEqual('EGCS Special', sections['egcs']['label'])
        self.assertEqual(0, sections['egcs']['enabled'])

    def test_auto_project_requires_valid_final_active_window(self):
        missing = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Invalid auto', 'auto_generate': True,
        })
        self.assertEqual(400, missing.status_code)

        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Valid auto',
            'active_from': '2026-08-01', 'active_to': '2026-08-31',
            'auto_generate': True,
        }).get_json()
        inverted = self.client.patch(f"/api/dock-daily/projects/{project['id']}", json={
            'active_from': '2026-09-01',
        })
        self.assertEqual(400, inverted.status_code)
        cleared = self.client.patch(f"/api/dock-daily/projects/{project['id']}", json={
            'active_to': None,
        })
        self.assertEqual(400, cleared.status_code)
        unchanged = self.client.get('/api/dock-daily/projects').get_json()[0]
        self.assertEqual('2026-08-01', unchanged['active_from'])
        self.assertEqual('2026-08-31', unchanged['active_to'])

    def test_svms_preview_reports_utf8_bytes_and_publish_disabled(self):
        p = self.client.post('/api/dock-daily/projects', json={'vessel_id': self.vessel, 'title': 'Test DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate", json={'report_date': '2026-08-20'}).get_json()
        self.client.put(f"/api/dock-daily/reports/{r['id']}", json={'revision': r['revision'], 'operations': [{'section_key': 'vendor', 'block_type': 'paragraph', 'content': {'body': '한글 😀'}}]})
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertEqual(len(preview['fields']['RMK_VNDR'].encode('utf-8')), preview['byte_counts']['RMK_VNDR'])
        self.assertEqual(503, self.client.post(f"/api/dock-daily/reports/{r['id']}/svms-publish").status_code)

    def test_email_preview_uses_outlook_numbered_card_format(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Email DD',
            'berthing_date': '2026-03-24',
            'special_sections': [{'section_key': 'egcs', 'label': 'EGCS Retrofit', 'enabled': True}],
        }).get_json()
        r = self.client.post(
            f"/api/dock-daily/projects/{p['id']}/reports/generate",
            json={'report_date': '2026-05-07'},
        ).get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [
                {'section_key': 'shipyard', 'block_type': 'paragraph',
                 'content': {'body': 'Main deck repair complete\nCrane test <Hull & Valve> "ongoing"'}},
                {'section_key': 'egcs', 'block_type': 'paragraph',
                 'content': {'body': 'Funnel structure welding complete'}},
            ],
        })
        self.assertEqual(200, saved.status_code)
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()
        self.assertIn('수 신 : 곽인섭 팀장님 / 탱커관리 3팀', preview['text'])
        self.assertIn('발 신 : 손유석 감독 / 탱커관리 3팀', preview['text'])
        self.assertEqual('곽인섭 팀장님 / 탱커관리 3팀', preview['to'])
        self.assertEqual('손유석 감독 / 탱커관리 3팀', preview['from'])
        self.assertIn('안녕하십니까.', preview['text'])
        self.assertIn('아래와 같이 금일 입거공사 진행사항을 보고드립니다.', preview['text'])
        self.assertNotIn('Dear all', preview['text'])
        self.assertNotIn('Safety first', preview['text'])
        self.assertIn('안녕하십니까.', preview['html'])
        self.assertIn('아래와 같이 금일 입거공사 진행사항을 보고드립니다.', preview['html'])
        self.assertNotIn('Dear all', preview['html'])
        self.assertNotIn('Safety first', preview['html'])
        self.assertIn('<b>수 신 :</b> 곽인섭 팀장님 / 탱커관리 3팀', preview['html'])
        self.assertIn('<b>발 신 :</b> 손유석 감독 / 탱커관리 3팀', preview['html'])
        self.assertIn('발 신 : 손유석 감독 / 탱커관리 3팀\n\n안녕하십니까.\n\n아래와 같이 금일 입거공사 진행사항을 보고드립니다.\n\nVESSEL ITINERARY', preview['text'])
        self.assertIn('BERTHING\t2026.03.24', preview['text'])
        self.assertIn('1. Shipyard', preview['text'])
        self.assertIn('1) Main deck repair complete', preview['text'])
        self.assertIn('2) Crane test <Hull & Valve> "ongoing"', preview['text'])
        self.assertIn('2. EGCS Retrofit', preview['text'])
        self.assertIn('<table style="border-collapse:collapse', preview['html'])
        self.assertIn('<b>1. &nbsp;Shipyard</b>', preview['html'])
        self.assertIn('</table><p style="margin:0;line-height:1.5">&nbsp;</p><p style="margin:0 0 6px"><b>1. &nbsp;Shipyard</b>', preview['html'])
        self.assertIn('<table role="presentation" cellpadding="0" cellspacing="0" border="0"', preview['html'])
        font = 'font-family:Arial,Helvetica,sans-serif;font-size:11pt'
        self.assertIn('<td width="24" style="%s;width:24px">&nbsp;</td>' % font, preview['html'])
        self.assertIn('<td style="%s;vertical-align:top;padding:3px 8px 3px 0;white-space:nowrap">2)</td>'
                      '<td style="%s;padding:3px 0">Crane test &lt;Hull &amp; Valve&gt; &quot;ongoing&quot;</td>'
                      % (font, font), preview['html'])
        self.assertIn('Crane test &lt;Hull &amp; Valve&gt; &quot;ongoing&quot;</td></tr></table><p style="margin:0;line-height:1.5">&nbsp;</p><p style="margin:0 0 6px"><b>2. &nbsp;EGCS Retrofit</b>', preview['html'])
        self.assertNotIn('<Hull & Valve>', preview['html'])

    def test_email_html_declares_11pt_on_every_table_and_cell(self):
        """Outlook renders <table> with the client default font: a font-size on the
        wrapping <div> does not reach the cells. Pasting into Outlook showed the
        itinerary table and the numbered items smaller than the paragraphs, so the
        declaration has to be repeated on each table and cell."""
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Font DD', 'berthing_date': '2026-03-24'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-05-11'}).get_json()
        self.assertEqual(200, self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph',
                            'content': {'body': '갑판 도장 진행중\n프로펠러 검사 완료'}}],
        }).status_code)
        body = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()['html']
        tags = re.findall(r'<(?:table|td)\b[^>]*>', body)
        self.assertTrue(tags)
        for tag in tags:
            self.assertIn('font-size:11pt', tag, tag)
            self.assertIn('font-family:Arial,Helvetica,sans-serif', tag, tag)
        # Inheritance alone would leave exactly one declaration, on the wrapper div.
        self.assertGreater(body.count('font-size:11pt'), 1)

    def test_card_written_numbers_are_never_rendered_twice(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Numbered DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-05-09'}).get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [
                # Exactly what the auto-numbering card now stores, plus a hand
                # typed variant with odd spacing and an out-of-order number.
                {'section_key': 'shipyard', 'block_type': 'paragraph',
                 'content': {'body': '1) Tank cleaning\n2 )  Hull blasting\n7) Anode renewal'}},
                {'section_key': 'vendor', 'block_type': 'paragraph',
                 'content': {'body': '1) Vendor attendance'}},
            ],
        })
        self.assertEqual(200, saved.status_code)
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()
        self.assertIn('1) Tank cleaning', preview['text'])
        self.assertIn('2) Hull blasting', preview['text'])
        self.assertIn('3) Anode renewal', preview['text'])
        self.assertNotIn('1) 1)', preview['text'])
        self.assertNotIn('7)', preview['text'])
        self.assertNotIn('1) 1)', preview['html'])
        item_cell = '<td style="font-family:Arial,Helvetica,sans-serif;font-size:11pt;padding:3px 0">%s</td>'
        self.assertIn(item_cell % 'Tank cleaning', preview['html'])
        self.assertIn(item_cell % 'Anode renewal', preview['html'])
        svms = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertEqual('1) Tank cleaning\n2) Hull blasting\n3) Anode renewal',
                         svms['fields']['RMK_SYD'])
        self.assertEqual('1) Vendor attendance', svms['fields']['RMK_VNDR'])
        self.assertNotIn('1. 1)', svms['fields']['RMK'])

    def test_legacy_table_image_and_multi_block_sections_still_render(self):
        """Blocks the textarea editor never creates must keep rendering."""
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Legacy Blocks DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-05-10'}).get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [
                {'section_key': 'shipyard', 'block_type': 'table', 'sort_order': 0,
                 'content': {'columns': ['WBT', 'Plan'], 'rows': [['No.1', '05-01'], ['No.2', '05-03']]}},
                {'section_key': 'shipyard', 'block_type': 'image', 'sort_order': 1,
                 'content': {'caption': 'Hull photo', 'attachment_id': 0}},
                {'section_key': 'shipyard', 'block_type': 'item', 'sort_order': 2,
                 'content': {'title': 'Runner collected item', 'body': 'ignored by plain render'}},
                {'section_key': 'shipyard', 'block_type': 'paragraph', 'sort_order': 3,
                 'content': {'body': ''}},
            ],
        })
        self.assertEqual(200, saved.status_code)
        svms = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        syd = svms['fields']['RMK_SYD']
        self.assertIn('WBT | Plan', syd)
        self.assertIn('No.1 | 05-01', syd)
        self.assertIn('[Image] Hull photo', syd)
        self.assertIn('Runner collected item', syd)
        self.assertNotIn('1) 1)', syd)
        # Numbering stays gapless and empty blocks never claim a number.
        self.assertEqual(['1', '2', '3', '4', '5'], [x.split(')')[0] for x in syd.splitlines()])
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()
        self.assertIn('[Image] Hull photo', preview['text'])
        self.assertNotIn('1) 1)', preview['text'])

    def test_email_preview_translates_legacy_defaults_but_preserves_custom_footer(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Legacy Email DD',
        }).get_json()
        r = self.client.post(
            f"/api/dock-daily/projects/{p['id']}/reports/generate",
            json={'report_date': '2026-05-08'},
        ).get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'metadata': {
                'email_intro': 'Dear all,\nPlease find the dock daily report below.',
                'safety_footer': 'Safety first. Please advise if any unsafe condition is observed.',
            },
        }).get_json()
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()
        self.assertIn('안녕하십니까.', preview['text'])
        self.assertNotIn('Dear all', preview['text'])
        self.assertNotIn('Safety first', preview['text'])

        custom = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': saved['revision'],
            'metadata': {
                'email_intro': '사용자 지정 인사말',
                'safety_footer': '별도 안전 유의사항',
            },
        })
        self.assertEqual(200, custom.status_code)
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()
        self.assertIn('사용자 지정 인사말', preview['text'])
        self.assertIn('별도 안전 유의사항', preview['text'])


if __name__ == '__main__':
    unittest.main()
