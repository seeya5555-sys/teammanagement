"""경계 간 의존 그래프 — 3단계(Blueprint 전환)의 1순위 선행조건.

왜 필요한가
----------
`app.py` 는 추출 경계 5개를 `exec(..., globals(), globals())` 로 로드한다.
그래서 경계들은 서로의 심볼을 **import 선언 없이** 자유변수로 쓴다. 결과:

  · 어느 경계가 어느 심볼에 의존하는지가 **소스 어디에도 적혀 있지 않다.**
  · 오타 난 헬퍼 이름은 import 시점에 안 걸리고, 그 코드경로가 실행되는
    **요청 시점에 NameError** 로만 드러난다. (일반 모듈이면 정적분석이 잡아준다)

Blueprint 전환은 이 자유변수들을 explicit import 로 바꾸는 작업이다. 그래서
전환 전에 그래프를 **명시화하고 고정**한다. 이 파일이 그 역할을 한다.

스코프 정확도 — 이 파일의 핵심 전제
----------------------------------
이름 해석은 반드시 **스코프를 구분**해야 한다. 초판은 `ast.walk` 로 파일 전체의
바인딩을 한 집합에 뭉갰는데, 그러면 *다른 함수의 로컬 변수·인자*가 module
provider 로 오인된다. 어느 함수에 `qeury` 라는 로컬이 하나라도 있으면 딴 곳의
진짜 오타 `qeury(...)` 가 "바인딩됨" 으로 통과한다 = 과소검출.
(2026-08-11 올마이트 지적, `changes-needed`. 그 상태면 이 게이트가 정적분석
대체물로 성립하지 않는다.)

그래서 표준 라이브러리 `symtable` 을 쓴다. CPython 이 컴파일에 쓰는 것과 같은
스코프 분석이라, 함수/클래스/comprehension 로컬·closure free variable·
`global` 선언을 전부 정확히 구분한다. 새 의존성은 없다.

  provider  = 모듈 스코프 바인딩(대입·import·def/class) + 함수 안 `global x; x=…`
  consumer  = 각 스코프에서 **전역 조회**로 컴파일된 참조(is_global)
              → 로컬·인자·closure·comprehension 변수는 애초에 후보가 아니다

세 가지를 본다
  ① 미해결 이름 0 — 어떤 경계도 아무도 제공하지 않는 이름을 참조하지 않는다.
     exec 구조가 잃어버린 "정의되지 않은 이름" 검사를 되돌려 놓는 것.
  ② provider 충돌 0 — 같은 최상위 이름을 두 경계가 제공하지 않는다.
     충돌이 있으면 실제 승자는 **로드 순서상 마지막** 이라, 그래프의
     "어느 경계에 의존" 자체가 모호해진다. 그래서 모호함을 허용하지 않는다.
  ③ 의존 그래프 고정 — 경계 간 결합이 늘거나 방향이 바뀌면 fixture diff 로
     드러나고 리뷰 대상이 된다.

한계 (정직하게)
  · load-time 참조와 request-time 참조를 구분하지 않는다. 그래서 이 그래프는
    "필요한 import 목록" 이지 "import 해도 순환이 안 난다" 는 보증이 아니다.
  · 동적 접근(`globals()['name']`, `getattr`)은 보이지 않는다.

fixture 갱신:  python -m tests.test_boundary_dependency_graph --update
(의도적으로 결합을 바꾼 변경에서만 갱신하고, diff 를 리뷰에 첨부할 것)
"""
import ast
import builtins
import json
import symtable
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "boundary_dependency_graph.json"
BUILTIN_NAMES = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__builtins__", "__spec__", "__loader__", "__package__",
}


