"""돈경로 가드 계약 — 90일 Blueprint 전환의 전제조건.

왜 이 파일이 필요한가
--------------------
돈경로(fundreq·invoice·jeonja)는 Blueprint 전환에서 **최후**에 옮기기로 되어 있다.
옮길 때 실제로 무너지는 것은 URL 이 아니라 **가드**다. `test_productization_gates.py`
의 url_map 스냅샷은 rule·method·endpoint·strict_slashes·defaults 만 비교하므로
`@admin_required` 가 사라져도 통과한다. 즉 지금 상태에서 돈경로를 옮기면
"URL 계약은 동일한데 권한만 조용히 열린" 배포가 게이트를 그대로 통과한다.

두 층으로 본다 (하나만으로는 부족하다)
  ① 정적(AST): 모든 돈경로 라우트가 기대 가드 데코레이터를 달고 있는지.
     런타임만 보면 전역 훅이 우연히 막아준 것을 "가드 보존"으로 착각한다.
  ② 런타임: 비인증·비관리자 요청이 실제로 거부되는지.
     정적만 보면 데코레이터가 붙어 있으나 무력화된 경우를 못 잡는다.

추가로 ①에서 **데코레이터 순서**를 검사한다. `@app.route` 가 가드보다 아래에 있으면
Flask 는 가드가 안 씌워진 원본 함수를 등록하고 가드는 버려진다 — 데코레이터가
"붙어 있는데도" 무방비가 되는 조용한 실패 모드다.

범위에 대한 정직한 한계
  이 파일은 감사 항목 ⑤(fundreq·invoice·jeonja 계약 테스트 보강) 중 **가드 층만**
  덮는다. 요청/응답 schema·DB side effect·CSRF·멱등 재생 동작의 계약은 여전히
  미보강이며, 그건 각 도메인 이동 직전에 도메인별로 채워야 한다.
"""
import ast
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import app as appmod


ROOT = Path(__file__).resolve().parents[1]
POLICY_FIXTURE = ROOT / "tests" / "fixtures" / "money_route_policy.json"

# 돈경로 식별 토큰 — url_map 과 AST 양쪽에서 같은 기준을 쓴다.
# `liscr` = 기국 인보이스 신규등록(Case 2). URL 에 "invoice" 문자열이 없지만 승인 한 번이
# SVMS 에 인보이스를 만들어내므로 돈경로다 — 토큰이 URL 문자열 기반이라 이름만 보고
# 빠뜨리기 쉬운 자리다.
MONEY_TOKENS = ("fundreq", "invoice", "jeonja", "liscr")

# 가드는 접두어로 갈린다:
#   /api/ext/*  = 맥 러너(워커)용 X-API-Key 경로  → api_key_required (401)
#   그 외        = 사람이 브라우저로 쓰는 관리자 경로 → admin_required (401/403)
EXT_PREFIX = "/api/ext/"
GUARD_FOR_EXT = "api_key_required"
GUARD_FOR_ADMIN = "admin_required"


def is_money(rule: str) -> bool:
    return any(token in rule for token in MONEY_TOKENS)


def expected_guard(rule: str) -> str:
    return GUARD_FOR_EXT if rule.startswith(EXT_PREFIX) else GUARD_FOR_ADMIN


def concrete_path(rule: str) -> str:
    """URL 규칙을 실제로 때릴 수 있는 경로로 바꾼다. 가드가 살아 있으면 뷰는 실행되지 않는다."""
    return re.sub(r"<[^>]+>", "x", re.sub(r"<int:[^>]+>", "1", rule))


def scanned_sources() -> list:
    """app.py + app.py 가 exec 로 로드하는 추출 모듈 전부.

    로드 목록을 app.py 소스에서 뽑기 때문에, 새 추출 모듈이 생기면 스캔 범위가
    자동으로 따라온다. 목록을 이 파일에 손으로 박으면 새 모듈에 추가된 돈경로가
    조용히 미검사로 남는다.
    """
    # source_bundle 이 exec 경계와 Blueprint 전환 모듈을 모두 AST 로 도출한다.
    # 여기서 regex 를 따로 두면 두 목록이 어긋나는 순간 이 파일만 조용히
    # 좁은 범위를 스캔하게 된다 — 정본 하나를 공유한다.
    from source_bundle import APP_SOURCE_PATHS

    return list(APP_SOURCE_PATHS)


