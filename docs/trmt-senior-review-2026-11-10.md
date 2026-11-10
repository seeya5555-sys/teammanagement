# TRMT 웹·iOS·자동화 시니어 리뷰 (2026-11-10)

선행: `trmt-cross-surface-audit-2026-09-12.md`, `trmt-architecture-audit-2026-09-27.md`, 11-07 리뷰.
이번 범위: 11-07 이후 변경분(SOA 검토함·진행표, ship3d, 자동화 화면)과 자동화 전체. 기능 불변.

## 1. 아키텍처 요약

```
브라우저 ─ 쿠키 세션+CSRF ─┐
iOS 앱 ─── Bearer 토큰 ────┼─▶ Flask(gunicorn gthread) ─▶ SQLite instance/trmt.db + instance/ 파일 캐시
맥 러너 ── X-API-Key ──────┘        Blueprint: routes_core / calendar_dock / dock_submit / dock_daily /
                                     *_mail / rpcost / repair_request / followup / liscr / brain / ship3d …
                                     공유 척추: app_core(DB·설정) → helpers_shared(인증·큐·공용) → routes_*
```

- **자동화 큐**: 화면 버튼·무인 스케줄이 `automation_run` 에 적재 → 맥 `auto_run.sh`(90초) 가
  `/api/ext/automation/claim` 으로 1건씩 가져가 `auto_runner.cmd_for` 배차표대로 SVMS 스크립트 실행 → `/done`.
  무인 적재는 verify 만, live(상신·승인)는 사람 버튼만. running 은 항상 1건.
- **카드형 2단 게이트**: 맥 prep 이 SVMS 를 읽어 AOR·FundReq·Invoice·수리구매 카드를 적재 →
  사람이 승인/리젝 → 같은 큐로 실행 run 적재.
- **메일 자동화**: 맥 Outlook(AppleScript) → Daily·Vetting·Class·CS·브리지 러너 → `/api/ext/*` 로 TRMT 반영.
- **iOS**: SwiftUI + ViewModel, `Repository` → `APIClient`, 오프라인 Outbox, 화면별 7초 폴링.

## 2. 문제 구간과 조치

| # | 구간 | 문제 | 조치 |
|---|---|---|---|
| W1 | 큐 적재 5곳 (`routes_dock_submit` 2, `routes_calendar_dock._queue_aor`, `routes_repair_request._queue_save`, `routes_rpcost`) | "조회 후 INSERT" 2단계 — 동시 요청이 둘 다 통과해 같은 task 2건 적재 가능. 같은 로직 5벌 | **적용** `helpers_shared.automation_enqueue_once` / `automation_reuse_or_enqueue` (한 문장 `INSERT…SELECT…WHERE NOT EXISTS`) |
| W2 | `api_ext_automation_claim` | running 확인과 claim UPDATE 가 별도 문장 | **적용** UPDATE 에 `AND NOT EXISTS(running)` |
| W3 | AOR·FundReq·Invoice 목록 API | 카드마다 캐시 폴더 전체 `listdir` (카드 수 × 파일 수), 스캐너 3벌 | **적용** `_att_disk_index` 1회 스캔 |
| W4 | 큐 안전장치 | 무인 verify 전용 잠금·stage2 가드·6시간 만료에 테스트 0건 | **적용** `tests/test_refactor_equivalence_2026_11_10.py` |
| W5 | `/done` | 상태 가드 없음(만료된 run 에 늦은 done 이 덮음) | 미적용 — 가드를 넣으면 동작이 바뀜 |
| W6 | `invoice_draft.inv_cd`, `fundreq_draft.opex_cd` | dedup 조회 컬럼 인덱스 없음 | 미적용 — 현 행수에서 이득 미실측 |
| I1 | iOS `AutomationViewModel.start()` | `loadAll` 대기 중 화면이 사라지면 `stop()` 뒤에 폴링이 생성돼 계속 돎 | **적용** 취소 확인 후 생성, self 해제 시 루프 종료 |
| I2 | iOS `loadAll()` | 취소가 오류 알림으로 뜰 수 있음 | **적용** 취소 무시 |
| I3 | iOS `loadGroups()` | 그룹·진행표 GET 직렬 | **적용** 동시 시작(적용 조건 동일) |
| I4 | iOS SOA 확인 문구 | 그룹 목록 문자열 4벌 | **적용** 1벌 |
| I5 | iOS `loadAll` 중복 호출·응답 역전 | 진행 중 가드 없음 | 미적용 — 갱신 타이밍이 바뀜 |
| A1 | `auto_runner.cmd_for` | 돈경로 배차표에 테스트 없음, yyyyMM 검증기 3벌 | **적용** `test_auto_runner_dispatch.py`, `_ym` 1벌 |
| A2 | `prep_repair.py`, `expcd_map.py` | 학습맵을 직접 덮어씀(쓰는 중 종료 시 반쪽 JSON 이 7일 캐시) | **적용** tmp → `os.replace` |
| A3 | `dock_fast.sh` / `dock_sync.sh` | 같은 `$LOG.tmp` 로 같은 로그를 자름 | **적용** tmp 이름에 pid |
| A4 | `logrotate/rotate.py` | `glob("**")` 가 `.state/`·`.wf_state/` 를 건너뜀 → 큰 로그가 회전 안 됨 | 미적용 — 형 결정 |
| A5 | autopull vs 전용 plist | AOR prep 09:30, Invoice 16:00, FundReq 18:00 가 잠금 없이 같은 분에 2벌 기동 | 미적용 — 형 결정 |
| A6 | workspace git | `.pyc`·잠금파일·실행 로그가 추적됨 | 미적용 — 형 결정 |
| A7 | 인터프리터 | 돈경로 대부분이 PATH `python3` 의존 | 미적용 — 형 결정 |

## 3. 리팩터링 전략

1. **안전망 먼저**: 수정 전 기준선(웹 150/0)을 찍고, 바꾸는 계약마다 등가 테스트를 같이 넣는다.
2. **한 문장 원자성**: 상태 확인과 상태 변경을 SQL 한 문장으로 묶는다(큐 적재·claim).
3. **N회 → 1회**: 목록 화면의 반복 디스크 스캔은 요청당 1회 맵으로.
4. **응답 계약은 호출부에**: 공용 함수는 "넣었다/안 넣었다"만 답하고 409·skipped·재사용은 호출부가 그대로 결정.
5. **동작이 바뀌는 것은 적용하지 않고 목록으로**: W5·I5·A4~A7.

## 4. 검증

- 웹 `./run_tests.sh`, iOS `xcodebuild test`, `automation/test_auto_runner_dispatch.py` — 수치는 출하 기록 참조.
- 올마이트 1회: changes-needed → blocking 3건(무조건 INSERT 폴백, AOR 중복 인덱스 비등가, 숫자 변환 예외) 반영.
- 고정 기준선 변경: 동적 SQL site 203→202(삭제 1·추가 0), 의존그래프 간선 +5(삭제 0).
