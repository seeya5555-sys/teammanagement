"""nav 하위그룹 active 유일성 계약.

base.html 의 nlink 매크로는 링크별 alias 목록(eps)을 받아 active 를 칠한다.
공용 리스트를 그룹 내 여러 링크에 넘기면 한 페이지에서 형제 링크가 동시에
active 로 칠해진다(2026-08-18 Report 그룹 실사고: Dry Dock / Boarding 둘 다 검정).
그래서 특정 탭을 하드코딩하지 않고 "한 페이지 렌더에서 각 하위그룹의 active
링크는 최대 1개" 라는 구조 계약으로 잠근다 — 다른 그룹의 동일 실수까지 잡힌다.

🔴 반대방향 결함(active 가 0개)도 같이 잠근다. "<=1" 만 보면 데스크탑/모바일 중
한쪽에서 alias 를 빼먹어 하이라이트가 아예 사라져도 통과하기 때문(올마이트 지적).
그래서 Report 그룹은 data-nav-group hook 으로 데스크탑·모바일 2개를 구조로 집어
각각 정확히 1개, 그리고 그게 올바른 라벨인지 본다.
"""

import os
import re
import tempfile
import unittest
from html.parser import HTMLParser

import app as appmod

# 하위그룹 컨테이너 — 데스크탑 드롭다운 / 모바일 드로어
GROUP_CLASSES = ('nav-submenu', 'mm-sub-group')
# Report 그룹은 데스크탑 + 모바일 = 정확히 2벌이어야 한다
REPORT_GROUP = 'report'
REPORT_GROUP_COUNT = 2


class _NavGroups(HTMLParser):
    """하위그룹 컨테이너별로 그 안의 nav-link(전체/active)를 수집."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.groups = []          # [{'cls','hook','all','active'}]
        self.all_links = []       # (href, is_active) — 그룹 밖 링크까지 전부
        self._depth = 0           # div 중첩 깊이
        self._open = None         # 현재 그룹 인덱스
        self._open_depth = None
        self._active = False      # 지금 여는 a 가 active 인지
        self._in_link = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get('class') or '').split()
        if tag == 'div':
            self._depth += 1
            if any(c in GROUP_CLASSES for c in classes):
                # 그룹 중첩은 현재 구조에 없다. 생기면 조용히 삼키지 말고 깨뜨린다.
                assert self._open is None, 'nav 하위그룹이 중첩됨 — 파서 가정 붕괴'
                self.groups.append({
                    'cls': next(c for c in classes if c in GROUP_CLASSES),
                    'hook': attrs.get('data-nav-group'),
                    'all': [], 'active': [],
                })
                self._open = len(self.groups) - 1
                self._open_depth = self._depth
        elif tag == 'a' and 'nav-link' in classes:
            self._in_link = True
            self._active = 'active' in classes
            self._buf = []
            self.all_links.append((attrs.get('href'), self._active))

    def handle_endtag(self, tag):
        if tag == 'a' and self._in_link:
            label = re.sub(r'\s+', ' ', ''.join(self._buf)).strip() or '(no-label)'
            if self._open is not None:
                self.groups[self._open]['all'].append(label)
                if self._active:
                    self.groups[self._open]['active'].append(label)
            self._in_link = False
        elif tag == 'div':
            if self._open is not None and self._depth == self._open_depth:
                self._open = None
                self._open_depth = None
            self._depth -= 1

    def handle_data(self, data):
        if self._in_link:
            self._buf.append(data)

    def by_hook(self, hook):
        return [g for g in self.groups if g['hook'] == hook]


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_HTML = os.path.join(REPO, 'templates', 'base.html')


def base_src():
    with open(BASE_HTML, encoding='utf-8') as fh:
        return fh.read()


def nav_endpoints():
    """base.html 에서 nav 판정에 쓰이는 endpoint 를 전부 뽑는다.

    경로를 테스트에 하드코딩하면 라우트가 바뀔 때 조용히 커버리지가 빠진다
    (실제로 첫 시도에서 '/repair-request' 오타로 걸렸다). nlink 호출과 *_eps
    리스트를 소스에서 파싱해 커버 범위를 템플릿에 종속시킨다.
    이 정규식이 뭘 놓치는지는 개수 하한이 아니라 렌더 결과와의 대조로 검증한다
    (test_regex_covers_every_rendered_nav_link).
    """
    src = base_src()
    eps = set(re.findall(r"""nlink\(\s*['"]([a-z_]+\.[a-z_0-9]+)['"]""", src))
    for body in re.findall(r"\{%\s*set\s+\w*_eps\s*=\s*\[(.*?)\]\s*%\}", src, re.S):
        eps.update(re.findall(r"""['"]([a-z_]+\.[a-z_0-9]+)['"]""", body))
    return sorted(eps)


def paths_for(endpoint):
    """endpoint → 라우트 매칭되는 경로 전부. <int:rid> 같은 인자는 1 로 채운다.

    한 endpoint 에 rule 이 여러 개일 수 있어 첫 rule 만 보지 않는다.
    판정은 request.endpoint 만 보므로 DB 행이 필요 없다(edit 경로는 실제 GET 시
    404 지만 test_request_context 에서 endpoint 는 정상 해석됨).
    """
    return [re.sub(r'<[^>]+>', '1', r.rule)
            for r in appmod.app.url_map.iter_rules() if r.endpoint == endpoint]


class NavActiveUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def _render(self, path):
        """admin 세션으로 base.html 을 렌더 — 조건부 nav 까지 전부 나오게."""
        with appmod.app.test_request_context(path):
            from flask import render_template, session
            session['user_id'] = 1
            session['username'] = 'admin'
            session['role'] = 'admin'
            endpoint = appmod.request.endpoint
            self.assertIsNotNone(endpoint, f'{path}: 라우트 매칭 실패')
            parser = _NavGroups()
            parser.feed(render_template('base.html'))
            return endpoint, parser

    def _all_nav_paths(self):
        pairs = []
        for ep in nav_endpoints():
            paths = paths_for(ep)
            self.assertTrue(paths, f'{ep}: url_map 에 rule 없음 — 템플릿이 죽은 endpoint 참조')
            pairs += [(ep, p) for p in paths]
        return pairs

    def test_every_nav_path_has_at_most_one_active_per_group(self):
        for ep, path in self._all_nav_paths():
            with self.subTest(endpoint=ep, path=path):
                _, parser = self._render(path)
                self.assertTrue(parser.groups, 'nav 하위그룹 0개 — 파서/템플릿 drift')
                for g in parser.groups:
                    self.assertLessEqual(
                        len(g['active']), 1,
                        f"{path} ({ep}) 의 .{g['cls']}"
                        f"[{g['hook']}] active {len(g['active'])}개: {g['active']}",
                    )

    def test_report_subtabs_highlight_exactly_their_own_page_desktop_and_mobile(self):
        """실사고 지점 고정 — 데스크탑·모바일 2벌 각각 정확히 1개, 라벨까지 일치."""
        expected = {
            'routes_core.dry_dock_page': 'Dry Dock Report',
            'routes_core.dry_dock_edit_page': 'Dry Dock Report',
            'routes_core.boarding_page': 'Boarding Report',
            'routes_core.boarding_edit_page': 'Boarding Report',
        }
        for ep, label in expected.items():
            for path in paths_for(ep):
                with self.subTest(endpoint=ep, path=path):
                    _, parser = self._render(path)
                    groups = parser.by_hook(REPORT_GROUP)
                    self.assertEqual(
                        REPORT_GROUP_COUNT, len(groups),
                        f'{path}: data-nav-group="{REPORT_GROUP}" 그룹 {len(groups)}개 '
                        f'(데스크탑+모바일 {REPORT_GROUP_COUNT}개여야 함)')
                    for g in groups:
                        self.assertEqual(
                            [label], g['active'],
                            f"{path}: .{g['cls']}[{REPORT_GROUP}] active = {g['active']}")

    def test_report_group_header_active_on_every_report_page(self):
        """그룹 헤더(합집합 report_eps)는 하위 어느 페이지에서도 켜져야 한다."""
        report_eps = [ep for ep in nav_endpoints()
                      if ep.split('.')[-1].startswith(('dry_dock', 'boarding'))]
        self.assertEqual(4, len(report_eps), f'report endpoint 파싱 이상: {report_eps}')
        for ep in report_eps:
            for path in paths_for(ep):
                with self.subTest(endpoint=ep, path=path):
                    with appmod.app.test_request_context(path):
                        from flask import render_template, session
                        session['user_id'] = 1
                        session['role'] = 'admin'
                        html = render_template('base.html')
                    self.assertIn('nav-group active', html,
                                  f'{path}: Report 그룹 헤더 active 아님')

    def test_regex_covers_every_rendered_nav_link(self):
        """nav_endpoints() 정규식 drift 탐지 — 렌더된 nav-link 를 독립 소스로 대조.

        개수 하한(>=15)은 부분 누락을 못 잡는다. 실제로 템플릿이 만들어낸 링크의
        href 를 url_map 으로 역매핑해, 파싱 집합이 그걸 전부 덮는지 본다.
        """
        parsed = set(nav_endpoints())
        _, parser = self._render('/dashboard')
        hrefs = {h for h, _ in parser.all_links if h}
        self.assertGreaterEqual(len(hrefs), 15, f'렌더된 nav-link 가 너무 적음: {len(hrefs)}')
        adapter = appmod.app.url_map.bind('localhost')
        missing = []
        for href in sorted(hrefs):
            try:
                endpoint, _ = adapter.match(href.split('?')[0])
            except Exception:
                continue  # 정적/외부 링크는 대상 아님
            if endpoint not in parsed:
                missing.append((href, endpoint))
        self.assertEqual([], missing, f'정규식이 놓친 nav endpoint: {missing}')

    def test_parser_sees_every_group_container_in_template(self):
        """파서 커버리지 게이트 — 템플릿의 그룹 컨테이너 수와 파싱된 수 일치."""
        src = base_src()
        declared = sum(src.count(f'"{c}"') for c in GROUP_CLASSES)
        self.assertGreater(declared, 0, '템플릿에서 그룹 컨테이너를 못 찾음')
        _, parser = self._render('/dashboard')
        self.assertEqual(declared, len(parser.groups),
                         f'선언 {declared}개 vs 파싱 {len(parser.groups)}개 — 파서가 일부를 놓침')

    def test_parser_catches_sibling_double_active_and_zero_active(self):
        """negative control — 파서가 양방향 결함을 실제로 구분하는지."""
        double = ('<div class="nav-submenu" data-nav-group="report">'
                  '<a class="nav-link active">A</a><a class="nav-link active">B</a></div>')
        p = _NavGroups()
        p.feed(double)
        self.assertEqual(['A', 'B'], p.by_hook('report')[0]['active'])

        zero = ('<div class="mm-sub-group" data-nav-group="report">'
                '<a class="nav-link">A</a><a class="nav-link">B</a></div>')
        p = _NavGroups()
        p.feed(zero)
        self.assertEqual([], p.by_hook('report')[0]['active'])
        self.assertEqual(['A', 'B'], p.by_hook('report')[0]['all'])


if __name__ == '__main__':
    unittest.main()
