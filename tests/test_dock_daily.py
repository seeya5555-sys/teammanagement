"""Dock Daily Report contracts (web MVP).

These tests use the same temporary-DB pattern as the existing Flask tests and
avoid any external Dock Manager/SVMS service.
"""
import inspect
import json
import io
import os
import re
import tempfile
import unittest
import zipfile

from html.parser import HTMLParser

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

    def _png(self):
        """Smallest byte string that passes the PNG magic check in _file_mime."""
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
        # The html branch has no route behind it yet, so it is exercised
        # directly -- that is where a dangling id becomes a 404 `<img>`.
        with appmod.app.app_context():
            before = routes_dock_daily._render_section(rid, section, 'Shipyard', as_html=True)
        self.assertIn(f'src="/api/dock-daily/attachments/{aid}"', before)

        gone = self.client.delete(f'/api/dock-daily/attachments/{aid}')
        self.assertEqual(200, gone.status_code, gone.get_data(as_text=True))
        self.assertFalse(os.path.exists(stored))
        after = self.client.get(f'/api/dock-daily/reports/{rid}').get_json()
        images = [b for b in after['blocks'] if b['block_type'] == 'image']
        self.assertEqual(1, len(images), 'the block itself must survive with its caption')
        self.assertIsNone(images[0]['content']['attachment_id'])
        self.assertEqual('Hull shot', images[0]['content']['caption'])
        with appmod.app.app_context():
            after_html = routes_dock_daily._render_section(rid, section, 'Shipyard', as_html=True)
        self.assertNotIn('<img', after_html)
        self.assertIn('Hull shot', after_html, 'caption 은 남아야 한다')
        # 메일 본문(현행 live 경로)도 그대로 나가야 한다.
        mail = self.client.get(f'/api/dock-daily/reports/{rid}/email-preview').get_json()['html']
        self.assertNotIn(f'/api/dock-daily/attachments/{aid}', mail)

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
        self.assertIn("data-del-report", script)
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
