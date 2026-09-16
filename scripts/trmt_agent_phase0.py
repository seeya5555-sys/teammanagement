#!/usr/bin/env python3
"""Phase 0 measurement tools for the TRMT agent interface.

This module deliberately performs no network calls and no writes to TRMT data.
It inventories the codebase, measures frozen payload projections, and evaluates
externally captured agent runs whose token counters come from the model client.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/agent_interface_phase0_data.json"
ROUTE_RE = re.compile(r"^/(?:api/)?")
URL_RE = re.compile(r"(?:trmt://[A-Za-z0-9_?&=./:-]+|/[A-Za-z0-9_?&=./:-]+)")
REQUIRED_VARIANTS = {"current_ui", "task_tool", "webmcp"}
REQUIRED_CASES = {"list_issues", "get_issue", "vessel_overview"}
REQUIRED_RUN_FIELDS = {
    "case", "variant", "iteration", "input_tokens", "output_tokens",
    "schema_tokens", "image_tokens", "duration_ms", "success",
    "scope_violation", "side_effect",
}


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    rank = max(0, math.ceil((pct / 100) * len(ordered)) - 1)
    return ordered[rank]


def _literal_string(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def route_manifest(root: Path = ROOT) -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    excluded = {"tests", ".git", ".venv-test", "instance", "static"}
    for path in sorted(root.rglob("*.py")):
        if excluded.intersection(path.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr != "route" or not dec.args:
                    continue
                route = _literal_string(dec.args[0])
                if not route or not ROUTE_RE.match(route):
                    continue
                methods = ["GET"]
                for kw in dec.keywords:
                    if kw.arg == "methods":
                        try:
                            methods = sorted(str(x).upper() for x in ast.literal_eval(kw.value))
                        except (ValueError, TypeError, SyntaxError):
                            methods = ["UNKNOWN"]
                # Python applies decorators bottom-up. Flask registers the view
                # when @route runs, so only wrappers textually *below* @route
                # are part of the registered view. A guard above @route wraps
                # the function name too late and must not be reported here.
                route_index = node.decorator_list.index(dec)
                decorators = []
                for item in node.decorator_list[route_index + 1:]:
                    target = item.func if isinstance(item, ast.Call) else item
                    if isinstance(target, ast.Name):
                        decorators.append(target.id)
                    elif isinstance(target, ast.Attribute):
                        decorators.append(target.attr)
                routes.append({
                    "path": route,
                    "methods": methods,
                    "function": node.name,
                    "source": str(path.relative_to(root)),
                    "line": node.lineno,
                    "guards": sorted(d for d in decorators if d != "route"),
                })
    routes.sort(key=lambda r: (r["path"], r["methods"], r["source"], r["line"]))
    return {
        "schema_version": 1,
        "generated_from": "static AST; no application import or database access",
        "guard_limitations": "Decorator guards below @route only; before_request/add_url_rule/runtime wrappers are not inferred.",
        "route_count": len(routes),
        "routes": routes,
    }


def deep_link_inventory(root: Path = ROOT) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for folder in (root / "templates", root / "static/js"):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if path.suffix not in {".html", ".js"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                if "URLSearchParams" not in line and "location." not in line and "href" not in line:
                    continue
                values = sorted(set(URL_RE.findall(line)))
                if values or "URLSearchParams" in line:
                    candidates.append({
                        "source": str(path.relative_to(root)),
                        "line": line_no,
                        "values": values,
                        "uses_query_params": "URLSearchParams" in line,
                    })
    return {
        "schema_version": 1,
        "note": "Candidates require behavioral verification; presence is not proof of a working deep link.",
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def compact_json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def fixture_baseline(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data["issues"]
    summary_fields = ("id", "vessel", "item_topic", "priority", "status", "due_date")
    thin = [{k: issue.get(k) for k in summary_fields} for issue in issues]
    one_id = data["benchmark_issue_id"]
    detail = next(issue for issue in issues if issue["id"] == one_id)
    vessel = data["benchmark_vessel"]
    vessel_issues = [issue for issue in thin if issue["vessel"] == vessel]
    overview = {
        "vessel": vessel,
        "open": sum(i["status"] != "Closed" for i in vessel_issues),
        "urgent": sum(i["priority"] == "Urgent" and i["status"] != "Closed" for i in vessel_issues),
        "next_due": min((i["due_date"] for i in vessel_issues if i["due_date"]), default=None),
    }
    cases = {
        "list_issues": {"current_full_list": issues, "proposed_task_result": thin},
        "get_issue": {"current_full_list_then_filter": issues, "proposed_task_result": detail},
        "vessel_overview": {"current_full_list_then_aggregate": issues, "proposed_task_result": overview},
    }
    measurements = {}
    for name, variants in cases.items():
        rows = {key: compact_json_bytes(value) for key, value in variants.items()}
        current_key = next(key for key in rows if key.startswith("current_"))
        proposed = rows["proposed_task_result"]
        current = rows[current_key]
        measurements[name] = {
            "payload_bytes": rows,
            "payload_reduction_pct": round((1 - proposed / current) * 100, 1),
        }
    return {
        "schema_version": 1,
        "fixture": str(path.relative_to(ROOT)),
        "measurement_kind": "serialized payload bytes; not model tokens",
        "measurements": measurements,
    }


def load_runs(path: Path) -> list[dict[str, Any]]:
    runs = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no}: each JSON value must be an object")
        missing = REQUIRED_RUN_FIELDS - row.keys()
        if missing:
            raise ValueError(f"line {line_no}: missing fields {sorted(missing)}")
        if row["variant"] not in REQUIRED_VARIANTS:
            raise ValueError(f"line {line_no}: unknown variant {row['variant']!r}")
        for field in ("input_tokens", "output_tokens", "schema_tokens", "image_tokens", "duration_ms"):
            if isinstance(row[field], bool) or not isinstance(row[field], (int, float)) or row[field] < 0:
                raise ValueError(f"line {line_no}: {field} must be non-negative")
        for field in ("success", "scope_violation", "side_effect"):
            if type(row[field]) is not bool:
                raise ValueError(f"line {line_no}: {field} must be a JSON boolean")
        runs.append(row)
    if not runs:
        raise ValueError("run file is empty")
    return runs


def compare_runs(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in runs:
        grouped.setdefault((row["case"], row["variant"]), []).append(row)
    cases = sorted({case for case, _ in grouped})
    if set(cases) != REQUIRED_CASES:
        raise ValueError(f"cases must be exactly {sorted(REQUIRED_CASES)}")
    report: dict[str, Any] = {"schema_version": 1, "cases": {}, "decision": {}}
    for case in cases:
        present = {variant for c, variant in grouped if c == case}
        if present != REQUIRED_VARIANTS:
            raise ValueError(f"case {case!r}: variants must be {sorted(REQUIRED_VARIANTS)}")
        case_report = {}
        for variant in sorted(REQUIRED_VARIANTS):
            rows = grouped[(case, variant)]
            if len(rows) < 5:
                raise ValueError(f"case {case!r}/{variant}: at least 5 iterations required")
            iterations = [row["iteration"] for row in rows]
            if len(set(iterations)) != len(iterations):
                raise ValueError(f"case {case!r}/{variant}: duplicate iteration")
            total_tokens = [sum(float(row[k]) for k in ("input_tokens", "output_tokens", "schema_tokens", "image_tokens")) for row in rows]
            durations = [float(row["duration_ms"]) for row in rows]
            case_report[variant] = {
                "iterations": len(rows),
                "token_median": statistics.median(total_tokens),
                "token_p95": percentile(total_tokens, 95),
                "duration_ms_median": statistics.median(durations),
                "duration_ms_p95": percentile(durations, 95),
                "success_rate": sum(bool(row["success"]) for row in rows) / len(rows),
                "scope_violations": sum(bool(row["scope_violation"]) for row in rows),
                "side_effects": sum(bool(row["side_effect"]) for row in rows),
            }
        current = case_report["current_ui"]
        decisions = {}
        for variant in ("task_tool", "webmcp"):
            result = case_report[variant]
            reduction = (1 - result["token_median"] / current["token_median"]) * 100 if current["token_median"] else 0
            decisions[variant] = {
                "token_reduction_pct": round(reduction, 1),
                "adopt": reduction >= 30
                and result["duration_ms_median"] <= current["duration_ms_median"]
                and result["success_rate"] >= current["success_rate"]
                and result["scope_violations"] == 0
                and result["side_effects"] == 0,
            }
        report["cases"][case] = {"variants": case_report, "decisions": decisions}
    report["decision"] = {
        variant: all(report["cases"][case]["decisions"][variant]["adopt"] for case in cases)
        for variant in ("task_tool", "webmcp")
    }
    return report


def dump(value: Any, output: Path | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("routes", "deep-links", "fixture-baseline"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--output", type=Path)
    compare = sub.add_parser("compare")
    compare.add_argument("runs", type=Path, help="JSONL captured from real agent executions")
    compare.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "routes":
        result = route_manifest()
    elif args.command == "deep-links":
        result = deep_link_inventory()
    elif args.command == "fixture-baseline":
        result = fixture_baseline()
    else:
        result = compare_runs(load_runs(args.runs))
    dump(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
