# TRMT 웹·iOS 전량 아키텍처 점검 및 기능불변 리팩터링 — 2026-09-27

선행 감사: `docs/trmt-cross-surface-audit-2026-09-12.md`(Q1~Q9). 이 문서는 그 이후 새로 생긴 문제와
미조치 항목만 다룬다. 원칙: **URL·JSON·권한·화면 동작 불변, 품질만 개선.** 동작이 달라지는 항목은
"보류(계약 필요)"로 따로 적었다.

## 1. 아키텍처 요약

```text
브라우저(웹, templates + static/js)      iOS(SwiftUI, TRMT + TRMTWidget)      Mac 러너/워커(automation/*)
   │ 세션쿠키 + CSRF(fetch 래퍼)            │ Bearer(Keychain 공유그룹)             │ X-API-Key (/api/ext/*)
   └──────────────┬───────────────────────┴───────────────┬─────────────────────┘
                  ▼                                        ▼
  Flask(gunicorn 1 worker / 8 gthread) ─ Blueprint 10개(routes_*.py, ai_gemini.py)
        → app.py(인증 훅·마이그레이션·등록) → helpers_shared.py(공용 가드·도메인 헬퍼) → app_core.py(Flask·DB)
        → 서비스/프로젝션(calendar_service, *_projection, report_export_service …) → app_core
  SQLite instance/trmt.db (WAL) · 배포 = main push → trmt-autodeploy.timer(60s) → init_db+_auto_migrate → 재시작
```

- **쓰기 흐름:** 웹은 탭별 스크립트가 각자 `api()`로 호출(CSRF 헤더는 `_csrf.html`이 `window.fetch`를 감싸 일괄 부착).
  iOS는 `actor APIClient`(세대 카운터·오프라인 캐시·outbox) → `Repository`/feature API.
  러너는 claim(`/api/ext/automation/claim`) → SVMS 실행 → 결과 POST. 돈경로는 `admin_required`/`api_key_required` +
  money policy fixture 로 고정.
- **동시성 규약:** 진행경과(actions JSON 배열)는 행 단위 CAS(`WHERE actions=?`) + `prev`/`count` 대조로 fail-closed.

## 2. 문제 구간 (우선순위순)

| # | 구분 | 위치 | 문제 | 처리 |
|---|---|---|---|---|
| A1 | 유지보수 | tests 11개 | 08-31~09-19 변경 뒤 **안전망이 3주간 빨간 상태**. 원인: 스냅샷 6건 노후, 권한 테스트 5건이 52ea9b7(세션 role DB 동기화)·89201bb(계정캐시 uid키) 이후 쿠키 role 만 바꿔 실제로는 admin 으로 돌며 scope 분기를 검증 못 함 | **수정** — 추가 라우트 32·돈경로 4는 가드 확인 후 명시 등재, 권한 테스트는 실제 member 계정으로 로그인 |
| A2 | 구조 | `routes_dock_submit.py:2880` | Blueprint → Blueprint 직접 import(`routes_followup`) — 계층 위반 | **수정** — `helpers_shared.AUTOMATION_CLAIM_HOOKS` 훅 등록 |
| A3 | 성능 | `routes_core.py` `/api/cs/surveys` | 설문마다 2쿼리(N+1, 120개 ≈ 240쿼리) | **수정** — 집계 2쿼리, 순수 projection 공유(실측 5쿼리 고정) |
| A4 | 성능 | `routes_dock_daily.py` 프로젝트 목록(웹·ext) | 프로젝트마다 섹션 조회(1+P) | **수정** — `IN (...)` 1쿼리 |
| A5 | 성능 | `automation_run` | 인덱스 0개, 러너 claim 마다 3~4회 풀스캔 | **수정** — `(status,id)`, `(run_id)` 인덱스(가산적) |
| A6 | 데이터 | `POST /api/ext/issues/<iid>/actions` | 읽고-쓰기 사이 CAS 없음 → 웹/iOS 에서 지운 줄이 러너 append 로 부활 | **수정** — CAS + 최대 3회 재시도(성공 응답 동일, 계속 밀릴 때만 409) |
| A8 | 정합 | `app_core.execute_rc` | `execute` 와 transaction 플래그 판정이 달라 purge 중 중간 commit 가능(잠재) | **수정** — `_in_explicit_transaction()` 공유 |
| A9 | 중복 | dock/boarding 편집권한, is_template 블록, xlsx 셀 파서 | 테이블명만 다른 복제본 | **수정** — `_can_edit_report`, `_apply_template_flag`, `_xlsx_cell`(기존 이름은 얇은 래퍼로 유지) |
| W1 | 버그 | `cs.js`/`vt.js` 업로드 | 여러 파일 선택 시 첫 파일만 업로드(라이브 FileList 를 넘긴 뒤 비움) | **수정** |
| W2 | 버그 | invoice/fundreq | 8초 자동새로고침이 입력 중인 리젝 사유를 지움 | **수정** — 카드별 보존·복원, 숨김탭·입력중 skip |
| W3 | 버그 | `todayISO` 등 3곳 | UTC 기준이라 한국시간 09시 전 "오늘"이 어제 | **수정** — 로컬 날짜 |
| W4 | 보안 | automation/aor/fundreq/dashboard | 서버 문자열을 innerHTML 에 escape 없이/부분 escape 로 삽입 | **수정** — 5문자 escape |
| W5 | 성능 | shipwiki/reqgen/health/dock_procure 폴링 | 숨김탭에서도 폴링, 응답 지연 시 tick 중첩 | **수정** — hidden skip + in-flight 가드 |
| W6 | 유지보수 | app.js/cal/cs/dde/dd | 호출처 0 죽은 함수 14개 | **삭제**(템플릿·onclick·테스트까지 참조 0 확인) |
| I1 | 안정성 | `APIClient`/`DockAPIClient` | `URLComponents(...)!`/`url!` 강제언랩 — 특수문자 경로에서 크래시 | **수정** — `makeURL` throws |
| I2 | 성능 | Automation/Approval/Followup/Survey | 렌더마다 Formatter/Regex/JSONDecoder 재생성, evidence JSON 행당 3회 decode | **수정** — static, 1회 decode |
| I3 | 중복 | iOS 전반 | 에러문구 변환 17벌, D-day 텍스트 4벌, 단일필드 patch 구조체 7개 | **수정** — `APIErrorText`, `DDay.text`, `FieldPatch`(인코딩 동일 테스트) |
| I4 | 성능 | `AttachmentKit` 파일 가져오기 (선행 Q7) | 확장자·용량 검사 전에 파일 전체를 메모리로 읽음 | **수정** — 확장자→메타데이터 용량→읽기(사후검사 유지) |
| I5 | 안정성 | alert `.constant(x != nil)` | 시스템이 값을 못 지워 알림 재등장 가능 | **수정** — `Binding(presenting:)` |