def boundary_files():
    """app.py + app.py 가 exec 로 로드하는 경계 전부 (로드 순서 유지).

    목록은 AST 로 뽑는다. regex 로 소스를 긁으면 주석·문자열·죽은 코드 안의
    호출까지 경계로 오인하고, 반대로 조건부 호출은 형태가 달라 놓친다.
    """
    main = ROOT / "app.py"
    tree = ast.parse(main.read_text(encoding="utf-8"), filename=str(main))
    loaded, bad = [], []
    # 최상위 Expr(Call) 만 실제 로더다. ast.walk 전체를 세면 함수 안이나 죽은
    # 분기의 호출을 로드된 경계로 오인한다 — 그런 호출은 발견 즉시 실패시킨다
    # (조용히 무시하면 그 경계가 분석 범위에서 빠진 채 초록이 된다).
    top_level_calls = {
        id(stmt.value) for stmt in tree.body
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
    }
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_load_extracted_module"):
            continue
        if id(node) not in top_level_calls:
            bad.append(f"L{node.lineno}: 최상위 문장이 아닌 로더 호출")
        elif node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            loaded.append(node.args[0].value)
        else:
            bad.append(f"L{node.lineno}: 인자가 리터럴이 아님 — 정적 분석 불가")
    if bad:
        raise AssertionError(
            "경계 로드 호출이 정적 분석 계약을 벗어남 — 이 게이트가 조용히 범위를 잃는다: "
            + "; ".join(bad)
        )
    return [main] + [ROOT / name for name in loaded]


def converted_modules():
    """Blueprint 로 전환된 실제 모듈 목록 — app.py 의 register_blueprint 에서 도출.

    수동 목록은 다음 전환 때 등록 누락으로 검사가 조용히 빠진다. 최상위
    `app.register_blueprint(<mod>.bp)` 형태만 인정한다.
    """
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = []
    for stmt in tree.body:
        node = stmt.value if isinstance(stmt, ast.Expr) else None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_blueprint"
                and node.args and isinstance(node.args[0], ast.Attribute)
                and isinstance(node.args[0].value, ast.Name)):
            names.append(node.args[0].value.id + ".py")
    return names


def _table(path):
    source = path.read_text(encoding="utf-8")
    return symtable.symtable(source, str(path), "exec")


def module_provides(top):
    """실행되면 애플리케이션 namespace 에 올라가는 이름.

    모듈 스코프의 대입·import·def/class 와, 함수 안에서 `global x` 로 선언하고
    대입하는 경우를 포함한다(후자도 런타임에 전역을 만든다).
    """
    provides = set()
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            provides.add(sym.get_name())

    def scan(table):
        for sym in table.get_symbols():
            if sym.is_global() and sym.is_assigned():
                provides.add(sym.get_name())
        for child in table.get_children():
            scan(child)

    for child in top.get_children():
        scan(child)
    return provides


def global_lookups(top):
    """전역 조회로 컴파일된 참조 이름 — 로컬/인자/closure 는 제외된다."""
    names = set()

    def scan(table, is_module):
        for sym in table.get_symbols():
            if not sym.is_referenced():
                continue
            if is_module:
                # 모듈 스코프에서 참조되지만 이 파일이 바인딩하지 않는 이름
                if not (sym.is_assigned() or sym.is_imported() or sym.is_namespace()):
                    names.add(sym.get_name())
            elif sym.is_global():
                names.add(sym.get_name())
        for child in table.get_children():
            scan(child, False)

    scan(top, True)
    return names


def analyze():
    tables, provides, lookups = {}, {}, {}
    for path in boundary_files():
        top = _table(path)
        tables[path.name] = top
        provides[path.name] = module_provides(top)
        lookups[path.name] = global_lookups(top)

    conflicts = {}
    for name in sorted(set().union(*provides.values()) if provides else set()):
        owners = sorted(f for f, syms in provides.items() if name in syms)
        if len(owners) > 1:
            conflicts[name] = owners

    graph, unresolved = {}, {}
    for name in tables:
        cross, missing = {}, set()
        for used in sorted(lookups[name]):
            if used in BUILTIN_NAMES or used in provides[name]:
                continue
            owners = sorted(o for o in provides if o != name and used in provides[o])
            if owners:
                for owner in owners:
                    cross.setdefault(owner, []).append(used)
            else:
                missing.add(used)
        graph[name] = {provider: sorted(syms) for provider, syms in sorted(cross.items())}
        unresolved[name] = sorted(missing)
    return graph, unresolved, conflicts


class BoundaryDependencyGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph, cls.unresolved, cls.conflicts = analyze()

    def test_no_unresolved_names(self):
        """아무 경계도 제공하지 않는 이름을 참조하면 요청 시점 NameError 다."""
        offenders = {f: names for f, names in self.unresolved.items() if names}
        self.assertEqual(
            {}, offenders,
            "제공자가 없는 이름 참조(런타임 NameError 후보):\n"
            + "\n".join(f"  {f}: {names}" for f, names in offenders.items()),
        )

    def test_no_provider_conflicts(self):
        """같은 최상위 이름을 두 경계가 제공하면 실제 승자는 로드 순서상 마지막이다.

        그 상태에서는 "어느 경계에 의존하는가" 가 정의되지 않으므로 그래프도,
        Blueprint 전환의 import 목록도 신뢰할 수 없다. 모호함 자체를 금지한다.
        """
        self.assertEqual(
            {}, self.conflicts,
            "최상위 이름이 여러 경계에서 제공됨(로드 순서에 의존 = 그래프 모호):\n"
            + "\n".join(f"  {n}: {owners}" for n, owners in self.conflicts.items()),
        )

    def test_dependency_graph_is_frozen(self):
        """경계 간 결합 변화는 리뷰 대상이다 — Blueprint 전환의 explicit import 목록."""
        self.assertTrue(
            FIXTURE.exists(),
            f"fixture 없음: {FIXTURE} — "
            "`python -m tests.test_boundary_dependency_graph --update` 로 생성",
        )
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(expected, self.graph)

    def test_every_boundary_is_covered(self):
        """새 경계가 추가되면 fixture 에도 등재돼야 한다 — 미분석 경계 방지.

        비교 대상은 **fixture** 다. 분석 결과(`self.graph`)와 비교하면 양변이 같은
        `boundary_files()` 에서 나오므로 항진명제가 되어 새 경계를 절대 못 잡는다
        (2026-08-11 negative control 로 실제 미검출 확인 후 수정).
        """
        self.assertTrue(FIXTURE.exists(), f"fixture 없음: {FIXTURE}")
        recorded = set(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.assertEqual(
            {p.name for p in boundary_files()},
            recorded,
            "app.py 의 로드 목록과 fixture 등재 경계가 다름 — "
            "`python -m tests.test_boundary_dependency_graph --update` 로 갱신하고 diff 를 리뷰할 것",
        )

    def test_layering_no_sibling_dependencies(self):
        """경계 층위 계약: 형제 경계 참조(꼬리물기)가 조용히 돌아오는 것을 금지한다.

        2026-08-11 helpers_shared.py 추출 후 허용되는 의존 방향은 세 가지뿐이다
        (app.py ↔ helpers_shared.py 상호참조는 의도된 예외 — 둘이 foundation
        층 하나로 묶이며, 양방향 모두 호출 시점 참조뿐임을 실측함. "DAG" 가
        아니라 "형제 참조 금지" 가 이 계약의 정확한 이름이다):
          · helpers_shared.py → app.py
          · 그 외 경계        → app.py, helpers_shared.py
          · app.py            → helpers_shared.py
        경계가 형제 경계를 다시 참조하기 시작하면 (공용이면) helpers_shared.py 로
        옮기거나 (아니면) 자기 파일 안에 두어야 한다 — 형제 참조 자체가 위반이다.
        이 검사가 없으면 frozen fixture 를 --update 로 갱신하는 순간 순환이
        "리뷰된 변경" 처럼 통과한다.
        """
        allowed = {
            "helpers_shared.py": {"app.py"},
            "app.py": {"helpers_shared.py"},
        }
        default_allowed = {"app.py", "helpers_shared.py"}
        violations = {
            src: sorted(set(deps) - allowed.get(src, default_allowed))
            for src, deps in self.graph.items()
            if set(deps) - allowed.get(src, default_allowed)
        }
        self.assertEqual(
            {}, violations,
            "경계 층위 위반(형제 경계 참조 — 공용이면 helpers_shared.py 로 옮길 것):\n"
            + "\n".join(f"  {s} → {d}" for s, d in violations.items()),
        )

    def test_converted_modules_are_self_contained(self):
        """Blueprint 로 전환된 실제 모듈의 계약: 모든 전역 참조는 명시 import 다.

        exec 경계는 공유 네임스페이스라 그래프 분석이 필요했지만, 전환된 모듈은
        더 강한 계약을 직접 검증할 수 있다 — 모듈 안에서 참조하는 모든 이름이
        그 모듈의 import/def/대입으로 바인딩돼 있어야 한다(미해결 이름 0).
        미해결 이름이 하나라도 있으면 그 라우트는 호출 시점 NameError 다.

        검사 대상은 손으로 관리하지 않고 app.py 의 register_blueprint 호출에서
        자동 도출한다(올마이트 2026-08-11: 수동 목록은 다음 전환 때 등록 누락
        가능). 아울러 import 대상도 계약이다: 형제 routes_* 경계나 exec 경계인
        helpers_shared 를 import 하면 층위 위반이므로 여기서 함께 금지한다 —
        허용은 stdlib/서드파티/app 뿐.
        """
        converted = converted_modules()
        self.assertTrue(converted, "전환 모듈 자동 도출 실패 — register_blueprint 호출을 못 찾음")
        boundary_names = {p.stem for p in boundary_files()} - {"app"}
        for filename in converted:
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"), filename=filename)
            banned = []
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module.split(".")[0]]
                banned += [m for m in mods if m in boundary_names or m in {Path(c).stem for c in converted if c != filename}]
            self.assertEqual(
                [], banned,
                f"{filename}: 형제 경계 import 금지 위반 — 공용이면 helpers_shared(즉 app 경유)로: {banned}",
            )
        for filename in converted:
            path = ROOT / filename
            top = _table(path)
            provided = {
                s.get_name() for s in top.get_symbols()
                if s.is_assigned() or s.is_imported() or s.is_namespace()
            }
            referenced = set()

            def scan(table, is_module):
                for sym in table.get_symbols():
                    if not sym.is_referenced():
                        continue
                    if is_module:
                        if not (sym.is_assigned() or sym.is_imported() or sym.is_namespace()):
                            referenced.add(sym.get_name())
                    elif sym.is_global():
                        referenced.add(sym.get_name())
                for child in table.get_children():
                    scan(child, False)

            scan(top, True)
            unresolved = sorted(referenced - provided - BUILTIN_NAMES)
            self.assertEqual(
                [], unresolved,
                f"{filename}: import 없이 참조되는 이름(호출 시점 NameError): {unresolved}",
            )

    def test_endpoint_short_names_are_unique(self):
        """endpoint 의 마지막 조각(함수명)은 앱 전체에서 유일해야 한다.

        base.html 설명서 JS 는 `request.endpoint.split('.').pop()` 으로 정규화해
        짧은 키의 manuals 를 조회한다. 서로 다른 Blueprint 에 같은 함수명이
        생기면 그 정규화가 두 화면을 한 키로 합쳐 버린다 — 여기서 유일성을
        얼려서 그런 충돌이 리뷰 없이 못 들어오게 한다.
        """
        import app as appmod
        from collections import defaultdict

        shorts = defaultdict(list)
        for rule in appmod.app.url_map.iter_rules():
            shorts[rule.endpoint.split(".")[-1]].append(rule.endpoint)
        dups = {k: sorted(set(v)) for k, v in shorts.items() if len(set(v)) > 1}
        self.assertEqual({}, dups, f"short endpoint 이름 충돌(JS 설명서 키 오염): {dups}")

    def test_recorded_boundary_paths_match_loader(self):
        """app.py 가 reloader 에 넘기는 경로 목록이 실제 로드 목록과 같은지.

        `EXTRACTED_BOUNDARY_PATHS` 가 비거나 어긋나면 개발 reloader 가 경계를
        감시하지 못하고 "고쳤는데 안 바뀜" 이 조용히 돌아온다.
        """
        import app as appmod

        recorded = [Path(p) for p in appmod.EXTRACTED_BOUNDARY_PATHS]
        self.assertEqual([p for p in boundary_files() if p.name != "app.py"], recorded)
        for path in recorded:
            self.assertTrue(path.is_file(), f"reloader 감시 경로가 실제 파일이 아님: {path}")


if __name__ == "__main__":
    if "--update" in sys.argv:
        graph, unresolved, conflicts = analyze()
        if any(unresolved.values()):
            print(f"미해결 이름이 있어 갱신 중단: {unresolved}", file=sys.stderr)
            sys.exit(1)
        if conflicts:
            print(f"provider 충돌이 있어 갱신 중단: {conflicts}", file=sys.stderr)
            sys.exit(1)
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                           encoding="utf-8")
        edges = sum(len(v) for v in graph.values())
        symbols = sum(len(s) for v in graph.values() for s in v.values())
        print(f"갱신 완료: {FIXTURE} — 경계 {len(graph)}개 · 간선 {edges}개 · 심볼 {symbols}개")
    else:
        unittest.main()
