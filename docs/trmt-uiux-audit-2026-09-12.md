# TRMT 웹·iOS UI/UX 감사 및 개선 — 2026-09-12

## 범위와 검증 수준

- Astra 작업 세션에서 웹 `/Users/jeonghyejin/projects/teammanagement`와 iOS `/Users/jeonghyejin/.openclaw/workspace/trmt-mobile/ios/TRMT` 정본을 분석·수정함.
- 전체 탭의 화면 진입점, 편집/저장/선택/첨부/토글의 이벤트 경계를 **소스 기준**으로 분류함. 전체 화면을 실사용 계정으로 조작하거나 iPhone 실기기에서 시각 검수했다는 의미는 아님.
- 변경은 기존 editor를 여는 UI 경로와 표현 계층에 한정. API, 권한, 데이터 모델, 저장·승인·전송·삭제 계약은 변경하지 않음.
- 별도 Dock Manager 웹 앱은 `templates/dock_manager.html` → `/drydock/` iframe → `wsgi.py`의 외부 `DRYDOCK_DIR` mount 구조임. TRMT 저장소에는 원본 UI가 없음. 해당 외부 웹 앱 자체의 전량 검토·수정은 이 패킷에 포함하지 않음. iOS 네이티브 Dock 화면은 아래 범위에 포함함.
- 본 보고서는 작업·테스트 결과임. commit/push/deploy와 라이브·OTA 검증 결과는 main 출하 단계에서 별도로 확정해야 함.

## 화면 인벤토리와 판정

