#!/usr/bin/env python3
"""CSRF 방어 계약 (`csrf.py` + `templates/_csrf.html`).

이 앱의 쓰기를 인증하는 경로는 셋이다: 세션쿠키(브라우저) · `Authorization:
Bearer`(네이티브 앱) · `/api/ext/` 의 `X-API-Key`(Mac 러너).  이 중 브라우저가
알아서 실어보내는 것은 쿠키 하나뿐이라, 토큰을 요구하는 대상도 쿠키 하나다.
나머지 둘에 토큰을 요구하면 앱과 러너만 죽고 막히는 구멍은 없다.

여기서 잠그는 것:
  ① 쿠키 쓰기 + 토큰 없음/틀림 → 403 + `X-CSRF-Fail: 1`
  ② 쿠키 쓰기 + 올바른 토큰 → 통과
  ③ Bearer · `/api/ext/` · GET → 검사 대상 아님
  ④ 비인증 쓰기는 401/302 그대로 — CSRF 실패로 오분류하지 않는다
  ⑤ 훅 등록 순서: `_bearer_auth` **뒤**, `_idem_replay` **앞**
  ⑥ 기본값은 켜짐(테스트 클라이언트 밖에서는 config 없이도 enforce)
  ⑦ 브라우저 배선: base/mobile 이 partial 을 include 하고, 래퍼가 표시된 403
     하나만 1회 재시도한다

실행: ~/.venvs/trmt-test/bin/python tests/test_csrf.py
"""
import os
import tempfile
import unittest

import app as appmod
import csrf

# 이 파일은 CSRF 그 자체를 본다 — 기본값(테스트에서 꺼짐)에 기대면 아무것도
# 검증하지 못하므로 명시적으로 켠다.
appmod.app.config['CSRF_PROTECT'] = True


class CsrfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'csrf.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['CSRF_PROTECT'] = True
        with appmod.app.app_context():
            appmod.init_db(False)
            self.uid = appmod.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active)"
                " VALUES(?,?,?,?,1)",
                ('csrf-test', appmod.generate_password_hash('pw-csrf-test'), 'CSRF', 'admin'))
            self.vessel = appmod.execute(
                "INSERT INTO vessels(name, vsl_cd, imo) VALUES(?,?,?)",
                ('CSRF TEST', 'C001', 'IMO-CSRF'))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=self.uid, username='csrf-test', display_name='CSRF',
                     role='admin', permanent=False)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        appmod.app.config['CSRF_PROTECT'] = True
        self.tmp.cleanup()

    # 실제 쓰기 엔드포인트를 쓴다. 테스트 전용 라우트를 달면 훅이 그 라우트에만
    # 걸려 있어도 통과해버려서, "앱 전체에 걸렸다" 는 주장을 증명하지 못한다.
    def _write(self, headers=None, client=None):
        return (client or self.client).post(
            '/api/dock-daily/projects',
            json={'vessel_id': self.vessel, 'title': 'CSRF probe'},
            headers=headers or {})

    def _token(self):
        res = self.client.get('/api/csrf-token')
        self.assertEqual(200, res.status_code)
        return res.get_json()['token']

    def test_cookie_write_without_token_is_refused(self):
        res = self._write()
        self.assertEqual(403, res.status_code)
        # 표식이 없으면 브라우저 래퍼가 권한부족 403 과 구분할 수 없어서, 고칠
        # 수 없는 실패를 영원히 재시도하거나 고칠 수 있는 실패를 포기한다.
        self.assertEqual('1', res.headers.get('X-CSRF-Fail'))
        self.assertEqual('csrf', res.get_json()['error'])

    def test_cookie_write_with_token_passes(self):
        res = self._write({'X-CSRF-Token': self._token()})
        self.assertEqual(201, res.status_code)

    def test_wrong_token_is_refused(self):
        res = self._write({'X-CSRF-Token': self._token()[:-1] + '@'})
        self.assertEqual(403, res.status_code)
        self.assertEqual('1', res.headers.get('X-CSRF-Fail'))

    def test_token_is_stable_within_a_session(self):
        # 요청마다 새로 발급하면 다른 탭이 들고 있던 토큰이 즉시 죽는다.
        self.assertEqual(self._token(), self._token())

    def test_get_is_never_blocked(self):
        res = self.client.get('/api/dock-daily/projects')
        self.assertEqual(200, res.status_code)
        self.assertNotIn('X-CSRF-Fail', res.headers)

    def test_bearer_write_is_exempt(self):
        # 새 클라이언트 — 쿠키 세션이 있으면 _bearer_auth 가 Bearer 를 보지도 않는다.
        client = appmod.app.test_client()
        login = client.post('/api/auth/token',
                            json={'username': 'csrf-test', 'password': 'pw-csrf-test'})
        self.assertEqual(200, login.status_code)
        token = login.get_json()['token']
        res = self._write({'Authorization': 'Bearer %s' % token}, client=client)
        self.assertEqual(201, res.status_code)
        self.assertNotIn('X-CSRF-Fail', res.headers)

    def test_api_ext_with_a_key_header_is_exempt(self):
        # 키가 틀려서 거절되긴 하는데, 거절하는 주체가 api_key_required 여야 한다.
        # 여기서 CSRF 가 먼저 끊으면 Mac 러너 전체가 죽는다.
        res = self.client.post('/api/ext/dock-daily/merge', json={},
                               headers={'X-API-Key': 'whatever'})
        self.assertNotIn('X-CSRF-Fail', res.headers)

    def test_cookie_authenticated_ext_routes_are_still_checked(self):
        """`/api/ext/` 를 경로만 보고 통째로 빼면 안 되는 이유.

        `/api/ext/key/regenerate` 는 api_key_required 가 아니라 admin_required
        다 — 즉 쿠키로 인증되고, 살아있는 자동화 키를 즉시 무효화한다. 경로
        접두사로 면제하면 이게 위조 가능해진다. 면제는 실제로 제시된 자격증명
        (X-API-Key 헤더)에만 걸려야 한다.
        """
        res = self.client.post('/api/ext/key/regenerate')
        self.assertEqual(403, res.status_code)
        self.assertEqual('1', res.headers.get('X-CSRF-Fail'))
        # 토큰을 붙이면 통과해야 한다 — 막는 게 목적이 아니라 출처를 확인하는 것.
        ok = self.client.post('/api/ext/key/regenerate',
                              headers={'X-CSRF-Token': self._token()})
        self.assertEqual(200, ok.status_code)

    def test_ext_route_decoration_is_what_the_exemption_assumes(self):
        """면제의 근거를 코드에서 다시 읽는다.

        `X-API-Key` 헤더 면제가 안전한 것은 그 헤더를 쓰는 라우트가 실제로
        api_key_required 로 막혀 있기 때문이다. 새 `/api/ext/` 라우트가
        쿠키 인증으로 추가되면 여기서 드러난다.
        """
        import ast
        import glob
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cookie_authed = []
        for path in sorted(glob.glob(os.path.join(root, '*.py'))):
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                decorators = [ast.unparse(d) for d in node.decorator_list]
                if not any("route('/api/ext/" in d for d in decorators):
                    continue
                if not any('api_key_required' in d for d in decorators):
                    cookie_authed.append(node.name)
        # 현재 알려진 둘. 늘어나면 면제 규칙을 다시 보라는 신호다.
        self.assertEqual(['api_ext_key_get', 'api_ext_key_regen'],
                         sorted(cookie_authed))

    def test_unauthenticated_write_is_not_mislabeled(self):
        res = self._write(client=appmod.app.test_client())
        self.assertNotIn('X-CSRF-Fail', res.headers)
        self.assertIn(res.status_code, (302, 401))

    def test_hook_is_registered_after_bearer_auth_and_before_idem_replay(self):
        """등록 순서가 계약이다.

        `_bearer_auth` 위에 두면 g._token_auth 가 아직 없어서 네이티브 앱의 모든
        쓰기가 403 이 된다. `_idem_replay` 아래에 두면 위조 요청이 거절되기 전에
        멱등 key 를 선점해서, 나중에 오는 정상 재전송이 그 자리를 못 쓴다.
        """
        names = [f.__name__ for f in appmod.app.before_request_funcs[None]]
        self.assertIn('enforce', names)
        self.assertLess(names.index('_bearer_auth'), names.index('enforce'))
        self.assertLess(names.index('enforce'), names.index('_idem_replay'))

    def test_default_is_on_when_not_testing(self):
        """config 키를 지워도 켜져 있어야 한다.

        기본값은 `not app.testing` 이다. 배포된 앱은 TESTING 을 세우지 않으므로
        아무 설정 없이 켜지고, 끄려면 명시적인 config 변경이 필요하다.
        """
        self.assertFalse(appmod.app.testing)
        removed = appmod.app.config.pop('CSRF_PROTECT')
        try:
            self.assertEqual(403, self._write().status_code)
        finally:
            appmod.app.config['CSRF_PROTECT'] = removed

    def test_off_switch_needs_an_explicit_config_change(self):
        appmod.app.config['CSRF_PROTECT'] = False
        self.assertEqual(201, self._write().status_code)

    def test_pages_carry_the_token_and_the_fetch_wrapper(self):
        html = self.client.get('/dock-daily').get_data(as_text=True)
        self.assertIn('<meta name="csrf-token"', html)
        self.assertIn("headers.set('X-CSRF-Token', token)", html)
        # 래퍼가 다른 스크립트보다 먼저 깔려야 한다. 늦게 깔리면 먼저 실행된
        # 코드가 잡아둔 fetch 참조는 래퍼를 타지 않고 403 을 맞는다.
        self.assertLess(html.index('csrf-token'), html.index('js/dock_daily.js'))

    def test_partial_retries_only_the_marked_403_and_only_once(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'templates', '_csrf.html'),
                  encoding='utf-8') as f:
            src = f.read()
        self.assertIn("res.headers.get('X-CSRF-Fail') !== '1'", src)
        self.assertIn('/api/csrf-token', src)
        # 재시도는 native fetch 로 한 번만 — 래퍼를 다시 타면 무한 재시도가 된다.
        self.assertNotIn('window.fetch(input', src)
        # 우리 토큰을 외부 출처로 실어보내면 그게 곧 토큰 유출이다.
        self.assertIn('sameOrigin(url)', src)

    def test_every_html_root_template_includes_the_partial(self):
        """<head> 를 직접 가진 템플릿은 partial 을 include 해야 한다.

        예외는 msg_preview.html 하나 — 읽기 전용 페이지라 쓰기가 0건이고, 토큰을
        발급받을 이유가 없다. 여기에 쓰기가 생기면 이 테스트가 잡는다.
        """
        root = os.path.join(os.path.dirname(__file__), '..', 'templates')
        readonly = {'msg_preview.html'}
        for name in sorted(os.listdir(root)):
            if not name.endswith('.html') or name.startswith('_'):
                continue        # partial 자신은 대상이 아니다
            with open(os.path.join(root, name), encoding='utf-8') as f:
                src = f.read()
            if '<head>' not in src:
                continue
            writes = any(m in src for m in ("method:'POST'", "method: 'POST'",
                                            "method:'PUT'", "method:'PATCH'",
                                            "method:'DELETE'"))
            if name in readonly:
                self.assertFalse(writes, '%s 에 쓰기가 생겼다 — partial 을 넣어야 한다' % name)
                continue
            self.assertIn("include '_csrf.html'", src, name)

    def test_login_form_carries_the_hidden_field(self):
        # 로그인된 클라이언트로 /login 을 열면 대시보드로 리다이렉트된다.
        html = appmod.app.test_client().get('/login').get_data(as_text=True)
        self.assertIn('name="_csrf"', html)

    def _login_page_token(self, client):
        html = client.get('/login').get_data(as_text=True)
        marker = 'name="_csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start:html.index('"', start)]

    def test_login_post_without_a_token_does_not_create_a_session(self):
        """강제 로그인(login CSRF) 차단.

        검사하지 않으면 외부 페이지가 형을 공격자 계정으로 로그인시킬 수 있고,
        그 뒤 형이 입력하는 것이 전부 그 계정에 쌓인다. 비인증 요청 일반은
        면제지만 로그인만은 예외다 — 로그인 페이지 렌더가 토큰을 발급하므로
        정상 제출자는 항상 토큰을 갖고 있다.
        """
        client = appmod.app.test_client()
        client.get('/login')                     # 토큰은 발급받되 제출하지 않는다
        res = client.post('/login', data={'username': 'csrf-test',
                                          'password': 'pw-csrf-test'})
        self.assertEqual('1', res.headers.get('X-CSRF-Fail'))
        # form 제출은 navigation 이므로 JSON 이 아니라 로그인 페이지로 되돌린다.
        self.assertEqual(302, res.status_code)
        self.assertIn('/login', res.headers['Location'])
        with client.session_transaction() as s:
            self.assertNotIn('user_id', s)

    def test_login_post_with_the_token_succeeds(self):
        client = appmod.app.test_client()
        token = self._login_page_token(client)
        res = client.post('/login', data={'username': 'csrf-test',
                                          'password': 'pw-csrf-test',
                                          '_csrf': token})
        self.assertEqual(302, res.status_code)
        self.assertNotIn('X-CSRF-Fail', res.headers)
        with client.session_transaction() as s:
            self.assertEqual(self.uid, s['user_id'])

    def test_forged_write_does_not_claim_the_idempotency_key(self):
        """거절이 `_idem_replay` 앞에서 나야 하는 이유.

        위조 요청이 먼저 멱등 key 를 선점하면, 형이 같은 key 로 보내는 정상
        요청이 "이미 처리됨" 으로 응답받고 실제로는 아무것도 안 된다.
        """
        key = 'csrf-idem-probe-1'
        forged = self._write({'Idempotency-Key': key})
        self.assertEqual(403, forged.status_code)
        ok = self._write({'Idempotency-Key': key, 'X-CSRF-Token': self._token()})
        self.assertEqual(201, ok.status_code, '정상 재전송이 선점된 key 에 막혔다')

    def test_wrapper_does_not_leak_the_token_off_origin(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'templates', '_csrf.html'),
                  encoding='utf-8') as f:
            src = f.read()
        # fetch 와 form 둘 다 같은 출처 검사를 통과해야 헤더/필드를 붙인다.
        self.assertIn('if (!UNSAFE[method] || !sameOrigin(url)) return null;', src)
        self.assertIn("sameOrigin(form.getAttribute('action')", src)
        # Request 객체는 body 가 이미 소비돼 재전송이 불가능하므로 재시도하지 않는다.
        self.assertIn('input instanceof URL', src)

    def test_hooks_running_before_enforce_have_no_persistent_side_effects(self):
        """`enforce` 앞의 훅들은 위조 요청에도 돈다 — 그래서 무해해야 한다.

        측정 결과: `_require_runtime_initialization` 은 raise 만,
        `_limit_non_stt_upload` 은 abort(413) 과 요청 단위 상한 설정만 한다.
        `_bearer_auth` 는 세션을 건드리지만 Bearer 요청에서만이고, 그 요청은
        애초에 면제 대상이다.  `_guard_dock_daily_blobs` 는 GET 경로 하나를 401 로
        끊기만 하고 아무것도 쓰지 않는다 -- CSRF 검사 위에 있어도 무해하고, 아래로
        내리면 `g._token_auth` 를 못 읽는다.
        """
        names = [f.__name__ for f in appmod.app.before_request_funcs[None]]
        before = names[:names.index('enforce')]
        self.assertEqual(['_require_runtime_initialization', '_limit_non_stt_upload',
                          '_bearer_auth', '_guard_dock_daily_blobs'], before)
        self.assertIn('_require_runtime_initialization', csrf.__doc__)

    def test_wsgi_entry_point_pins_protection_on(self):
        """기본값이 나중에 바뀌어도 실서비스가 조용히 열리지 않게."""
        with open(os.path.join(os.path.dirname(__file__), '..', 'wsgi.py'),
                  encoding='utf-8') as f:
            self.assertIn('config["CSRF_PROTECT"] = True', f.read())

    def test_form_posts_are_read_from_the_body(self):
        """헤더를 못 붙이는 클래식 form 은 hidden 필드로 통과해야 한다."""
        with appmod.app.test_request_context(
                '/x', method='POST', data={csrf.FIELD: 'abc'}):
            self.assertEqual('abc', csrf._submitted())
        # JSON 본문에서는 form 을 건드리지 않는다(파일 업로드에서 조기 파싱 유발).
        with appmod.app.test_request_context('/x', method='POST', json={csrf.FIELD: 'abc'}):
            self.assertIsNone(csrf._submitted())

    def test_dock_manager_app_is_not_covered_and_says_so(self):
        """별도 마운트된 Dock Manager 는 다른 Flask 인스턴스다.

        이 훅은 그 요청을 보지 못한다. "앱 전체 보호" 라고 부르면 애플리케이션
        하나만큼 틀린 말이 되므로, 모듈이 그 경계를 문서에 적어두게 못박는다.
        """
        self.assertIn('DispatcherMiddleware', csrf.__doc__)


if __name__ == '__main__':
    unittest.main(verbosity=2)
