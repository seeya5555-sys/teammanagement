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
EXPECTED_COUNT = 136
EXPECTED_SHA256 = "a970356def3eac4b0a39fa82fa601f79dd7938a97b2e89653285110cce5e684b"
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
