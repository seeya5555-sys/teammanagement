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
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = []
    for stmt in tree.body:
        node = stmt.value if isinstance(stmt, ast.Expr) else None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_load_extracted_module"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            names.append(node.args[0].value)
    if not names:
        raise AssertionError("app.py 에서 _load_extracted_module 호출을 못 찾음 — 번들이 비게 된다")
    return names


APP_SOURCE_PATHS = (ROOT / "app.py",) + tuple(ROOT / name for name in _loaded_boundaries())


def read_app_sources():
    return "\n".join(path.read_text(encoding="utf-8") for path in APP_SOURCE_PATHS)


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