def money_pairs() -> list:
    """[(rule, method)] — 등록된 돈경로 전건. HEAD/OPTIONS 는 Flask 자동생성이라 제외."""
    pairs = []
    for rule in sorted(appmod.app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
        if not is_money(rule.rule):
            continue
        for method in sorted(set(rule.methods) - {"HEAD", "OPTIONS"}):
            pairs.append((rule.rule, method))
    return pairs


def current_policy_matrix() -> dict:
    """{"METHOD rule": {"guard":…, "anonymous":…, "non_admin":…}} — 기대 정책 행렬."""
    matrix = {}
    for rule, method in money_pairs():
        matrix[f"{method} {rule}"] = {
            "guard": expected_guard(rule),
            "anonymous": 401,
            "non_admin": 401 if rule.startswith(EXT_PREFIX) else 403,
        }
    return matrix


def route_decorators(node: ast.FunctionDef) -> list:
    """(index, rule) 목록 — @app.route / @bp.route('<rule>', ...) 데코레이터.

    Blueprint 전환 모듈은 @bp.route 를 쓴다. 여기서 app 만 인정하면 전환된
    돈경로가 전부 미검출이 되는데, 그때는 test_source_and_url_map_agree 가
    크게 터지도록 설계돼 있다(조용한 미검사 방지). 둘 다 인정한다.
    """
    found = []
    for idx, dec in enumerate(node.decorator_list):
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        if not (isinstance(func, ast.Attribute) and func.attr == "route"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id in ("app", "bp")):
            continue
        if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
            found.append((idx, dec.args[0].value))
    return found


def decorator_names(node: ast.FunctionDef) -> list:
    """(index, name) — 가드 후보 데코레이터 이름."""
    names = []
    for idx, dec in enumerate(node.decorator_list):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.append((idx, target.id))
        elif isinstance(target, ast.Attribute):
            names.append((idx, target.attr))
    return names


def collect_money_routes_from_source() -> list:
    """[(rule, endpoint_func_name, guards, route_idx, source_file)] — 돈경로만."""
    collected = []
    for path in scanned_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            routes = route_decorators(node)
            if not routes:
                continue
            names = decorator_names(node)
            for route_idx, rule in routes:
                if not is_money(rule):
                    continue
                guards = [n for i, n in names if i != route_idx and n != "route"]
                collected.append((rule, node.name, guards, route_idx, path.name))
    return collected


class MoneyPathGuardStaticTests(unittest.TestCase):
    """소스에 가드가 실제로 박혀 있는지 — 데코레이터 존재 + 순서."""

    @classmethod
    def setUpClass(cls):
        cls.routes = collect_money_routes_from_source()

    def test_money_routes_found_in_source(self):
        self.assertTrue(self.routes, "소스에서 돈경로 라우트를 하나도 못 찾음 — 스캔 범위가 깨졌음")

    def test_every_money_route_has_expected_guard(self):
        missing = [
            f"{rule} ({func} in {src}): guards={guards or '없음'}, 기대={expected_guard(rule)}"
            for rule, func, guards, _idx, src in self.routes
            if expected_guard(rule) not in guards
        ]
        self.assertEqual([], missing, "가드가 없거나 기대와 다른 돈경로:\n  " + "\n  ".join(missing))

    def test_route_decorator_is_outermost(self):
        """@app.route 가 가드보다 아래면 Flask 는 무방비 원본 함수를 등록한다."""
        inverted = []
        for rule, func, guards, route_idx, src in self.routes:
            guard_indexes = [
                i for i, n in decorator_names_by_rule(src, func) if n in (GUARD_FOR_ADMIN, GUARD_FOR_EXT)
            ]
            if any(gi < route_idx for gi in guard_indexes):
                inverted.append(f"{rule} ({func} in {src}) — 가드가 @app.route 위에 있어 등록 대상이 무방비")
        self.assertEqual([], inverted, "\n  ".join(inverted))

    def test_source_and_url_map_agree(self):
        """AST 로 본 돈경로 집합 == 실제 등록된 돈경로 집합.

        어긋나면 스캔되지 않은 파일에 돈경로가 있다는 뜻이고, 그 라우트는 위의
        가드 검사를 통째로 빠져나간다.
        """
        from_source = {rule for rule, *_ in self.routes}
        from_map = {r.rule for r in appmod.app.url_map.iter_rules() if is_money(r.rule)}
        self.assertEqual(
            from_map,
            from_source,
            "AST 미검출(=미검사) 돈경로: {} / 소스에만 있는 것: {}".format(
                sorted(from_map - from_source), sorted(from_source - from_map)
            ),
        )


def decorator_names_by_rule(src_name: str, func_name: str) -> list:
    """순서 검사용 — 해당 파일의 해당 함수 데코레이터 (index, name) 목록."""
    path = ROOT / src_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return decorator_names(node)
    return []


class MoneyPathGuardRuntimeTests(unittest.TestCase):
    """가드가 실제로 거부하는지 — 43개 rule×method 전건.

    기대값은 추측이 아니라 실측(2026-08-11)이다: 비인증 전건 401,
    비관리자 세션은 admin 경로 403 / ext 경로 401.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        self.old_testing = appmod.app.config.get("TESTING")
        db = os.path.join(self.tmp.name, "money_guard.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            # 세션이 가리키는 계정은 DB 에 실제로 있어야 한다. `login_required` 가 요청마다
            # users 를 재확인(비활성 계정 차단)하므로, 없는 uid 로 세션을 위조하면 admin 판정
            # 전에 401 로 끊겨 **admin 가드가 살아 있는지를 못 재게 된다**(계약이 헐거워짐).
            appmod.execute(
                "INSERT OR IGNORE INTO users (id,username,password_hash,display_name,role,active) "
                "VALUES (2,'money-user','x','Money User','member',1)")
        self.anon = appmod.app.test_client()
        self.user = appmod.app.test_client()
        with self.user.session_transaction() as session:
            session.update(
                user_id=2, username="money-user", display_name="Money User",
                role="user", supervisor_id=None,
            )

    def tearDown(self):
        # TESTING 도 원복한다. 이 앱은 프로세스 전역 단일 인스턴스라, 켠 채로 두면
        # 뒤에 도는 테스트가 다른 오류처리 경로를 타게 되어 결과가 실행순서에 의존한다.
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        appmod.app.config["TESTING"] = self.old_testing
        self.tmp.cleanup()

    def _row_counts(self):
        """모든 테이블 행수 — 거부된 요청이 DB 를 건드리지 않았는지 확인용."""
        with appmod.app.app_context():
            tables = [r["name"] for r in appmod.query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            return {t: appmod.query(f"SELECT COUNT(*) AS n FROM \"{t}\"", one=True)["n"]
                    for t in tables}

    def test_anonymous_is_rejected_everywhere(self):
        policy = current_policy_matrix()
        leaks = []
        for rule, method in money_pairs():
            code = self.anon.open(concrete_path(rule), method=method).status_code
            expected = policy[f"{method} {rule}"]["anonymous"]
            if code != expected:
                leaks.append(f"{method} {rule} → {code}, 기대 {expected}")
        self.assertEqual([], leaks, "비인증 요청이 막히지 않음:\n  " + "\n  ".join(leaks))

    def test_non_admin_session_is_rejected_everywhere(self):
        policy = current_policy_matrix()
        leaks = []
        for rule, method in money_pairs():
            code = self.user.open(concrete_path(rule), method=method).status_code
            expected = policy[f"{method} {rule}"]["non_admin"]
            if code != expected:
                leaks.append(f"{method} {rule} → {code}, 기대 {expected}")
        self.assertEqual([], leaks, "비관리자 세션이 막히지 않음:\n  " + "\n  ".join(leaks))

    def test_rejected_requests_have_no_database_side_effect(self):
        """거부는 상태코드만의 문제가 아니다 — 막혔다면 쓰기도 없어야 한다.

        가드가 뷰 실행 **뒤에** 판정하도록 잘못 배치되면 응답은 401/403 인데
        레코드는 생기는 조용한 실패가 가능하다. 전 테이블 행수로 확인한다.
        """
        before = self._row_counts()
        for rule, method in money_pairs():
            self.anon.open(concrete_path(rule), method=method)
            self.user.open(concrete_path(rule), method=method)
        after = self._row_counts()
        changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
        self.assertEqual({}, changed,
                         f"거부된 요청이 DB 를 변경함(테이블: 이전→이후) {changed}")

    def test_money_policy_matrix_is_frozen(self):
        """rule×method 별 기대 정책을 fixture 로 고정한다.

        개수만 세면 라우트가 **교체**될 때(하나 빠지고 하나 들어옴) 합이 같아서
        조용히 통과한다. 특히 admin 경로가 빠지고 ext 경로가 들어오면 요구 가드가
        바뀌는데도 숫자는 그대로다. 그래서 pair 단위로 못박고, 변경은 fixture
        diff 로 리뷰받게 한다.

        갱신: python -m tests.test_money_path_guard_contract --update-policy
        """
        self.assertTrue(POLICY_FIXTURE.exists(),
                        f"fixture 없음: {POLICY_FIXTURE} — --update-policy 로 생성")
        expected = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(expected, current_policy_matrix())


if __name__ == "__main__":
    if "--update-policy" in sys.argv:
        POLICY_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        matrix = current_policy_matrix()
        POLICY_FIXTURE.write_text(
            json.dumps(matrix, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"갱신 완료: {POLICY_FIXTURE} — 돈경로 {len(matrix)} pair")
    else:
        unittest.main()
