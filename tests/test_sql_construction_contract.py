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
# 2026-08-15 baseline update (2) — 같은 커밋(02b7308)이 6 site 를 추가했는데 위 항목이
#   `routes_repair_request.py` 2 site 만 등재하고 `routes_calendar_dock.py` 4 site 를 빠뜨려
#   출하 후에도 게이트가 red 로 남아 있었다(154 vs 158). 빠진 4 site 전건 검토:
#   1/2/3. `f"DELETE FROM {aor,fundreq,invoice}_draft WHERE id=? AND status IN ({_DRAFT_DELETABLE_SQL})"`
#      — 테이블명은 f-string 이 아닌 **인라인 하드코딩**이고, 보간되는 건 모듈 상수
#      `_DRAFT_DELETABLE_SQL`(라인 5661) 하나뿐인 리터럴이다. `did` 는 `<int:did>` 컨버터라
#      정수 확정이며 그와 별개로 파라미터 바인딩된다. request 값이 SQL 문법에 닿지 않는다.
#   4. `_draft_delete_conflict` 의 `f'SELECT status FROM {table} WHERE id=?'`
#      — `table` 은 호출부 3곳(5681/6387/6896)에서 전부 문자열 **리터럴**로만 넘어온다
#      (AST 전수검사 non-literal 0). 읽기 전용 조회이고 `did` 는 바인딩된다.
#   ⚠️ 교훈: baseline 을 고칠 땐 count 를 **실측값으로 재계산**해라. 신규 site 를 눈으로 세면
#      이번처럼 일부만 반영돼 게이트가 조용히 red 로 남는다(= 다음 커밋의 진짜 위험 SQL 이 가려진다).
# 2026-08-19 baseline update — `app.py:_auto_migrate()` 의 liscr_job ALTER 1 site. 검토:
#   · `'ALTER TABLE liscr_job ADD COLUMN %s %s' % (col, ddl)` — `col`/`ddl` 은 **같은 문장 안의
#     인라인 리터럴 튜플**에서만 온다(profile/hard_json/vndr_cd/vndr_nm/exp_nm). 호출 인자도,
#     설정값도, request 값도 이 루프에 닿는 경로가 없다.
#   · ALTER 의 컬럼명·타입은 **파라미터 바인딩이 불가능한 식별자/DDL** 자리다. 값 보간이 아니다.
#   · 부팅 경로(배포마다 1회)에서만 돌고 요청 처리 중엔 실행되지 않는다.
#   · count 는 눈으로 세지 않고 `repository_fingerprints()` 실측 재계산으로 갱신했다(위 2026-08-15 교훈).
# 2026-08-20 baseline update — `app.py:_auto_migrate()` 의 vt_attachments provenance ALTER 1 site.
#   · `ddl` 은 같은 문장 안의 인라인 리터럴 튜플에서만 온다(source/source_type/external IDs/hash/time).
#     request·설정·DB 값은 식별자나 DDL 문법에 닿지 않는다.
#   · 기존 행을 manual source 로 보존하는 부팅 migration 이며 요청 처리 중에는 실행되지 않는다.
#   · count/hash 는 `repository_fingerprints()` 출력으로 실측했다.
# 2026-08-20 baseline update (2) — `routes_core.api_widget_issues()` 경량 feed 변경 1 site.
#   · 기존 동적 SQL site의 SELECT 목록에 고정 리터럴 `i.id`를 추가하고, 선택적 supervisor scope를
#     붙였다. 동적 문법 조각은 여전히 서버 리터럴 `AND` 절만 `join`하며 request 값은 `?`에 바인딩된다.
#   · 신규 site 없음(count 161 유지), 식별자·테이블명에 request 값이 닿는 경로 없음.
# 2026-08-20 baseline update (3) — `routes_dock_daily.py` 신규 3 site 전건 검토:
#   1. project PATCH의 `sets`는 서버 allowlist 필드와 고정 date 필드에서만 만들어진다.
#   2. report PUT의 metadata UPDATE도 서버 고정 4필드만 사용한다. 두 곳 모두 값은 `?` 바인딩이다.
#   3. `_sections`의 `q`는 완전한 서버 리터럴 두 조각만 조건부 연결한다.
#   request 값이 SQL 식별자/문법에 닿는 경로 없음. count/hash는 실측 갱신했다.
EXPECTED_COUNT = 164
EXPECTED_SHA256 = "ce3151eb0a71d578e1fb5b598c74ecbf2d2f9029bc0c9455d5019a5c650c3bd4"
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