## 3. 보류 — 동작이 달라지므로 별도 계약/결정 필요

- **영어 export 번역 무제한 대기**(`helpers_shared._translate_texts_en`): 분할 재시도 최악 ≈1.9×문장수 호출 × 90s. 총 시간 상한은
  느린 upstream 에서 결과(일부 원문 유지)를 바꾸므로 올마이트 지적대로 이번 범위에서 제외. 남은 budget 을 upstream timeout 에 넘기는
  계약과 함께 별도 작업.

- **돈경로 claim 루프 9벌**(aor/invoice/reqgen/fundreq/dock_submit): fundreq·dock_submit 만 CAS 안에서 `decided_*` 재확인. 현재 악용 경로는 없으나 통합은 돈경로라 최종상태 테스트 확보 후 별도 작업.
- **쿠키 세션 scope 갱신**: `login_required` 는 role 만 DB 동기화, `supervisor_id`/`app_scope` 는 로그인 시점 값이 최대 7일 유지(Bearer 는 매번 갱신). 권한 강화라 순수 리팩터링 아님.
- **비관리자 읽기 scope 불일치**: `/api/widget/issues` 는 서버 scope, `/api/issues`·`/api/issues/export` 는 쿼리 파라미터 supervisor_id 수용. 정책 결정 필요.
- **`/api/followup/indicators` N+1(페이지뷰당 수백 쿼리)**, `/api/automation/soa/reviews` LIMIT 없음.
- **Daily 한 칸 저장 후 `reloadAll()`(3요청+전체 재렌더)**: 서버 파생값(번호·카운트) 계약 확인 후 로컬 패치로.
- **웹 헬퍼 통합(el 14벌·api 22벌·escape 20벌)**: 같은 이름이라도 동작이 다름(401 리다이렉트 유무, 204 처리, `class:null`). 변형별 어댑터로만 통합 가능 — 페이지별 DOM 비교 테스트 후.
- **`bre.js`/`dde.js` 45개 동명 함수, `cs.js`/`vt.js` 24개**: 표 코드가 이미 크게 갈라짐(490줄 vs 149줄) → diff 감사 먼저.
- **iOS**: DockAdvancedViews 한 줄 2,000자 코드 정리·분리, 토큰 Keychain 매 요청 조회 캐시(위젯 공유 키체인 동기화 실기기 검증 필요), Q8 permissive decoding(지원 shape fixture 먼저), Dock 파일 가져오기 2곳 선검사(메시지 우선순위가 바뀜).
- 1,000줄 이상 파일: 웹 `routes_calendar_dock.py` 8.7k(09-12 대비 +1k), iOS `FamilyAssetsView` 1.8k 등 — 서비스/프로젝션 추출(Q5 전략) 지속.

## 3-1. 이번에 포함했지만 관찰 가능한 동작이 바뀌는 항목 (버그 수정)

