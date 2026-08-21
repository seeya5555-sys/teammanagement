"""Dock Daily Report contracts (web MVP).

These tests use the same temporary-DB pattern as the existing Flask tests and
avoid any external Dock Manager/SVMS service.
"""
import base64
import inspect
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile

from html.parser import HTMLParser

from PIL import Image as PILImage

import app as appmod
import routes_dock_daily

# 이 파일은 `client.session_transaction()` 으로 로그인해서 CSRF 토큰을 가진
# 적이 없다. TESTING 을 세우지 않는 파일이라 csrf.enforce 의 기본값(=켜짐)에
# 걸리므로, 여기서 명시적으로 끈다. 검사 자체는 tests/test_csrf.py 가 본다.
appmod.app.config['CSRF_PROTECT'] = False


class _Tree(HTMLParser):
    """Minimal element tree for the mail body, so structure tests do not rely on
    regex. A regex can pass on markup that never closes a tag or that nests a
    cell wrongly, which is exactly what the Outlook paste is sensitive to.

    Each node is {'tag', 'attrs', 'kids', 'text'}; 'text' is the character data
    that is a *direct* child of the node. Unbalanced markup raises."""

    VOID = {'br', 'img', 'meta', 'hr', 'input'}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = {'tag': None, 'attrs': {}, 'kids': [], 'text': ''}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': dict(attrs), 'kids': [], 'text': ''}
        self.stack[-1]['kids'].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if len(self.stack) < 2 or self.stack[-1]['tag'] != tag:
            raise AssertionError('unbalanced </%s> under %r' % (tag, self.stack[-1]['tag']))
        self.stack.pop()

    def handle_data(self, data):
        self.stack[-1]['text'] += data

    def handle_entityref(self, name):
        self.stack[-1]['text'] += '&%s;' % name

    @classmethod
    def parse(cls, markup):
        parser = cls()
        parser.feed(markup)
        parser.close()
        if len(parser.stack) != 1:
            raise AssertionError('unclosed %r' % [n['tag'] for n in parser.stack[1:]])
        return parser.root

    @staticmethod
    def find(node, tag):
        found = [node] if node['tag'] == tag else []
        for kid in node['kids']:
            found.extend(_Tree.find(kid, tag))
        return found


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

    def test_project_list_carries_the_vessel_name_and_code_clients_identify_by(self):
        """Both clients label a project by vessel, not by IMO.

        The web sidebar has always shown `vessel_name`; iOS showed `imo` until
        2026-08-21 and now reads these same two joined fields.  They come from the
        `vessels` join rather than the project row, so dropping the join -- or
        renaming an alias -- would silently reduce every chip to its fallback
        without failing any request.  A vessel whose code was corrected after the
        project was created is the case that separates the two: the project row
        keeps the copy it was created with, and the joined value is current.
        """
        pid = self.client.post('/api/dock-daily/projects',
                               json={'vessel_id': self.vessel, 'title': 'Test DD'}).get_json()['id']
        with appmod.app.app_context():
            appmod.execute('UPDATE vessels SET vsl_cd=? WHERE id=?', ('D002', self.vessel))
        row = next(p for p in self.client.get('/api/dock-daily/projects').get_json() if p['id'] == pid)
        self.assertEqual('DOCK DAILY TEST', row['vessel_name'])
        self.assertEqual('D002', row['vessel_vsl_cd'], 'joined code must be the live one')
        self.assertEqual('D001', row['vsl_cd'], 'the project row keeps its creation-time copy')
        self.assertEqual('IMO-TEST', row['vessel_imo'])
        # There is no single-project GET; PATCH is the other response the clients
        # decode into the same model, so it has to carry the join too or an edit
        # would blank the label until the next full list load.
        patched = self.client.patch(f'/api/dock-daily/projects/{pid}', json={'title': 'Renamed DD'})
        self.assertEqual(200, patched.status_code)
        body = patched.get_json()
        body = body.get('project', body)
        self.assertEqual('DOCK DAILY TEST', body['vessel_name'])
        self.assertEqual('D002', body['vessel_vsl_cd'])

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
        # The declaration has to reach the run (<span>) as well: Word resolves
        # inherited block and table fonts differently, so inheritance alone left
        # the pasted mail with two sizes.
        font = 'font-family:Arial,Helvetica,sans-serif;font-size:11pt'
        self.assertIn('<p style="margin:0;line-height:1.5"><span style="%s">&nbsp;</span></p></div>${v.html}' % font,
                      script)
        self.assertIn('%s;line-height:1.5;color:#222"><p style="margin:0">'
                      '<span style="%s"><b>제목: ${esc(v.subject)}</b></span></p>' % (font, font),
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

    def _png(self, size=(1200, 800)):
        """Pillow 가 실제로 읽는 PNG. 메일 본문 인라인이 이 바이트를 다시 읽으므로
        magic 바이트만 있는 가짜로는 사진 경로를 검증할 수 없다."""
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', size, (200, 120, 60)).save(buf, format='PNG')
        return buf.getvalue()

    def _fake_png(self):
        """PNG magic 은 통과하지만 Pillow 는 못 읽는 바이트. HEIC 처럼 본문에 넣을 수
        없는 첨부가 어떻게 처리되는지 보는 데 쓴다."""
        return b'\x89PNG\r\n\x1a\n' + b'0' * 32

    def _project_with_attachment(self, title, report_date='2026-08-20'):
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': title,
        }).get_json()
        report = self.client.post(
            f"/api/dock-daily/projects/{project['id']}/reports/generate",
            json={'report_date': report_date},
        ).get_json()
        up = self.client.post(
            f"/api/dock-daily/reports/{report['id']}/attachments",
            data={'file': (io.BytesIO(self._png()), 'shot.png')},
            content_type='multipart/form-data',
        )
        self.assertEqual(201, up.status_code, up.get_data(as_text=True))
        stored = os.path.join(routes_dock_daily.UPLOAD_DIR, up.get_json()['stored_name'])
        self.assertTrue(os.path.exists(stored))
        return project, report, stored

    def test_report_delete_removes_children_and_blobs_but_guards_final(self):
        """Deleting a report date takes its blocks, revisions and attachment
        files with it.  A finalized report is edit-locked, so the delete needs
        an explicit token instead of being either a plain click or impossible --
        a report finalized by mistake has no unlock route."""
        project, report, stored = self._project_with_attachment('Delete Report DD')
        rid = report['id']
        self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': report['revision'], 'status': 'final', 'operations': [],
        })
        blocked = self.client.delete(f'/api/dock-daily/reports/{rid}')
        self.assertEqual(409, blocked.status_code)
        self.assertIn('delete-final', blocked.get_json()['error'])
        self.assertTrue(os.path.exists(stored), 'a refused delete must not touch the blob')

        gone = self.client.delete(f'/api/dock-daily/reports/{rid}', json={'confirm': 'delete-final'})
        self.assertEqual(200, gone.status_code, gone.get_data(as_text=True))
        self.assertEqual({'attachments_found': 1, 'attachments_removed': 1, 'deleted': rid},
                         gone.get_json())
        self.assertFalse(os.path.exists(stored))
        self.assertEqual(404, self.client.get(f'/api/dock-daily/reports/{rid}').status_code)
        self.assertEqual([], self.client.get(
            f"/api/dock-daily/projects/{project['id']}/reports").get_json())
        with appmod.app.app_context():
            for table in ('dock_daily_block', 'dock_daily_attachment',
                          'dock_daily_report_revision', 'dock_daily_source_link'):
                self.assertEqual([], appmod.query(
                    'SELECT id FROM %s WHERE report_id=?' % table, (rid,)), table)
        self.assertEqual(404, self.client.delete(f'/api/dock-daily/reports/{rid}').status_code)

    def test_project_delete_needs_confirmation_then_cascades(self):
        """The project delete wipes every report under it, so a bare call is
        refused: a mis-click on the sidebar row must not erase a whole dock."""
        project, report, stored = self._project_with_attachment('Delete Project DD')
        pid = project['id']
        blocked = self.client.delete(f'/api/dock-daily/projects/{pid}')
        self.assertEqual(409, blocked.status_code)
        self.assertIn('delete-project', blocked.get_json()['error'])
        self.assertTrue(os.path.exists(stored))
        self.assertEqual(1, len(self.client.get('/api/dock-daily/projects').get_json()))

        gone = self.client.delete(f'/api/dock-daily/projects/{pid}', json={'confirm': 'delete-project'})
        self.assertEqual(200, gone.status_code, gone.get_data(as_text=True))
        self.assertEqual(1, gone.get_json()['attachments_removed'])
        self.assertFalse(os.path.exists(stored))
        self.assertEqual([], self.client.get('/api/dock-daily/projects').get_json())
        self.assertEqual(404, self.client.get(f"/api/dock-daily/reports/{report['id']}").status_code)
        with appmod.app.app_context():
            self.assertEqual([], appmod.query(
                'SELECT id FROM dock_daily_section_def WHERE project_id=?', (pid,)))
            self.assertEqual([], appmod.query(
                'SELECT id FROM dock_daily_report WHERE project_id=?', (pid,)))
        self.assertEqual(404, self.client.delete(
            f'/api/dock-daily/projects/{pid}', json={'confirm': 'delete-project'}).status_code)

    def test_soft_deleted_attachment_blob_is_purged_with_its_report(self):
        """`deleted_at` only hides the row; the file stays on disk.  A report
        delete that skipped hidden rows would leak those blobs forever."""
        _project, report, stored = self._project_with_attachment('Soft Delete DD')
        with appmod.app.app_context():
            appmod.execute("UPDATE dock_daily_attachment SET deleted_at=datetime('now') WHERE report_id=?",
                           (report['id'],))
        self.assertEqual([], self.client.get(
            f"/api/dock-daily/reports/{report['id']}").get_json()['attachments'])
        gone = self.client.delete(f"/api/dock-daily/reports/{report['id']}")
        self.assertEqual(200, gone.status_code)
        self.assertEqual(1, gone.get_json()['attachments_removed'])
        self.assertFalse(os.path.exists(stored))

    def test_purge_count_is_the_real_one_not_the_row_count(self):
        """`attachments_removed` must not be the number of rows found. If a blob
        is already gone from disk the row still cascades away, and reporting the
        row count would hide a leak that nothing else can detect."""
        _project, report, stored = self._project_with_attachment('Missing Blob DD')
        os.remove(stored)                        # blob vanished behind the app's back
        gone = self.client.delete(f"/api/dock-daily/reports/{report['id']}")
        self.assertEqual(200, gone.status_code)
        self.assertEqual(1, gone.get_json()['attachments_found'])
        self.assertEqual(0, gone.get_json()['attachments_removed'])

    def test_single_attachment_delete_takes_the_row_and_the_blob(self):
        """첨부 1건 삭제는 행과 파일을 함께 지운다.

        A `deleted_at` tombstone would be wrong here: the block path can leave
        one because deleting the report later sweeps the blob, but a report that
        lives on never revisits the row -- the file would sit on disk with
        nothing able to reach it.  The report itself must survive untouched, and
        a second delete of the same id is a 404, not a silent success."""
        _project, report, stored = self._project_with_attachment('Attachment Delete DD')
        rid = report['id']
        aid = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()['attachments'][0]['id']

        gone = self.client.delete(f'/api/dock-daily/attachments/{aid}')
        self.assertEqual(200, gone.status_code, gone.get_data(as_text=True))
        self.assertEqual({'attachments_found': 1, 'attachments_removed': 1, 'deleted': aid},
                         gone.get_json())
        self.assertFalse(os.path.exists(stored))
        with appmod.app.app_context():
            self.assertEqual([], appmod.query('SELECT id FROM dock_daily_attachment WHERE id=?', (aid,)))
        survivor = self.client.get(f'/api/dock-daily/reports/{rid}')
        self.assertEqual(200, survivor.status_code)
        self.assertEqual([], survivor.get_json()['attachments'])
        self.assertEqual(404, self.client.delete(f'/api/dock-daily/attachments/{aid}').status_code)
        self.assertEqual(404, self.client.get(f'/api/dock-daily/attachments/{aid}').status_code)

    def test_attachment_delete_is_refused_on_a_final_report(self):
        """Uploading to a 확정본 is 409 `final report is locked`, so removing from
        one has to be too.  If only the delete were open the edit lock would hold
        in one direction and the same content could still be changed -- by
        subtraction.  확정 취소 is the way in, and it is the report's decision."""
        _project, report, stored = self._project_with_attachment('Final Lock Att DD')
        rid = report['id']
        aid = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()['attachments'][0]['id']
        self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': report['revision'], 'status': 'final', 'operations': [],
        })
        blocked = self.client.delete(f'/api/dock-daily/attachments/{aid}')
        self.assertEqual(409, blocked.status_code)
        self.assertEqual('final report is locked', blocked.get_json()['error'])
        self.assertTrue(os.path.exists(stored), 'a refused delete must not touch the blob')
        self.assertEqual(1, len(self.client.get(f'/api/dock-daily/reports/{rid}').get_json()['attachments']))

        # 확정을 풀면 같은 호출이 통과해야 한다 -- 잠금이지 영구 봉인이 아니다.
        current = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        self.client.post(f'/api/dock-daily/reports/{rid}/status',
                         json={'status': 'editing', 'revision': current['revision']})
        self.assertEqual(200, self.client.delete(f'/api/dock-daily/attachments/{aid}').status_code)
        self.assertFalse(os.path.exists(stored))

    def test_attachment_delete_purges_a_hidden_row_and_counts_real_unlinks(self):
        """A row hidden by `deleted_at` is unreachable in the UI but its blob is
        still on disk, so the delete must accept it.  And the count reported is
        the number of files actually unlinked: if the blob already vanished
        behind the app's back, saying 1 would hide the only detectable leak."""
        _project, report, stored = self._project_with_attachment('Hidden Att DD')
        aid = self.client.get(
            f"/api/dock-daily/reports/{report['id']}").get_json()['attachments'][0]['id']
        with appmod.app.app_context():
            appmod.execute("UPDATE dock_daily_attachment SET deleted_at=datetime('now') WHERE id=?", (aid,))
        gone = self.client.delete(f'/api/dock-daily/attachments/{aid}')
        self.assertEqual(200, gone.status_code)
        self.assertEqual(1, gone.get_json()['attachments_removed'])
        self.assertFalse(os.path.exists(stored))

        _p2, report2, stored2 = self._project_with_attachment('Missing Att Blob DD', '2026-08-21')
        aid2 = self.client.get(
            f"/api/dock-daily/reports/{report2['id']}").get_json()['attachments'][0]['id']
        os.remove(stored2)
        counted = self.client.delete(f'/api/dock-daily/attachments/{aid2}').get_json()
        self.assertEqual(1, counted['attachments_found'])
        self.assertEqual(0, counted['attachments_removed'])

    def test_attachment_delete_unlinks_the_image_block_it_was_shown_in(self):
        """A block pointing at the deleted file must not keep pointing at it.

        The live mail body does not render images (`_render_section` is only
        called with `as_html=False` today), so a stale id breaks nothing that is
        visible right now -- which is why it would be missed.  The html branch
        turns any non-zero `attachment_id` into an `<img src=...>`, and after
        the delete that src is a 404 with nothing left to say what it was.  The
        block itself survives: it holds the caption the user wrote, and deleting
        a paragraph as a side effect of removing a file is the bigger
        surprise."""
        _project, report, stored = self._project_with_attachment('Linked Att DD')
        rid = report['id']
        fetched = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        aid = fetched['attachments'][0]['id']
        section = fetched['sections'][0]['section_key']
        saved = self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': fetched['revision'],
            'operations': [{'op': 'upsert', 'section_key': section, 'block_type': 'image',
                            'content': {'caption': 'Hull shot', 'attachment_id': aid}}],
        })
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        # 메일 본문이 사진을 싣는 유일한 경로다(형 지시 2026-08-21). 사진은 URL 이 아니라
        # 바이트로 들어가므로, 첨부가 사라지면 실을 것 자체가 없어진다.
        before = self.client.get(
            f'/api/dock-daily/reports/{rid}/email-preview').get_json()['html']
        self.assertIn('<img src="data:image/jpeg;base64,', before)
        self.assertIn('Hull shot', before)

        gone = self.client.delete(f'/api/dock-daily/attachments/{aid}')
        self.assertEqual(200, gone.status_code, gone.get_data(as_text=True))
        self.assertFalse(os.path.exists(stored))
        after = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        images = [b for b in after['blocks'] if b['block_type'] == 'image']
        self.assertEqual(1, len(images), 'the block itself must survive with its caption')
        self.assertIsNone(images[0]['content']['attachment_id'])
        self.assertEqual('Hull shot', images[0]['content']['caption'])
        after_html = self.client.get(
            f'/api/dock-daily/reports/{rid}/email-preview').get_json()['html']
        self.assertNotIn('<img', after_html)
        self.assertIn('Hull shot', after_html, 'caption 은 남아야 한다')
        self.assertIn('연결된 사진이 없습니다', after_html,
                      '사진이 빠진 사실이 본문에 남아야 한다 -- 조용히 빠지면 그대로 발송된다')
        self.assertNotIn(f'/api/dock-daily/attachments/{aid}', after_html)

    def test_attachment_unlink_lands_in_the_same_transaction_as_the_delete(self):
        """The reference is cleared before the commit, not in a second
        transaction: a render landing in between would see the row already gone
        while the block still pointed at it -- exactly the broken image the
        unlink exists to prevent."""
        src = inspect.getsource(routes_dock_daily._delete_cascade)
        body = src.split("db.execute('BEGIN IMMEDIATE')", 1)[1].split('db.commit()', 1)[0]
        self.assertIn('also(db, row)', body)
        self.assertIn('also', inspect.getsource(routes_dock_daily.attachment_delete))

    def test_attachment_rows_carry_their_own_delete_button(self):
        """The x cannot be nested inside the preview button -- invalid HTML, and
        one click would fire both -- so each attachment is a flex pair.  On a
        확정본 it is left out entirely instead of shown disabled: the server
        answers 409 there, and a button that is always refused reads as a bug."""
        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'dock_daily.js'),
                  encoding='utf-8') as f:
            script = f.read()
        self.assertIn('data-del-attachment', script)
        self.assertIn('dd-attachment-row', script)
        self.assertIn("state.report.status==='final'", script)
        self.assertIn('async function deleteAttachment', script)
        block = script.split('async function deleteAttachment', 1)[1].split('\n  }', 1)[0]
        self.assertIn('confirm(', block, '되돌릴 수 없는 삭제는 물어보고 나가야 한다')
        self.assertIn("method:'DELETE'", block)
        # The row leaves local state instead of triggering a report re-fetch: a
        # reload here would discard unsaved section edits, and the request only
        # changed attachments anyway. A failed call throws before the filter, so
        # the list cannot show a row the server still has -- or hide one it kept.
        self.assertIn('state.report.attachments=', block)
        self.assertNotIn('await api(`/api/dock-daily/reports/', block)
        # 404 counts as done: the row is already gone server-side (another tab,
        # another device), so keeping it on screen leaves the list behind the
        # server -- the one thing this local-removal path must not do.
        self.assertIn('r.status!==404', block)
        html = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('.dd-attachment-row{', html)
        self.assertIn('.dd-att-del{', html)

    def test_delete_reads_status_and_blob_names_inside_the_transaction(self):
        """The guard and the blob-name read both happen inside the delete's
        `BEGIN IMMEDIATE`. Reading either one earlier decided on stale state: a
        upload landing in between would keep its file while its row cascaded
        away, and a 확정 landing in between would let an unconfirmed delete
        through."""
        src = inspect.getsource(routes_dock_daily._delete_cascade)
        body = src.split("db.execute('BEGIN IMMEDIATE')", 1)[1]
        self.assertIn('guard(row)', body, 'the status guard must run inside the transaction')
        # The statement itself lives in `_CASCADE_*` (literal SQL at the execute
        # keeps the dynamic-SQL fence intact), so what is asserted here is where
        # it is *called* from -- which is the ordering contract this test is about.
        self.assertIn('target.select_blobs(', body, 'blob names must be read inside the transaction')
        self.assertIn('cur.rowcount != 1', body, 'a lost race must not report success')
        # The purge itself is the one part that must happen after the commit,
        # or a rolled back transaction leaves rows pointing at missing files.
        self.assertNotIn('_purge_files', body.split('db.commit()', 1)[0])
        self.assertIn('_purge_files', src.split('db.commit()', 1)[1])

    def test_attachment_upload_is_a_multi_file_dropzone_and_lists_have_delete(self):
        """첨부파일 등록 opens a drag-and-drop modal that takes several files at
        once, and both sidebar lists carry their own delete button.  The delete
        cannot be nested inside the selector button -- invalid HTML, and one
        click would fire both -- so each row is a flex pair."""
        html = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('id="dd-upload-modal"', html)
        self.assertIn('id="dd-dropzone"', html)
        self.assertIn('파일을 여기로 끌어다 놓으세요', html)
        self.assertIn('id="dd-upload-list"', html)
        self.assertIn('<input id="dd-file-input" type="file" multiple hidden', html)
        # The picker's filter must not be narrower than what the server accepts,
        # or iPhone .heic captures are invisible in the dialog.
        accept = re.search(r'id="dd-file-input"[^>]*accept="([^"]+)"', html).group(1)
        self.assertEqual(set(), {'.' + e for e in routes_dock_daily.ALLOWED_EXT} - set(accept.split(',')))

        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'dock_daily.js'),
                  encoding='utf-8') as f:
            script = f.read()
        self.assertIn('dropzone.addEventListener', script)
        self.assertIn("data-del-project", script)
        # 프로젝트는 행마다 삭제가 붙어 있지만, 보고서 일자는 드롭다운으로 접혔으므로
        # (형 지시 2026-08-21 ②) 행별 삭제가 아니라 "열려 있는 일자" 하나를 지우는
        # 버튼이 정본이다. 삭제 자체가 사라지면 안 된다는 계약은 그대로다.
        self.assertNotIn("data-del-report", script)
        self.assertIn("$('#dd-report-del').onclick", script)
        self.assertIn('deleteReport(id)', script)
        self.assertIn("confirm:'delete-project'", script)
        self.assertIn("confirm:'delete-final'", script)
        self.assertIn('class="dd-list-row"', script)
        # Uploads are sequential with a single reload at the end; parallel posts
        # would race the reload and could drop a row the server actually kept,
        # and a reload per file would discard the editor state repeatedly.
        self.assertIn('for(const file of list)', script)
        # Counts call sites, not the declaration, so 'await ' is part of the needle.
        self.assertEqual(1, script.count('await uploadOne(rid,file)'), 'one post per file, in the loop only')
        # The report id is pinned before the first post and a second batch is
        # refused while one is running: re-reading state.report per file would
        # scatter a batch across two reports if the user switched mid-upload.
        self.assertIn('const rid=state.report.id', script)
        self.assertIn('if(uploading)', script)
        self.assertIn('uploading=false', script)
        # And the closing reload is conditional, or edits typed during the
        # upload would be replaced by the server copy without warning.
        self.assertIn('if(state.report?.id===rid&&!state.dirty)', script)
        # A cancelled confirm() must release the delete button again.
        self.assertIn('finally { button.disabled = false; }', script)

    def _script(self, name='dock_daily.js'):
        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', name),
                  encoding='utf-8') as f:
            return f.read()

    def test_report_dates_are_a_dropdown_with_search_and_status_filter(self):
        """형 지시 2026-08-21 ②: "날짜가 많아지면 여기 칸이 쓸데없이 늘어남".
        일자 목록은 세로로 쌓지 않고 드롭다운 + 날짜검색 + 상태필터로 접는다.
        규칙 정본은 dock_daily_filter.js 이고 아이폰 앱과 같은 규칙을 쓴다."""
        html = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('id="dd-report-select"', html)
        self.assertIn('id="dd-report-search"', html)
        self.assertIn('id="dd-report-status"', html)
        self.assertIn('id="dd-report-del"', html)
        self.assertIn('js/dock_daily_filter.js', html)
        # 세로 목록은 사라졌다.
        self.assertNotIn('id="dd-report-list"', html)
        # 상태 필터의 값은 서버 status 어휘와 같아야 한다 — 하나라도 어긋나면
        # 그 필터는 영구히 0건을 낸다.
        options = set(re.findall(r'<option value="(\w+)">', html))
        self.assertEqual({'auto_draft', 'editing', 'final'}, options & {'auto_draft', 'editing', 'final'})

        script = self._script()
        self.assertIn('window.DockDailyReportFilter', script)
        self.assertIn("FILTER.apply(state.reports", script)
        self.assertIn("$('#dd-report-search').oninput=renderReportDates", script)
        self.assertIn("$('#dd-report-status').onchange=renderReportDates", script)
        # 규칙을 여기서 다시 구현하면 앱·모듈과 3중 정본이 된다.
        self.assertNotIn('function reportMatches', script)
        # 열린 보고서가 필터 밖으로 밀리면 드롭다운은 첫 행을 고른 것처럼 보인다.
        # 그 어긋남을 막는 stray 옵션이 계약이다.
        self.assertIn('필터 밖(열림)', script)
        # 저장 안 한 편집을 들고 다른 일자로 넘어가면 물어봐야 하고, 취소하면
        # 선택값을 되돌려야 한다.
        self.assertIn('!canLeaveDraft()){renderReportDates();return;}', script)
        # 프로젝트를 바꿀 때 필터를 비우지 않으면 새 프로젝트가 "보고서 없음" 으로 보인다.
        self.assertIn("$('#dd-report-search').value = ''", script)

    def test_report_date_filter_module_rules_actually_run(self):
        """문자열 검사만으론 규칙이 지켜지는지 알 수 없다. node 가 있으면 실행형
        테스트(tests/dock_daily_filter.test.js)를 그대로 돌린다."""
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node 없음 — 필터 실행형 테스트는 `node --test tests/dock_daily_filter.test.js`')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        done = subprocess.run([node, '--test', os.path.join('tests', 'dock_daily_filter.test.js')],
                              cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def test_report_date_can_be_corrected_from_the_web_ui(self):
        """형 지시 2026-08-21 ③: 잘못 입력한 날짜를 제자리에서 고친다.
        삭제 후 재생성으로 고치면 본문·첨부가 함께 날아가므로, 새 라우트가 아니라
        기존 PUT 에 `report_date` 를 실어 revision CAS·확정잠금을 물려받는다."""
        html = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('id="dd-date-modal"', html)
        self.assertIn('id="dd-date-edit"', html)
        self.assertIn('id="dd-date-new"', html)
        self.assertIn('id="dd-date-save"', html)
        self.assertIn('id="dd-notice"', html)
        # 다시 수집하지 않는다는 사실을 화면에서 밝힌다(사람이 고친 본문을 덮지 않으려고
        # 일부러 안 돌린다).
        self.assertIn('다시 수집하지 않습니다', html)

        script = self._script()
        self.assertIn("operations:[],report_date:next", script)
        self.assertIn("method:'PUT'", script)
        # 🔴 409 세 종류는 사람이 할 일이 서로 다르다. `error` 문구가 아니라 서버가 주는
        # `code` 로 갈라야 한다 — 확정잠금에 "최신본 불러오기" 를 띄우면 형은 풀리지 않는
        # 동작을 반복한다(올마이트 지적 2026-08-21, 아이폰 앱과 같은 분기).
        for code in ('date_taken', 'final_locked', 'revision_conflict'):
            self.assertIn(code + ':', script)
        self.assertIn('e.code = body.code', script)
        self.assertIn('conflictText(e)', script)
        self.assertIn('확정을 취소한 뒤 고치세요', script)
        # 날짜 정정도 같은 PUT 이라 revision 이 오른다. 남은 편집을 먼저 저장하지 않으면
        # 그 다음 저장이 409 로 막힌다.
        self.assertIn('if(state.dirty&&!locked)await save()', script)
        # await 사이에 다른 일자로 옮겨갔으면 그 보고서를 건드리면 안 된다.
        self.assertIn("if(state.report?.id!==rid)return;", script)
        # 프로젝트 id 도 미리 고정한다. await 뒤에 state 를 다시 읽으면 그 사이 옮겨간
        # 다른 프로젝트의 목록을 덮어쓴다(올마이트 지적 2026-08-21).
        self.assertIn('pid=state.project.id', script)
        self.assertIn("if(state.project?.id!==pid)return;", script)
        # 409 가 아닌 응답을 `code` 로 갈라 읽으면 엉뚱한 해법을 안내한다.
        self.assertIn("if (!e || e.status !== 409) return", script)
        # 드롭다운을 빠르게 바꾸면 응답이 역전될 수 있다. 늦게 온 이전 요청이 화면을
        # 덮으면 선택값과 본문이 어긋나므로 마지막 요청만 반영한다.
        self.assertIn('let selectSeq = 0', script)
        self.assertIn('if (seq !== selectSeq) return;', script)

    def test_web_itinerary_note_counts_the_final_reports_it_will_change(self):
        """🔴 일정은 프로젝트 열이고 확정본도 조인으로 읽는다 → 초안에서 저장한 일정이
        이미 확정된 보고서에도 반영된다. 막으면 정상적인 출거일 연기가 영구히 불가능해지므로
        계약으로 두고 몇 건이 함께 바뀌는지 세어 알린다(아이폰 앱과 같은 문구)."""
        html = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('id="dd-itinerary-note"', html)
        script = self._script()
        self.assertIn("state.reports.filter(r=>r.status==='final').length", script)
        self.assertIn('확정 ${finals}건 포함', script)
        # 일정 PATCH 는 일정 키만 보낸다 — 프로젝트 제목·자동생성 스위치가 함께 실리면
        # 일정만 고치려던 저장이 그 둘을 덮는다.
        patch = script.split('async function save()', 1)[1].split('const operations', 1)[0]
        self.assertIn(".dd-itinerary-date').forEach", patch)
        self.assertNotIn('title', patch)
        self.assertNotIn('auto_generate', patch)

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

    def _copy_fixture(self, dates=('2026-08-20', '2026-08-21')):
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Copy DD'}).get_json()
        out = []
        for d in dates:
            out.append(self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                        json={'report_date': d}).get_json())
        return p, out

    def test_previous_report_is_copied_card_for_card_without_the_files(self):
        """이전 일자 가져오기(형 지시 2026-08-21) -- 자동초안을 폐기한 대신 들어온 경로.

        복사된 카드는 이 보고서의 것이 되어야 한다: origin='manual', manual_override=1.
        원본이 자동수집이었더라도 그 provenance 는 이 날짜의 근거가 아니고, 다음 저장에서
        덮이면 형이 손으로 고친 문장이 사라진다.
        """
        p, (src, dst) = self._copy_fixture()
        src = self.client.put(f"/api/dock-daily/reports/{src['id']}", json={
            'revision': src['revision'], 'email_intro': '어제 인사말', 'safety_footer': '어제 안전문구',
            'operations': [
                {'section_key': 'shipyard', 'block_type': 'item', 'sort_order': 0,
                 'content': {'text': '탱크 세정 진행중'}},
                {'section_key': 'shipyard', 'block_type': 'paragraph', 'sort_order': 1,
                 'content': {'body': '내일 이어서 진행'}},
                {'section_key': 'survey', 'block_type': 'image', 'sort_order': 0,
                 'content': {'caption': '사진'}},
            ]}).get_json()
        with appmod.app.app_context():
            appmod.execute("UPDATE dock_daily_block SET origin='dock_auto' WHERE report_id=? "
                           "AND section_key='shipyard'", (src['id'],))
        got = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from",
                               json={'revision': dst['revision'], 'source_report_id': src['id']})
        self.assertEqual(200, got.status_code)
        body = got.get_json()
        self.assertEqual(2, body['copied_blocks'])
        self.assertEqual(1, body['skipped_blocks'], 'image 블록은 파일이 따라오지 않으므로 제외')
        self.assertEqual(dst['revision'] + 1, body['revision'])
        self.assertEqual('editing', body['status'])
        texts = sorted(json.dumps(b['content'], ensure_ascii=False) for b in body['blocks'])
        self.assertIn('탱크 세정 진행중', ' '.join(texts))
        self.assertIn('내일 이어서 진행', ' '.join(texts))
        for b in body['blocks']:
            self.assertEqual('manual', b['origin'])
            self.assertEqual(1, b['manual_override'])
        # 인사말·안전문구는 따라오지만 제목은 날짜를 품고 있어 그대로 남는다.
        self.assertEqual('어제 인사말', body['email_intro'])
        self.assertEqual('어제 안전문구', body['safety_footer'])
        self.assertIn('8/21', body['email_subject'])
        # 원본은 건드리지 않는다.
        origin = self.client.get(f"/api/dock-daily/reports/{src['id']}").get_json()
        self.assertEqual(3, len(origin['blocks']))
        self.assertEqual(src['revision'], origin['revision'])

    def test_copy_append_keeps_what_is_already_typed_and_replace_warns(self):
        p, (src, dst) = self._copy_fixture()
        self.client.put(f"/api/dock-daily/reports/{src['id']}", json={
            'revision': src['revision'], 'operations': [
                {'section_key': 'shipyard', 'block_type': 'item', 'content': {'text': '어제 작업'}}]})
        dst = self.client.put(f"/api/dock-daily/reports/{dst['id']}", json={
            'revision': dst['revision'], 'operations': [
                {'section_key': 'shipyard', 'block_type': 'item', 'content': {'text': '오늘 쓴 것'}}]}).get_json()
        appended = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': dst['revision'], 'source_report_id': src['id'], 'mode': 'append'}).get_json()
        joined = json.dumps(appended['blocks'], ensure_ascii=False)
        self.assertIn('오늘 쓴 것', joined, 'append 는 쓰고 있던 카드를 지우지 않는다')
        self.assertIn('어제 작업', joined)
        self.assertEqual(2, len(appended['blocks']))
        orders = [b['sort_order'] for b in appended['blocks'] if b['section_key'] == 'shipyard']
        self.assertEqual(len(set(orders)), len(orders), 'append 는 기존 뒤로 붙어 순서가 겹치지 않는다')
        replaced = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': appended['revision'], 'source_report_id': src['id'], 'mode': 'replace'}).get_json()
        self.assertEqual(1, len(replaced['blocks']))
        self.assertNotIn('오늘 쓴 것', json.dumps(replaced['blocks'], ensure_ascii=False))

    def test_copy_from_refuses_final_other_project_itself_and_stale_revision(self):
        p, (src, dst) = self._copy_fixture()
        other = self.client.post('/api/dock-daily/projects',
                                 json={'vessel_id': self.vessel, 'title': 'Other DD'}).get_json()
        stranger = self.client.post(f"/api/dock-daily/projects/{other['id']}/reports/generate",
                                    json={'report_date': '2026-08-22'}).get_json()
        cross = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': dst['revision'], 'source_report_id': stranger['id']})
        self.assertEqual(400, cross.status_code)
        self.assertEqual('cross_project', cross.get_json()['code'],
                         '다른 선박의 작업내역이 조용히 섞이면 안 된다')
        itself = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': dst['revision'], 'source_report_id': dst['id']})
        self.assertEqual(400, itself.status_code)
        self.assertEqual('same_report', itself.get_json()['code'])
        stale = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': dst['revision'] + 9, 'source_report_id': src['id']})
        self.assertEqual(409, stale.status_code)
        self.assertEqual('revision_conflict', stale.get_json()['code'])
        self.assertEqual(404, self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': dst['revision'], 'source_report_id': 987654}).status_code)
        final = self.client.post(f"/api/dock-daily/reports/{dst['id']}/status",
                                 json={'status': 'final', 'revision': dst['revision']}).get_json()
        locked = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': final['revision'], 'source_report_id': src['id']})
        self.assertEqual(409, locked.status_code)
        self.assertEqual('final_locked', locked.get_json()['code'],
                         '확정본은 다른 모든 쓰기와 같은 이유로 막힌다')

    def test_copy_replaces_greeting_even_when_no_card_can_follow(self):
        """카드가 0개 복사돼도 replace 의 인사말·안전문구는 따라와야 한다.

        올마이트 지적 2026-08-21: 원본이 이미지 카드만 가졌고 대상이 비어 있으면
        copied/deleted 가 모두 0 이라 "바뀐 게 없다" 로 판정해 rollback 이 인사말 UPDATE
        까지 되돌리고도 200 을 줬다. 계약이 말하는 것과 실제 동작이 갈라지는 자리다.
        """
        p, (src, dst) = self._copy_fixture()
        src = self.client.put(f"/api/dock-daily/reports/{src['id']}", json={
            'revision': src['revision'], 'email_intro': '어제 인사말', 'safety_footer': '어제 안전문구',
            'operations': [{'section_key': 'survey', 'block_type': 'image', 'sort_order': 0,
                            'content': {'caption': '사진뿐'}}]}).get_json()
        got = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from",
                               json={'revision': dst['revision'], 'source_report_id': src['id']})
        self.assertEqual(200, got.status_code)
        body = got.get_json()
        self.assertEqual(0, body['copied_blocks'])
        self.assertEqual(1, body['skipped_blocks'])
        self.assertEqual('어제 인사말', body['email_intro'])
        self.assertEqual('어제 안전문구', body['safety_footer'])
        self.assertEqual(dst['revision'] + 1, body['revision'],
                         '머리말이 바뀌었으면 revision 도 올라가야 다음 저장이 안전하다')
        # 두 번 눌러도 바뀌는 게 없으면 revision 을 올리지 않는다 -- 빈 스냅샷만 쌓인다.
        again = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from",
                                 json={'revision': body['revision'],
                                       'source_report_id': src['id']}).get_json()
        self.assertEqual(body['revision'], again['revision'])

    def test_copy_from_keeps_attachments_when_it_replaces_the_cards(self):
        """본문 교체가 파일 삭제까지 뜻하면 안 된다.

        블록이 사라지면 FK 가 attachment.block_id 를 NULL 로 풀 뿐이고, 첨부 행과 파일은
        목록에 그대로 남아야 한다 -- 형이 올린 파일을 본문 복사가 지우면 복구 경로가 없다.
        """
        p, (src, dst) = self._copy_fixture()
        dst = self.client.put(f"/api/dock-daily/reports/{dst['id']}", json={
            'revision': dst['revision'], 'operations': [
                {'section_key': 'shipyard', 'block_type': 'item', 'content': {'text': '지워질 카드'}}]}).get_json()
        up = self.client.post(f"/api/dock-daily/reports/{dst['id']}/attachments",
                              data={'file': (io.BytesIO(b'hello'), 'note.txt')},
                              content_type='multipart/form-data')
        self.assertEqual(201, up.status_code)
        # 업로드도 revision 을 올린다. 화면이 하듯 최신 revision 을 다시 읽고 보낸다.
        fresh = self.client.get(f"/api/dock-daily/reports/{dst['id']}").get_json()
        after = self.client.post(f"/api/dock-daily/reports/{dst['id']}/copy-from", json={
            'revision': fresh['revision'], 'source_report_id': src['id'], 'mode': 'replace'})
        self.assertEqual(200, after.status_code)
        self.assertEqual(1, len(after.get_json()['attachments']))

    def test_web_ui_offers_the_carry_forward_and_no_longer_offers_auto_draft(self):
        page = self.client.get('/dock-daily').get_data(as_text=True)
        script = self._script()
        self.assertIn('id="dd-copy-from"', page)
        self.assertIn('id="dd-copy-modal"', page)
        self.assertIn('id="dd-copy-src"', page)
        self.assertIn('id="dd-copy-append"', page)
        self.assertIn('/copy-from', script)
        self.assertIn("runCopy('append')", script)
        # 확정본으로 가져오기는 막고, 확정본에서 가져오기는 막지 않는다.
        self.assertIn("const rid=state.report.id, pid=state.project.id, src=+$('#dd-copy-src').value", script)
        self.assertIn('첨부파일과 이미지 카드, 지금 프로젝트에 없는 섹션의 카드는 따라오지 않습니다', page)
        # "이전 일자" 라는 이름대로 후보는 앞선 날짜만이다. 화면 DOM 하네스가 없어
        # 배선 문자열로 잠근다(백로그: jsdom 하네스).
        self.assertIn('r.report_date<today', script)
        self.assertIn('이미지 카드와 없어진 섹션의 카드', script,
                      'skipped_blocks 는 첨부 수가 아니다 — 문구가 계약을 따라야 한다')
        # 🔴 자동초안 opt-in 은 사라졌다. 남아 있으면 켤 수 있는데 동작하지 않는 스위치가 된다.
        for gone in ('id="ddp-auto"', 'id="ddp-active-from"', 'id="ddp-active-to"', 'id="ddp-source-ids"'):
            self.assertNotIn(gone, page, gone)
        # 주석에는 왜 껐는지 남아 있어도 되지만, 전송값과 배선은 사라져야 한다.
        for gone in ('setAutoFields', "$('#ddp-auto", 'auto_generate,', 'dock_manager_project_ids'):
            self.assertNotIn(gone, script, gone)

    def test_section_cards_grow_instead_of_scrolling_inside(self):
        """카드는 내용만큼 늘어난다(형 지시 2026-08-21).

        안쪽 스크롤바도, 손으로 끄는 리사이즈 핸들도 없어야 한다. 높이 자체는 DOM 없이
        확인할 수 없어(백로그: jsdom 하네스) 계약을 만드는 배선 문자열로 잠근다.
        """
        page = self.client.get('/dock-daily').get_data(as_text=True)
        script = self._script()
        self.assertIn('resize:none', page, '손으로 끄는 핸들이 남아 있으면 안 된다')
        self.assertNotIn('resize:vertical', page)
        # overflow:hidden 이 있어야 늘어나기 전에 안쪽 스크롤바가 뜨지 않는다.
        self.assertRegex(page, r'\.dd-section-edit\{[^}]*overflow:hidden')
        # 상한(max-height)을 두면 넘치는 순간 다시 안쪽 스크롤이 생긴다.
        self.assertNotRegex(page, r'\.dd-section-edit\{[^}]*max-height')
        self.assertIn("ta.style.height='auto'", script,
                      "먼저 줄이지 않으면 지워도 카드가 안 줄어든다")
        self.assertIn('ta.scrollHeight', script)
        # 입력·엔터번호·폭변화 3경로 모두 다시 재야 한다. 프로그램이 value 를 갈아끼우는
        # 엔터/번호붙이기 경로는 input 이벤트가 뜨지 않아 별도로 불러줘야 한다.
        self.assertIn("addEventListener('input',()=>autoGrow(ta))", script)
        # 숨은 동안(scrollHeight 0)에는 잴 수 없으니 보일 때 다시 잴 경로가 있어야 한다.
        # 컨테이너 폭이 0→N 으로 바뀌는 순간이 그 지점이다.
        self.assertIn('new ResizeObserver', script)
        self.assertIn("observe($('#dd-report'))", script)
        self.assertRegex(script, r'function applyNumbering\([^)]*\)\{[^}]*autoGrow\(ta\)')

    def test_web_never_edits_table_or_image_blocks(self):
        """표·이미지는 앱에서만 편집한다(형 지시 2026-08-21로 앱 편집기가 생겼다).

        웹은 모든 블록을 textarea 하나로 뿌리고 저장할 때 `block_type:'paragraph'` 로
        고정 전송한다. 표를 그 칸에 넣으면 한 번 건드리는 순간 표가 JSON 문자열 문단으로
        뭉개진다 — 되돌릴 수 없는 유실이라 편집 자체를 막는다.
        """
        page = self.client.get('/dock-daily').get_data(as_text=True)
        script = self._script()
        self.assertIn("function isTextBlock(b)", script)
        self.assertNotIn('JSON.stringify(c.rows', script,
                         '표를 textarea 에 JSON 으로 뿌리던 경로가 남아 있으면 안 된다')
        self.assertIn(':readOnlyBlock(b)', script, '표·이미지는 읽기 전용으로 그린다')
        # 저장 대상에서 제외 — textarea 가 없어 _edit 가 붙을 일도 없지만, 다른 경로로
        # 표시가 붙어도 paragraph 로 덮어쓰지 않게 한 겹 더 막는다.
        self.assertIn("!b._delete&&isTextBlock(b)&&(b._new||b._edit)", script)
        # 표만 있는 섹션도 글 칸을 받아야 한다(앱 DockDailySectionEditing 과 같은 판정).
        self.assertIn('b.section_key === s.section_key && isTextBlock(b)', script)
        self.assertIn('dd-block-table', page)
        # 표·이미지 삭제는 한 번 확인을 받는다 — 웹에는 다시 만들 수단이 없고, 이미지
        # 블록 삭제는 서버가 연결된 첨부까지 지운다(routes: block_id soft-delete).
        self.assertRegex(script, r'if\(!isTextBlock\(b\)&&!confirm\(')
        # 서버 upsert 는 content_json 을 통째로 교체한다. 종류와 나머지 키를 그대로 실어
        # 보내지 않으면 item 블록의 progress/status 가 저장 한 번에 사라진다.
        self.assertNotIn("block_type:'paragraph',content:{body:blockText(b)}", script)
        self.assertIn("block_type:b.block_type||'paragraph',content:{...(b.content||{})}", script)
        self.assertNotIn("b.block_type='paragraph'", script,
                         '수정만으로 블록 종류를 갈아치우면 안 된다')
        # 이미지 id 는 서버와 같은 판정(isdigit). Number() 는 -5·12.5 를 통과시킨다.
        self.assertIn(r"/^\d+$/.test(raw)", script)

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

    def test_report_date_is_corrected_in_place_and_carries_the_auto_subject(self):
        """날짜를 잘못 입력했을 때 지우지 않고 고친다(형 지시 2026-08-21).

        삭제 후 재생성으로 고치면 그 날짜에 이미 쓴 본문·첨부가 함께 사라진다.  그래서
        기존 PUT 을 타서 `_cas_begin` 의 revision CAS·확정잠금·스냅샷을 그대로 물려받는다.
        자동생성 제목은 날짜를 품고 있으므로 함께 따라와야 한다 -- 안 그러면 8/30 보고서가
        `(8/20)` 제목을 영구히 달고 나간다.
        """
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Date DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        self.assertIn('(8/20)', r['email_subject'])
        moved = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                json={'revision': r['revision'], 'operations': [],
                                      'report_date': '2026-08-30'})
        self.assertEqual(200, moved.status_code)
        body = moved.get_json()
        self.assertEqual('2026-08-30', body['report_date'])
        self.assertIn('(8/30)', body['email_subject'])
        # revision 이 오르고 스냅샷에 새 날짜가 남아야 감사 추적이 끊기지 않는다.
        self.assertGreater(body['revision'], r['revision'])
        with appmod.app.app_context():
            snaps = appmod.query(
                'SELECT snapshot_json FROM dock_daily_report_revision WHERE report_id=? ORDER BY id DESC',
                (r['id'],))
            self.assertTrue(snaps)
            self.assertIn('2026-08-30', snaps[0]['snapshot_json'])

    def test_report_date_correction_keeps_a_hand_written_subject(self):
        """손으로 쓴 제목은 덮지 않는다.  덮는 쪽의 손실이 더 크다."""
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Date DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        named = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                json={'revision': r['revision'], 'operations': [],
                                      'email_subject': '손으로 쓴 제목'}).get_json()
        moved = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                json={'revision': named['revision'], 'operations': [],
                                      'report_date': '2026-08-30'})
        self.assertEqual(200, moved.status_code)
        self.assertEqual('손으로 쓴 제목', moved.get_json()['email_subject'])
        self.assertEqual('2026-08-30', moved.get_json()['report_date'])

    def test_report_date_correction_refuses_empty_taken_and_final(self):
        """빈 값은 400, 이미 있는 날짜는 409 + `conflicting_report_id`, 확정본은 409.

        중복 날짜를 `IntegrityError` 로 흘리면 500 이 되고 호출자는 "저장 실패" 만 받는다.
        클라이언트가 revision 충돌과 갈라 읽을 수 있게 충돌 상대 id 를 함께 준다 --
        두 경우의 해법이 정반대다(최신본 불러오기 ↔ 다른 날짜 고르기).
        """
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Date DD'}).get_json()
        a = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        b = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-30'}).get_json()
        blank = self.client.put(f"/api/dock-daily/reports/{a['id']}",
                                json={'revision': a['revision'], 'operations': [], 'report_date': '  '})
        self.assertEqual(400, blank.status_code)
        taken = self.client.put(f"/api/dock-daily/reports/{a['id']}",
                                json={'revision': a['revision'], 'operations': [],
                                      'report_date': '2026-08-30'})
        self.assertEqual(409, taken.status_code)
        self.assertEqual(b['id'], taken.get_json().get('conflicting_report_id'))
        # 거절된 요청이 revision 을 올리거나 날짜를 옮기면 안 된다.
        still = self.client.get(f"/api/dock-daily/reports/{a['id']}").get_json()
        self.assertEqual('2026-08-20', still['report_date'])
        self.assertEqual(a['revision'], still['revision'])
        done = self.client.post(f"/api/dock-daily/reports/{a['id']}/status",
                                json={'status': 'final', 'revision': a['revision']}).get_json()
        locked = self.client.put(f"/api/dock-daily/reports/{a['id']}",
                                 json={'revision': done['revision'], 'operations': [],
                                       'report_date': '2026-09-01'})
        self.assertEqual(409, locked.status_code)
        self.assertIsNone(locked.get_json().get('conflicting_report_id'))

    def test_report_date_correction_survives_a_vessel_rename(self):
        """선박명이 바뀐 뒤에도 자동 제목의 날짜는 따라와야 한다(올마이트 지적 2026-08-21).

        전엔 저장된 제목을 `_auto_subject(현재 선박명, 옛 날짜)` 통짜와 비교했다.  개명 후엔
        어떤 제목도 그 문자열과 같지 않으므로, 8/30 으로 옮긴 보고서가 `(8/20)` 제목을
        영구히 달고 나갔다.  지금은 생성 제목의 **모양 + 옛 날짜 꼬리**만 맞춘다.
        """
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Rename DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        with appmod.app.app_context():
            appmod.execute('UPDATE vessels SET name=? WHERE id=?', ('RENAMED HULL', self.vessel))
        moved = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                json={'revision': r['revision'], 'operations': [],
                                      'report_date': '2026-08-30'})
        self.assertEqual(200, moved.status_code)
        subject = moved.get_json()['email_subject']
        self.assertIn('(8/30)', subject)
        self.assertNotIn('(8/20)', subject)
        # 개명 전 선박명은 제목에 그대로 남는다 -- 날짜만 옮기는 것이 이 경로의 계약이다.
        self.assertIn('DOCK DAILY TEST', subject)

    def test_report_date_correction_leaves_an_unrelated_subject_alone(self):
        """생성 제목 모양이 아니면 손대지 않는다."""
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Keep DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        for subject in ('입거 보고 (8/20)', '[Dock] 다른 양식 (8/20)', '[Dock] M/T X - Dock Daily Report (9/9)'):
            with self.subTest(subject=subject):
                with appmod.app.app_context():
                    appmod.execute('UPDATE dock_daily_report SET email_subject=? WHERE id=?',
                                   (subject, r['id']))
                current = self.client.get(f"/api/dock-daily/reports/{r['id']}").get_json()
                moved = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                        json={'revision': current['revision'], 'operations': [],
                                              'report_date': '2026-08-3%d' % (len(subject) % 9)})
                self.assertEqual(200, moved.status_code)
                self.assertEqual(subject, moved.get_json()['email_subject'])

    def test_rejected_date_correction_rolls_back_the_whole_request(self):
        """날짜 거절이 같은 요청의 본문 수정까지 되돌리는지(올마이트 지적).

        `_cas_begin` 이 `BEGIN IMMEDIATE` 를 열어둔 상태라, 날짜 검사에서 rollback 하면
        같은 요청의 `email_subject`·operations 도 함께 사라져야 한다.  일부만 남으면
        형은 "저장 실패" 를 보고도 본문이 바뀐 화면을 받는다.
        """
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Rollback DD'}).get_json()
        a = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                         json={'report_date': '2026-08-30'})
        taken = self.client.put(f"/api/dock-daily/reports/{a['id']}",
                                json={'revision': a['revision'], 'report_date': '2026-08-30',
                                      'email_subject': '함께 날아가야 하는 제목',
                                      'operations': [{'section_key': 'shipyard',
                                                      'block_type': 'paragraph',
                                                      'content': {'body': 'rolled back'}}]})
        self.assertEqual(409, taken.status_code)
        self.assertEqual('date_taken', taken.get_json().get('code'))
        after = self.client.get(f"/api/dock-daily/reports/{a['id']}").get_json()
        self.assertEqual('2026-08-20', after['report_date'])
        self.assertEqual(a['revision'], after['revision'])
        self.assertNotEqual('함께 날아가야 하는 제목', after['email_subject'])
        self.assertEqual([], [b for b in after['blocks']
                              if 'rolled back' in json.dumps(b.get('content_json') or {})])

    def test_conflicts_carry_a_machine_readable_code(self):
        """409 세 종류가 문자열이 아니라 `code` 로 구분되는지(올마이트 지적).

        클라이언트가 `error` 문구로 갈라 읽으면 문구 한 글자만 바뀌어도 확정잠금이
        revision 충돌 안내("다른 사용자가 먼저 저장함")로 오안내된다 -- 해법이 정반대다.
        """
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Code DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        stale = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                json={'revision': r['revision'] + 5, 'operations': []})
        self.assertEqual(409, stale.status_code)
        self.assertEqual('revision_conflict', stale.get_json().get('code'))
        done = self.client.post(f"/api/dock-daily/reports/{r['id']}/status",
                                json={'status': 'final', 'revision': r['revision']}).get_json()
        locked = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                 json={'revision': done['revision'], 'operations': []})
        self.assertEqual(409, locked.status_code)
        self.assertEqual('final_locked', locked.get_json().get('code'))

    def test_itinerary_patch_writes_nulls_and_reaches_final_reports(self):
        """iOS 일정 편집기의 서버쪽 계약.

        ① 명시적 `null` 이 NULL 로 들어가야 한다 -- 키를 빼면 "그대로 두기" 이므로
        화면에서 지운 날짜가 서버에 남는다.
        ② 🔴 일정은 프로젝트 열이고 확정본도 조인으로 읽는다 → 초안에서 저장한 일정이
        **이미 확정된 보고서에도 반영된다**.  이건 확정 잠금 우회이지만, 막으면 정상적인
        출거일 연기가 영구히 불가능해지므로 계약으로 인정하고 화면에 경고를 띄운다
        (올마이트 지적 2026-08-21).  여기서 잠그면 웹도 함께 깨진다.
        """
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Itin DD'}).get_json()
        early = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                 json={'report_date': '2026-08-20'}).get_json()
        draft = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                 json={'report_date': '2026-08-30'}).get_json()
        self.client.post(f"/api/dock-daily/reports/{early['id']}/status",
                         json={'status': 'final', 'revision': early['revision']})
        set_all = self.client.patch(f"/api/dock-daily/projects/{p['id']}",
                                   json={'berthing_date': '2026-08-30', 'dock_in_date': '2026-09-01',
                                         'dock_out_date': '2026-09-20', 'departure_date': '2026-10-04'})
        self.assertEqual(200, set_all.status_code)
        seen = self.client.get(f"/api/dock-daily/reports/{draft['id']}").get_json()['itinerary']
        self.assertEqual('2026-09-01', seen['dry_dock_in'])
        # ② 확정본도 같은 값을 읽는다.
        final_seen = self.client.get(f"/api/dock-daily/reports/{early['id']}").get_json()['itinerary']
        self.assertEqual('2026-09-01', final_seen['dry_dock_in'])
        cleared = self.client.patch(f"/api/dock-daily/projects/{p['id']}",
                                   json={'dock_in_date': None, 'dock_out_date': None})
        self.assertEqual(200, cleared.status_code)
        after = self.client.get(f"/api/dock-daily/reports/{draft['id']}").get_json()['itinerary']
        self.assertIsNone(after['dry_dock_in'])
        self.assertIsNone(after['dry_dock_out'])
        # ① 보내지 않은 키는 그대로 남는다.
        self.assertEqual('2026-10-04', after['departure'])
        self.assertEqual('2026-08-30', after['berthing'])

    def test_same_report_date_is_a_no_op_not_a_conflict(self):
        """같은 날짜를 그대로 보내도 자기 자신과 충돌났다고 하면 안 된다."""
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Date DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        same = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                               json={'revision': r['revision'], 'operations': [],
                                     'report_date': '2026-08-20'})
        self.assertEqual(200, same.status_code)
        self.assertEqual('2026-08-20', same.get_json()['report_date'])

    def _final_report(self):
        p = self.client.post('/api/dock-daily/projects',
                             json={'vessel_id': self.vessel, 'title': 'Toggle DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-20'}).get_json()
        done = self.client.post(f"/api/dock-daily/reports/{r['id']}/status",
                                json={'status': 'final', 'revision': r['revision']})
        self.assertEqual(200, done.status_code)
        return p, done.get_json()

    def test_status_route_is_the_only_way_out_of_final(self):
        """확정 / 확정취소 한 버튼 토글.

        `_cas_begin` 은 final 행의 모든 쓰기를 409 로 막고, 그 거절이 곧 잠금이다.
        해제를 그 헬퍼로 보내면 잠금은 영원히 못 열리므로, 상태 전용 라우트가
        자기 CAS 를 돌린다. 내용 쓰기가 해제에 편승할 수 없다는 게 요점이다.
        """
        p, final = self._final_report()
        self.assertEqual('final', final['status'])
        # 잠긴 상태에서 내용 저장은 여전히 막힌다.
        body = {'revision': final['revision'], 'operations': [
            {'section_key': 'shipyard', 'block_type': 'paragraph', 'content': {'body': 'x'}}]}
        self.assertEqual(409, self.client.put(f"/api/dock-daily/reports/{final['id']}",
                                              json=body).status_code)
        released = self.client.post(f"/api/dock-daily/reports/{final['id']}/status",
                                    json={'status': 'editing', 'revision': final['revision']})
        self.assertEqual(200, released.status_code)
        self.assertEqual('editing', released.get_json()['status'])
        # revision 은 올라간다 — 다른 탭이 들고 있던 값이 낡았다는 신호다.
        self.assertGreater(released.get_json()['revision'], final['revision'])
        # 해제 후엔 저장이 다시 열린다.
        again = self.client.put(f"/api/dock-daily/reports/{final['id']}",
                                json={'revision': released.get_json()['revision'],
                                      'operations': body['operations']})
        self.assertEqual(200, again.status_code)

    def test_status_change_lands_an_attributed_revision_snapshot(self):
        """잠금 해제는 조용해선 안 된다 — 누가 열었는지 남아야 한다."""
        _, final = self._final_report()
        self.client.post(f"/api/dock-daily/reports/{final['id']}/status",
                         json={'status': 'editing', 'revision': final['revision']})
        with appmod.app.app_context():
            rows = appmod.query(
                'SELECT revision, actor FROM dock_daily_report_revision'
                ' WHERE report_id=? ORDER BY revision', (final['id'],))
        self.assertEqual(final['revision'] + 1, rows[-1]['revision'])
        self.assertTrue(rows[-1]['actor'])

    def test_release_clears_the_stale_source_changed_flag(self):
        """`source_changed_after_final` 은 아무도 0 으로 되돌리지 않았다.

        확정이 단방향일 때는 보이지 않던 결함이다. 해제 경로가 생기면 재확정이
        지난 회차의 경고를 물려받고, 열린 초안이 이미 없는 잠금에 대한 깃발을
        계속 들고 있게 된다.
        """
        _, final = self._final_report()
        with appmod.app.app_context():
            appmod.execute('UPDATE dock_daily_report SET source_changed_after_final=1 WHERE id=?',
                           (final['id'],))
        released = self.client.post(f"/api/dock-daily/reports/{final['id']}/status",
                                    json={'status': 'editing', 'revision': final['revision']})
        self.assertEqual(200, released.status_code)
        with appmod.app.app_context():
            row = appmod.query('SELECT source_changed_after_final FROM dock_daily_report WHERE id=?',
                               (final['id'],), one=True)
        self.assertEqual(0, row['source_changed_after_final'])

    def test_status_route_rejects_bad_input_and_stale_revisions(self):
        _, final = self._final_report()
        rid = final['id']
        # 임의 상태로는 못 간다 — auto_draft 로 되돌리면 러너가 사람이 쓴 걸 덮는다.
        self.assertEqual(400, self.client.post(f'/api/dock-daily/reports/{rid}/status',
                                               json={'status': 'auto_draft',
                                                     'revision': final['revision']}).status_code)
        self.assertEqual(400, self.client.post(f'/api/dock-daily/reports/{rid}/status',
                                               json={'status': 'editing'}).status_code)
        # bool 은 int 의 서브클래스다 — revision=True 가 1 로 통과하면 안 된다.
        self.assertEqual(400, self.client.post(f'/api/dock-daily/reports/{rid}/status',
                                               json={'status': 'editing',
                                                     'revision': True}).status_code)
        stale = self.client.post(f'/api/dock-daily/reports/{rid}/status',
                                 json={'status': 'editing', 'revision': final['revision'] - 1})
        self.assertEqual(409, stale.status_code)
        self.assertEqual(final['revision'], stale.get_json()['current_revision'])
        self.assertEqual(404, self.client.post('/api/dock-daily/reports/999999/status',
                                               json={'status': 'editing', 'revision': 1}).status_code)

    def test_same_status_is_not_an_error_but_still_checks_revision(self):
        """더블클릭은 오류가 아니다. 다만 낡은 revision 을 숨겨주지도 않는다."""
        _, final = self._final_report()
        same = self.client.post(f"/api/dock-daily/reports/{final['id']}/status",
                                json={'status': 'final', 'revision': final['revision']})
        self.assertEqual(200, same.status_code)
        # no-op 이므로 revision 은 그대로 — 헛 bump 는 다른 탭을 이유 없이 깨운다.
        self.assertEqual(final['revision'], same.get_json()['revision'])

    def test_final_button_is_a_single_toggle_and_stays_clickable_when_locked(self):
        """확정 버튼은 잠긴 상태에서도 살아있어야 한다 — 잠금을 여는 유일한 통로다."""
        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'dock_daily.js'),
                  encoding='utf-8') as f:
            script = f.read()
        self.assertIn("['#dd-save','#dd-attach']", script)
        self.assertNotIn("'#dd-final','#dd-attach'", script)
        self.assertIn("finalBtn.disabled=false", script)
        self.assertIn("finalBtn.textContent=locked?'확정취소':'확정'", script)
        self.assertIn("state.report?.status==='final'?'editing':'final'", script)
        # 해제는 PUT 이 아니라 상태 전용 라우트로 간다.
        self.assertIn("/status`", script)

    def _api_key(self):
        with appmod.app.app_context():
            key = 'dock-daily-test-key'
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", (key,))
        return key

    def test_auto_draft_ingestion_is_retired_at_every_entrance(self):
        """자동 초안 수집 폐기(형 지시 2026-08-21).

        입구가 4개라 하나만 닫으면 나머지로 계속 들어온다.  목록까지 닫는 이유는
        러너가 "대상 있음"을 보고 merge 에서만 실패하면 폐기된 기능이 매일 실패
        알림을 내기 때문이다.
        """
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Retired DD', 'auto_generate': True,
            'active_from': '2026-08-01', 'active_to': '2026-08-31',
            'dock_manager_project_ids': ['v_DM17'],
        }).get_json()
        report = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                  json={'report_date': '2026-08-20'}).get_json()
        key = self._api_key()
        head = {'X-API-Key': key}
        event = {'source_table': 'jobs', 'source_id': '1', 'source_subkey': 'job:1:remark:2026-08-20',
                 'date': '2026-08-20', 'source_updated_at': '2026-08-20T17:00:00+09:00',
                 'kind': 'job_remark', 'title': 'Job', 'body': 'Done', 'suggested_section': 'shipyard'}
        payload = {'report_date': '2026-08-20', 'events': [event], 'complete': True, 'partial': False}
        calls = [
            self.client.post(f"/api/ext/dock-daily/projects/{p['id']}/merge", json=payload, headers=head),
            self.client.post(f"/api/ext/dock-daily/reports/{report['id']}/merge", json=payload, headers=head),
            self.client.post('/api/ext/dock-daily/merge',
                             json={**payload, 'project_id': p['id']}, headers=head),
            self.client.get('/api/ext/dock-daily/projects', headers=head),
        ]
        for res in calls:
            self.assertEqual(410, res.status_code)
            self.assertEqual('auto_draft_retired', res.get_json().get('code'))
        with appmod.app.app_context():
            # 🔴 폐기는 "거절"이어야 한다. 부분 적용이 남으면 사람이 쓴 본문과 섞인다.
            self.assertEqual(0, appmod.query('SELECT COUNT(*) n FROM dock_daily_block',
                                             one=True)['n'])
            self.assertEqual(0, appmod.query('SELECT COUNT(*) n FROM dock_daily_source_link',
                                             one=True)['n'])

    def test_runner_idempotency_and_partial_fail_closed(self):
        """되돌릴 수 있게 남겨 둔 수집 코드가 썩지 않았는지 본다.

        입구는 AUTO_DRAFT_INGEST_ENABLED 한 곳에서 닫혀 있고, 이 테스트만 그 스위치를
        올려 예전 계약(멱등 · partial fail-closed · 사라진 원천 정리)을 그대로 확인한다.
        """
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Test DD', 'auto_generate': True,
            'active_from': '2026-08-01', 'active_to': '2026-08-31',
            'dock_manager_project_ids': ['v_DM17'],
        }).get_json()
        event = {'source_table': 'jobs', 'source_id': '1', 'source_subkey': 'job:1:remark:2026-08-20',
                 'date': '2026-08-20', 'source_updated_at': '2026-08-20T17:00:00+09:00',
                 'kind': 'job_remark', 'title': 'Job', 'body': 'Done', 'suggested_section': 'shipyard'}
        key = self._api_key()
        routes_dock_daily.AUTO_DRAFT_INGEST_ENABLED = True
        self.addCleanup(setattr, routes_dock_daily, 'AUTO_DRAFT_INGEST_ENABLED', False)
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
        self.assertIn('<b>1. &nbsp;Shipyard</b>', preview['html'])
        font = 'font-family:Arial,Helvetica,sans-serif;font-size:11pt'
        run = '<span style="%s">%%s</span>' % font
        spacer = '<p style="margin:0;line-height:1.5">%s</p>' % (run % '&nbsp;')
        # Itinerary is a bordered table, and each cell's text sits in a <p>.
        box = 'border:1px solid #777;padding:3px 9px;%s' % font
        td = '<td style="%s"><p style="margin:0;%s">%%s</p></td>' % (box, font)
        self.assertIn('<table style="border-collapse:collapse;margin:0;%s">' % font, preview['html'])
        self.assertIn('<tr>%s%s</tr>' % (td % (run % 'BERTHING'), td % (run % '<b>2026.03.24</b>')),
                      preview['html'])
        self.assertIn('%s<p style="margin:0 0 6px">%s' % (spacer, run % '<b>1. &nbsp;Shipyard</b>'),
                      preview['html'])
        # Work items are hanging indented paragraphs.
        self.assertIn('<p style="margin:0 0 3px 52px;text-indent:-30px">%s</p>'
                      % (run % '2)&nbsp;&nbsp;Crane test &lt;Hull &amp; Valve&gt; &quot;ongoing&quot;'),
                      preview['html'])
        self.assertIn('Crane test &lt;Hull &amp; Valve&gt; &quot;ongoing&quot;</span></p>%s'
                      '<p style="margin:0 0 6px">%s' % (spacer, run % '<b>2. &nbsp;EGCS Retrofit</b>'),
                      preview['html'])
        self.assertNotIn('<Hull & Valve>', preview['html'])

    def test_email_only_table_is_the_itinerary_and_its_cells_wrap_text_in_paragraphs(self):
        """On the Outlook iOS paste that was measured, text sitting directly inside a
        <td> came out at ~8pt while paragraph text outside any table held the
        declared 11pt, and that did not move as the declaration was added to the
        wrapper <div>, then every <table>/<td>, then a <span> per text node. Cell
        text therefore goes inside a <p>; whether Outlook honours 11pt for a <p>
        inside a <td> is an untested hypothesis, so this test locks the markup
        shape only. The one remaining table is the itinerary, which needs the
        borders; work items stay paragraphs."""
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
        # Parsed, not regexed: unbalanced or mis-nested markup raises here.
        root = _Tree.parse(body)
        tables = _Tree.find(root, 'table')
        self.assertEqual(1, len(tables), body)
        self.assertFalse(_Tree.find(root, 'th') or _Tree.find(root, 'thead'), body)
        rows = _Tree.find(tables[0], 'tr')
        self.assertEqual(4, len(rows), 'itinerary is BERTHING/IN/OUT/DEPARTURE')
        for row in rows:
            cells = [k for k in row['kids'] if k['tag'] == 'td']
            self.assertEqual(2, len(cells), row)
            self.assertEqual(cells, row['kids'], 'only cells may sit in a row')
            for td in cells:
                # The whole point: a cell's text is inside a <p>, never in the cell.
                self.assertEqual(['p'], [k['tag'] for k in td['kids']], td)
                self.assertEqual('', td['text'].strip(), 'text sits directly in the <td>')
                self.assertIn('font-size:11pt', td['kids'][0]['attrs'].get('style', ''))
        # Every container still declares the size for clients that do inherit, and
        # every paragraph carries one text run declaring it. Matched inside tags
        # only: report text could contain the same literal string.
        tags = re.findall(r'<[^>]+>', body)
        containers = [t for t in tags if re.match(r'<(?:div|table|td)\b', t, re.I)]
        self.assertTrue(containers)
        for tag in containers:
            self.assertIn('font-size:11pt', tag)
        paragraphs = [t for t in tags if re.match(r'<p\b', t, re.I)]
        runs = [t for t in tags if re.match(r'<span\b', t, re.I)]
        self.assertTrue(paragraphs)
        self.assertEqual(len(paragraphs), len(runs))
        for tag in runs:
            self.assertIn('font-size:11pt', tag)

    def test_email_html_puts_every_text_run_in_an_11pt_span(self):
        """Cell level declarations were still not enough: the Outlook iOS paste kept
        a 16px cap height for <p> text against 12px inside <td>. The mechanism is
        not proven, only the divergence, so the workaround is to leave nothing to
        inherit — every text node sits in a <span> carrying the declaration."""
        font = 'font-family:Arial,Helvetica,sans-serif;font-size:11pt'
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Run DD', 'berthing_date': '2026-03-24'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-05-12'}).get_json()
        self.assertEqual(200, self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph',
                            'content': {'body': '갑판 도장 진행중\n프로펠러 검사 완료'}}],
        }).status_code)
        body = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()['html']
        opens = list(re.finditer(r'<p\b[^>]*>', body))
        self.assertTrue(opens)
        for tag in opens:
            self.assertTrue(body[tag.end():].startswith('<span style="%s">' % font),
                            body[tag.start():tag.end() + 80])
        # Nothing readable may be left outside those runs.
        outside = re.sub(r'<span style="%s">.*?</span>' % re.escape(font), '', body, flags=re.S)
        self.assertEqual('', re.sub(r'<[^>]+>', '', outside).strip())

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
        item_line = ('<p style="margin:0 0 3px 52px;text-indent:-30px">'
                     '<span style="font-family:Arial,Helvetica,sans-serif;font-size:11pt">'
                     '%s)&nbsp;&nbsp;%s</span></p>')
        self.assertIn(item_line % (1, 'Tank cleaning'), preview['html'])
        self.assertIn(item_line % (3, 'Anode renewal'), preview['html'])
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
        # SVMS 본문에는 표·사진을 넣지 않는다(형 지시 2026-08-21). 파이프로 편 표는
        # RMK 에서 표로 읽히지도 않는다.
        svms = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        syd = svms['fields']['RMK_SYD']
        self.assertEqual('1) Runner collected item', syd)
        for absent in ('WBT | Plan', 'No.1 | 05-01', '[Image]', 'Hull photo'):
            self.assertNotIn(absent, syd)
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/email-preview").get_json()
        # 메일에서는 표가 표로 나간다. 셀 텍스트는 `<td>` 안 `<p>` 계약을 지켜야 한다.
        self.assertIn('<table style="border-collapse:collapse', preview['html'])
        self.assertIn('<b>WBT</b></span></p></td>', preview['html'])
        self.assertIn('>No.1</span></p></td>', preview['html'])
        self.assertNotIn('1) 1)', preview['text'])
        self.assertIn('WBT | Plan', preview['text'], 'plain 폴백에는 표를 글로 남긴다')
        # 첨부가 없는 사진 카드는 이유를 본문에 남긴다.
        # 캡션은 `<내용>` 으로 표시한다(형 지시 2026-08-21).
        self.assertIn('<Hull photo> (연결된 사진이 없습니다)', preview['text'])
        self.assertNotIn('[Image] Hull photo', preview['text'])

    def _report_with_photo_block(self, title, payload=None, name='shot.png', caption='Hull shot'):
        """사진 첨부 1개 + 그 첨부를 가리키는 image 블록 1개가 있는 보고서."""
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': title}).get_json()
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-01'}).get_json()
        rid = report['id']
        up = self.client.post(f'/api/dock-daily/reports/{rid}/attachments',
                              data={'file': (io.BytesIO(payload or self._png()), name)},
                              content_type='multipart/form-data')
        self.assertEqual(201, up.status_code, up.get_data(as_text=True))
        aid = up.get_json()['id']
        fetched = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        saved = self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': fetched['revision'],
            'operations': [{'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'image',
                            'content': {'caption': caption, 'attachment_id': aid}}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        return rid, aid

    def _mail(self, rid):
        response = self.client.get(f'/api/dock-daily/reports/{rid}/email-preview')
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        return response.get_json()

    def test_mail_carries_the_photo_as_bytes_because_a_url_cannot_work(self):
        """형 지시 2026-08-21: "사진도 ios에는 나오는데 이메일 미리보기로는 안보임".

        URL 로는 절대 안 된다 -- 첨부 라우트는 `login_required` 이고 경로도 상대라서
        메일 클라이언트는 사진이 아니라 로그인 리다이렉트를 받는다. 그래서 바이트를
        본문에 싣는다. 이 테스트가 깨지면 다시 URL 로 되돌아간 것이다.
        """
        rid, aid = self._report_with_photo_block('Mail Photo DD')
        mail = self._mail(rid)
        self.assertIn('<img src="data:image/jpeg;base64,', mail['html'])
        self.assertNotIn(f'/api/dock-daily/attachments/{aid}', mail['html'])
        # 폭·높이는 HTML 속성으로 준다 -- Word HTML 엔진은 CSS 폭을 무시한다.
        self.assertIn(f'width="{routes_dock_daily.MAIL_IMAGE_SHOW_PX}"', mail['html'])
        self.assertRegex(mail['html'], r'height="\d+"')
        self.assertIn('Hull shot', mail['text'])

    def test_inline_photo_is_shrunk_before_it_goes_into_the_mail(self):
        """원본을 그대로 실으면 사진 한 장에 메일이 수 MB 가 된다(실측: 4.19MB 첨부)."""
        rid, _aid = self._report_with_photo_block('Mail Big Photo DD')
        html_body = self._mail(rid)['html']
        encoded = html_body.split('data:image/jpeg;base64,')[1].split('"')[0]
        raw = base64.b64decode(encoded)
        with PILImage.open(io.BytesIO(raw)) as shrunk:
            self.assertLessEqual(max(shrunk.size), routes_dock_daily.MAIL_IMAGE_MAX_PX)
        self.assertLess(len(raw), len(self._png()),
                        '줄인 사진이 원본보다 크면 줄인 의미가 없다')

    def test_inline_photo_follows_the_exif_rotation_the_iphone_wrote(self):
        """아이폰 사진은 회전값을 EXIF 로 들고 온다. 무시하면 메일에서 옆으로 눕는다."""
        landscape = PILImage.new('RGB', (1200, 800), (10, 20, 30))
        exif = landscape.getexif()
        exif[274] = 6                          # orientation: 90도 회전해서 봐야 함
        buf = io.BytesIO()
        landscape.save(buf, format='JPEG', exif=exif)
        rid, _aid = self._report_with_photo_block('Mail EXIF DD', payload=buf.getvalue(),
                                                  name='rotated.jpg')
        html_body = self._mail(rid)['html']
        width = int(re.search(r'<img src="data:image/jpeg;base64,[^"]+" width="(\d+)"',
                              html_body).group(1))
        height = int(re.search(r'width="%d" height="(\d+)"' % width, html_body).group(1))
        self.assertGreater(height, width, 'EXIF 를 적용하면 세로 사진이 되어야 한다')

    def test_mail_says_why_a_photo_could_not_go_inline(self):
        """HEIC 처럼 서버가 못 읽는 형식. 조용히 빠지면 형은 사진이 실렸다고 믿고 보낸다."""
        rid, _aid = self._report_with_photo_block('Mail Bad Photo DD',
                                                 payload=self._fake_png())
        mail = self._mail(rid)
        self.assertNotIn('<img', mail['html'])
        self.assertIn('본문에 넣을 수 없습니다', mail['html'])
        self.assertIn('Hull shot', mail['html'], 'caption 은 남아야 한다')
        self.assertIn('본문에 넣을 수 없습니다', mail['text'])

    def test_mail_photo_budget_is_bounded_and_the_overflow_is_visible(self):
        """메일 한 통 크기에 상한이 있어야 한다. 넘긴 사진은 이유를 남긴다."""
        rid, _aid = self._report_with_photo_block('Mail Budget DD')
        old = routes_dock_daily.MAIL_IMAGE_BUDGET
        routes_dock_daily.MAIL_IMAGE_BUDGET = 10
        try:
            mail = self._mail(rid)
        finally:
            routes_dock_daily.MAIL_IMAGE_BUDGET = old
        self.assertNotIn('<img', mail['html'])
        self.assertIn('용량 상한', mail['html'])

    def test_mail_table_widens_instead_of_dropping_a_long_row(self):
        """🔴 헤더보다 긴 행은 그 칸도 형이 적은 값이다. 서버도 자르지 않는다."""
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Mail Ragged Table DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-06-02'}).get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [{'section_key': 'shipyard', 'block_type': 'table', 'sort_order': 0,
                            'content': {'columns': ['A', 'B'],
                                        'rows': [['1', '2', '3', '4'], ['5'], 'not a row']}}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        mail = self._mail(r['id'])
        for value in ('1', '2', '3', '4', '5'):
            self.assertIn('>%s</span></p></td>' % value, mail['html'])
        # 배열이 아닌 행('not a row')도 한 칸 행으로 살아남는다 -- 값을 버리지 않는다.
        self.assertEqual(4, mail['html'].count('<tr>') - 4,
                         '헤더 1행 + 배열 2행 + 스칼라 1행이어야 한다')
        self.assertIn('>not a row</span>', mail['html'],
                      '배열이 아닌 행을 조용히 버리면 형이 적은 값이 사라진다')
        self.assertIn('A | B |  | ', mail['text'], '열을 넓혀 사각형으로 맞춘다')

    def test_table_shows_an_empty_cell_for_a_missing_value(self):
        """`None` 셀이 'None' 이라는 글자로 보이면 적지 않은 값을 적은 것처럼 나간다."""
        cols, rows = routes_dock_daily._table_grid(
            {'columns': ['A', None], 'rows': [[None, 'x']]})
        self.assertEqual(['A', ''], cols)
        self.assertEqual([['', 'x']], rows)

    def test_table_ignores_a_header_that_is_not_a_list(self):
        """문자열 columns 를 그대로 순회하면 글자마다 열이 하나씩 생긴다."""
        cols, rows = routes_dock_daily._table_grid({'columns': 'abc', 'rows': [['1']]})
        self.assertEqual([''], cols, '문자열 헤더는 무시하고 열 개수만 행에서 맞춘다')
        self.assertEqual([['1']], rows)

    def test_inline_photo_is_refused_before_the_file_is_opened_when_the_budget_is_gone(self):
        """예산이 없으면 변환하지 않는다. 거절 전에 디코드하면 그게 자원 소모 경로다."""
        opened = []
        real = routes_dock_daily._attachment_path
        routes_dock_daily._attachment_path = lambda name: opened.append(name) or real(name)
        try:
            image, note = routes_dock_daily._inline_image('whatever.png', 0)
        finally:
            routes_dock_daily._attachment_path = real
        self.assertIsNone(image)
        self.assertIn('용량 상한', note)
        self.assertEqual([], opened, '예산이 0이면 파일을 찾지도 않아야 한다')

    def test_inline_photo_refuses_a_resolution_too_large_to_decode(self):
        """20MB 업로드 게이트를 통과해도 디코드 후 메모리는 수십 배가 될 수 있다."""
        rid, aid = self._report_with_photo_block('픽셀 상한', payload=self._png((300, 300)))
        original = routes_dock_daily.MAIL_IMAGE_MAX_PIXELS
        routes_dock_daily.MAIL_IMAGE_MAX_PIXELS = 100
        try:
            html = self._mail(rid)['html']
        finally:
            routes_dock_daily.MAIL_IMAGE_MAX_PIXELS = original
        self.assertNotIn('<img src="data:image/jpeg', html)
        self.assertIn('해상도가 너무 큽니다', html)

    def test_mail_photo_count_is_capped_and_the_reason_is_visible(self):
        """장수 상한은 예산과 별개다. 상한을 넘은 사진은 이유를 본문에 남긴다."""
        rid, aid = self._report_with_photo_block('장수 상한', payload=self._png((60, 40)))
        original = routes_dock_daily.MAIL_IMAGE_MAX_COUNT
        routes_dock_daily.MAIL_IMAGE_MAX_COUNT = 0
        try:
            html = self._mail(rid)['html']
        finally:
            routes_dock_daily.MAIL_IMAGE_MAX_COUNT = original
        self.assertNotIn('<img src="data:image/jpeg', html)
        self.assertIn('장수 상한', html)

    def test_mail_never_claims_a_failed_photo_is_attached_to_the_mail(self):
        """'첨부파일로 확인' 은 이 메일에 첨부됐다는 뜻으로 읽힌다 -- 그 보장이 없다."""
        rid, aid = self._report_with_photo_block('문구', payload=self._fake_png())
        html = self._mail(rid)['html']
        self.assertIn('본문에 넣을 수 없습니다', html)
        self.assertNotIn('첨부파일로 확인', html)

    def _report_with_gallery(self, title, count=3, columns=2, captions=None, sizes=None):
        """사진 여러 장을 담은 격자 카드를 만든다(도크 리포트와 같은 계약).

        `sizes` 로 사진마다 원본 크기를 달리 줄 수 있다 -- 가로·세로가 섞였을 때 칸 높이가
        맞는지 보려면 같은 크기 사진만으로는 아무것도 증명되지 않는다.
        """
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': title}).get_json()
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-09'}).get_json()
        rid = report['id']
        ids = []
        for n in range(count):
            size = (sizes or [])[n] if n < len(sizes or []) else (900, 600)
            up = self.client.post(f'/api/dock-daily/reports/{rid}/attachments',
                                  data={'file': (io.BytesIO(self._png(size)), 'p%d.png' % n)},
                                  content_type='multipart/form-data')
            self.assertEqual(201, up.status_code, up.get_data(as_text=True))
            ids.append(up.get_json()['id'])
        names = captions or ['사진 %d' % (n + 1) for n in range(count)]
        fetched = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        saved = self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': fetched['revision'],
            'operations': [{'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'image',
                            'content': {'columns': columns,
                                        'images': [{'attachment_id': i, 'caption': c}
                                                   for i, c in zip(ids, names)]}}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        return rid, ids

    def test_grid_cells_get_one_height_without_cropping_the_photos(self):
        """가로·세로 사진을 같은 4:3 칸에 letterbox 하고 원본 전체를 보인다."""
        rid, _ids = self._report_with_gallery('높이 정렬 DD', count=2, columns=2,
                                              sizes=[(1200, 800), (720, 1560)])
        html = self._mail(rid)['html']
        boxes = re.findall(r'<img src="data:image/jpeg;base64,[^"]+" width="(\d+)" height="(\d+)"',
                           html)
        self.assertEqual(2, len(boxes), html[:400])
        self.assertEqual(boxes[0], boxes[1], '가로 사진과 세로 사진이 같은 칸을 받아야 한다')
        width, height = int(boxes[0][0]), int(boxes[0][1])
        ratio = routes_dock_daily.MAIL_IMAGE_CELL_RATIO
        self.assertEqual(round(width * ratio[1] / ratio[0]), height)

        # 세로 사진은 4:3 으로 잘라내지 않고 흰 좌우 여백 안에 보존한다. data URI 의
        # 실제 JPEG 를 열어 가운데는 사진색, 좌우 가장자리는 letterbox 인지 확인한다.
        payloads = re.findall(r'<img src="data:image/jpeg;base64,([^"]+)"', html)
        from PIL import Image
        portrait = Image.open(io.BytesIO(base64.b64decode(payloads[1]))).convert('RGB')
        encoded_width, encoded_height = portrait.size
        self.assertEqual(round(encoded_width * ratio[1] / ratio[0]), encoded_height)
        self.assertGreater(min(portrait.getpixel((0, encoded_height // 2))), 235)
        self.assertLess(portrait.getpixel((encoded_width // 2, encoded_height // 2))[0], 230)

    def test_a_photo_smaller_than_the_cell_still_lines_up(self):
        """🔴 작은 사진의 표시 크기를 실제 픽셀로 줄이면 그 칸만 좁아져 다시 어긋난다.

        `width`/`height` 는 HTML 속성이라 실제보다 크게 줘도 렌더러가 늘려준다. 정렬을
        우선하고 약간 흐려지는 쪽을 택했다는 계약을 여기서 잠근다.
        """
        rid, _ids = self._report_with_gallery('작은 사진 DD', count=2, columns=2,
                                              sizes=[(1200, 800), (80, 60)])
        boxes = re.findall(r'width="(\d+)" height="(\d+)"', self._mail(rid)['html'])
        photo_boxes = [b for b in boxes if int(b[0]) > 20]
        self.assertEqual(photo_boxes[0], photo_boxes[1],
                         '작은 사진도 같은 칸 크기를 받는다')

    def test_one_column_photo_keeps_its_whole_frame(self):
        """🔴 크롭은 옆 칸과 높이를 맞추기 위한 것이다. 1열은 옆 칸이 없다.

        여기서 크롭하면 형 사진의 위아래만 버린다(세로 사진은 절반 가까이). 옛 한 장짜리
        카드도 1열로 읽히므로 이 판정이 옛 보고서 사진까지 지킨다.
        """
        rid, _ids = self._report_with_gallery('1열 세로 DD', count=1, columns=1,
                                              sizes=[(720, 1560)])
        html = self._mail(rid)['html']
        box = re.search(r'<img src="data:image/jpeg;base64,[^"]+" width="(\d+)" height="(\d+)"',
                        html)
        self.assertIsNotNone(box, html[:400])
        self.assertGreater(int(box.group(2)), int(box.group(1)),
                           '세로 사진이 1열에서 가로로 잘리면 안 된다')

    def test_grid_caption_is_centered_and_wrapped_in_angle_brackets(self):
        """형 지시 2026-08-21: "사진 캡션 가운데 맞춤 + <내용>로 표시"."""
        rid, _ids = self._report_with_gallery('캡션 DD', count=2, columns=2,
                                              captions=['선수 도장', ''])
        mail = self._mail(rid)
        self.assertIn('&lt;선수 도장&gt;', mail['html'])
        self.assertIn('text-align:center', mail['html'])
        self.assertIn('- <선수 도장>', mail['text'])
        # 빈 캡션은 감싸지 않는다 -- 내용 없는 `<>` 는 형이 안 적은 것이 적힌 것처럼 보인다.
        self.assertNotIn('&lt;&gt;', mail['html'])
        self.assertNotIn('- <>', mail['text'])

    def test_grid_frames_each_photo_and_caption_in_the_same_cell(self):
        """형 지시 2026-08-22: 메일에서도 사진+캡션이 한 프레임의 격자로 보여야 한다."""
        rid, _ids = self._report_with_gallery('사진 프레임 DD', count=2, columns=2,
                                              captions=['좌현', '우현'])
        root = _Tree.parse(self._mail(rid)['html'])
        framed = [td for td in _Tree.find(root, 'td')
                  if 'border:1px solid #9CA3AF' in td['attrs'].get('style', '')]
        self.assertEqual(2, len(framed))
        for td in framed:
            self.assertEqual(1, len(_Tree.find(td, 'img')))
            captions = _Tree.find(td, 'p')
            self.assertEqual(1, len(captions), '캡션은 사진과 같은 td 안에 있어야 한다')
            self.assertIn('text-align:center', captions[0]['attrs'].get('style', ''))

    def test_a_caption_that_already_has_brackets_is_not_wrapped_twice(self):
        """🔴 형이 캡션에 직접 꺾쇠를 적어둔 옛 데이터가 `<<내용>>` 으로 나가면 안 된다."""
        rid, _ids = self._report_with_gallery('꺾쇠 중복 DD', count=2, columns=2,
                                              captions=['<선수 도장>', '선미'])
        mail = self._mail(rid)
        self.assertIn('&lt;선수 도장&gt;', mail['html'])
        self.assertNotIn('&lt;&lt;', mail['html'])
        self.assertIn('- <선미>', mail['text'])

    def test_empty_legacy_photo_card_makes_no_photo_at_all(self):
        """🔴 `{attachment_id:null, caption:''}` 은 **빈 카드**다. 사진 0장으로 읽어야 한다.

        값이 아니라 키의 존재로 판정하면 빈 칸 하나가 생기고, 메일에 형이 쓰지도 않은
        "연결된 사진이 없습니다" 가 나간다. 서버·웹·앱 세 곳이 같은 기준을 써야 한다.
        """
        items, columns = routes_dock_daily._image_gallery(
            {'attachment_id': None, 'caption': ''})
        self.assertEqual([], items)
        self.assertEqual(1, columns)
        # 캡션만 있으면 형이 적은 것이므로 칸이 생긴다(사유와 함께 나간다).
        items, _ = routes_dock_daily._image_gallery({'attachment_id': None, 'caption': '선수'})
        self.assertEqual(1, len(items))

    def test_photo_count_cap_counts_files_opened_not_photos_shipped(self):
        """🔴 상한은 연 파일 수로 센다. 성공 장수만 세면 예산이 마른 뒤부터 상한이 풀린다.

        예산을 10 으로 줄이면 어느 사진도 실리지 않는다. 그때도 장수 상한을 넘긴 사진은
        디코드를 시도하지 않고 상한 문구를 받아야 한다 -- 그게 자원 소모를 막는 지점이다.
        """
        over = routes_dock_daily.MAIL_IMAGE_MAX_COUNT + 2
        rid, _ids = self._report_with_gallery('장수 상한 DD', count=over, columns=4)
        budget = routes_dock_daily.MAIL_IMAGE_BUDGET
        routes_dock_daily.MAIL_IMAGE_BUDGET = 10
        try:
            mail = self._mail(rid)
        finally:
            routes_dock_daily.MAIL_IMAGE_BUDGET = budget
        self.assertNotIn('<img', mail['html'], '예산 10 이면 한 장도 실리지 않는다')
        cap = '본문 사진 장수 상한(%d장)' % routes_dock_daily.MAIL_IMAGE_MAX_COUNT
        self.assertEqual(2, mail['text'].count(cap),
                         '상한을 넘긴 2장은 파일을 열지 않고 상한 문구를 받는다')

    def test_section_key_survives_a_unique_collision(self):
        """🔴 SELECT→계산→INSERT 는 경쟁 구간이다. 겹치면 500 대신 다음 번호로 들어간다."""
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '키 충돌 DD'}).get_json()
        real = routes_dock_daily.execute
        state = {'hits': 0}

        def collide(sql, *args, **kwargs):
            # 첫 INSERT 만 다른 요청이 먼저 넣은 것처럼 만든다.
            if 'INSERT INTO dock_daily_section_def' in sql and state['hits'] == 0:
                state['hits'] += 1
                real(sql, *args, **kwargs)
                raise sqlite3.IntegrityError('UNIQUE constraint failed')
            return real(sql, *args, **kwargs)

        routes_dock_daily.execute = collide
        try:
            created = self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                                       json={'label': '비용 정산표'})
        finally:
            routes_dock_daily.execute = real
        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        keys = {s['section_key'] for s in created.get_json()['sections']}
        self.assertTrue({'sec_1', 'sec_2'} <= keys, keys)

    def test_image_gallery_reads_the_new_and_the_legacy_card(self):
        """형 지시 2026-08-21: 사진 카드는 도크 리포트처럼 여러 장 + 열 수를 갖는다.

        옛 한 장짜리 카드도 마이그레이션 없이 계속 열려야 한다 -- 라이브에 이미 그
        모양으로 저장된 카드가 있다.
        """
        items, columns = routes_dock_daily._image_gallery(
            {'images': [{'attachment_id': 7, 'caption': 'A'}, {'attachment_id': '8', 'caption': ''}],
             'columns': 3})
        self.assertEqual([{'attachment_id': 7, 'caption': 'A'},
                          {'attachment_id': 8, 'caption': ''}], items)
        self.assertEqual(3, columns)
        legacy, legacy_columns = routes_dock_daily._image_gallery(
            {'attachment_id': 4, 'caption': 'Hull'})
        self.assertEqual([{'attachment_id': 4, 'caption': 'Hull'}], legacy)
        self.assertEqual(1, legacy_columns, '옛 카드는 한 칸 격자로 읽는다')

    def test_image_gallery_clamps_columns_and_drops_junk_entries(self):
        """열 수는 1~4 다(도크 리포트와 같은 상한). 손상된 값이 격자를 깨면 안 된다."""
        for raw, expected in ((0, 1), (-3, 1), (99, 4), ('four', 1), (None, 1), (2, 2)):
            items, columns = routes_dock_daily._image_gallery({'images': [], 'columns': raw})
            self.assertEqual(expected, columns, 'columns=%r' % raw)
            self.assertEqual([], items)
        items, _ = routes_dock_daily._image_gallery(
            {'images': ['nope', None, 12, {'attachment_id': 'x', 'caption': 'C'}]})
        self.assertEqual([{'attachment_id': None, 'caption': 'C'}], items,
                         'dict 아닌 항목은 버리고, 숫자 아닌 id 는 빈 칸으로 남긴다')

    def test_mail_lays_the_photos_out_in_the_grid_the_card_asked_for(self):
        """2열이면 한 줄에 사진 두 장. 셀 폭은 본문 폭을 나눈 값이다."""
        rid, _ids = self._report_with_gallery('사진 격자 DD', count=3, columns=2)
        mail = self._mail(rid)
        html_body = mail['html']
        self.assertEqual(3, html_body.count('<img src="data:image/jpeg;base64,'))
        frame_extra = 2 * (routes_dock_daily.MAIL_IMAGE_FRAME_PAD_PX * 2 + 2)
        cell = (routes_dock_daily.MAIL_BODY_PX - frame_extra) // 2
        self.assertIn('width="%d"' % cell, html_body)
        self.assertNotIn('width="%d"' % routes_dock_daily.MAIL_IMAGE_SHOW_PX, html_body,
                         '2열 사진에 1열 표시폭을 쓰면 옆 칸을 밀어낸다')
        for caption in ('사진 1', '사진 2', '사진 3'):
            self.assertIn(caption, html_body)
            self.assertIn(caption, mail['text'])

    def test_mail_photo_bytes_shrink_with_the_column_count(self):
        """4열에서 작게 보이는 사진에 1열짜리 바이트를 싣는 건 낭비다."""
        wide, _ = self._report_with_gallery('사진 1열 DD', count=1, columns=1)
        narrow, _ = self._report_with_gallery('사진 4열 DD', count=1, columns=4)

        def payload(rid):
            body = self._mail(rid)['html']
            return base64.b64decode(body.split('data:image/jpeg;base64,')[1].split('"')[0])

        self.assertLess(len(payload(narrow)), len(payload(wide)))

    def test_mail_omits_empty_caption_paragraphs_without_adding_a_grid_row(self):
        """빈 캡션은 불필요한 문단을 만들지 않고, 캡션은 언제나 별도 표 행이 아니다."""
        with_caption, _ = self._report_with_gallery('캡션 있음 DD', count=2, columns=2)
        rows = self._mail(with_caption)['html'].count('<tr>')
        without, _ = self._report_with_gallery('캡션 없음 DD', count=2, columns=2,
                                               captions=['', ''])
        empty_html = self._mail(without)['html']
        self.assertEqual(rows, empty_html.count('<tr>'), '캡션은 별도 표 행이 아니다')
        root = _Tree.parse(empty_html)
        framed = [td for td in _Tree.find(root, 'td')
                  if 'border:1px solid #9CA3AF' in td['attrs'].get('style', '')]
        self.assertEqual(2, len(framed))
        self.assertTrue(all(len(_Tree.find(td, 'p')) == 0 for td in framed))

    def test_mail_gives_a_table_or_photo_card_no_item_number(self):
        """🔴 형 지시 2026-08-21: 표·사진은 하위항목이 아니라 그 자리에 놓인 블록이다."""
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '번호 없음 DD'}).get_json()
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-10'}).get_json()
        rid = report['id']
        up = self.client.post(f'/api/dock-daily/reports/{rid}/attachments',
                              data={'file': (io.BytesIO(self._png((300, 200))), 'g.png')},
                              content_type='multipart/form-data')
        aid = up.get_json()['id']
        fetched = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        saved = self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': fetched['revision'],
            'operations': [
                {'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'item',
                 'sort_order': 1, 'content': {'title': '첫 작업'}},
                {'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'table',
                 'sort_order': 2, 'content': {'columns': ['항목', '금액'], 'rows': [['도장', '100']]}},
                {'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'image',
                 'sort_order': 3, 'content': {'columns': 1,
                                              'images': [{'attachment_id': aid, 'caption': '외판'}]}},
                {'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'item',
                 'sort_order': 4, 'content': {'title': '둘째 작업'}}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        mail = self._mail(rid)
        # 글 항목만 번호를 잇는다. 표·사진이 번호를 먹으면 2) 가 사라져 형이 보기에
        # 작업이 빠진 것처럼 된다.
        self.assertIn('1) 첫 작업', mail['text'])
        self.assertIn('2) 둘째 작업', mail['text'])
        self.assertNotIn('3)', mail['text'])
        self.assertNotIn('4)', mail['text'])
        # html 도 같다. 표는 `1)` 줄 **뒤에** 번호 없이 오고, 그 다음 글이 `2)` 를 받는다.
        html = mail['html']
        first = html.index('>1)&nbsp;&nbsp;첫 작업</span></p>')
        second = html.index('>2)&nbsp;&nbsp;둘째 작업</span></p>')
        grid = html.index('<table style="border-collapse:collapse;margin:0 0 8px 52px">')
        self.assertLess(first, grid)
        self.assertLess(grid, second)

    def test_attachment_delete_only_empties_its_own_slot_in_a_gallery(self):
        """격자에서 사진 하나를 지우면 그 칸만 빈다. 나머지 장과 캡션·열 수는 남는다."""
        rid, ids = self._report_with_gallery('격자 첨부삭제 DD', count=3, columns=3)
        removed = self.client.delete(f'/api/dock-daily/attachments/{ids[1]}')
        self.assertEqual(200, removed.status_code, removed.get_data(as_text=True))
        after = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        blocks = [b for b in after['blocks'] if b['block_type'] == 'image']
        self.assertEqual(1, len(blocks))
        content = blocks[0]['content']
        self.assertEqual(3, content['columns'], '열 수는 유지된다')
        self.assertEqual([ids[0], None, ids[2]], [x['attachment_id'] for x in content['images']])
        self.assertEqual(['사진 1', '사진 2', '사진 3'], [x['caption'] for x in content['images']],
                         '캡션은 남는다 -- 사진만 사라진 것이다')

    def test_section_create_route_makes_a_titled_section_of_its_own(self):
        """형 지시 2026-08-21: 표는 하위항목이 아니라 제목을 가진 하나의 섹션이다."""
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '섹션 추가 DD'}).get_json()
        created = self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                                   json={'label': '비용 정산표'})
        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        sections = {s['section_key']: s for s in created.get_json()['sections']}
        self.assertIn('sec_1', sections, 'key 는 서버가 만든다 -- 웹·앱이 각자 만들면 갈라진다')
        self.assertEqual('비용 정산표', sections['sec_1']['label'])
        self.assertEqual('special', sections['sec_1']['kind'])
        self.assertTrue(sections['sec_1']['enabled'])
        # 두 번째 섹션은 키가 겹치지 않아야 한다.
        again = self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                                 json={'label': '검사 일정'})
        self.assertEqual(201, again.status_code)
        keys = {s['section_key'] for s in again.get_json()['sections']}
        self.assertTrue({'sec_1', 'sec_2'} <= keys, keys)
        # 제목은 필수다. 빈 제목은 이름 없는 섹션을 만든다.
        self.assertEqual(400, self.client.post(
            f"/api/dock-daily/projects/{project['id']}/sections", json={'label': '  '}).status_code)
        self.assertEqual(404, self.client.post(
            '/api/dock-daily/projects/999999/sections', json={'label': 'X'}).status_code)

    def test_added_section_gets_its_own_numbered_mail_heading(self):
        """새 섹션은 메일에서 다른 섹션과 같은 `N. 제목` 머리글을 받는다."""
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '섹션 메일 DD'}).get_json()
        self.assertEqual(201, self.client.post(
            f"/api/dock-daily/projects/{project['id']}/sections",
            json={'label': '비용 정산표'}).status_code)
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-11'}).get_json()
        fetched = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': fetched['revision'],
            'operations': [{'op': 'upsert', 'section_key': 'sec_1', 'block_type': 'table',
                            'content': {'columns': ['항목', '금액'], 'rows': [['도장', '100']]}}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        mail = self._mail(report['id'])
        self.assertIn('2. 비용 정산표', mail['text'],
                      'Shipyard 다음, Survey 앞이 special 자리다')
        self.assertIn('비용 정산표', mail['html'])
        self.assertIn('도장', mail['html'])

    def test_inline_image_refuses_a_path_outside_the_upload_dir(self):
        """`stored_name` 은 DB 값이지만 경로 조립은 여기서 한다."""
        with appmod.app.app_context():
            self.assertIsNone(routes_dock_daily._attachment_path('../../etc/passwd'))
            self.assertIsNone(routes_dock_daily._attachment_path(None))
            image, note = routes_dock_daily._inline_image('../../etc/passwd', 10 ** 9)
        self.assertIsNone(image)
        self.assertIn('찾을 수 없습니다', note)

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