| 업무 영역 | 웹 화면/코드 | iOS 화면 | 판정·조치 |
|---|---|---|---|
| 공통 탐색 | `base.html`, `main.css` | `RootView`, `Theme`, `MoreView` | 웹 확대 차단 해제, 키보드 메뉴/현재 페이지/포커스 개선. iOS native TabView 유지, 공통 편집 요약 Button 추가, More 장식 축소 |
| 대시보드 | `dashboard.html`, `dashboard_classic.html` | `DashboardView`, `FleetMapView` | 지도·선박 카드 이동 의미 유지. 현행 웹 KPI/카드의 뜨는 그림자·이동 효과 축소. 지도 가독성용 그림자는 유지 |
| Daily | `index.html`, `mobile.html`, `app.js` | `DailyView`, `IssueDetailView`, `IssueEditView` | 기존 행 상세열기·진행 인라인 편집 보존. iOS 현안 정보 요약을 탭해 기존 편집 시트 열기 |
| 자동화 | `automation.html`, `health.html` | `AutomationView`, 승인대기/러너 상태 | 실행·중지·money 확인·그룹 편집·로그 펼침 의미 유지. 카드 전체를 실행/편집 버튼으로 바꾸지 않음 |
| AOR | `aor.html` | 자동화/승인 상세 | 기존 선택·입력·첨부·승인 경계 유지 |
| 비용청구 | `fundreq.html` | 자동화/승인 상세 | 검토·첨부 미리보기·실행 확인 경계 유지 |
| 인보이스 | `invoice.html` | 자동화/승인 상세 | 후보 선택·원문 열기·편집/승인 흐름 유지 |
| 송금요청 후보 | `remittance.html` | 관련 자동화 진입 | 금융 경로는 화면 클릭으로 실행되도록 바꾸지 않음 |
| 인보이스 등록 | `liscr.html` | `LiscrView` | 기존 카드 내 수정 필드/첨부/승인 유지. 편집 중 polling 보류 로직 보존 |
| Class Status | `class_status.html`, `cls.js` | Survey/Class, `ClsItemEditView` | 웹 contenteditable·iOS 지적 본문 버튼 편집이 이미 존재하여 유지 |
| Condition Survey | `condition_survey.html`, `cs.js` | 독립 native 탭 없음 | 분기 펼침·표 셀 편집 유지. Overall Remark 본문을 누르면 기존 수검 모달 열기. 빈 메모도 추가 가능 |
| Vetting | `vetting_status.html`, `vt.js` | Survey/Vetting, finding/observation editor | 웹 메모 본문 클릭 → 기존 메모 모달. iOS 수검 요약 → 기존 수검 시트. 상태 토글/경고/지적 펼침은 분리 유지 |
| Dry Dock Report | `dry_dock.html`, `dd.js`, `dry_dock_edit.html`, `dde.js` | Report 목록/상세/section/block editor | 웹 카드의 기존 본문 편집 이동 유지, `⋮`를 명시적 `편집`으로 교체. iOS 메타 요약 → 권한이 있을 때 기존 편집 시트 |
| Boarding Report | `boarding.html`, `brep.js`, `boarding_edit.html`, `bre.js` | 같은 Report 계층 | Dry Dock과 동일. 블록 직접 편집/표/첨부/이동 흐름 유지 |
| 수리신청서 | `repair_requests.html`, 공용 발주 뷰 | `RepairRequestView` | 기존 폼·목록선택·견적·첨부·결재 진입 보존 |
| 도크 자재/수리 | `reqgen.html` | `ReqGenView` | 기존 장비 필드의 명시적 저장·DRAFT 검토/생성 유지 |
| Dock 발주현황 | `dock_procure.html` | `DockProcureView`, `DPLineCard` | 웹 선택 카드/합계 그라디언트·글로우 축소. iOS 발주 제목·장비 요약 → 기존 편집 시트. OWNER 토글/상신/복사/견적/첨부 분리 |
| 입거 Daily Report | `dock_daily.html`, `dock_daily*.js` | `DockDailyReportView`, section/block editors | 이미 인라인 직접 편집. 섹션순서·날짜·읽기전용·최종 상태·첨부·미리보기 계약 유지 |
| Dock Manager | shell/bridge만 | Dock Overview / Jobs / Note-Class / Note-Daily / Documents / Tracking / Tank Plan / Pipe Plan | iOS Jobs 기간·비용, Note 상세 본문 → 기존 편집 시트. 제목 탭의 펼침, 첨부, 하위 작업 접기, 도면/배치 편집은 원래 의미 유지 |
| 회의록 | `meeting.html` | `MeetingMinutesView`, detail | 웹 헤더는 오디오/본문 펼침이라 재지정하지 않음. iOS 제목 탭 → 기존 제목 수정 alert. 요약 장식 sparkles → 문서 아이콘 |
| Calendar | `calendar.html`, `cal.js` | `CalendarView`, `EventEditorSheet` | 기존 이벤트 탭 → 편집 경로 존재. 날짜 선택·월 이동·시스템 일정 의미 유지 |
| Expenses | `expenses.html`, `exp.js`, `expense_detail.html`, `expd.js` | `ExpensesView`, trip/receipt editor | 웹 목록 `⋮` → `편집`. 상세 영수증 인라인 유지. iOS 출장 요약 제목·기간 표시, 편집권한 있는 요약 탭 → 기존 시트. 영수증 미리보기/행편집 분리 유지 |
| 선박 위키·KRCON | `shipwiki.html`, `krcon.html` | 해당 웹 전용 | 지식 검토·원문 열기·리젝/승격을 편집 제스처로 대체하지 않음 |
| 관리·계정·로그인 | base 공용 모달, `login.html` | Login/Admin/API/Notify settings | 공통 focus/zoom/44pt 터치영역 개선. 계정·권한 변경 API/폼은 그대로 |
| 우리자산·오프라인 | 별도 화면/업무 외 기능 | FamilyAssets / Outbox | 생체인증·실제 처리 상태·재전송/삭제 보호 보존. PRIVATE/ADMIN/실제 대기건수 배지는 장식과 구분하여 유지 |
| 오류·미리보기 | `404.html`, `msg_preview.html` | 각 attachment/QuickLook | 읽기·복사·다운로드 의미 유지 |

## 확인한 문제와 적용한 개선

### 1. 편집 위치와 실제 편집 진입점이 멀리 떨어져 있음

iOS 현안·보고서·출장·수검·발주·도크 상세는 읽는 값과 toolbar/작은 편집 버튼이 분리되어 있었음. 상단을 찾아 돌아가거나 메뉴를 열어야 했음.

- `editableSummary`는 **컨트롤이 없는 읽기 요약만** native `Button`으로 감쌈.
- 화면에 `현안 정보 편집`, `보고서 정보 편집` 같은 작은 명시적 affordance를 표시함.
- 기존 toolbar/menu 편집은 그대로 유지함. 새 버튼은 기존 시트 상태만 변경하고 저장하지 않음.
- 보고서는 `meta.canEdit`, 출장은 상세의 `can_edit == true`인 경우에만 새 진입점을 노출함. 발주 mutation 중에는 새 진입점을 숨김.
- 출장 상세는 편집 대상인 제목·기간을 요약에 같이 보여주고 수정 후 제목이 현재 상세 데이터로 반영되게 함.

```swift
summary
    .editableSummary("보고서 정보 편집", enabled: meta.canEdit) {
        editing = .init(kind: kind, meta: meta)
    }
    .card()
```

### 2. 웹 메모 본문은 읽기만 되고 작은 편집 버튼을 따로 찾아야 함

