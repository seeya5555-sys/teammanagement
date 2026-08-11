"""모듈 간 의존 그래프 + 층위 계약.

왜 필요한가
----------
2026-08-12 이전 `app.py` 는 `helpers_shared.py` 를 `exec(..., globals(), globals())`
로 로드했다. `app.py` 와 `helpers_shared.py` 가 서로를 필요로 하는 **순환** 이라
import 로 표현할 수 없었기 때문이다. 대가는 두 가지였다.

  · 어느 파일이 어느 심볼에 의존하는지가 **소스 어디에도 적혀 있지 않다.**
  · 오타 난 헬퍼 이름은 import 시점에 안 걸리고, 그 코드경로가 실행되는
    **요청 시점에 NameError** 로만 드러난다.

바닥 층 `app_core.py`(config · Flask 인스턴스 · DB 원시)를 떼어 그 순환을 끊고
나서는 모든 파일이 평범한 import 모듈이다. 그래서 이 게이트의 역할이 바뀌었다:
**잃어버린 의존 정보를 복원하는 것**에서 **선언된 의존을 얼리고 방향을 강제하는
것**으로. 프리즈가 없으면 결합이 슬금슬금 늘어나도 아무도 모른다.

스코프 정확도 — 이 파일의 핵심 전제
----------------------------------
이름 해석은 반드시 **스코프를 구분**해야 한다. 초판은 `ast.walk` 로 파일 전체의
바인딩을 한 집합에 뭉갰는데, 그러면 *다른 함수의 로컬 변수·인자*가 provider 로
오인된다. 어느 함수에 `qeury` 라는 로컬이 하나라도 있으면 딴 곳의 진짜 오타
`qeury(...)` 가 "바인딩됨" 으로 통과한다 = 과소검출. (2026-08-11 올마이트 지적,
`changes-needed`.)

그래서 표준 라이브러리 `symtable` 을 쓴다. CPython 이 컴파일에 쓰는 것과 같은
스코프 분석이라, 함수/클래스/comprehension 로컬·closure free variable·`global`
선언을 전부 정확히 구분한다. 새 의존성은 없다.

또 하나 — **정의(define)와 재수출(re-export)을 구분**한다. 이제 모든 파일이
`os`·`request` 같은 이름을 각자 import 하므로, `is_imported()` 를 소유로 세면
provider 충돌이 수십 건 쏟아지고 소유 판정이 무의미해진다. 소유 = 그 파일이
**대입·def·class 로 만든** 이름뿐이다.

여섯 가지를 본다
  ① 미해결 이름 0 — 어느 모듈도 import 없이 이름을 참조하지 않는다.
     (요청 시점 NameError 후보를 정적으로 잡는 검사)
  ② 척추 정의 충돌 0 — app_core·helpers_shared·app 이 같은 이름을 두 번 정의하면
     아래층을 안 쓰고 재구현한 것이다.
  ③ 의존 그래프 고정 — 결합이 늘거나 방향이 바뀌면 fixture diff 로 드러난다.
  ④ 층위 — 아래층만 참조한다. 형제 참조(꼬리물기) 금지.
  ⑤ Blueprint 상향 예외는 등록 목적으로만 — `app.py` 가 Blueprint 에서 심볼을
     빌려 쓰거나 `bp` 외 속성을 만지기 시작하면 그건 진짜 순환이다.
  ⑥ import 한 이름 재정의 0 — 소유 판정이 그림자에 가려지는 사각지대 트립와이어.
  ⑦ exec 경계 0 — 옛 로더가 어떤 형태로도 돌아오지 않는다.

한계 (정직하게)
  · 동적 접근(`globals()['name']`, `getattr`)은 보이지 않는다.
  · import 문의 존재만 보므로 "쓰지 않는 import" 는 걸러내지 않는다.

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

# 층위 계약. 숫자가 작을수록 아래층이고, 모듈은 **자기보다 아래층만** import 한다.
# Blueprint 모듈은 최상층이며 여기 적지 않는다(등록에서 자동 도출 — 수동 목록은
# 다음 전환 때 등재 누락으로 검사가 조용히 빠진다).
LAYERS = {"app_core": 0, "helpers_shared": 1, "app": 2}
BLUEPRINT_LAYER = 3
# 지원 라이브러리(APNs 전송, docx 생성, KR 포털 클라이언트 …) — `app_core` 바로 위.
# 목록을 손으로 들지 않고 "누가 import 하는데 층위표에도 Blueprint 에도 없는 로컬
# 모듈" 로 도출한다. `app_core`(설정·Flask 인스턴스) 는 봐도 되지만 그 위 애플리케이션
# 층은 못 본다 — 아래 층위 검사가 그 선을 강제한다.
LEAF_LAYER = 0.5


def _local_module_names():
    """저장소 최상위의 우리 소스 모듈 이름 — import 대상이 로컬인지 판별용."""
    return {p.stem for p in ROOT.glob("*.py")}


def registered_blueprints():
    """app.py 의 `app.register_blueprint(<mod>.bp)` 에서 도출한 Blueprint 모듈."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = []
    for stmt in tree.body:
        node = stmt.value if isinstance(stmt, ast.Expr) else None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_blueprint"
                and node.args and isinstance(node.args[0], ast.Attribute)
                and isinstance(node.args[0].value, ast.Name)):
            names.append(node.args[0].value.id)
    if not names:
        raise AssertionError("register_blueprint 호출을 못 찾음 — 분석 범위가 비게 된다")
    return names