| 항목 | 이전 | 이후 | 판단 |
|---|---|---|---|
| 웹 "오늘" 기본값(Daily 신규·진행 추가, Vetting, 모바일) | UTC 날짜 → 한국 00:00~08:59 에 **어제** | 로컬(KST) 날짜 | 명백한 오류 수정 |
| 여러 파일 동시 업로드(CS·Vetting) | 첫 파일만 업로드되는 경우 발생 | 선택한 전부 | 명백한 오류 수정 |
| 인보이스·비용청구 리젝 사유 | 8초 새로고침에 입력 중 텍스트 소실 | 보존(요청 도중 입력분은 여전히 한계) | 입력 유실 수정 |
| 러너 진행경과 추가 경합 | 지운 줄 부활·수정 되돌림 | 최신 원문 위 append, 3회 연속 경합 시 409 | 데이터 유실 수정 |
| iOS 파일 가져오기(읽을 수 없는 파일) | 미지원 확장자·메타데이터상 20MB 초과면 조용히 건너뜀 | 기존 안내 문구 표시 | 드문 경우의 안내 |

## 4. 리팩터링 전략

1. **안전망 먼저**: 테스트가 빨간 채로는 어떤 리팩터링도 검증 불가 → 이번에 116/116 복구. 스냅샷은 "재생성"이 아니라 추가분을 가드 확인 후 명시 등재(삭제·변경 0 확인).
2. **순수 함수 추출 + 어댑터 유지**: `_cs_survey_counts_projection` 처럼 DB 무접근 계산을 분리하고 단건/목록이 공유 → 동등성 테스트로 잠금.
3. **이름 보존**: 통합 후에도 기존 함수명은 얇은 래퍼/별칭으로 남겨 import·테스트 호환.
4. **동작이 바뀌는 건 분리**: 경쟁조건·권한·돈경로는 계약 테스트 먼저, 별도 커밋.
5. **측정 후 성능 투자**: 쿼리 수는 테스트로 고정(설문 목록 ≤6), 그 외 캐시/페이지네이션은 운영 규모 측정 후.

## 5. 적용한 개선 코드 (발췌)

```python
# routes_core.py — N+1 제거, 단건/목록이 같은 순수 계산을 공유
def _cs_surveys_with_counts_bulk(surveys):
    sids = [s['id'] for s in surveys]
    counts, attach = {}, {}
    if sids:
        ph = ','.join('?' * len(sids))
        for r in query(f"SELECT survey_id, category, status, COUNT(*) AS n FROM cs_findings "
                       f"WHERE survey_id IN ({ph}) GROUP BY survey_id, category, status", tuple(sids)):
            counts.setdefault(r['survey_id'], []).append(r)
        for r in query(f'SELECT survey_id, COUNT(*) AS n FROM cs_attachments '
                       f'WHERE survey_id IN ({ph}) GROUP BY survey_id', tuple(sids)):
            attach[r['survey_id']] = r['n']
    return {s['id']: _cs_survey_counts_projection(s, counts.get(s['id'], []), attach.get(s['id'], 0))
            for s in surveys}
```

```python
# helpers_shared.py / routes_followup.py — Blueprint 간 직접 import 제거
AUTOMATION_CLAIM_HOOKS = []                       # helpers_shared (하위 계층)
if enqueue_tracked not in AUTOMATION_CLAIM_HOOKS:  # routes_followup (소유 모듈이 등록)
    AUTOMATION_CLAIM_HOOKS.append(enqueue_tracked)
for _hook in list(AUTOMATION_CLAIM_HOOKS):          # routes_dock_submit claim — 실패는 로그만(기존과 동일)
    try: _hook()
    except Exception: current_app.logger.exception('followup tracking enqueue failed')
```

```python
# routes_calendar_dock.py — 러너 append 도 CAS (삭제한 줄 부활 방지)
for _ in range(3):
    raw = query('SELECT actions FROM issues WHERE id=?', (iid,), one=True)['actions']
    actions = (json.loads(raw) if raw else []) + [entry]
    rc = execute_rc('UPDATE issues SET actions=? … WHERE id=? AND actions=?', (json.dumps(actions), iid, raw))
    if rc: return jsonify({'id': iid, 'ref': _ref('issue', iid), 'actions_count': len(actions)})
return jsonify({'error': 'concurrent update, retry'}), 409
```

```swift
// APIClient.swift — 강제언랩 크래시 → 오류
private static func makeURL(path: String, query: [URLQueryItem]) throws -> URL {
    guard var comps = URLComponents(url: APIConfig.baseURL.appendingPathComponent(path),
                                    resolvingAgainstBaseURL: false)
    else { throw APIError.transport("invalid URL") }
    if !query.isEmpty { comps.queryItems = query }
    guard let url = comps.url else { throw APIError.transport("invalid URL") }
    return url
}
```

## 6. 검증

- 웹: `./run_tests.sh` **116 통과 · 0 실패**(작업 전 103/11). 신규 `tests/test_refactor_equivalence_2026_09_27.py` 29 체크.
  node 테스트 9파일 통과, 전 JS·템플릿 inline script 문법 검사 통과, GET 페이지 30개 렌더 정상.
- iOS: 시뮬레이터 빌드 성공, `xcodebuild test` **462 통과 · 0 실패**(신규 인코딩 동등성 3개 포함).
- 알려진 차이(의도): 읽을 수 없는 파일 중 ①미지원 확장자 ②메타데이터상 20MB 초과인 경우에 기존 "조용히 건너뜀" 대신 기존 안내 문구가 뜸.
