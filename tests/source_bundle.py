"""Source-level contract helpers for the split Flask application.

Runtime compatibility intentionally keeps every extracted boundary in the
``app`` module namespace.  Tests that inspect source must therefore inspect the
whole executable bundle instead of assuming every function still lives in
``app.py``.

The boundary list is derived from the ``_load_extracted_module`` calls in
``app.py`` rather than hard-coded: a hard-coded list silently loses coverage
the moment a new boundary is added (this actually happened when
``helpers_shared.py`` was extracted — source contracts kept passing while no
longer reading the code they were written to constrain).
"""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _loaded_boundaries():
    # 최상위 Expr(Call) 만 실제 로더로 인정한다: ast.walk 전체를 세면 함수 안이나
    # 죽은 분기의 호출까지 로드된 경계로 오인한다 (올마이트 2026-08-11 지적).
    #
    # 번들은 두 종류의 경계를 모두 포함해야 한다:
    #   · exec 로 로드되는 경계  — _load_extracted_module("<file>.py")
    #   · Blueprint 로 전환된 실제 모듈 — app.register_blueprint(<mod>.bp)
    # 전환된 모듈을 빼먹으면 소스 계약 테스트가 그 코드를 더는 읽지 않는데도
    # 초록으로 남는다 (helpers_shared 추출 때 실제로 벌어진 사각지대와 동형).
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = []
    for stmt in tree.body:
        node = stmt.value if isinstance(stmt, ast.Expr) else None
        if not isinstance(node, ast.Call):
            continue
        if (isinstance(node.func, ast.Name) and node.func.id == "_load_extracted_module"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            names.append(node.args[0].value)
        elif (isinstance(node.func, ast.Attribute) and node.func.attr == "register_blueprint"
                and node.args and isinstance(node.args[0], ast.Attribute)
                and isinstance(node.args[0].value, ast.Name)):
            names.append(node.args[0].value.id + ".py")
    if not names:
        raise AssertionError("app.py 에서 경계 로드(_load_extracted_module/register_blueprint)를 못 찾음 — 번들이 비게 된다")
    return names


APP_SOURCE_PATHS = (ROOT / "app.py",) + tuple(ROOT / name for name in _loaded_boundaries())


def read_app_sources():
    return "\n".join(path.read_text(encoding="utf-8") for path in APP_SOURCE_PATHS)


def _converted_names():
    # register_blueprint(<mod>.bp) 만 — exec 경계(helpers_shared)는 모듈 객체가 없다.
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = []
    for stmt in tree.body:
        node = stmt.value if isinstance(stmt, ast.Expr) else None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_blueprint"
                and node.args and isinstance(node.args[0], ast.Attribute)
                and isinstance(node.args[0].value, ast.Name)):
            names.append(node.args[0].value.id)
    return names


class _SharedNamespace:
    """옛 exec 공유 네임스페이스의 의미론을 테스트에서 재현하는 프록시.

    Blueprint 전환 후 각 라우트 모듈은 `from app import X` 로 이름을 **값으로**
    물었다. 그래서 테스트가 `app.X = fake` 로 몽키패치해도 라우트에는 안 닿고
    (스테일 바인딩), 반대로 라우트 모듈이 소유한 심볼은 app 에 더는 없다.

    - 읽기: app → 전환 모듈 순으로 실제 소유자를 찾아 돌려준다.
    - 쓰기: 그 이름을 가진 **모든** 곳(app + 전환 모듈 전부)에 같은 값을 심는다
      — 공유 전역이던 시절과 동일한 관측 효과. teardown 에서 원본을 같은
      방식으로 되돌리면 복원도 완전하다. mock.patch.object 와도 호환된다.
    """

    def _modules(self):
        import importlib

        return [importlib.import_module("app")] + [
            importlib.import_module(n) for n in _converted_names()
        ]

    def __getattr__(self, name):
        # 같은 이름이 서로 다른 객체로 여러 모듈에 있으면 "어느 것" 인지가
        # 정의되지 않는다(올마이트 2026-08-11 지적). 첫 일치를 조용히 돌려주는
        # 대신 모호성을 즉시 실패로 드러낸다. 현재 유일한 중복은 각 모듈의
        # `bp`(설계상 모듈별 소유)로, 테스트가 프록시로 만질 이름이 아니다.
        found = [(m, getattr(m, name)) for m in self._modules() if hasattr(m, name)]
        if not found:
            raise AttributeError(f"{name}: app 에도 전환 모듈 어디에도 없음")
        if len({id(v) for _m, v in found}) > 1:
            owners = [m.__name__ for m, _v in found]
            raise AttributeError(f"{name}: 여러 모듈이 서로 다른 객체로 보유 {owners} — 소유 모듈을 직접 지정할 것")
        return found[0][1]

    def __setattr__(self, name, value):
        holders = [m for m in self._modules() if hasattr(m, name)]
        if not holders:
            raise AttributeError(f"{name}: 존재하지 않는 이름에 패치 시도 — 오타이거나 이미 제거된 심볼")
        for mod in holders:
            setattr(mod, name, value)

    def patch(self, name, value):
        """mock.patch.object 대체용 컨텍스트 매니저.

        mock.patch.object 를 이 프록시에 직접 쓰면 종료 시 delattr 로 복원을
        시도하다 실패하고 — 더 나쁘게는 — 패치가 모듈들에 그대로 남는다.
        여기서는 모듈별 원본을 저장했다가 정확히 되돌린다.
        """
        import contextlib

        @contextlib.contextmanager
        def _cm():
            saved = [(m, getattr(m, name)) for m in self._modules() if hasattr(m, name)]
            if not saved:
                raise AttributeError(f"{name}: 존재하지 않는 이름에 패치 시도")
            try:
                for mod, _orig in saved:
                    setattr(mod, name, value)
                yield value
            finally:
                for mod, orig in saved:
                    setattr(mod, name, orig)

        return _cm()


shared_ns = _SharedNamespace()


def function_source(name):
    """번들 어딘가에 있는 최상위 함수 ``name`` 의 소스 본문을 돌려준다.

    텍스트 인덱스 슬라이스는 함수가 파일 사이를 이사하면 조용히 엉뚱한 범위를
    자르므로, AST 로 정의 위치와 끝 줄을 정확히 집는다.

    같은 이름이 여러 번 정의돼 있으면 **로드 순서상 마지막** 정의를 돌려준다 —
    exec 공유 네임스페이스의 실제 승자와 같게 (첫 정의를 돌려주면 런타임에는
    없는 코드를 검사하게 됨). 경계 간 이중 정의는 별도 gate 가 금지하지만,
    같은 파일 안의 재정의는 그 gate 범위 밖이라 여기서 방어한다.
    """
    found = None
    for path in APP_SOURCE_PATHS:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                lines = src.splitlines(keepends=True)
                found = "".join(lines[node.lineno - 1:node.end_lineno])
    if found is None:
        raise AssertionError(f"함수 {name} 를 어느 경계에서도 못 찾음")
    return found
