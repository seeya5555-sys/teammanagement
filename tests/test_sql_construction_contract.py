"""Static regression gate for reviewed non-literal SQL execution sites.

The repository still has a small number of intentional dynamic identifier/list
fragments and SQL variables. Their source fingerprints are frozen here so any
new non-literal SQL at a DB execution boundary cannot enter without an explicit
security review and baseline update.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DB_CALLS = {"execute", "execute_rc", "executemany", "executescript", "query"}
# 2026-08-14 baseline update — the gate had been red since 471657a (drift accumulated over
# several commits, so it was NOT caught at deploy time). Reviewed AST diff, all 5 added sites:
#   1. routes_calendar_dock.py `f'SELECT * FROM {table} WHERE id=?'` — table comes from the fixed
#      `_MSG_PREVIEW_SOURCES` whitelist (404 on miss); no request identifier reaches SQL.
#   2/3. routes_dock_submit.py `{line_where}` (agg + lines) — one of two module-local literals chosen
#      by the validated `scope` in {dock, repair}; every value is still bound as a parameter.
#   4/5. routes_repair_request.py INSERT `{cols}/{qs}` and PATCH `{sets}` — identifiers are the fixed
#      literal keys of the server-built `_payload()` dict, never request keys; values are bound.
# Removed 1 site (the dock lines query was replaced by 3.).
# 2026-08-15 baseline update — `deploy/oneoff/merge_vsl_nm.py` 가 12개 site 를 추가했다. 전건 검토:
#   · 식별자(`{tab}` / `{t}` / `{idx["name"]}` / `{where}`)는 전부 **DB 스키마에서 파생**된다
#     (`sqlite_master` 테이블명, `PRAGMA index_list/index_info` 컬럼명). request 값이 닿는 경로가 없다.
#   · `{OWNER}` 는 모듈 상수 `dock_procure_vessel`. `{','.join('?' * len(touched))}` 는 placeholder 뿐.
#   · 선박명 등 모든 **값은 파라미터 바인딩**이다.
#   · 이 파일은 웹에서 import 되지 않는 오프라인 CLI 다. 그래도 `EXCLUDED_DIRS` 로 빼지 않고 등재한다 —
#     디렉토리를 제외하면 나중에 `deploy/` 아래에 앱이 import 하는 코드가 생겨도 감시가 안 붙는다.
# 2026-08-15 baseline update — `routes_repair_request.py` 의 `_CANON_VSL_SQL` 2 site. 검토:
#   · 모듈 상수인 **완전한 리터럴 SELECT** 다. f-string 도, 식별자 조립도, 문자열 연결도 없다
#     (여기 걸린 이유는 인자가 인라인 리터럴이 아니라 Name 노드이기 때문뿐).
#   · 값 2개(`vsl_cd`, `vsl_nm`)는 전부 파라미터 바인딩. request 값이 SQL 문법에 닿지 않는다.
#   · 일부러 상수로 뽑았다 — 생성(`_reserve_rows`, BEGIN IMMEDIATE 안이라 같은 커넥션의
#     `db.execute` 를 써야 한다)과 수정(`_apply_canon_vsl_nm`, 트랜잭션 밖 `query`)이
#     **같은 정규화 규칙**을 써야 하고, 두 벌로 복사하면 갈라진다(2026-08-15 중복행 실사고 원인).
EXPECTED_COUNT = 154
EXPECTED_SHA256 = "cc9395c575566e2cdce4cbde74e4dca37c8d91f8c74cce261bdc819940b7b70a"
EXCLUDED_DIRS = {
    ".git", ".venv-test", "__pycache__", "instance", "node_modules", "tests",
}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _sql_argument(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg in {"sql", "query", "statement"}:
            return keyword.value
    return None


def dynamic_sql_fingerprints(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in DB_CALLS:
            continue
        sql = _sql_argument(node)
        if sql is not None and not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
            # Source text is stable across the Python 3.9 production-parity and
            # 3.12 CI parsers, unlike ast.dump() details between interpreter versions.
            expression = ast.get_source_segment(source, sql)
            if expression is None:
                raise AssertionError(f"cannot fingerprint SQL expression in {filename}")
            found.append(f"{filename}:{_call_name(node)}:{expression.strip()}")
    return sorted(found)


def repository_fingerprints(root: Path = ROOT) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        found.extend(dynamic_sql_fingerprints(path.read_text(encoding="utf-8"), str(relative)))
    return sorted(found)


class DynamicSQLConstructionContractTests(unittest.TestCase):
    def test_reviewed_dynamic_sql_baseline_is_unchanged(self):
        fingerprints = repository_fingerprints()
        digest = hashlib.sha256("\n".join(fingerprints).encode()).hexdigest()
        self.assertEqual(
            EXPECTED_COUNT,
            len(fingerprints),
            "dynamic SQL site count changed; security-review the exact AST diff before updating this baseline",
        )
        self.assertEqual(
            EXPECTED_SHA256,
            digest,
            "dynamic SQL construction changed; security-review identifier/list provenance before updating baseline",
        )

    def test_scanner_detects_direct_indirect_and_keyword_dynamic_sql(self):
        source = '''
def unsafe(cursor, table):
    sql = f"SELECT * FROM {table}"
    cursor.execute(f"DELETE FROM {table}")
    cursor.execute(sql)
    cursor.execute(sql="UPDATE " + table)
'''
        self.assertEqual(3, len(dynamic_sql_fingerprints(source, "probe.py")))

    def test_recursive_scan_includes_future_subpackages(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "future" / "module.py"
            package.parent.mkdir()
            package.write_text('def f(c, sql):\n    c.execute(sql)\n', encoding="utf-8")
            self.assertEqual(1, len(repository_fingerprints(Path(directory))))


if __name__ == "__main__":
    unittest.main()