def module_imports(path, local):
    """이 모듈이 import 하는 **로컬 모듈** → 가져오는 이름들.

    `import X` 는 이름 목록이 없으므로 빈 리스트로 남는다(간선 자체는 기록).
    최상위가 아닌 import(함수 안 지연 import)도 결합이므로 똑같이 센다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    edges = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in local:
                    edges.setdefault(root, set())
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in local:
                edges.setdefault(root, set()).update(a.name for a in node.names)
    return {mod: sorted(names) for mod, names in edges.items()}


def leaf_modules():
    """층위표·Blueprint 어디에도 없는데 우리 모듈이 import 하는 로컬 모듈."""
    local = _local_module_names()
    known = set(LAYERS) | set(registered_blueprints())
    leaves = set()
    for name in sorted(known):
        leaves |= set(module_imports(ROOT / f"{name}.py", local)) - known
    return sorted(leaves)


def analysed_modules():
    """분석 대상 = 층위표 모듈 + 등록된 Blueprint + 그들이 쓰는 leaf 라이브러리."""
    names = list(LAYERS) + registered_blueprints() + leaf_modules()
    seen, out = set(), []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        path = ROOT / f"{name}.py"
        if not path.is_file():
            raise AssertionError(f"분석 대상 모듈이 실제 파일이 아님: {path}")
        out.append(path)
    return out


def _table(path):
    return symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")


def module_defines(top):
    """이 파일이 **만든** 최상위 이름 — import 로 들여온 이름은 제외한다.

    함수 안에서 `global x` 로 선언하고 대입하는 경우도 런타임에 전역을 만드므로
    포함한다.
    """
    imported = {s.get_name() for s in top.get_symbols() if s.is_imported()}
    defines = {
        s.get_name() for s in top.get_symbols()
        if (s.is_assigned() or s.is_namespace()) and s.get_name() not in imported
    }

    def scan(table):
        for sym in table.get_symbols():
            if sym.is_global() and sym.is_assigned():
                defines.add(sym.get_name())
        for child in table.get_children():
            scan(child)

    for child in top.get_children():
        scan(child)
    return defines


def module_bindings(top):
    """이 파일에 실제로 바인딩된 최상위 이름 전부(정의 + import)."""
    return {
        s.get_name() for s in top.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace()
    }


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
    local = _local_module_names()
    graph, defines, unresolved = {}, {}, {}
    for path in analysed_modules():
        top = _table(path)
        defines[path.name] = module_defines(top)
        unresolved[path.name] = sorted(global_lookups(top) - module_bindings(top) - BUILTIN_NAMES)
        graph[path.name] = {
            f"{mod}.py": names for mod, names in sorted(module_imports(path, local).items())
        }

    # 충돌 판정은 **공유 척추(app_core·helpers_shared·app)** 안에서만 본다.
    # exec 시절엔 모든 동명 심볼이 로드 순서로 승자가 갈려 전부 위험했지만, 지금은
    # import 문에 모듈명이 같이 적히므로 동명 자체는 모호하지 않다. 남은 위험은
    # 하나 — 아래층에 이미 있는 헬퍼를 위층이 다시 구현하는 것. 그건 척추에서만
    # 의미가 있고, Blueprint 모듈의 `bp` 나 docx 라이브러리들의 `build_docx` 처럼
    # 모듈마다 하나씩 있는 게 정상인 이름까지 세면 검사가 늘 빨개져 무시하게 된다
    # — 무시되는 게이트는 없는 게이트다.
    spine = [f"{name}.py" for name in LAYERS]
    conflicts = {}
    for name in sorted(set().union(*(defines[f] for f in spine))):
        owners = sorted(f for f in spine if name in defines[f])
        if len(owners) > 1:
            conflicts[name] = owners
    return graph, unresolved, conflicts


class BoundaryDependencyGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph, cls.unresolved, cls.conflicts = analyze()

    def test_no_unresolved_names(self):
        """import 없이 참조되는 이름은 그 코드경로가 도는 순간 NameError 다."""
        offenders = {f: names for f, names in self.unresolved.items() if names}
        self.assertEqual(
            {}, offenders,
            "import 없이 참조되는 이름(런타임 NameError 후보):\n"
            + "\n".join(f"  {f}: {names}" for f, names in offenders.items()),
        )

    def test_no_definition_conflicts(self):
        """공유 척추 3층이 같은 이름을 두 번 정의하면 아래층을 안 쓰고 재구현한 것이다.

        재수출(import 로 들여온 이름)은 소유가 아니므로 세지 않는다 — 그걸 세면
        `os`·`request` 같은 흔한 이름이 전부 충돌로 잡혀 검사가 무의미해진다.
        """
        self.assertEqual(
            {}, self.conflicts,
            "같은 이름을 여러 모듈이 정의함(어느 구현인지 모호):\n"
            + "\n".join(f"  {n}: {owners}" for n, owners in self.conflicts.items()),
        )

    def test_dependency_graph_is_frozen(self):
        """모듈 간 결합 변화는 리뷰 대상이다."""
        self.assertTrue(
            FIXTURE.exists(),
            f"fixture 없음: {FIXTURE} — "
            "`python -m tests.test_boundary_dependency_graph --update` 로 생성",
        )
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(expected, self.graph)

    def test_every_module_is_covered(self):
        """새 모듈이 생기면 fixture 에도 등재돼야 한다 — 미분석 모듈 방지.

        비교 대상은 **fixture** 다. 분석 결과(`self.graph`)와 비교하면 양변이 같은
        `analysed_modules()` 에서 나오므로 항진명제가 되어 새 모듈을 절대 못 잡는다
        (2026-08-11 negative control 로 실제 미검출 확인 후 수정).
        """
        self.assertTrue(FIXTURE.exists(), f"fixture 없음: {FIXTURE}")
        recorded = set(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.assertEqual(
            {p.name for p in analysed_modules()},
            recorded,
            "분석 대상과 fixture 등재 모듈이 다름 — "
            "`python -m tests.test_boundary_dependency_graph --update` 로 갱신하고 diff 를 리뷰할 것",
        )

    def test_layering_is_downward_only(self):
        """층위 계약: 모듈은 자기보다 **아래층만** import 한다.

            app_core  ←  support/*  ←  helpers_shared  ←  app  ←  routes_*/ai_gemini

        형제 참조(routes_* → routes_*)와 상향 참조(helpers_shared → app 등)를
        금지한다. 상향이 하나라도 들어오면 그게 곧 옛 순환이고, 순환이 생기면
        `exec` 로 되돌아가는 것 말고는 표현할 방법이 없어진다.

        지원 라이브러리(APNs 전송·docx 생성·KR 포털 클라이언트)는 최하층이라
        `app_core` 위로는 아무것도 못 본다. 서로는 참조해도 되지만 그건 아래
        `test_support_libraries_are_acyclic` 이 순환만 막는다 — 이들 사이에 억지로
        전순서를 매기면 파일 하나 추가할 때마다 숫자를 손보게 되고, 그런 계약은
        지켜지지 않는다.

        예외는 딱 하나 — `app.py` 가 Blueprint 모듈을 import 하는 것. Flask 등록
        패턴상 상위층을 끌어와야 하므로 구조적으로 불가피하고, `app.py` 맨 아래
        등록 블록에서만 일어난다. 그래서 "DAG" 가 아니라 **"등록 1건을 뺀 하향
        단방향"** 이 이 계약의 정확한 이름이다. 그 예외를 아래 별도 검사가
        등록 블록으로 묶어 둔다.
        """
        blueprints = set(registered_blueprints())
        support = set(leaf_modules())
        layer = {name: LEAF_LAYER for name in support}
        layer.update(LAYERS)
        layer.update({name: BLUEPRINT_LAYER for name in blueprints})

        violations = {}
        for src, deps in self.graph.items():
            src_name = src[:-3]
            for dep in deps:
                dep_name = dep[:-3]
                self.assertIn(dep_name, layer, f"{src}: 층위 미등재 모듈 import — {dep}")
                if src_name == "app" and dep_name in blueprints:
                    continue                      # Blueprint 등록 예외 (아래 검사가 담당)
                if src_name in support and dep_name in support:
                    continue                      # 지원 라이브러리끼리 — 순환만 금지
                if layer[dep_name] >= layer[src_name]:
                    violations.setdefault(src, []).append(dep)
        self.assertEqual(
            {}, violations,
            "층위 위반(형제·상향 참조 — 공용이면 아래층으로 내릴 것):\n"
            + "\n".join(f"  {s} → {d}" for s, d in violations.items()),
        )

    def test_support_libraries_are_acyclic(self):
        """지원 라이브러리끼리는 참조해도 되지만 **순환은 안 된다**.

        순환이 생기면 import 시점에 반쯤 초기화된 모듈이 보이고, 증상이 "가끔
        AttributeError" 로 나와 원인 추적이 어렵다. 층위 숫자를 매기는 대신
        여기서 순환만 막는 이유는 위 docstring 에 적었다.
        """
        support = set(leaf_modules())
        edges = {
            src[:-3]: [d[:-3] for d in deps if d[:-3] in support]
            for src, deps in self.graph.items() if src[:-3] in support
        }
        state, cycle = {}, []

        def visit(node, path):
            if state.get(node) == "done":
                return
            if state.get(node) == "open":
                cycle.append(" → ".join(path[path.index(node):] + [node]))
                return
            state[node] = "open"
            for nxt in edges.get(node, []):
                visit(nxt, path + [node])
            state[node] = "done"

        for name in sorted(support):
            visit(name, [])
        self.assertEqual([], cycle, f"지원 라이브러리 간 순환: {cycle}")

    def test_blueprint_imports_are_only_for_registration(self):
        """app.py 가 Blueprint 모듈을 끌어오는 건 **등록 때문에만** 허용된다.

        등록과 무관하게 상위층을 쓰기 시작하면 그건 진짜 순환이고, 위 층위 검사의
        예외가 구멍으로 바뀐다. 그래서 import 된 Blueprint 모듈 집합과 등록된
        집합이 정확히 같은지, 그리고 app.py 가 그 모듈들에서 `bp` 말고 다른 이름을
        가져오지 않는지 본다.
        """
        blueprints = set(registered_blueprints())
        imported = {d[:-3] for d in self.graph["app.py"]} & blueprints
        self.assertEqual(
            blueprints, imported,
            "등록된 Blueprint 와 app.py 가 import 한 Blueprint 가 불일치",
        )
        borrowed = {
            dep: names for dep, names in self.graph["app.py"].items()
            if dep[:-3] in blueprints and names
        }
        self.assertEqual(
            {}, borrowed,
            f"app.py 가 Blueprint 모듈에서 심볼을 직접 가져옴(등록 목적 외 상향 결합): {borrowed}",
        )
        # import 이름 목록만 보면 `import routes_core` 뒤에 `routes_core.foo()` 를
        # 쓰는 경로가 통째로 안 보인다(가져온 이름이 0개라 위 검사를 그냥 통과).
        # 속성 접근까지 봐야 예외가 "등록만" 으로 좁혀진다. (2026-08-12 올마이트 지적)
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        used = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in blueprints and node.attr != "bp"):
                used.setdefault(node.value.id, set()).add(node.attr)
        self.assertEqual(
            {}, {k: sorted(v) for k, v in used.items()},
            f"app.py 가 Blueprint 모듈의 `bp` 외 속성을 사용함(상향 결합): {used}",
        )

    def test_no_import_then_rebind(self):
        """import 해 온 이름을 같은 파일이 다시 정의하지 않는다.

        `module_defines()` 는 소유에서 import 이름을 **뺀다**(재수출을 소유로 세면
        `os`·`request` 가 전 파일 충돌이라 판정이 무의미해지므로). 그런데 어떤 파일이
        `from x import foo` 한 뒤 `def foo()` 로 덮으면, `symtable` 에서 그 이름은
        imported 이자 assigned 라 소유에서 **빠진다** — 진짜 소유자가 그림자에 가려
        provider 충돌 검사와 소유 판정이 동시에 눈이 먼다. (2026-08-12 올마이트 지적)

        현재 해당 사례 0. 그러니 이 검사는 "고치는" 게 아니라 그 사각지대가 생기는
        순간 조용히 통과하지 말고 **크게 실패**하게 만드는 트립와이어다.
        """
        offenders = {}
        for path in analysed_modules():
            top = _table(path)
            both = sorted(
                s.get_name() for s in top.get_symbols()
                if s.is_imported() and (s.is_assigned() or s.is_namespace())
            )
            if both:
                offenders[path.name] = both
        self.assertEqual(
            {}, offenders,
            f"import 한 이름을 재정의함 — 소유 판정이 이 이름들에서 눈이 멂: {offenders}",
        )

    def test_no_exec_boundary_loading(self):
        """옛 exec 로더가 어떤 형태로도 돌아오지 않는다.

        `exec(src, globals(), globals())` 는 의존을 소스에서 지우고 오타를 요청
        시점 NameError 로 미루는 구조 그 자체였다. 되돌아오면 위 검사들이 전부
        무의미해지므로(참조가 어느 파일 것인지 정적으로 알 수 없게 된다) 여기서
        원천 차단한다.
        """
        offenders = []
        for path in analysed_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in {"exec", "compile"}):
                    offenders.append(f"{path.name}:L{node.lineno} {node.func.id}()")
        self.assertEqual([], offenders, f"exec/compile 기반 로딩 부활: {offenders}")

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


if __name__ == "__main__":
    if "--update" in sys.argv:
        graph, unresolved, conflicts = analyze()
        if any(unresolved.values()):
            print(f"미해결 이름이 있어 갱신 중단: {unresolved}", file=sys.stderr)
            sys.exit(1)
        if conflicts:
            print(f"정의 충돌이 있어 갱신 중단: {conflicts}", file=sys.stderr)
            sys.exit(1)
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                           encoding="utf-8")
        edges = sum(len(v) for v in graph.values())
        symbols = sum(len(s) for v in graph.values() for s in v.values())
        print(f"갱신 완료: {FIXTURE} — 모듈 {len(graph)}개 · 간선 {edges}개 · 심볼 {symbols}개")
    else:
        unittest.main()
