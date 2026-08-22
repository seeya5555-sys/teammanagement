"""Dock Daily Report contracts (web MVP).

These tests use the same temporary-DB pattern as the existing Flask tests and
avoid any external Dock Manager/SVMS service.
"""
import base64
import builtins
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
        # `save` 는 섹션 순서를 함께 실어 보낼 수 있어 인자를 받는다(형 지시 2026-08-22).
        patch = script.split('async function save(', 1)[1].split('const operations', 1)[0]
        self.assertIn(".dd-itinerary-date').forEach", patch)
        self.assertNotIn('title', patch)
        self.assertNotIn('auto_generate', patch)

    def test_web_section_cards_carry_the_move_and_delete_tools(self):
        """웹에도 카드 이동·삭제가 있어야 한다(형 지시 2026-08-22 — "웹은 카드 이동,
        삭제 기능은 없어?").  앱은 카드 제목줄 롱프레스로 같은 일을 한다."""
        html = self.client.get('/dock-daily').get_data(as_text=True)
        # 순서 규칙은 앱과 같은 값을 내야 하므로 순수 모듈 한 곳에 있다.
        self.assertIn('js/dock_daily_section_order.js', html)
        script = self._script()
        self.assertIn('data-move-section', script)
        self.assertIn('data-del-section-card', script)
        # 🔴 순서와 미저장 글은 **한 번의 CAS PUT** 으로 간다.  나눠 보내면 첫 요청이
        # revision 을 올려 두 번째가 409 로 튕기고, 그때 보내는 순서는 옛 목록 기준이라
        # 다른 기기가 방금 바꾼 순서를 조용히 되돌린다.
        move = script.split('async function moveSection(', 1)[1].split('async function toggleSpecial', 1)[0]
        self.assertIn('save(ORDER.payload(next))', move)
        self.assertNotIn("method:'PUT'", move, '순서 전용 PUT 을 따로 만들지 않는다')
        # 🔴 고정 섹션에는 삭제 버튼을 아예 안 낸다 — 서버가 `fixed_section` 으로 거절하므로
        # 늘 실패하는 버튼이 된다.
        tools = script.split('function sectionTools(', 1)[1].split('function renderSections', 1)[0]
        self.assertIn("s.kind==='special'", tools)
        self.assertIn('if(locked)return', tools, '확정본은 도구를 내지 않는다')

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

    def test_web_never_edits_image_blocks_and_never_puts_a_table_in_a_textarea(self):
        """사진은 웹에서 편집하지 않고, 표는 **절대 textarea 로 뿌리지 않는다**.

        옛 웹은 모든 블록을 textarea 하나로 뿌리고 저장할 때 `block_type:'paragraph'` 로
        고정 전송했다. 표를 그 칸에 넣으면 한 번 건드리는 순간 표가 JSON 문자열 문단으로
        뭉개진다 -- 되돌릴 수 없는 유실이다. 표 편집기가 생긴 뒤에도(형 질문 2026-08-22)
        그 경로는 여전히 금지다. 사진은 업로드 정본이 첨부 카드라 읽기 전용을 유지한다.
        """
        page = self.client.get('/dock-daily').get_data(as_text=True)
        script = self._script()
        self.assertIn("function isTextBlock(b)", script)
        self.assertNotIn('JSON.stringify(c.rows', script,
                         '표를 textarea 에 JSON 으로 뿌리던 경로가 남아 있으면 안 된다')
        self.assertIn('readOnlyBlock(b)', script, '사진·확정본 표는 읽기 전용으로 그린다')
        # 🔴 사진 블록은 저장 대상에서 **영구 제외**다. 웹에 편집기가 없으므로 화면이
        # 읽어들인 모양(옛 한 장 카드 등)을 되쓰면 앱이 만든 계약을 덮어쓴다.
        self.assertIn("if(b.block_type==='image')return false;", script)
        self.assertIn("!b._delete&&(b._new||b._edit)&&savable(b)", script)
        # 🔴 표를 담은 special 섹션에는 글 칸을 만들지 않는다(형 지시 2026-08-22).
        # 앱 `DockDailySectionEditing.needsTextDraft` 와 같은 **값 기준** 판정이다 —
        # 표 전용 플래그가 없으므로 "special 인데 표가 들어 있다" 를 그 표식으로 쓴다.
        # 고정 섹션(Shipyard/Survey/Vendor/Remark)은 표가 있어도 글 칸을 유지한다.
        self.assertIn("s.kind === 'special' && own.some(b => b.block_type === 'table')", script)
        self.assertIn('own.some(b => isTextBlock(b))', script)
        self.assertIn('dd-block-table', page)
        # 표·이미지 삭제는 한 번 확인을 받는다 — 표는 적어 둔 내용이 통째로 사라지고,
        # 이미지 블록 삭제는 서버가 연결된 첨부까지 지운다(routes: block_id soft-delete).
        self.assertRegex(script, r'if\(!isTextBlock\(b\)&&!confirm\(')
        # 서버 upsert 는 content_json 을 통째로 교체한다. 종류와 나머지 키를 그대로 실어
        # 보내지 않으면 item 블록의 progress/status 가 저장 한 번에 사라진다.
        self.assertNotIn("block_type:'paragraph',content:{body:blockText(b)}", script)
        self.assertIn("block_type:b.block_type||'paragraph',content:{...(b.content||{})}", script)
        self.assertNotIn("b.block_type='paragraph'", script,
                         '수정만으로 블록 종류를 갈아치우면 안 된다')
        # 이미지 id 는 서버와 같은 판정(isdigit). Number() 는 -5·12.5 를 통과시킨다.
        self.assertIn(r"/^\d+$/.test(raw)", script)

    def test_web_can_add_a_table_section_and_edit_the_table(self):
        """웹에도 표 섹션 버튼과 표 편집기가 있어야 한다.

        형 질문 2026-08-22 "표 섹션 넣는 버튼은 웹에 있니?" -- 앱은 OTA 245 부터 있었고
        웹에는 `＋ 섹션` 하나뿐이라 표는 앱에서만 만들 수 있었다.  버튼만 달면 웹에서
        채울 수가 없어 빈 표 카드만 늘어나므로 편집기까지 함께 둔다.
        """
        page = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('js/dock_daily_table.js', page)
        script = self._script()
        self.assertIn('dd-section-add-table', script)
        # 🔴 형 지시 2026-08-22 "기존 섹션에 표추가는 필요 없어(ios랑 똑같이 해줘)".
        # 표는 제목을 직접 받는 `＋ 표 섹션` 으로만 만든다.  카드의 `＋ 표` 는 앱
        # `canAddTable` 과 같은 조건 -- **빈 special 섹션**(표 삽입이 실패해 남은 고아
        # 카드) 에만 남긴다.  아예 없애면 그 카드를 채울 길이 사라진다(올마이트 지적).
        self.assertIn('data-add-table', script, '고아 섹션 복구 경로는 남아 있어야 한다')
        tools = script.split('function sectionTools(', 1)[1].split('function renderSections(', 1)[0]
        self.assertIn('TABLE.canAddTable(', tools, '표 버튼은 게이트를 통과해야 한다')
        # 🔴 앱은 초안을 `report.blocks` 에 넣지 않는다.  웹은 `ensureSectionEditors` 가
        # 빈 카드에 글칸 초안을 밀어넣으므로 그걸 세면 고아 카드에서 버튼이 사라진다.
        self.assertIn('bs.filter(b=>!b._new).length', script)
        add = script.split('async function addSection(', 1)[1].split('async function addTable(', 1)[0]
        # 🔴 확정 판정이 섹션 생성보다 **먼저**다(앱 addTableSection 과 같은 순서).
        # 뒤에 보면 표 저장만 409 로 튕기고 빈 섹션은 모든 일자에 영구히 남는다.
        self.assertLess(add.index("status==='final'"), add.index('/sections`'),
                        '확정본 게이트가 POST /sections 보다 앞에 있어야 한다')
        # 🔴 방금 만든 키는 서버가 준 `created_section_key` 를 쓴다.  응답 목록의 차집합으로
        # 되짚으면 다른 기기가 같은 순간에 만든 남의 섹션을 고른다(create_section 주석).
        self.assertIn('updated.created_section_key', add)
        table = script.split('async function addTable(', 1)[1].split('async function deleteSection(', 1)[0]
        # 🔴 실패와 "확인 못 했다" 를 합치지 않는다 -- 커밋 뒤 응답만 유실될 수 있다.
        for outcome in ("'ok'", "'stale'", "'failed'", "'unknown'"):
            self.assertIn(outcome, table)
        self.assertIn("block_type:'table'", table)
        self.assertNotIn("method:'PUT'", table, '표 전용 저장 경로를 따로 만들지 않는다')
        # 🔴 "그 섹션에 표가 있는가" 로 성공을 판정하면 이미 표가 있던 섹션에서 저장이
        # 실패해도 성공으로 보고한다.  새 블록에는 서버 id 가 없으니 개수로 센다.
        self.assertIn('tables(latest)>before', table)
        self.assertNotIn('.some(b=>b.section_key===key&&b.block_type===', table)
        # 🔴 확실히 실패했거나 확인조차 못 했으면 낙관적 draft 를 걷어낸다.  남기면 실제로는
        # 커밋됐던 표를 다음 저장이 한 번 더 만든다.
        self.assertEqual(table.count('dropDraft()'), 2)
        # 🔴 실패 경로에서 서버본으로 덮지 않는다 -- 같은 PUT 에 실려 있던 미저장 글이
        # 통째로 사라진다.  `state.report=latest` 는 성공을 확인했을 때만.
        self.assertEqual(table.count('state.report=latest'), 1)
        self.assertLess(table.index('tables(latest)>before'), table.index('state.report=latest'))
        editor = script.split('function tableEditor(', 1)[1].split('function setTable(', 1)[0]
        self.assertIn('dd-table-edit', editor)
        self.assertIn('data-tbl-add-row', editor)
        self.assertIn('data-tbl-del-col', editor)
        # 🔴 셀 입력에서는 다시 그리지 않는다 -- 한 글자마다 renderSections 를 돌리면
        # 포커스와 커서가 날아가 한글 조합이 깨진다.  구조 변경 버튼에서만 다시 그린다.
        bind = script.split('function bindTableEditors(', 1)[1].split('function renderSections(', 1)[0]
        cell = bind.split('.dd-tbl-cell', 1)[1].split('const mutate', 1)[0]
        self.assertNotIn('renderSections()', cell)
        self.assertIn('renderSections()', bind.split('const mutate', 1)[1])

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

    def test_svms_preview_reports_utf8_bytes_and_queues_only_after_final_confirmation(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Test DD', 'svms_dk_cd': 'ATGRMD2607130001'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate", json={'report_date': '2026-08-20'}).get_json()
        self.client.put(f"/api/dock-daily/reports/{r['id']}", json={'revision': r['revision'], 'operations': [{'section_key': 'vendor', 'block_type': 'paragraph', 'content': {'body': '한글 😀'}}]})
        with self._svms_env():
            preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
            self.assertEqual(len(preview['fields']['RMK_VNDR'].encode('utf-8')), preview['byte_counts']['RMK_VNDR'])
            self.assertEqual(400, self.client.post(f"/api/dock-daily/reports/{r['id']}/svms-publish").status_code)
            current = self.client.get(f"/api/dock-daily/reports/{r['id']}").get_json()
            final = self.client.post(f"/api/dock-daily/reports/{r['id']}/status",
                                     json={'status': 'final', 'revision': current['revision']})
            self.assertEqual(200, final.status_code, final.get_data(as_text=True))
            queued = self.client.post(f"/api/dock-daily/reports/{r['id']}/svms-publish",
                                      json={'confirmation': 'user_preview_approved'})
            self.assertEqual(202, queued.status_code, queued.get_data(as_text=True))
            key = self._api_key()
            claim = self.client.post('/api/ext/dock-daily/svms-claim', json={'limit': 1},
                                     headers={'X-API-Key': key})
            self.assertEqual(200, claim.status_code, claim.get_data(as_text=True))
            job = claim.get_json()['jobs'][0]
            self.assertEqual('ATGRMD2607130001', job['dk_cd'])
            self.assertEqual('', job['dk_seq'])
            self.assertNotIn('egcs', job['rmk'])
            result = self.client.post('/api/ext/dock-daily/svms-result',
                                      json={'report_id': job['report_id'], 'claim_token': job['claim_token'],
                                            'status': 'synced', 'dk_seq': '0002',
                                            'readback_hash': 'sha256:test'},
                                      headers={'X-API-Key': key})
            self.assertEqual(200, result.status_code, result.get_data(as_text=True))
            self.assertEqual('0002', self.client.get(f"/api/dock-daily/reports/{r['id']}").get_json()['svms_dk_seq'])

    def test_report_json_hides_the_runner_claim_token_and_folds_the_result(self):
        """🔴 `svms_claim_token` 은 맥 러너의 **능력치**다 — 이 토큰으로 ext API 가 첨부
        원본(`/api/ext/dock-daily/attachments/<aid>/bytes`)을 내려주고 결과 기록도 받는다.
        보고서 JSON 에 실리면 화면·앱 캐시·로그로 퍼지므로 응답 경계에서 걷어낸다.

        러너 결과 JSON 도 원본을 넘기지 않는다. 넘기면 러너 내부 필드 이름이 UI 계약이
        되고, 첨부 실패 목록 같은 구조가 그대로 노출된다."""
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Token DD', 'svms_dk_cd': 'ATGRMD2607130001'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': '2026-08-21'}).get_json()
        self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph',
                            'content': {'body': 'hull cleaning'}}]})
        with self._svms_env():
            current = self.client.get(f"/api/dock-daily/reports/{r['id']}").get_json()
            self.client.post(f"/api/dock-daily/reports/{r['id']}/status",
                             json={'status': 'final', 'revision': current['revision']})
            self.assertEqual(202, self.client.post(
                f"/api/dock-daily/reports/{r['id']}/svms-publish",
                json={'confirmation': 'user_preview_approved'}).status_code)
            key = self._api_key()
            job = self.client.post('/api/ext/dock-daily/svms-claim', json={'limit': 1},
                                   headers={'X-API-Key': key}).get_json()['jobs'][0]
            # claim 중(=토큰이 살아 있는 순간)에 조회해야 유출을 잡을 수 있다.
            body = self.client.get(f"/api/dock-daily/reports/{r['id']}").get_data(as_text=True)
            self.assertNotIn('svms_claim_token', body)
            self.assertNotIn(job['claim_token'], body)
            self.assertEqual('submitting',
                             self.client.get(f"/api/dock-daily/reports/{r['id']}").get_json()['svms_sync_status'])

            # 본문은 들어갔고 첨부만 빠진 상태. 🔴 실패 건수가 화면에 남아야 한다 —
            # 이 문장이 없으면 형이 첨부까지 올라간 줄 알고 SVMS 를 그대로 넘긴다.
            self.assertEqual(200, self.client.post(
                '/api/ext/dock-daily/svms-result',
                # 키 모양은 러너 실측 계약이다(`svms_dr_push.process()` 반환값이 그대로
                # 본문이 된다) — `note`·`attachments` 는 최상위다.
                json={'report_id': job['report_id'], 'claim_token': job['claim_token'],
                      'status': 'partial', 'dk_seq': '0003', 'readback_hash': 'sha256:x',
                      'note': '본문 저장 완료',
                      'attachments': {'uploaded': 1, 'count': 1,
                                      'failed': [{'id': 1, 'error': 'timeout'},
                                                 {'id': 2, 'error': 'timeout'}]}},
                headers={'X-API-Key': key}).status_code)
            fetched = self.client.get(f"/api/dock-daily/reports/{r['id']}")
            after = fetched.get_json()
            self.assertEqual('partial', after['svms_sync_status'])
            self.assertNotIn('svms_result_json', after)
            self.assertIn('본문 저장 완료', after['svms_error'])
            self.assertIn('첨부 2건 업로드 실패', after['svms_error'])
            # 🔴 러너 본문 전체가 `svms_result_json` 으로 저장되고 그 안에는 claim_token 이
            #    들어 있다. 한 줄로 접어 주지 않으면 여기서 토큰이 그대로 새어 나온다.
            self.assertNotIn(job['claim_token'], fetched.get_data(as_text=True))

    def _svms_to_unknown(self, title, date, status='unknown', extra=None):
        """상신 → claim → 러너 결과까지 밀어 `unknown`/`partial` 보고서를 만든다."""
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': title, 'svms_dk_cd': 'ATGRMD2607130001'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': date}).get_json()
        self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph',
                            'content': {'body': 'hull cleaning'}}]})
        current = self.client.get(f"/api/dock-daily/reports/{r['id']}").get_json()
        self.client.post(f"/api/dock-daily/reports/{r['id']}/status",
                         json={'status': 'final', 'revision': current['revision']})
        self.client.post(f"/api/dock-daily/reports/{r['id']}/svms-publish",
                         json={'confirmation': 'user_preview_approved'})
        key = self._api_key()
        job = self.client.post('/api/ext/dock-daily/svms-claim', json={'limit': 1},
                               headers={'X-API-Key': key}).get_json()['jobs'][0]
        body = {'report_id': job['report_id'], 'claim_token': job['claim_token'], 'status': status}
        if status in ('synced', 'partial'):
            body['dk_seq'] = '0007'
        body.update(extra or {})
        self.client.post('/api/ext/dock-daily/svms-result', json=body, headers={'X-API-Key': key})
        return r['id'], job['claim_token']

    def test_runner_result_never_stores_the_claim_token_in_the_report_row(self):
        """응답 경계에서 걷어내는 것만으로는 부족하다(올마이트) — 토큰이 `svms_result_json`
        에 남으면 DB 백업·debug export 로 새어 나간다. **저장 전에** 지운다."""
        with self._svms_env():
            rid, token = self._svms_to_unknown('Token Store DD', '2026-08-18', 'partial')
            row = self.client.get(f"/api/dock-daily/reports/{rid}").get_json()
            self.assertEqual('partial', row['svms_sync_status'])
        with appmod.app.app_context():
            stored = appmod.query('SELECT svms_result_json FROM dock_daily_report WHERE id=?',
                                  (rid,), one=True)['svms_result_json']
        self.assertIn('partial', stored)          # 결과 기록 자체는 남는다
        self.assertNotIn('claim_token', stored)
        self.assertNotIn(token, stored)

    def test_malformed_runner_attachment_failure_does_not_500_the_report(self):
        """🔴 `failed` 가 list 라는 보장은 없다. `len()` 이 터지면 그건 보고서 GET 500 이고,
        하필 형이 SVMS 결과를 확인하려는 순간 화면이 죽는다(올마이트)."""
        with self._svms_env():
            rid, _ = self._svms_to_unknown(
                'Malformed DD', '2026-08-17', 'partial',
                extra={'note': '본문 저장 완료', 'attachments': {'failed': 3}})
            fetched = self.client.get(f"/api/dock-daily/reports/{rid}")
            self.assertEqual(200, fetched.status_code)
            note = fetched.get_json()['svms_error']
            self.assertIn('본문 저장 완료', note)
            self.assertIn('첨부 업로드 실패', note)   # 셀 수 없으면 건수 없이 적는다

    def test_manual_reconcile_closes_unknown_both_ways(self):
        """🔴 `unknown`/`partial` 은 재상신이 상태로 막혀 있어서, 확인 결과를 기록할 출구가
        없으면 **영구 고착**이다(올마이트 blocking). 서버는 판정하지 않고 사람이 본 것을
        기록만 한다 — SVMS 에 다시 쓰지 않는다."""
        with self._svms_env():
            rid, _ = self._svms_to_unknown('Reconcile DD', '2026-08-16', 'unknown')
            path = f"/api/dock-daily/reports/{rid}/svms-reconcile"
            self.assertEqual('unknown',
                             self.client.get(f"/api/dock-daily/reports/{rid}").get_json()['svms_sync_status'])
            # 확인 플래그·resolution·DK_SEQ 검증
            self.assertEqual('confirmation_required',
                             self.client.post(path, json={'resolution': 'synced'}).get_json()['code'])
            self.assertEqual(400, self.client.post(path, json={
                'confirmation': 'user_checked_svms', 'resolution': 'maybe'}).status_code)
            self.assertEqual('dk_seq_required', self.client.post(path, json={
                'confirmation': 'user_checked_svms', 'resolution': 'synced'}).get_json()['code'])
            self.assertEqual('dk_seq_invalid', self.client.post(path, json={
                'confirmation': 'user_checked_svms', 'resolution': 'synced',
                'dk_seq': '2 rows'}).get_json()['code'])
            # 반영됨으로 닫기 — DK_SEQ 는 SVMS 표기대로 4자 0패딩으로 저장한다.
            done = self.client.post(path, json={'confirmation': 'user_checked_svms',
                                                'resolution': 'synced', 'dk_seq': '2'})
            self.assertEqual(200, done.status_code)
            self.assertEqual('0002', done.get_json()['dk_seq'])
            after = self.client.get(f"/api/dock-daily/reports/{rid}").get_json()
            self.assertEqual('synced', after['svms_sync_status'])
            self.assertEqual('0002', after['svms_dk_seq'])
            self.assertIn('사람 확인', after['svms_error'])
            # 이미 닫힌 보고서는 같은 경로로 다시 만질 수 없다.
            self.assertEqual('reconcile_not_applicable', self.client.post(path, json={
                'confirmation': 'user_checked_svms', 'resolution': 'not_saved'}).get_json()['code'])
            # `not_saved` 는 `failed` 로 내려 상신을 다시 열어준다.
            rid2, _ = self._svms_to_unknown('Reconcile DD2', '2026-08-15', 'unknown')
            path2 = f"/api/dock-daily/reports/{rid2}/svms-reconcile"
            self.assertEqual(200, self.client.post(path2, json={
                'confirmation': 'user_checked_svms', 'resolution': 'not_saved'}).status_code)
            reopened = self.client.get(f"/api/dock-daily/reports/{rid2}").get_json()
            self.assertEqual('failed', reopened['svms_sync_status'])
            # 그리고 실제로 다시 상신이 받아진다(=고착 해제).
            self.assertEqual(202, self.client.post(
                f"/api/dock-daily/reports/{rid2}/svms-publish",
                json={'confirmation': 'user_preview_approved'}).status_code)

    def test_report_list_carries_svms_state_without_the_claim_token(self):
        """어느 일자가 이미 SVMS 로 넘어갔는지 보고서를 하나씩 열지 않고 알아야 한다.
        🔴 그렇다고 `SELECT *` 로 넓히면 목록에 `svms_claim_token` 까지 실린다."""
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'List DD'}).get_json()
        self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                         json={'report_date': '2026-08-19'})
        listed = self.client.get(f"/api/dock-daily/projects/{p['id']}/reports")
        rows = listed.get_json()
        self.assertIn('svms_sync_status', rows[0])
        self.assertIn('svms_dk_seq', rows[0])
        self.assertNotIn('svms_claim_token', listed.get_data(as_text=True))

    def test_svms_state_module_rules_actually_run(self):
        """상태 → 배지·상신 허용 규칙은 앱과 웹 두 미러가 같아야 한다. 문자열 검사
        대신 node 로 실제 실행해 잠근다(tests/dock_daily_svms.test.js)."""
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node 없음 — `node --test tests/dock_daily_svms.test.js`')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        done = subprocess.run([node, '--test', os.path.join('tests', 'dock_daily_svms.test.js')],
                              cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def test_dock_daily_page_loads_the_svms_state_module(self):
        page = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('js/dock_daily_svms.js', page)
        self.assertIn('id="dd-svms-state"', page)

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
        # Outlook paste drops paragraph margin-left, so a real 24px spacer cell indents items.
        self.assertIn('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                      'width="100%"', preview['html'])
        self.assertIn('<td width="24" style="width:24px;vertical-align:top;%s">'
                      '<p style="margin:0;%s">%s</p></td>' % (font, font, run % '&nbsp;'),
                      preview['html'])
        self.assertIn('<td width="28" style="width:28px;vertical-align:top;white-space:nowrap;%s">'
                      '<p style="margin:0;%s">%s</p></td>' % (font, font, run % '2)'),
                      preview['html'])
        self.assertIn('Crane test &lt;Hull &amp; Valve&gt; &quot;ongoing&quot;</span></p></td></tr></table>%s'
                      '<p style="margin:0 0 6px">%s' % (spacer, run % '<b>2. &nbsp;EGCS Retrofit</b>'),
                      preview['html'])
        self.assertNotIn('<Hull & Valve>', preview['html'])

    def test_email_table_cells_wrap_text_in_paragraphs(self):
        """On the Outlook iOS paste that was measured, text sitting directly inside a
        <td> came out at ~8pt while paragraph text outside any table held the
        declared 11pt, and that did not move as the declaration was added to the
        wrapper <div>, then every <table>/<td>, then a <span> per text node. Cell
        text therefore goes inside a <p>; whether Outlook honours 11pt for a <p>
        inside a <td> is an untested hypothesis, so this test locks the markup
        shape only. The itinerary needs borders; work items use borderless presentation
        tables because Outlook drops paragraph margin-left."""
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
        self.assertFalse(_Tree.find(root, 'th') or _Tree.find(root, 'thead'), body)
        itinerary = [table for table in tables if table['attrs'].get('role') != 'presentation']
        items = [table for table in tables if table['attrs'].get('role') == 'presentation']
        self.assertEqual(1, len(itinerary))
        self.assertTrue(items)
        for table in items:
            rows = _Tree.find(table, 'tr')
            self.assertEqual(1, len(rows), table)
            cells = [k for k in rows[0]['kids'] if k['tag'] == 'td']
            self.assertEqual(3, len(cells), rows[0])
            self.assertEqual(cells, rows[0]['kids'], 'only cells may sit in an item row')
            self.assertEqual('24', cells[0]['attrs'].get('width'))
            self.assertEqual('28', cells[1]['attrs'].get('width'))
            for td in cells:
                self.assertEqual(['p'], [k['tag'] for k in td['kids']], td)
                self.assertEqual('', td['text'].strip(), 'text sits directly in the <td>')
                self.assertIn('font-size:11pt', td['kids'][0]['attrs'].get('style', ''))
        rows = _Tree.find(itinerary[0], 'tr')
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
        item_run = ('<span style="font-family:Arial,Helvetica,sans-serif;font-size:11pt">'
                    '%s</span></p></td>')
        self.assertIn(item_run % '1)', preview['html'])
        self.assertIn(item_run % 'Tank cleaning', preview['html'])
        self.assertIn(item_run % '3)', preview['html'])
        self.assertIn(item_run % 'Anode renewal', preview['html'])
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

    def _heic(self, size=(1200, 900)):
        """진짜 HEIC 바이트. 없으면 이 테스트는 건너뛴다(형 지시 2026-08-22로 서버엔 설치됨)."""
        try:
            import pillow_heif                     # noqa: F401  등록만으로는 부족, 저장도 필요
        except Exception:
            self.skipTest('pillow-heif 미설치 — HEIC 경로를 검증할 수 없다')
        from PIL import Image
        pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.new('RGB', size, (30, 90, 150)).save(buf, format='HEIF')
        return buf.getvalue()

    def test_mail_inlines_an_iphone_heic_photo(self):
        """🔴 형 지시 2026-08-22: 아이폰 기본 포맷(HEIC)도 메일 본문에 실려야 한다.

        pillow-heif 등록 전에는 `Image.open` 이 예외를 내고 '이 형식은 본문에 넣을 수
        없습니다' 만 나갔다. 등록 뒤에도 본문 바이트는 여전히 JPEG data URI 다 --
        메일 클라이언트가 HEIC 를 못 그리므로 형식 변환은 서버가 끝내야 한다.
        """
        rid, _aid = self._report_with_photo_block('Mail HEIC DD', payload=self._heic(),
                                                  name='iphone.heic')
        mail = self._mail(rid)
        self.assertIn('<img src="data:image/jpeg;base64,', mail['html'])
        self.assertNotIn('본문에 넣을 수 없습니다', mail['html'])
        self.assertNotIn('data:image/heic', mail['html'],
                         'HEIC 를 그대로 실으면 Outlook 이 못 그린다')

    def test_heif_registration_is_idempotent_and_never_raises(self):
        """등록은 여러 번 불려도 안전해야 하고, 패키지가 없어도 앱을 죽이면 안 된다."""
        import app_core
        self.assertIs(app_core.ensure_heif_opener(), app_core.ensure_heif_opener())
        saved = app_core._heif_registered
        app_core._heif_registered = None
        real_import = builtins.__import__

        def _no_pillow_heif(name, *a, **kw):
            if name == 'pillow_heif':
                raise ImportError('simulated missing wheel')
            return real_import(name, *a, **kw)

        builtins.__import__ = _no_pillow_heif
        try:
            self.assertFalse(app_core.ensure_heif_opener(), '없으면 False 로 떨어져야 한다')
        finally:
            builtins.__import__ = real_import
            app_core._heif_registered = saved

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
        root = _Tree.parse(mail['html'])
        data_tables = []
        for table in _Tree.find(root, 'table'):
            if table['attrs'].get('role') == 'presentation':
                continue
            rows = _Tree.find(table, 'tr')
            if rows and len([kid for kid in rows[0]['kids'] if kid['tag'] == 'td']) == 4:
                data_tables.append(table)
        self.assertEqual(1, len(data_tables), mail['html'])
        self.assertEqual(4, len(_Tree.find(data_tables[0], 'tr')),
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
        first = html.index('>1)</span></p>')
        second = html.index('>2)</span></p>')
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

    def test_an_empty_section_is_deleted_outright(self):
        """형 지시 2026-08-22: 잘못 만든 섹션을 목록에서 아주 지운다.

        전엔 `enabled=0` 으로 숨기는 것뿐이라 빈 카드가 프로젝트에 계속 쌓였다.
        """
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '섹션 삭제 DD'}).get_json()
        self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                         json={'label': '잘못 만든 섹션'})
        gone = self.client.delete(f"/api/dock-daily/projects/{project['id']}/sections/sec_1")
        self.assertEqual(200, gone.status_code, gone.get_data(as_text=True))
        body = gone.get_json()
        self.assertEqual('sec_1', body['deleted_section_key'])
        self.assertEqual(0, body['deleted_blocks'])
        self.assertNotIn('sec_1', {s['section_key'] for s in body['sections']},
                         '숨김이 아니라 목록에서 사라져야 한다')
        # 🔴 고정 섹션은 못 지운다 -- 메일 서식과 SVMS 필드가 이름으로 물려 있다.
        for key in ('shipyard', 'survey', 'vendor', 'remark'):
            resp = self.client.delete(f"/api/dock-daily/projects/{project['id']}/sections/{key}")
            self.assertEqual(409, resp.status_code, key)
            self.assertEqual('fixed_section', resp.get_json()['code'])
        self.assertEqual(404, self.client.delete(
            f"/api/dock-daily/projects/{project['id']}/sections/sec_9").status_code)
        self.assertEqual(404, self.client.delete(
            '/api/dock-daily/projects/999999/sections/sec_1').status_code)

    def test_a_section_with_content_needs_confirmation_and_takes_its_blocks(self):
        """내용이 있으면 한 번 확인받고, 지울 땐 블록까지 함께 지운다.

        🔴 블록을 남기면 `create_section` 이 비어 있는 가장 작은 번호를 다시 쓰므로,
        `sec_1` 을 지우고 새 섹션을 만드는 순간 옛 블록이 그 안에서 되살아난다.
        """
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '내용 있는 섹션 삭제 DD'}).get_json()
        self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                         json={'label': '비용 정산표'})
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-13'}).get_json()
        fetched = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': fetched['revision'],
            'operations': [{'op': 'upsert', 'section_key': 'sec_1', 'block_type': 'table',
                            'content': {'columns': ['항목'], 'rows': [['도장']]}}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        before_rev = saved.get_json()['revision']

        blocked = self.client.delete(f"/api/dock-daily/projects/{project['id']}/sections/sec_1")
        self.assertEqual(409, blocked.status_code)
        self.assertEqual('section_not_empty', blocked.get_json()['code'])
        self.assertEqual(1, blocked.get_json()['blocks'])
        self.assertEqual(['2026-06-13'], blocked.get_json()['dates'])
        self.assertTrue(self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()['blocks'],
                        '거절된 요청은 아무것도 지우지 않는다')

        gone = self.client.delete(f"/api/dock-daily/projects/{project['id']}/sections/sec_1",
                                  json={'confirm': 'delete-section'})
        self.assertEqual(200, gone.status_code, gone.get_data(as_text=True))
        self.assertEqual(1, gone.get_json()['deleted_blocks'])
        after = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        self.assertEqual([], [b for b in after['blocks'] if b['section_key'] == 'sec_1'])
        # 🔴 revision 을 올려야 그 보고서를 열어 둔 다른 기기가 옛 값으로 되살리지 못한다.
        self.assertGreater(after['revision'], before_rev)

        # 같은 번호를 다시 써도 옛 블록이 따라오지 않는다.
        again = self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                                 json={'label': '새 표'})
        self.assertEqual('sec_1', again.get_json()['created_section_key'])
        fresh = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        self.assertEqual([], [b for b in fresh['blocks'] if b['section_key'] == 'sec_1'],
                         '지운 블록이 같은 key 의 새 섹션에서 되살아나면 안 된다')

    def test_a_stale_client_cannot_write_into_a_deleted_section(self):
        """삭제된 섹션으로 들어오는 저장은 서버가 끊는다.

        올마이트가 "블록이 없던 보고서는 revision 이 안 오르니, 그 보고서를 열어 둔
        기기가 지운 섹션에 그대로 저장해 고아 블록을 만든다" 고 지적했다. 실제로는
        보고서 `PUT` 이 upsert 마다 `dock_daily_section_def` 를 확인하므로 막힌다 --
        그 검증이 이 삭제 계약의 안전판이라 여기서 못박아 둔다(빠지면 `sec_N` 재사용
        때 고아 블록이 새 섹션에서 되살아난다).
        """
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'stale 저장 DD'}).get_json()
        self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                         json={'label': '비용 정산표'})
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-15'}).get_json()
        # 이 보고서에는 sec_1 블록이 없다 -> 삭제해도 revision 이 오르지 않는다.
        stale = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        self.assertEqual(200, self.client.delete(
            f"/api/dock-daily/projects/{project['id']}/sections/sec_1").status_code)
        after = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        self.assertEqual(stale['revision'], after['revision'],
                         '블록이 없던 보고서는 건드리지 않는다')

        rejected = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': stale['revision'],
            'operations': [{'op': 'upsert', 'section_key': 'sec_1', 'block_type': 'paragraph',
                            'content': {'text': '옛 화면에서 쓴 글'}}]})
        self.assertEqual(400, rejected.status_code, rejected.get_data(as_text=True))
        self.assertEqual([], self.client.get(
            f"/api/dock-daily/reports/{report['id']}").get_json()['blocks'])

    def test_a_final_report_blocks_section_deletion(self):
        """확정본에 내용이 있으면 거절한다 -- 확정 취소가 정상 경로다."""
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '확정 섹션 삭제 DD'}).get_json()
        self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                         json={'label': '비용 정산표'})
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-14'}).get_json()
        fetched = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': fetched['revision'],
            'operations': [{'op': 'upsert', 'section_key': 'sec_1', 'block_type': 'paragraph',
                            'content': {'body': '도장 100'}}]})
        self.assertEqual(200, saved.status_code)
        final = self.client.post(f"/api/dock-daily/reports/{report['id']}/status", json={
            'status': 'final', 'revision': saved.get_json()['revision']})
        self.assertEqual(200, final.status_code, final.get_data(as_text=True))

        for payload in (None, {'confirm': 'delete-section'}):
            resp = self.client.delete(f"/api/dock-daily/projects/{project['id']}/sections/sec_1",
                                      json=payload)
            self.assertEqual(409, resp.status_code, resp.get_data(as_text=True))
            self.assertEqual('final_report_has_content', resp.get_json()['code'])
            self.assertEqual(['2026-06-14'], resp.get_json()['dates'])
        still = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        self.assertTrue([b for b in still['blocks'] if b['section_key'] == 'sec_1'])

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
        # 🔴 메일 순서 = 화면 순서 = `sort_order` 다(형 지시 2026-08-22).  `create_section` 이
        # `MAX+1` 로 붙이므로 새 섹션은 고정 4개 뒤, 즉 5번이다.  전에는 메일만 special 을
        # Shipyard 바로 뒤로 못박아 두어 앱에서 맨 아래 보이던 카드가 메일에서는 2번으로
        # 튀어 올랐다.  형이 카드 순서를 직접 바꾸게 된 이상 그 어긋남은 거짓말이 된다.
        self.assertIn('5. 비용 정산표', mail['text'],
                      f"화면 순서(sort_order)를 그대로 따라야 한다: {mail['text']}")
        self.assertIn('비용 정산표', mail['html'])
        self.assertIn('도장', mail['html'])

    def test_sections_can_be_reordered_and_the_mail_follows(self):
        """형 지시 2026-08-22: 카드 순서를 바꾸면 메일 순서도 같이 바뀐다.

        순서는 프로젝트 값(`dock_daily_section_def.sort_order`)이고 출구 셋(앱·메일·SVMS)이
        모두 이 한 값을 읽는다.  🔴 고정 섹션은 **순서만** 열려 있다 -- 이름·표시여부까지
        열면 SVMS 필드(`RMK_SYD`/`RMK_VNDR`)와 메일 서식이 이름으로 물려 있어 깨진다.
        """
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '섹션 순서 DD'}).get_json()
        self.assertEqual(201, self.client.post(
            f"/api/dock-daily/projects/{project['id']}/sections",
            json={'label': '비용 정산표'}).status_code)
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-12'}).get_json()
        fetched = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        keys = [s['section_key'] for s in sorted(fetched['sections'], key=lambda s: s['sort_order'])]
        self.assertEqual(['shipyard', 'survey', 'vendor', 'remark', 'sec_1'], keys)

        # 새 섹션을 맨 앞으로. 클라이언트는 목록 전체를 다시 매겨 보낸다(앱
        # `DockDailySectionEditing.renumbered`) -- 고정 섹션 항목에는 `sort_order` 만 담는다.
        moved = ['sec_1', 'shipyard', 'survey', 'vendor', 'remark']
        saved = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': fetched['revision'],
            'section_updates': [{'section_key': k, 'sort_order': i + 1}
                                for i, k in enumerate(moved)],
            'operations': [{'op': 'upsert', 'section_key': 'sec_1', 'block_type': 'table',
                            'content': {'columns': ['항목', '금액'], 'rows': [['도장', '100']]}}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        after = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        self.assertEqual(moved, [s['section_key'] for s in
                                 sorted(after['sections'], key=lambda s: s['sort_order'])])
        mail = self._mail(report['id'])
        self.assertIn('1. 비용 정산표', mail['text'], mail['text'])
        self.assertLess(mail['text'].index('비용 정산표'), mail['text'].index('Shipyard'),
                        f"메일도 화면 순서를 따라야 한다: {mail['text']}")

        # 🔴 고정 섹션의 이름·표시여부는 거절한다.  조용히 무시하면 클라이언트는 바뀐 줄
        # 알고 옛 값을 화면에 남긴다.
        rev = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()['revision']
        for bad in ({'section_key': 'shipyard', 'sort_order': 1, 'label': '조선소'},
                    {'section_key': 'shipyard', 'sort_order': 1, 'enabled': False}):
            resp = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
                'revision': rev, 'section_updates': [bad]})
            self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))
            self.assertIn('fixed sections accept sort_order only',
                          resp.get_data(as_text=True))
        # 🔴 정수가 아닌 순서는 거절한다.  SQLite 는 `"3"`·3.5 를 그대로 넣고 `ORDER BY`
        # 가 조용히 섞여, 앱·메일·SVMS 순서가 한꺼번에 어긋나는데 에러는 안 난다.
        for bad_order in ('3', 3.5, True, [1]):
            resp = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
                'revision': rev,
                'section_updates': [{'section_key': 'sec_1', 'sort_order': bad_order}]})
            self.assertEqual(400, resp.status_code, f'{bad_order!r}: {resp.get_data(as_text=True)}')
            self.assertIn('sort_order must be an integer', resp.get_data(as_text=True))
        # 값이 그대로인 label 은 변경이 아니다 -- 목록을 통째로 되돌려 보내는 클라이언트가 있다.
        same = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': rev,
            'section_updates': [{'section_key': 'shipyard', 'sort_order': 2,
                                 'label': 'Shipyard'}]})
        self.assertEqual(200, same.status_code, same.get_data(as_text=True))

    def test_a_table_block_can_move_into_a_section_of_its_own(self):
        """형 지시 2026-08-22: 남의 카드에 딸려 있는 표를 제목 가진 자기 섹션으로 뺀다.

        앱의 "표를 별도 섹션으로 빼기" 가 이 계약 위에 서 있다 -- upsert 가 기존 블록의
        `section_key` 를 바꿔주지 않으면 표를 지우고 다시 치는 수밖에 없다.
        """
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '표 이사 DD'}).get_json()
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-12'}).get_json()
        content = {'columns': ['항목', '금액'], 'rows': [['도장', '100']]}
        fetched = self.client.get(f"/api/dock-daily/reports/{report['id']}").get_json()
        saved = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': fetched['revision'],
            'operations': [{'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'table',
                            'content': content}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        table = [b for b in saved.get_json()['blocks'] if b['block_type'] == 'table'][0]
        created = self.client.post(
            f"/api/dock-daily/projects/{project['id']}/sections", json={'label': '비용 정산표'})
        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        # 🔴 클라이언트가 응답 목록의 차집합으로 되짚으면, 다른 기기가 같은 순간에 섹션을
        # 추가했을 때 남의 섹션에 표를 넣는다. 방금 만든 key 를 서버가 직접 말해준다.
        new_key = created.get_json().get('created_section_key')
        self.assertEqual('sec_1', new_key)
        moved = self.client.put(f"/api/dock-daily/reports/{report['id']}", json={
            'revision': saved.get_json()['revision'],
            'operations': [{'op': 'upsert', 'id': table['id'], 'section_key': new_key,
                            'sort_order': 0, 'block_type': 'table', 'content': content}]})
        self.assertEqual(200, moved.status_code, moved.get_data(as_text=True))
        blocks = moved.get_json()['blocks']
        self.assertEqual(1, len([b for b in blocks if b['block_type'] == 'table']),
                         '옮긴 것이지 복사한 것이 아니다')
        self.assertEqual('sec_1', [b for b in blocks if b['id'] == table['id']][0]['section_key'])
        mail = self._mail(report['id'])
        self.assertIn('비용 정산표', mail['text'], '표는 이제 자기 제목 아래 있다')
        self.assertIn('도장', mail['html'], '내용은 그대로 따라온다')

    def test_mail_does_not_indent_a_table_that_is_its_own_section(self):
        """🔴 형 지시 2026-08-22: "표 섹션은 들여쓰기 하지말게(나머지는 현행 유지)".

        표 섹션은 섹션 제목이 곧 표의 제목이라 들여쓸 상위 항목이 없다.  판정은 `special`
        **이면서** 내용이 표뿐일 때로 좁힌다 -- 고정 섹션의 제목은 표 제목이 아니라
        분류명이고, 글·사진이 섞이면 위 번호 항목과 기준선을 맞춰야 한다.
        """
        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': '표 들여쓰기 DD'}).get_json()
        report = self.client.post(f"/api/dock-daily/projects/{project['id']}/reports/generate",
                                  json={'report_date': '2026-06-13'}).get_json()
        rid = report['id']
        pure = self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                                json={'label': '비용 정산표'}).get_json()['created_section_key']
        mixed = self.client.post(f"/api/dock-daily/projects/{project['id']}/sections",
                                 json={'label': '표와 사진'}).get_json()['created_section_key']
        up = self.client.post(f'/api/dock-daily/reports/{rid}/attachments',
                              data={'file': (io.BytesIO(self._png((300, 200))), 'g.png')},
                              content_type='multipart/form-data')
        aid = up.get_json()['id']
        content = {'columns': ['항목', '금액'], 'rows': [['도장', '100']]}
        fetched = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        saved = self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': fetched['revision'],
            'operations': [
                # ① special + 표뿐 = 표 섹션. 표가 둘이어도 마찬가지다.
                {'op': 'upsert', 'section_key': pure, 'block_type': 'table',
                 'sort_order': 1, 'content': content},
                {'op': 'upsert', 'section_key': pure, 'block_type': 'table',
                 'sort_order': 2, 'content': content},
                # ② special 이지만 사진이 섞였다 = 현행 유지
                {'op': 'upsert', 'section_key': mixed, 'block_type': 'table',
                 'sort_order': 1, 'content': content},
                {'op': 'upsert', 'section_key': mixed, 'block_type': 'image', 'sort_order': 2,
                 'content': {'columns': 1, 'images': [{'attachment_id': aid, 'caption': '외판'}]}},
                # ③ 🔴 고정 섹션에 표만 남아 있어도 52px (올마이트 지적)
                {'op': 'upsert', 'section_key': 'shipyard', 'block_type': 'table',
                 'sort_order': 1, 'content': content},
                # ④ 고정 섹션에 글과 섞인 표 = 현행 유지
                {'op': 'upsert', 'section_key': 'vendor', 'block_type': 'item',
                 'sort_order': 1, 'content': {'title': '첫 작업'}},
                {'op': 'upsert', 'section_key': 'vendor', 'block_type': 'table',
                 'sort_order': 2, 'content': content}]})
        self.assertEqual(200, saved.status_code, saved.get_data(as_text=True))
        body = self._mail(rid)['html']
        flush = '<table style="border-collapse:collapse;margin:0 0 8px 0px;'
        kept = '<table style="border-collapse:collapse;margin:0 0 8px 52px;'
        self.assertEqual(2, body.count(flush), '표 섹션의 표 둘만 들여쓰기가 없다')
        self.assertEqual(3, body.count(kept), '나머지 표 셋은 52px 를 지킨다')
        # 들여쓰기 없는 두 표는 표 섹션 제목과 그 다음 섹션 제목 사이에 있다.
        head, nxt = body.index('비용 정산표'), body.index('표와 사진')
        self.assertLess(head, body.index(flush))
        self.assertLess(body.rindex(flush), nxt)

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

    # ------------------------------------------------------------------
    # 2026-08-22 전체 검토(페이블 4개 차원)에서 나온 잠재결함들.
    # 각 테스트는 "고치기 전 구현으로 되돌리면 실패한다" 를 기준으로 썼다.
    # ------------------------------------------------------------------

    def _svms_env(self, syd='4000', vndr='4000', rmk='4000'):
        """`SVMS_DOCK_DAILY_MAX_*` 3개를 이 테스트 동안만 세운다."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            keys = {'SVMS_DOCK_DAILY_MAX_SYD': syd,
                    'SVMS_DOCK_DAILY_MAX_VNDR': vndr,
                    'SVMS_DOCK_DAILY_MAX_RMK': rmk}
            old = {k: os.environ.get(k) for k in keys}
            for k, v in keys.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            try:
                yield
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return _ctx()

    def _dd_project_report(self, title, report_date='2026-08-20', **project):
        payload = {'vessel_id': self.vessel, 'title': title}
        payload.update(project)
        p = self.client.post('/api/dock-daily/projects', json=payload).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                             json={'report_date': report_date}).get_json()
        return p, r

    def test_svms_preview_never_substitutes_the_vessel_code_for_the_dock_no(self):
        """🔴 `DK_CD` 가 없을 때 선박코드로 대체하면 미리보기가 거짓말을 한다.

        `DK_CD` 는 SVMS 가 발급한 dock 키(예: `KWPSMD2603250001`)이고 `vsl_cd` 는
        선박코드다.  대체값을 보여 주면 형은 "이 dock 에 반영된다" 고 읽는데, 실제로
        그 키로는 어떤 dock 도 안 열린다.  비어 있으면 비어 있다고 보이고 publish 는
        막혀야 한다.
        """
        p, r = self._dd_project_report('SVMS DK_CD DD')
        with self._svms_env():
            preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertEqual('', preview['fields']['DK_CD'], '선박코드(D001)로 대체하면 안 된다')
        self.assertFalse(preview['publishable'])
        self.assertIn('DK_CD 미설정', preview['blockers'])

        # 연결은 전용 라우트로만 바꾼다(PATCH 는 두 번째 writer 라 막혀 있다).
        self.client.post(f"/api/dock-daily/projects/{p['id']}/svms-dk-cd",
                         json={'dk_cd': 'KWPSMD2603250001', 'allow_unlisted': True,
                               'confirmation': 'user_selected_dock'})
        with self._svms_env():
            ok = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertEqual('KWPSMD2603250001', ok['fields']['DK_CD'])
        self.assertEqual([], ok['blockers'])
        self.assertTrue(ok['publishable'])

    def test_svms_byte_limit_that_cannot_be_read_counts_as_unset(self):
        """🔴 `"4,000"`·`"0"` 은 "한도 설정됨" 이 아니다.

        앞의 구현은 raw 문자열의 truthiness 로 `publishable` 을 열면서 화면에는
        `limits: null` 을 보여 줬다 -- 한도가 없는 상태로 반영 버튼이 열렸다.
        """
        p, r = self._dd_project_report('SVMS 한도 DD')
        self.client.post(f"/api/dock-daily/projects/{p['id']}/svms-dk-cd",
                         json={'dk_cd': 'DK-1', 'allow_unlisted': True,
                               'confirmation': 'user_selected_dock'})
        for bad in ('4,000', '0', '', 'abc', '-1'):
            with self._svms_env(rmk=bad):
                preview = self.client.get(
                    f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
            self.assertIsNone(preview['limits']['RMK'], bad)
            self.assertFalse(preview['publishable'], bad)
            self.assertIn('RMK byte 한도 계약 미설정', preview['blockers'], bad)
        with self._svms_env(rmk=None):
            missing = self.client.get(
                f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertIsNone(missing['limits']['RMK'])
        self.assertFalse(missing['publishable'])

    def test_svms_preview_blocks_a_body_over_the_byte_limit(self):
        """🔴 한도가 설정됐는지만 보고 **초과** 를 통과시키면 안 된다.

        SVMS 가 계약상 못 받는 본문에 "반영 준비 완료" 가 뜨면, 사람은 눌렀고 서버는
        거절한다.  자동 truncate 는 금지이므로(문장을 고르는 건 사람 일) 판정만 한다.
        """
        p, r = self._dd_project_report('SVMS 초과 DD')
        self.client.post(f"/api/dock-daily/projects/{p['id']}/svms-dk-cd",
                         json={'dk_cd': 'DK-1', 'allow_unlisted': True,
                               'confirmation': 'user_selected_dock'})
        self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': r['revision'],
            'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph',
                            'content': {'body': '가' * 40}}]})
        with self._svms_env(syd='10'):
            preview = self.client.get(
                f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertGreater(preview['byte_counts']['RMK_SYD'], 10)
        self.assertEqual(['RMK_SYD'], preview['over_limit'])
        self.assertFalse(preview['publishable'])
        self.assertTrue(any('byte 한도 초과' in b for b in preview['blockers']),
                        preview['blockers'])
        # 잘라 보내지는 않는다 -- 본문은 그대로 보이고 판정만 막는다.
        self.assertIn('가' * 40, preview['fields']['RMK_SYD'])

    def _dock_candidates(self, pid, candidates, vsl_cd='D001'):
        """맥 runner 가 하는 일: `SP_GET_DOCK` 결과를 프로젝트에 캐시한다."""
        body = {'project_id': pid, 'candidates': candidates}
        if vsl_cd is not None:
            body['vsl_cd'] = vsl_cd
        return self.client.post('/api/ext/dock-daily/svms-dock-candidates',
                                headers={'X-API-Key': self._api_key()}, json=body)

    def test_dock_candidates_refuse_another_vessels_result(self):
        """🔴 후보 캐시는 `project_id` 만 믿으면 안 된다.

        runner 가 선박별로 조회해 프로젝트마다 회신하는 구조라, 응답이 뒤바뀌거나 캐시가
        밀리면 **다른 배의 dock 목록**이 이 프로젝트에 저장되고, 열린 게 1건이면 그대로
        자동연결된다.  그 뒤 daily report 는 남의 입거에 쌓인다.
        """
        p, _ = self._dd_project_report('Dock 선박대조 DD')
        wrong = self._dock_candidates(p['id'], [{'dk_cd': 'BGBBMD2608050001', 'status': 'I'}],
                                      vsl_cd='BGBB')
        self.assertEqual(409, wrong.status_code, wrong.get_data(as_text=True))
        self.assertEqual('vsl_cd_mismatch', wrong.get_json()['code'])
        missing = self._dock_candidates(p['id'], [{'dk_cd': 'BGBBMD2608050001', 'status': 'I'}],
                                        vsl_cd=None)
        self.assertEqual(400, missing.status_code)
        # 거절된 요청은 캐시도 자동연결도 남기지 않는다.
        listed = self.client.get(f"/api/dock-daily/projects/{p['id']}/svms-docks").get_json()
        self.assertEqual([], listed['candidates'])
        self.assertIsNone(listed['dk_cd'])

    def test_dock_candidates_do_not_autobind_an_undocumented_status(self):
        """🔴 SVMS `STATUS` 전집합은 문서화돼 있지 않다(실측: `I` 진행, `D` draft, `C` 완료).

        "닫힌 게 아니면 후보" 라는 넓은 판정을 **자동연결까지** 그대로 쓰면, 모르는 코드를
        진행중으로 단정하는 셈이다.  목록엔 남기고 자동연결에서만 뺀다 -- 형이 직접 고르는
        길은 열려 있다.
        """
        p, _ = self._dd_project_report('Dock 미문서 STATUS DD')
        out = self._dock_candidates(p['id'], [{'dk_cd': 'ATGRMD2607130001', 'status': 'Z'}]).get_json()
        self.assertIsNone(out['auto_bound'])
        self.assertIsNone(out['dk_cd'])
        listed = self.client.get(f"/api/dock-daily/projects/{p['id']}/svms-docks").get_json()
        self.assertTrue(listed['candidates'][0]['open'], '목록에서까지 지우면 고를 수가 없다')
        # 실측된 진행중 코드는 그대로 자동연결된다.
        p2, _ = self._dd_project_report('Dock 진행중 DD', '2026-08-19')
        self.assertEqual('ATGRMD2607130001',
                         self._dock_candidates(p2['id'], [{'dk_cd': 'ATGRMD2607130001',
                                                           'status': 'I'}]).get_json()['auto_bound'])

    def test_dock_candidates_route_is_inside_the_lock_too(self):
        """🔴 후보 캐시 라우트도 `svms_dk_cd` 를 쓰는 **세 번째 입구**다.

        잠금을 두 입구에만 걸면 runner 회신 한 번으로 우회된다.  (지금 계약상 잠긴
        프로젝트는 dk_cd 가 이미 차 있어 COALESCE 가 no-op 이지만, 그건 다른 함수의
        성질에 기댄 것이라 여기서 직접 막는다.)
        """
        p, r = self._dd_project_report('Dock 후보 잠금 DD')
        with appmod.app.app_context():
            appmod.execute("UPDATE dock_daily_report SET svms_sync_status='partial' WHERE id=?",
                           (r['id'],))
            appmod.execute("UPDATE dock_daily_project SET svms_dk_cd=NULL WHERE id=?", (p['id'],))
        out = self._dock_candidates(p['id'], [{'dk_cd': 'ATGRMD2607130001',
                                               'status': 'I'}]).get_json()
        self.assertIsNone(out['auto_bound'])
        self.assertIsNone(out['dk_cd'])
        self.assertEqual(1, out['count'], '후보 목록 자체는 갱신돼야 한다')

    def test_project_create_refuses_a_malformed_dock_no(self):
        """🔴 생성도 `svms_dk_cd` writer 다 -- 여기만 검증이 없으면 형식이 깨진 채 굳는다."""
        bad = self.client.post('/api/dock-daily/projects',
                               json={'vessel_id': self.vessel, 'title': '생성 오타 DD',
                                     'svms_dk_cd': '한글코드'})
        self.assertEqual(400, bad.status_code)
        blank = self.client.post('/api/dock-daily/projects',
                                 json={'vessel_id': self.vessel, 'title': '생성 공백 DD',
                                       'svms_dk_cd': '   '}).get_json()
        self.assertIsNone(blank.get('svms_dk_cd'), '공백은 빈 값으로 저장한다')

    def test_dock_link_patch_is_not_a_second_writer(self):
        """🔴 잠기지 **않은** 프로젝트라도 PATCH 로 바꾸면 형식검증·목록대조·확인문자열이
        전부 없는 두 번째 writer 가 된다.  변경은 전용 라우트로만 받는다."""
        p, _ = self._dd_project_report('Dock PATCH DD')
        res = self.client.patch(f"/api/dock-daily/projects/{p['id']}",
                                json={'svms_dk_cd': 'ATGRMD2607130001'})
        self.assertEqual(409, res.status_code, res.get_data(as_text=True))
        self.assertEqual('dk_cd_route_required', res.get_json()['code'])

    def test_dock_link_allow_unlisted_must_be_a_real_true(self):
        """🔴 `allow_unlisted` 를 truthy 로 받으면 `"false"`·`0.1`·`[]` 같은 값이
        목록대조를 통째로 끈다.  진짜 `True` 만 받는다."""
        p, _ = self._dd_project_report('Dock unlisted DD')
        self._dock_candidates(p['id'], [{'dk_cd': 'ATGRMD2607130001', 'status': 'I'}])
        path = f"/api/dock-daily/projects/{p['id']}/svms-dk-cd"
        for value in ('false', 'no', 1, [1]):
            res = self.client.post(path, json={'dk_cd': 'BGBBMD2608050001',
                                               'allow_unlisted': value,
                                               'confirmation': 'user_selected_dock'})
            self.assertEqual(409, res.status_code, repr(value))
            self.assertEqual('dk_cd_unlisted', res.get_json()['code'], repr(value))

    def test_dock_link_is_the_only_exit_from_the_dk_cd_blocker(self):
        """🔴 `svms_dk_cd` 는 프로젝트 **생성 화면**에서만 넣을 수 있었다.

        만들 때 비워 둔 프로젝트는 상신이 영구히 불가였고(형 캡쳐 2026-08-22: 라이브
        프로젝트 2건 모두 NULL), 화면에는 사유도 없었다.  전용 라우트로 고칠 수 있어야
        하고, 조회 목록·잠금 상태가 같은 응답에 있어야 화면이 안내를 만들 수 있다.
        """
        p, r = self._dd_project_report('Dock 연결 DD')
        listed = self.client.get(f"/api/dock-daily/projects/{p['id']}/svms-docks").get_json()
        self.assertIsNone(listed['dk_cd'])
        self.assertFalse(listed['locked'])
        self.assertEqual([], listed['candidates'])

        self._dock_candidates(p['id'], [
            {'dk_cd': 'ATGRMD2607130001', 'subj': 'DD 2026', 'status': 'I'},
            {'dk_cd': 'ATGR22062701', 'status': 'C', 'dk_out_date': '20211105'},
        ])
        listed = self.client.get(f"/api/dock-daily/projects/{p['id']}/svms-docks").get_json()
        self.assertEqual(['ATGRMD2607130001', 'ATGR22062701'],
                         [c['dk_cd'] for c in listed['candidates']])
        # 열린 후보가 딱 1건이라 자동 연결된다.
        self.assertEqual('ATGRMD2607130001', listed['dk_cd'])
        with self._svms_env():
            preview = self.client.get(
                f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertEqual([], preview['blockers'])
        self.assertTrue(preview['publishable'])

    def test_dock_link_refuses_a_typo_and_needs_an_explicit_confirmation(self):
        """🔴 오타 한 글자면 **다른 배의 dock** 에 daily report 가 저장된다."""
        p, _ = self._dd_project_report('Dock 오타 DD')
        self._dock_candidates(p['id'], [{'dk_cd': 'ATGRMD2607130001', 'status': 'I'},
                                        {'dk_cd': 'BGBBMD2608050001', 'status': 'D'}])
        path = f"/api/dock-daily/projects/{p['id']}/svms-dk-cd"
        # 확인 문자열이 없으면 통과하지 않는다.
        no_conf = self.client.post(path, json={'dk_cd': 'ATGRMD2607130001'})
        self.assertEqual(400, no_conf.status_code)
        self.assertEqual('confirmation_required', no_conf.get_json()['code'])
        # 열린 후보가 2건이라 자동 연결은 안 됐다.
        self.assertIsNone(self.client.get(
            f"/api/dock-daily/projects/{p['id']}/svms-docks").get_json()['dk_cd'])

        bad = self.client.post(path, json={'dk_cd': '한', 'confirmation': 'user_selected_dock'})
        self.assertEqual(400, bad.status_code)
        self.assertEqual('dk_cd_invalid', bad.get_json()['code'])

        unlisted = self.client.post(path, json={'dk_cd': 'ATGRMD2607130002',
                                                'confirmation': 'user_selected_dock'})
        self.assertEqual(409, unlisted.status_code)
        self.assertEqual('dk_cd_unlisted', unlisted.get_json()['code'])
        self.assertIn('ATGRMD2607130001', unlisted.get_json()['candidates'])

        forced = self.client.post(path, json={'dk_cd': 'ATGRMD2607130002', 'allow_unlisted': True,
                                              'confirmation': 'user_selected_dock'})
        self.assertEqual(200, forced.status_code, forced.get_data(as_text=True))
        self.assertTrue(forced.get_json()['changed'])
        # 같은 값 재선택은 성공이지만 바뀐 게 없다 -- 화면이 "연결했습니다" 로 뭉개면 안 된다.
        again = self.client.post(path, json={'dk_cd': 'ATGRMD2607130002',
                                             'confirmation': 'user_selected_dock'}).get_json()
        self.assertFalse(again['changed'])

    def test_dock_link_is_locked_once_a_report_went_to_svms_at_every_entrance(self):
        """🔴 연결을 바꾸면 **이후 보고서가 다른 dock 으로 간다**.

        입구가 둘(전용 라우트, 프로젝트 `PATCH`)이라 하나만 잠그면 나머지로 우회된다.
        """
        p, r = self._dd_project_report('Dock 잠금 DD')
        self._dock_candidates(p['id'], [{'dk_cd': 'ATGRMD2607130001', 'status': 'I'}])
        with appmod.app.app_context():
            appmod.execute("UPDATE dock_daily_report SET svms_sync_status='synced' WHERE id=?",
                           (r['id'],))
        listed = self.client.get(f"/api/dock-daily/projects/{p['id']}/svms-docks").get_json()
        self.assertTrue(listed['locked'])
        self.assertIn('Dock 연결', listed['locked_reason'])

        direct = self.client.post(f"/api/dock-daily/projects/{p['id']}/svms-dk-cd",
                                  json={'dk_cd': 'BGBBMD2608050001', 'allow_unlisted': True,
                                        'confirmation': 'user_selected_dock'})
        self.assertEqual(409, direct.status_code)
        self.assertEqual('dk_cd_locked', direct.get_json()['code'])

        via_patch = self.client.patch(f"/api/dock-daily/projects/{p['id']}",
                                      json={'svms_dk_cd': 'BGBBMD2608050001'})
        self.assertEqual(409, via_patch.status_code, via_patch.get_data(as_text=True))
        self.assertEqual('dk_cd_locked', via_patch.get_json()['code'])
        # 같은 값 PATCH 는 변경이 아니므로 통과해야 한다(다른 필드 저장이 막히면 안 된다).
        same = self.client.patch(f"/api/dock-daily/projects/{p['id']}",
                                 json={'svms_dk_cd': 'ATGRMD2607130001', 'title': '이름만 변경'})
        self.assertEqual(200, same.status_code, same.get_data(as_text=True))

    def test_dock_candidates_never_overwrite_a_human_pick(self):
        """🔴 이미 붙은 값을 자동으로 갈아치우면 형이 고른 dock 이 조용히 바뀐다."""
        p, _ = self._dd_project_report('Dock 자동연결 DD')
        self.client.post(f"/api/dock-daily/projects/{p['id']}/svms-dk-cd",
                         json={'dk_cd': 'ATGRMD2607130001', 'allow_unlisted': True,
                               'confirmation': 'user_selected_dock'})
        out = self._dock_candidates(p['id'], [{'dk_cd': 'BGBBMD2608050001', 'status': 'I'}]).get_json()
        self.assertEqual('ATGRMD2607130001', out['dk_cd'])
        self.assertIsNone(out['auto_bound'])
        # 후보가 모호하면(열린 게 2건) 자동 연결하지 않는다.
        p2, _ = self._dd_project_report('Dock 모호 DD', '2026-08-19')
        out2 = self._dock_candidates(p2['id'], [{'dk_cd': 'ATGRMD2607130001', 'status': 'I'},
                                                {'dk_cd': 'BGBBMD2608050001', 'status': 'D'}]).get_json()
        self.assertIsNone(out2['dk_cd'])
        self.assertIsNone(out2['auto_bound'])
        # 닫힌 후보만 있으면 자동 연결하지 않는다 -- 지난 입거에 daily report 를 쓰는 사고.
        p3, _ = self._dd_project_report('Dock 종료 DD', '2026-08-18')
        out3 = self._dock_candidates(p3['id'], [
            {'dk_cd': 'ATGR22062701', 'status': 'C', 'dk_out_date': '20211105'}]).get_json()
        self.assertIsNone(out3['dk_cd'])
        listed = self.client.get(f"/api/dock-daily/projects/{p3['id']}/svms-docks").get_json()
        self.assertFalse(listed['candidates'][0]['open'])

    def _final_section_project(self, title):
        """확정본에 내용이 든 special 섹션 + 열려 있는 다음 일자 보고서."""
        p, first = self._dd_project_report(title, '2026-08-20')
        self.client.post(f"/api/dock-daily/projects/{p['id']}/sections", json={'label': '표 섹션'})
        saved = self.client.put(f"/api/dock-daily/reports/{first['id']}", json={
            'revision': first['revision'],
            'operations': [{'section_key': 'sec_1', 'block_type': 'paragraph',
                            'content': {'body': '확정본에 든 내용'}}]}).get_json()
        finalized = self.client.put(f"/api/dock-daily/reports/{first['id']}", json={
            'revision': saved['revision'], 'status': 'final', 'operations': []})
        self.assertEqual(200, finalized.status_code, finalized.get_data(as_text=True))
        second = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                  json={'report_date': '2026-08-21'}).get_json()
        return p, second

    def test_hiding_a_section_is_refused_while_a_final_report_holds_it(self):
        """🔴 `section_delete` 만 확정본을 봤고 `enabled=0` 은 안 봤다.

        감춘 섹션은 메일·SVMS 렌더에서 그냥 빠진다.  즉 확정본에 든 표 한 장이
        **삭제는 409 로 막히는데 체크 해제로는 조용히 사라졌다** -- 그러면 "확정" 이
        뜻하는 게 없다.  두 입구(`PUT` 의 `section_updates`, 프로젝트 `PATCH`)가 같은
        가드를 써야 한다.
        """
        p, second = self._final_section_project('확정 섹션 감추기 DD')
        via_put = self.client.put(f"/api/dock-daily/reports/{second['id']}", json={
            'revision': second['revision'],
            'section_updates': [{'section_key': 'sec_1', 'enabled': False}]})
        self.assertEqual(409, via_put.status_code, via_put.get_data(as_text=True))
        body = via_put.get_json()
        self.assertEqual('final_report_has_content', body['code'])
        self.assertEqual(['2026-08-20'], body['dates'], '어느 날짜가 막는지 알려 준다')

        via_patch = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={
            'sections': [{'section_key': 'sec_1', 'enabled': False}]})
        self.assertEqual(409, via_patch.status_code, via_patch.get_data(as_text=True))
        self.assertEqual('final_report_has_content', via_patch.get_json()['code'])
        # 두 경로 모두 아무것도 바꾸지 않았다.
        sections = {s['section_key']: s for s in
                    self.client.get(f"/api/dock-daily/reports/{second['id']}").get_json()['sections']}
        self.assertIn('sec_1', sections)
        self.assertTrue(sections['sec_1']['enabled'], '거절된 요청은 아무것도 안 바꾼다')
        with appmod.app.app_context():
            self.assertEqual(1, appmod.query(
                'SELECT enabled FROM dock_daily_section_def WHERE project_id=? AND section_key=?',
                (p['id'], 'sec_1'), one=True)['enabled'])

    def test_hiding_an_empty_section_still_works(self):
        """가드는 **내용이 있는** 확정본에만 걸린다. 빈 섹션은 그냥 감춰진다."""
        p, r = self._dd_project_report('빈 섹션 감추기 DD')
        self.client.post(f"/api/dock-daily/projects/{p['id']}/sections", json={'label': '빈 섹션'})
        ok = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={
            'sections': [{'section_key': 'sec_1', 'enabled': False}]})
        self.assertEqual(200, ok.status_code, ok.get_data(as_text=True))
        with appmod.app.app_context():
            row = appmod.query('SELECT enabled FROM dock_daily_section_def'
                               ' WHERE project_id=? AND section_key=?', (p['id'], 'sec_1'),
                               one=True)
        self.assertEqual(0, row['enabled'])

    def test_project_patch_saves_the_body_and_the_sections_atomically(self):
        """🔴 `execute()` 는 문장마다 commit 한다(app_core).

        전엔 섹션 하나가 터지면 프로젝트 UPDATE 와 앞선 섹션 INSERT 는 **이미 저장된
        채로** 500 이 나갔다.  호출자는 "아무것도 저장 안 됨" 으로 읽고 재시도해 같은
        일을 두 번 한다.
        """
        p, _r = self._dd_project_report('원자성 DD')
        broken = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={
            'title': '바뀌면 안 되는 제목',
            'sections': [{'section_key': 'good_one', 'label': '먼저 들어간 섹션'},
                         {'section_key': 'bad_one', 'sort_order': 'abc'}]})
        self.assertEqual(400, broken.status_code, broken.get_data(as_text=True))
        self.assertIn('sort_order', broken.get_json()['error'])
        row = next(x for x in self.client.get('/api/dock-daily/projects').get_json()
                   if x['id'] == p['id'])
        self.assertEqual('원자성 DD', row['title'], '프로젝트 본문이 함께 되돌려져야 한다')
        with appmod.app.app_context():
            keys = [x['section_key'] for x in appmod.query(
                'SELECT section_key FROM dock_daily_section_def WHERE project_id=?', (p['id'],))]
        self.assertNotIn('good_one', keys, '앞선 섹션 INSERT 도 되돌려져야 한다')
        # 문자열 정수는 계속 받는다 -- 기존 클라이언트가 `"20"` 을 보낸다.
        ok = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={
            'sections': [{'section_key': 'good_one', 'label': 'OK', 'sort_order': '20'}]})
        self.assertEqual(200, ok.status_code, ok.get_data(as_text=True))

    def test_a_section_definition_change_bumps_the_other_open_reports(self):
        """🔴 섹션 목록은 **프로젝트** 값인데 CAS 는 보고서 하나만 잠근다.

        올리지 않으면 다른 일자를 열어 둔 기기가 옛 목록을 그대로 되돌려 보내 방금 바꾼
        설정을 조용히 되돌린다 -- 양쪽 어디에도 409 가 안 뜬다.  여기서 늘어나는 409 는
        **정확한** CAS 동작이다(그 기기는 진짜로 낡은 목록을 들고 있다).
        """
        p, first = self._dd_project_report('형제 bump DD', '2026-08-20')
        self.client.post(f"/api/dock-daily/projects/{p['id']}/sections", json={'label': '원래 이름'})
        second = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                  json={'report_date': '2026-08-21'}).get_json()
        stale = self.client.get(f"/api/dock-daily/reports/{second['id']}").get_json()['revision']

        renamed = self.client.put(f"/api/dock-daily/reports/{first['id']}", json={
            'revision': self.client.get(
                f"/api/dock-daily/reports/{first['id']}").get_json()['revision'],
            'section_updates': [{'section_key': 'sec_1', 'label': '바뀐 이름'}]})
        self.assertEqual(200, renamed.status_code, renamed.get_data(as_text=True))
        bumped = self.client.get(f"/api/dock-daily/reports/{second['id']}").get_json()['revision']
        self.assertEqual(stale + 1, bumped, '다른 일자 보고서 revision 이 올라야 한다')
        conflict = self.client.put(f"/api/dock-daily/reports/{second['id']}", json={
            'revision': stale,
            'section_updates': [{'section_key': 'sec_1', 'label': '원래 이름'}]})
        self.assertEqual(409, conflict.status_code)
        self.assertEqual('revision_conflict', conflict.get_json()['code'])
        with appmod.app.app_context():
            label = appmod.query('SELECT label FROM dock_daily_section_def'
                                 ' WHERE project_id=? AND section_key=?', (p['id'], 'sec_1'),
                                 one=True)['label']
        self.assertEqual('바뀐 이름', label, '낡은 기기가 이름을 되돌리지 못한다')

    def test_echoing_the_section_list_back_does_not_bump_anything(self):
        """🔴 값이 그대로인 저장은 형제를 올리지 않는다.

        순서를 보낼 때 목록 전체를 echo 하는 클라이언트가 있다.  거기서 bump 하면
        **아무것도 안 바꾼 저장마다** 다른 기기가 409 를 맞는다.
        """
        p, first = self._dd_project_report('echo 저장 DD', '2026-08-20')
        created = self.client.post(f"/api/dock-daily/projects/{p['id']}/sections",
                                   json={'label': '그대로'}).get_json()['sections']
        current = {s['section_key']: s for s in created}
        second = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                  json={'report_date': '2026-08-21'}).get_json()
        before = self.client.get(f"/api/dock-daily/reports/{second['id']}").get_json()['revision']
        echo = self.client.put(f"/api/dock-daily/reports/{first['id']}", json={
            'revision': self.client.get(
                f"/api/dock-daily/reports/{first['id']}").get_json()['revision'],
            'section_updates': [{'section_key': k, 'label': v['label'],
                                 'sort_order': v['sort_order'],
                                 **({'enabled': bool(v['enabled'])} if v['kind'] == 'special' else {})}
                                for k, v in current.items()]})
        self.assertEqual(200, echo.status_code, echo.get_data(as_text=True))
        after = self.client.get(f"/api/dock-daily/reports/{second['id']}").get_json()['revision']
        self.assertEqual(before, after, 'echo 저장은 형제 revision 을 건드리지 않는다')

    def test_a_new_section_from_the_project_patch_bumps_the_open_reports(self):
        """프로젝트 PATCH 로 섹션이 새로 생겨도 열린 보고서는 목록이 낡는다."""
        p, first = self._dd_project_report('PATCH bump DD', '2026-08-20')
        before = self.client.get(f"/api/dock-daily/reports/{first['id']}").get_json()['revision']
        added = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={
            'sections': [{'section_key': 'extra_one', 'label': '추가'}]})
        self.assertEqual(200, added.status_code, added.get_data(as_text=True))
        after = self.client.get(f"/api/dock-daily/reports/{first['id']}").get_json()['revision']
        self.assertEqual(before + 1, after)
        # 같은 섹션을 다시 보내면 INSERT OR IGNORE 가 아무것도 안 하므로 올리지 않는다.
        again = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={
            'sections': [{'section_key': 'extra_one', 'label': '추가'}]})
        self.assertEqual(200, again.status_code)
        self.assertEqual(after, self.client.get(
            f"/api/dock-daily/reports/{first['id']}").get_json()['revision'])

    def test_a_final_report_is_bumped_too_because_it_can_be_unfinalized(self):
        """🔴 확정본을 건너뛰면 안 된다(올마이트 blocking, 내 첫 구현이 틀렸다).

        "PUT 이 `final_locked` 라 되살릴 주체가 없다" 는 전제가 거짓이다 -- `report_status`
        가 확정취소를 열어 뒀고 그 라우트는 revision 만 맞으면 통과한다.  건너뛰면 확정본
        revision 이 그대로 유효해서 낡은 기기가 **①확정취소 성공 → ②그 응답 revision 으로
        옛 섹션 목록 echo** 2단계로 방금 바꾼 설정을 되돌린다.  올려 두면 ①에서 끊긴다.
        """
        p, first = self._dd_project_report('확정본 bump DD', '2026-08-20')
        self.client.post(f"/api/dock-daily/projects/{p['id']}/sections", json={'label': '원래 이름'})
        rev = self.client.get(f"/api/dock-daily/reports/{first['id']}").get_json()['revision']
        finalized = self.client.put(f"/api/dock-daily/reports/{first['id']}",
                                    json={'revision': rev, 'status': 'final', 'operations': []})
        self.assertEqual(200, finalized.status_code, finalized.get_data(as_text=True))
        stale = finalized.get_json()['revision']

        renamed = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={
            'sections': [{'section_key': 'sec_1', 'label': '바뀐 이름'}]})
        self.assertEqual(200, renamed.status_code, renamed.get_data(as_text=True))
        self.assertGreater(self.client.get(
            f"/api/dock-daily/reports/{first['id']}").get_json()['revision'], stale,
            '확정본 revision 도 올라야 한다')
        # 낡은 기기의 1단계(확정취소)가 여기서 끊긴다.
        release = self.client.post(f"/api/dock-daily/reports/{first['id']}/status",
                                   json={'revision': stale, 'status': 'editing'})
        self.assertEqual(409, release.status_code, release.get_data(as_text=True))
        with appmod.app.app_context():
            self.assertEqual('바뀐 이름', appmod.query(
                'SELECT label FROM dock_daily_section_def WHERE project_id=? AND section_key=?',
                (p['id'], 'sec_1'), one=True)['label'])

    def test_block_operation_rejects_a_field_that_is_not_an_integer(self):
        """🔴 본문 계약 위반은 400 이다.  전엔 `int(op.get('id') or 0)` 이 그대로 터져
        500 이 갔고, 형에게는 "서버가 죽었다" 로 읽힌다.

        `bool` 은 int 의 하위형이라 `sort_order: true` 가 1번 자리로 조용히 들어갔다.
        """
        _p, r = self._dd_project_report('블록 op 400 DD')
        rev = r['revision']
        for op in ({'id': 'abc', 'section_key': 'shipyard', 'content': {'body': 'x'}},
                   {'sort_order': 'first', 'section_key': 'shipyard', 'content': {'body': 'x'}},
                   {'sort_order': True, 'section_key': 'shipyard', 'content': {'body': 'x'}},
                   {'op': 'delete', 'id': 'abc'}):
            resp = self.client.put(f"/api/dock-daily/reports/{r['id']}",
                                   json={'revision': rev, 'operations': [op]})
            self.assertEqual(400, resp.status_code, op)
            self.assertIn('integer', resp.get_json()['error'], op)
        # 아무것도 저장되지 않았다 -- revision 이 그대로다.
        self.assertEqual(rev, self.client.get(
            f"/api/dock-daily/reports/{r['id']}").get_json()['revision'])
        # 빈 문자열·None 은 "안 보냄" 과 같다(기존 클라이언트 계약).
        ok = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': rev,
            'operations': [{'id': '', 'sort_order': None, 'section_key': 'shipyard',
                            'content': {'body': 'x'}}]})
        self.assertEqual(200, ok.status_code, ok.get_data(as_text=True))

    def test_block_operation_rejects_an_unknown_parent_block(self):
        """🔴 `parent_id` 는 FK 다.  없는 id 를 그대로 넣으면 IntegrityError 가 500 으로
        새어 나가고 무엇이 틀렸는지 응답에 아무것도 안 남는다.  다른 보고서의 블록도
        남이다 -- 같은 보고서 안에서만 부모가 된다."""
        _p, r = self._dd_project_report('parent_id DD', '2026-08-20')
        rev = r['revision']
        bad_type = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': rev, 'operations': [{'section_key': 'shipyard', 'parent_id': 'abc',
                                             'content': {'body': 'x'}}]})
        self.assertEqual(400, bad_type.status_code)
        self.assertIn('parent_id', bad_type.get_json()['error'])
        missing = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': rev, 'operations': [{'section_key': 'shipyard', 'parent_id': 999999,
                                             'content': {'body': 'x'}}]})
        self.assertEqual(404, missing.status_code)
        self.assertEqual('parent block not found', missing.get_json()['error'])

        parent = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': rev, 'operations': [{'section_key': 'shipyard',
                                             'content': {'body': 'parent'}}]}).get_json()
        pbid = parent['blocks'][0]['id']
        child = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={
            'revision': parent['revision'],
            'operations': [{'section_key': 'shipyard', 'parent_id': pbid,
                            'content': {'body': 'child'}}]})
        self.assertEqual(200, child.status_code, child.get_data(as_text=True))
        self.assertIn(pbid, [b.get('parent_id') for b in child.get_json()['blocks']],
                      'parent_id 는 실제로 저장돼야 한다')

    def test_copy_from_refuses_a_source_that_is_not_an_earlier_date(self):
        """🔴 "이전 일자" 는 화면 문구가 아니라 계약이다.

        서버가 안 보면 웹 드롭다운만이 유일한 방어선이고, 앱 버그·러너·curl 이
        **뒷날 진행사항으로 앞날 기록을 덮을** 수 있다(`replace` 는 기존 카드를 지운다).
        """
        p, early = self._dd_project_report('copy 방향 DD', '2026-08-20')
        late = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                json={'report_date': '2026-08-21'}).get_json()
        self.client.put(f"/api/dock-daily/reports/{late['id']}", json={
            'revision': late['revision'],
            'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph',
                            'content': {'body': '뒷날 진행사항'}}]})
        early_rev = self.client.get(
            f"/api/dock-daily/reports/{early['id']}").get_json()['revision']
        backwards = self.client.post(f"/api/dock-daily/reports/{early['id']}/copy-from",
                                     json={'revision': early_rev,
                                           'source_report_id': late['id'], 'mode': 'replace'})
        self.assertEqual(400, backwards.status_code, backwards.get_data(as_text=True))
        self.assertEqual('not_earlier', backwards.get_json()['code'])
        # 자기 자신은 더 앞선 `same_report` 가드가 잡는다(더 구체적인 문구가 낫다).
        same = self.client.post(f"/api/dock-daily/reports/{early['id']}/copy-from",
                                json={'revision': early_rev, 'source_report_id': early['id']})
        self.assertEqual(400, same.status_code)
        self.assertEqual('same_report', same.get_json()['code'])
        # 앞날 기록은 그대로다.
        self.assertEqual([], self.client.get(
            f"/api/dock-daily/reports/{early['id']}").get_json()['blocks'])
        late_rev = self.client.get(
            f"/api/dock-daily/reports/{late['id']}").get_json()['revision']
        forwards = self.client.post(f"/api/dock-daily/reports/{late['id']}/copy-from",
                                    json={'revision': late_rev,
                                          'source_report_id': early['id']})
        self.assertEqual(200, forwards.status_code, forwards.get_data(as_text=True))

    def test_a_refused_attachment_upload_leaves_no_orphan_blob(self):
        """🔴 행 없는 blob 은 어떤 purge 경로도 못 찾는다(전부 행을 훑는다).

        영구 고아 파일이 되고, 그게 `_purge_files` 가 드러내려고 존재하는 실패다.
        """
        _p, r = self._dd_project_report('고아 blob DD')
        before = set(os.listdir(routes_dock_daily.UPLOAD_DIR)) if os.path.isdir(
            routes_dock_daily.UPLOAD_DIR) else set()
        refused = self.client.post(f"/api/dock-daily/reports/{r['id']}/attachments",
                                   data={'file': (io.BytesIO(self._png()), 'orphan.png'),
                                         'block_id': '999999'},
                                   content_type='multipart/form-data')
        self.assertEqual(404, refused.status_code, refused.get_data(as_text=True))
        self.assertEqual('block not found', refused.get_json()['error'])
        after = set(os.listdir(routes_dock_daily.UPLOAD_DIR)) if os.path.isdir(
            routes_dock_daily.UPLOAD_DIR) else set()
        self.assertEqual(before, after, '거절된 업로드는 파일을 남기지 않는다')
        with appmod.app.app_context():
            self.assertEqual([], appmod.query(
                'SELECT id FROM dock_daily_attachment WHERE report_id=?', (r['id'],)))

    def test_attachment_upload_checks_the_final_lock_inside_the_transaction(self):
        """확인과 INSERT 가 한 트랜잭션 안이라 확정본에는 어떤 경로로도 안 붙는다.

        확정 뒤에 붙은 첨부는 `attachment_delete` 가 다시 409 로 거절해서 확정취소
        없이는 못 지운다.
        """
        _p, r = self._dd_project_report('확정 첨부 DD')
        self.client.put(f"/api/dock-daily/reports/{r['id']}",
                        json={'revision': r['revision'], 'status': 'final', 'operations': []})
        blocked = self.client.post(f"/api/dock-daily/reports/{r['id']}/attachments",
                                   data={'file': (io.BytesIO(self._png()), 'late.png')},
                                   content_type='multipart/form-data')
        self.assertEqual(409, blocked.status_code)
        self.assertIn('locked', blocked.get_json()['error'])
        src = inspect.getsource(routes_dock_daily.attachment_post)
        self.assertIn('BEGIN IMMEDIATE', src, '상태 재확인은 트랜잭션 안이어야 한다')
        self.assertIn('_purge_files([stored])', src)

    def test_attachment_delete_bumps_the_revision_of_the_block_it_edited(self):
        """🔴 블록을 고쳤으면 revision 을 올린다.

        안 올리면 그 보고서를 열어 둔 다른 기기가 **아직 유효한** revision 으로 저장에
        성공하고(upsert 는 content_json 을 통째로 덮는다) 방금 끊어낸 attachment_id 를
        되살린다 -- 지운 사람은 깨끗이 지워졌다고 믿는데 메일에는 "첨부 파일을 찾을 수
        없습니다" 가 나간다.
        """
        rid, ids = self._report_with_gallery('첨부삭제 revision DD', count=2, columns=2)
        before = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        stale = before['revision']
        bid = next(b['id'] for b in before['blocks'] if b['block_type'] == 'image')
        removed = self.client.delete(f'/api/dock-daily/attachments/{ids[0]}')
        self.assertEqual(200, removed.status_code, removed.get_data(as_text=True))
        after = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        self.assertEqual(stale + 1, after['revision'])
        resurrect = self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': stale,
            'operations': [{'id': bid, 'section_key': 'shipyard', 'block_type': 'image',
                            'content': {'columns': 2,
                                        'images': [{'attachment_id': ids[0], 'caption': 'x'},
                                                   {'attachment_id': ids[1], 'caption': 'y'}]}}]})
        self.assertEqual(409, resurrect.status_code, resurrect.get_data(as_text=True))
        self.assertEqual('revision_conflict', resurrect.get_json()['code'])

    def test_deleting_an_unreferenced_attachment_also_bumps_the_revision(self):
        """🔴 아직 어느 블록도 안 가리키는 첨부가 정확히 그 구멍이다(올마이트 blocking).

        내 첫 구현은 블록을 고쳤을 때만 올렸다.  그런데 다른 기기가 첨부 카드에 올려 둔
        사진을 사진 카드에 **로컬로** 끼워 넣은 상태에서 이쪽이 그 첨부를 지우면, 저쪽
        revision 이 그대로 유효해서 저장이 통과하고 **지워진 attachment_id** 가 블록에
        박힌다.  첨부 목록 자체가 `_report_json` 의 일부이므로 첨부 삭제는 그 자체로
        보고서 상태 변경이다.
        """
        project, report, _stored = self._project_with_attachment('첨부삭제 미참조 DD')
        rid = report['id']
        before = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        stale = before['revision']
        aid = before['attachments'][0]['id']
        self.assertEqual([], [b for b in before['blocks'] if b['block_type'] == 'image'],
                         '이 첨부는 아직 어느 블록도 가리키지 않는다')
        self.assertEqual(200, self.client.delete(f'/api/dock-daily/attachments/{aid}').status_code)
        self.assertEqual(stale + 1, self.client.get(
            f'/api/dock-daily/reports/{rid}').get_json()['revision'])
        # 낡은 기기가 지워진 첨부를 사진 카드에 박아 넣는 저장이 여기서 끊긴다.
        resurrect = self.client.put(f'/api/dock-daily/reports/{rid}', json={
            'revision': stale,
            'operations': [{'section_key': 'shipyard', 'block_type': 'image',
                            'content': {'attachment_id': aid, 'caption': '로컬로 끼워 넣은 사진'}}]})
        self.assertEqual(409, resurrect.status_code, resurrect.get_data(as_text=True))
        self.assertEqual('revision_conflict', resurrect.get_json()['code'])

    def test_dock_daily_uploads_are_not_served_without_a_session(self):
        """🔴 `/static/uploads/dock_daily_*` 는 로그인 없이 내주지 않는다.

        실측(라이브, 게이트 전): `/static/uploads/<이름>` → 404, `/dock-daily` → 302
        login.  즉 파일 URL 을 아는 사람은 페이지 없이 사진을 꺼낼 수 있었다.

        🔴 **범위는 좁게.**  `/static/uploads/` 를 통째로 막으면 도크 리포트·승선·영수증
        사진이 깨진다 -- `routes_calendar_dock.py` 와 DOCX 생성기가 그 URL 을 **공개
        URL 로 일부러** 쓴다(메일 클라이언트·Word 는 세션이 없다).
        """
        anon = appmod.app.test_client()
        guarded = anon.get('/static/uploads/dock_daily_deadbeef.png')
        self.assertEqual(401, guarded.status_code, guarded.get_data(as_text=True))
        for public in ('/static/uploads/dock/x.jpg', '/static/uploads/boarding/y.jpg',
                       '/static/uploads/receipt/z.jpg'):
            self.assertNotEqual(401, anon.get(public).status_code, public)
        # 로그인한 세션은 게이트를 지나 **실제 바이트**를 받는다.  404 만 확인하면 게이트가
        # 웹 화면을 깨뜨리는지 아무것도 증명하지 못한다(올마이트 지적).
        real = os.path.join(appmod.app.static_folder, 'uploads', 'dock_daily_gate_probe.png')
        os.makedirs(os.path.dirname(real), exist_ok=True)
        payload = self._png((8, 8))
        with open(real, 'wb') as fh:
            fh.write(payload)
        try:
            served = self.client.get('/static/uploads/dock_daily_gate_probe.png')
            self.assertEqual(200, served.status_code)
            self.assertEqual(payload, served.get_data())
            self.assertEqual(401, anon.get('/static/uploads/dock_daily_gate_probe.png').status_code,
                             '파일이 실제로 있을 때도 익명은 못 받는다')
        finally:
            os.unlink(real)
        # ⚠️ Bearer 분기는 이 경로에서 **닿지 않는다**: `_bearer_auth` 는 `/api/` 로
        # 시작하지 않는 요청에서 즉시 return 하므로 `g._token_auth` 가 안 세워진다.
        # 지금 통과 조건은 쿠키 세션 하나뿐이고, 이 게이트가 앱을 깨지 않는 근거는
        # "iOS 가 이 URL 을 안 쓴다"(첨부는 `/api/dock-daily/attachments/<id>`)다.
        token_only = appmod.app.test_client()
        self.assertEqual(401, token_only.get('/static/uploads/dock_daily_gate_probe.png',
                                             headers={'Authorization': 'Bearer whatever'}).status_code)

    def test_report_generate_is_idempotent_under_a_double_click(self):
        """🔴 `BEGIN IMMEDIATE`.  `_create_report` 는 SELECT→INSERT 이고 컬럼에
        `UNIQUE(project_id, report_date)` 가 걸려 있다 -- deferred BEGIN 이면 두 번째
        INSERT 가 IntegrityError 로 500 이 된다.  이 라우트는 원래 이미 있는 보고서를
        200 으로 돌려주는 멱등 계약이다."""
        src = inspect.getsource(routes_dock_daily.report_generate)
        self.assertIn("db.execute('BEGIN IMMEDIATE')", src)
        p, first = self._dd_project_report('멱등 생성 DD')
        again = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate",
                                 json={'report_date': '2026-08-20'})
        self.assertEqual(200, again.status_code, again.get_data(as_text=True))
        self.assertEqual(first['id'], again.get_json()['id'])

    def test_mail_photo_failure_note_is_wrapped_for_outlook(self):
        """🔴 못 실은 이유를 `<td>` 직속 텍스트로 두면 Outlook 이 ~8pt 로 붙인다.

        읽기 힘든 크기로 나가면 형은 경고를 못 보고 발송한다 -- `cell()`/`item()` 과
        같은 11pt `<p>` 계약을 쓴다.
        """
        rid, _aid = self._report_with_photo_block('경고 서식 DD', payload=self._fake_png())
        mail = self._mail(rid)
        self.assertRegex(mail['html'], r'<p style="margin:0;[^"]*font-size:11pt[^"]*">'
                                       r'<span[^>]*>\(')
        self.assertNotIn('첨부파일로 확인', mail['html'])

    def test_frontend_writes_every_text_key_the_server_may_read(self):
        """🔴 서버 `_plain` 은 `title`→`body`→`text` 순으로 읽는다.

        한 키만 쓰면 다른 키를 든 블록은 **화면만 바뀌고 메일은 옛 글**이 나간다.
        정본 규칙은 iOS `DockDailyBlockText` 이고, 웹 미러가 같은 일을 해야 한다.
        """
        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js',
                               'dock_daily.js'), encoding='utf-8') as f:
            script = f.read()
        self.assertIn('function writeText(', script)
        self.assertIn("for(const k of ['title','body','text'])", script)
        self.assertIn('b.content=writeText(b,ta.value)', script)
        # 프로젝트·보고서를 바꾸는 사이 늦게 온 응답이 새 화면을 덮지 않는다.
        self.assertIn('projectSeq', script)
        self.assertIn('if (pseq !== projectSeq) return;', script)
        # 확정본에는 초안 글칸을 밀어넣지 않는다.
        self.assertIn("if (state.report.status === 'final') return;", script)
        # 삭제·확정은 누른 순간의 id 로 보낸다.
        self.assertIn('const pid=state.project.id', script)
        self.assertIn('const rid=state.report.id', script)
        # 두 번 눌러 두 번 저장하지 않는다.
        self.assertIn("once($('#dd-save')", script)
        self.assertIn("once($('#dd-section-add')", script)
        # 실패한 토글은 체크 상태를 되돌린다 -- 화면이 서버와 갈리면 안 된다.
        self.assertIn('t.checked = !wanted', script)


if __name__ == '__main__':
    unittest.main()
