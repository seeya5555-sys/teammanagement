#!/usr/bin/env bash
# TRMT 테스트 러너 — "clone 하고 ./run_tests.sh" 만으로 재현되는 것이 목표.
#
# 왜 개별 실행인가: tests/*.py 는 각각 import 시점에 app.DATABASE 를 임시파일로
# 갈아끼우고 init_db 를 돌린다(전역 상태). 한 프로세스에 모아 discover 하면
# 먼저 import 된 모듈의 DB 가 이겨서 뒤 파일이 깨진다 → 파일당 1 프로세스.
#
# 종료코드: 실패 0건이면 0, 1건 이상이면 1. SKIP 은 실패가 아니지만 항상 목록으로 출력한다.
#
# 환경변수:
#   TRMT_TEST_PY    사용할 인터프리터 (기본: python3.12 → python3)
#   TRMT_TEST_VENV  venv 경로 (기본: <repo>/.venv-test)
#   TRMT_SKIP_VENV=1  venv 부트스트랩 건너뛰고 현재 인터프리터로 실행
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WANT="$(tr -d '[:space:]' < .python-version 2>/dev/null || echo 3.12)"
VENV="${TRMT_TEST_VENV:-$ROOT/.venv-test}"

pick_py() {
  if [ -n "${TRMT_TEST_PY:-}" ]; then echo "$TRMT_TEST_PY"; return; fi
  for c in "python$WANT" python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)' 2>/dev/null && { command -v "$c"; return; }
  done
  return 1
}

if [ "${TRMT_SKIP_VENV:-0}" = "1" ]; then
  # 부트스트랩 생략 모드(CI 등)에서는 버전 게이트를 강제하지 않는다.
  # 프로덕션 서버는 3.9 라서, "3.9 에서 뭐가 깨지는지"를 재려면 3.9 로도 돌아야 한다.
  # 실제 차단 요인(hashlib.scrypt)은 아래에서 따로 검사한다.
  PY="${TRMT_TEST_PY:-$(command -v "python$WANT" || command -v python3)}"
  [ -n "$PY" ] || { echo "❌ python 인터프리터 없음"; exit 2; }
  "$PY" -c 'import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)' 2>/dev/null \
    || echo "⚠️ $("$PY" -V 2>&1) — 권장 $WANT 미만. 결과는 참고용으로 볼 것."
else
  BOOT="$(pick_py)" || { echo "❌ python >=3.10 없음 (필요: $WANT). pyenv/brew 로 $WANT 설치 후 재시도."; exit 2; }
  if [ ! -x "$VENV/bin/python" ]; then
    echo "· venv 생성: $VENV ($("$BOOT" -V 2>&1))"
    "$BOOT" -m venv "$VENV" || { echo "❌ venv 생성 실패"; exit 2; }
  fi
  PY="$VENV/bin/python"
  # 의존성 해시가 바뀌었을 때만 설치(반복 실행 빠르게).
  STAMP="$VENV/.deps.sha"
  NOW="$(cat requirements.txt requirements-dev.txt 2>/dev/null | "$PY" -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
  if [ "$(cat "$STAMP" 2>/dev/null || echo none)" != "$NOW" ]; then
    echo "· 의존성 설치 중..."
    "$PY" -m pip install -q --upgrade pip >/dev/null 2>&1
    "$PY" -m pip install -q -r requirements-dev.txt || { echo "❌ pip install 실패"; exit 2; }
    echo "$NOW" > "$STAMP"
  fi
fi

# hashlib.scrypt 없으면 app import 자체가 죽는다(일부 배포판의 python3.9).
"$PY" -c 'import hashlib;hashlib.scrypt(b"x",salt=b"y",n=2,r=8,p=1)' 2>/dev/null \
  || { echo "❌ $PY: hashlib.scrypt 미지원 → app import 불가. python$WANT 를 쓸 것."; exit 2; }

echo "· 인터프리터: $PY ($("$PY" -V 2>&1))"
echo

# tests/*.py 는 두 관습이 섞여 있다: (a) 스스로 repo root 를 sys.path 에 넣는 파일,
# (b) 넣지 않고 `python -m tests.xxx` 로 돌던 파일(그때는 cwd 가 sys.path 에 들어감).
# 파일을 직접 실행하면 sys.path[0] 은 tests/ 라서 (b) 는 `import app` 에서 죽는다.
# 14개 파일을 각각 고치는 대신 러너가 repo root 를 PYTHONPATH 로 고정한다(어느 관습이든 동작).
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# 이 저장소 밖(OpenClaw workspace)의 소스를 읽는 패리티 테스트는 workspace 없으면 SKIP.
WS="$HOME/.openclaw/workspace"
pass=0; fail=0; skip=0; FAILED=(); SKIPPED=()
for f in tests/test_*.py; do
  name="$(basename "$f")"
  if [ ! -d "$WS" ] && grep -q 'openclaw/workspace' "$f"; then
    skip=$((skip+1)); SKIPPED+=("$name (workspace 저장소 필요)"); printf 'SKIP %s\n' "$name"; continue
  fi
  if out="$("$PY" "$f" 2>&1)"; then
    pass=$((pass+1)); printf 'ok   %s\n' "$name"
  else
    fail=$((fail+1)); FAILED+=("$name")
    printf '❌   %s\n' "$name"
    printf '%s\n' "$out" | tail -25 | sed 's/^/       /'
  fi
done

echo
echo "──────── 결과: 통과 $pass · 실패 $fail · 건너뜀 $skip ────────"
if [ "$skip" -gt 0 ]; then
  echo "건너뛴 테스트(실패 아님, 하지만 이 실행에서는 검증되지 않음):"
  for s in "${SKIPPED[@]}"; do echo "  - $s"; done
fi
if [ "$fail" -gt 0 ]; then
  echo "실패 목록:"
  for s in "${FAILED[@]}"; do echo "  - $s"; done
  exit 1
fi
echo "✅ 전부 통과"