- Condition/Vetting Overall Remark를 실제 `button type="button"`으로 표현하여 마우스·Enter·Space 접근을 지원함.
- 동일한 기존 모달을 열고 현재 값·정확한 record ID를 전달함. 입력/저장 코드는 새로 만들지 않음.
- 빈 Condition 메모는 섹션 전체가 없어지던 동작 대신 `메모 추가` 진입점을 표시함.
- 선택 영역이 있는 마우스 클릭은 편집을 열지 않아 복사 동작을 보존함.
- 보고서/출장 카드 자체의 상세 이동과 상태 배지 즉시 전환은 그대로 두고, 모호한 `⋮` 편집 버튼을 `편집`으로 바꿈.

### 3. 전 탭 공통 탐색 접근성

- viewport의 `maximum-scale=1, user-scalable=no`를 제거하여 확대를 허용함.
- nav `aria-current=page`, 메뉴 `aria-expanded/aria-controls`를 실제 상태와 동기화함.
- ArrowDown으로 하위 메뉴 첫 항목에 진입, Escape로 닫고 트리거 복귀, 포커스가 그룹 밖으로 나가면 닫힘.
- 버튼/링크/명시적 tabindex에 보이는 키보드 focus를 제공함. 어두운 topnav와 흰 submenu의 outline 대비를 분리함.
- 터치 포인터에서 모달 닫기·보고서/출장 편집의 최소 hit target을 44px로 확장함. 밀도 높은 업무표 전체 셀을 일괄 확장하지는 않음.

### 4. 제품 작업에 도움되지 않는 장식

- 로그인 배경·Dock 발주 합계 그라디언트를 기존 웜페이퍼 토큰의 평면 surface로 정리함.
- 대시보드 카드가 hover에 떠오르는 효과·강한 그림자를 배경/테두리 변화로 바꿈.
- 발주 선택 선박은 얇은 accent 경계/좌측 표시로 구분함. drag 상태·시맨틱 경고 색은 유지함.
- More의 중복 영어 eyebrow·설명, `NEW`/`NATIVE` 홍보/구현 배지를 숨김. 실제 대기 건수·관리 권한·프라이버시 배지는 유지함.
- 회의 요약의 장식 sparkles를 문서 기반 시스템 아이콘으로 변경함.
- iOS native TabView, 웜페이퍼/다크 토큰, 지도 라벨의 대비용 그림자, 상태 의미 색은 그대로 유지함.

## 회귀 검증

- 웹 unittest **30/30 통과**: productization 페이지 전체 GET/URL contract, nav active uniqueness, drydock mobile entry, dock/boarding report projection, money path guards.
- 변경 JS 5개 `node --check` 통과, 변경 파일 `git diff --check` 통과.
- `tests/uiux_browser_smoke.cjs`: 로컬 Chrome 합성 DOM에서 CS/Vetting 빈/기존 메모의 mouse·Enter/Space, 정확한 기존 모달 값/ID, 텍스트 선택 복사 우선, nav 키보드/포커스/펼침 상태와 **네트워크 요청 0건** assertion 전부 PASS.
- 단, Chrome 종료 후 Playwright의 `browser.close()`가 완료되지 않는 호스트 문제가 있음. 테스트 harness는 10초 종료 제한 뒤 **exit 2**로 정확하게 기록함. 따라서 브라우저의 interaction assertions는 통과했지만 clean teardown까지 통과했다고 보고하면 안 됨.
- iOS 앱·위젯·전체 XCTest target의 `generic/platform=iOS Simulator` **build-for-testing 성공**. 실제 XCTest 실행·VoiceOver·Dynamic Type·iPhone 실기기 제스처/시각 검증은 이 작업에서 확보하지 않음.
- 테스트가 production 데이터나 실제 승인·메일·송금·업무 저장 경로를 호출하지 않도록 격리함.

## 후속 리팩터링 전략

1. 동일한 편집 폼을 하나의 저장 계약으로 유지하고, 진입 UI만 복수화함. 행 전체 click blanket-handler나 전체 카드 contenteditable은 사용하지 않음.
2. 폼 편집과 상세 열기/토글/선택/첨부/드래그를 별개의 명시적 이벤트 영역으로 유지함.
3. 스타일은 기존 design token으로 정리함. 대규모 컴포넌트·내비게이션 교체보다 실제 마찰이 확인된 요약/진입점부터 정규화함.
4. 위 인벤토리는 소스 기반 검토와 변경 기록임. 외부 Dock Manager 웹 원본, 실기기 전체 화면, 호스트 브라우저 종료 문제는 별도 검증 경계를 유지함.
