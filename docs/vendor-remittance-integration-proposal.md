# Vendor 송금요청 연동 제안서

작성일: 2026-08-31  
범위: SVMS My Vessel의 Fund Request(관리사 지급)와 Invoice(벤더 직불)를 통합해 TRMT에서 검토·선택하고, 외부 SINOKOR 자동화 대시보드의 Vendor 송금요청으로 등록하는 기능

## 1. 확인된 외부 시스템 및 조회 기준

- 관리사 지급 원천은 SVMS 비용청구(Fund Request) 탭 `VmsFundRequest`의 `PKG_CO_OPEX.SP_GET_OPEX` 중 상태가 **`HQ Transferred to financial`**인 행만 대상이다. 그 안에서 `RMT_YN != "Y"`를 미지급으로 확정한다.
- 벤더 직불 원천은 SVMS 비용/인보이스 탭 `VmsInvoice`의 `PKG_CO_INV.SP_GET_INV`다. SVMS Invoice 응답에는 지급여부 필드가 없어 `PAY_DT`는 가져오되 지급상태는 확인필요로 둔다.
- 두 조회 모두 My Vessel 범위 `VSL_CD:"M"`을 사용한다. 이는 로그인 사용자의 SVMS 홈>개인정보에 설정된 My Vessel을 서버가 적용하는 매직값이다.
- Invoice의 지급여부는 외부 Vendor 송금요청 이력과 `INV_NO` 대조 후 확정한다. 대조 전에는 자동 체크하지 않는다.
- 조회 기간(`FM_DT`,`TO_DT`)은 충분히 넓게 잡되, 누락·중복 방지를 위해 TRMT가 마지막 동기화 시각과 원천키(`OPEX_CD` 또는 `INV_CD`)를 보존한다.
- 두 원천 모두 지급기한은 `PAY_DT` 기준이다. 지급여부가 `unpaid`로 확정되고 `PAY_DT < 오늘`인 건만 자동 체크하며, 기한 전 건은 전체 표시 후 수동 선택한다.

- 로그인 후 Vendor 송금요청 화면은 `GET /api/remittance-req?type=vendor`로 이력 조회.
- 등록은 `POST /api/remittance-req`이며 payload는 `{items, supervisor}` 구조.
- 각 item은 `invoice_no`, `payment_request_date`, `reason`을 사용.
- 외부 시스템이 SVMS에서 invoice를 조회하고, 날짜순으로 정리한 뒤 Outlook 초안을 생성함.
- 현재 화면에는 Vendor별 요청 이력, 지급여부, 엑셀, 메일초안, 재생성, 삭제가 있음.
- `PUT`은 지급여부 변경 또는 메일초안 재생성, `DELETE`는 요청 이력 삭제 용도. 송금 실행 API로 보이지 않으며 연동 1차 범위에서 호출하지 않음.

## 2. 권장 아키텍처

```text
SVMS My Vessel Fund Request + Invoice
      │ OPEX: SP_GET_OPEX + RMT_YN / Invoice: SP_GET_INV + Vendor 이력 대조
      ▼
TRMT 통합 송금요청 큐 (관리사/벤더 구분 + 기한초과 자동체크 + 수동선택)
      │ 사람 확인 후 [송금요청 등록]
      ▼
맥 로컬 connector/runner ── 세션/API ── 외부 대시보드 POST
      │
      └─ 결과·외부 request_id·payload hash를 TRMT에 회신
```

외부 사이트의 계정/세션은 서버에 두지 않고 기존 맥 runner 방식으로 보관한다. TRMT 서버는 요청 큐와 화면만 담당한다. 브라우저 자동화는 API가 변경되거나 API 접근이 차단될 때의 fallback으로만 둔다.

## 3. 원클릭 사용자 흐름

1. TRMT에서 **SVMS My Vessel 미지급 목록 새로고침** 클릭.
2. TRMT가 `SP_GET_OPEX`와 `SP_GET_INV` 응답을 받아 관리사/벤더로 구분해 통합한다.
3. Fund Request는 `RMT_YN`으로 미지급을 판정하고, Invoice는 Vendor 송금요청 이력과 `INV_NO`를 대조한다.
4. 지급여부가 확정된 미지급 건 중 `PAY_DT`가 지난 건을 자동 체크한다. 기한 전 건은 전체 표시하되 기본 미체크로 둔다.
5. 중복 invoice, 지급완료/기존 요청, 금액·통화 누락, 선박 미매칭, Invoice 지급상태 확인불가는 HOLD.
6. 사용자가 긴급 건을 추가 체크하고 사유와 지급요청일을 확인한다.
7. **선택 건 송금요청 등록** 클릭 → TRMT draft를 approved로 바꾼 뒤 connector가 외부 POST.
8. 외부 응답의 성공/실패와 요청 이력을 TRMT에 표시. 실패는 재시도 가능하되 idempotency key로 중복 등록 방지.

버튼은 외부 사이트의 송금 자체가 아니라 **송금요청/Outlook 초안 생성**까지만 수행한다. 실제 지급여부 변경은 별도 수동 확인 영역으로 둔다.

## 4. 매핑 초안

| TRMT | 외부 요청 | 규칙 |
|---|---|---|
| Fund Request `OPEX_CD` (SVMS Fund No) | `items[].invoice_no` | 관리사 건은 Fund No를 외부 Invoice No로 사용 |
| Invoice `INV_NO` | `items[].invoice_no` | 직접지급 건은 후속 적용 범위 |
| 사용자가 확인한 지급요청일 | `items[].payment_request_date` | 기본 오늘, 건별 수정 가능 |
| 사유 템플릿 + 사용자 보정 | `items[].reason` | 빈 사유는 등록 차단 또는 확인 체크 |
| TRMT 로그인 사용자/담당 감독 | `supervisor` | 외부 권한 사용자명으로 별도 매핑 |

외부 API 응답의 `request_id`, 생성시각, 외부 상태, 전송 payload hash를 TRMT에 저장한다. 원본 비밀번호는 저장하지 않는다.

## 5. 안전 게이트

- 기본 상태는 `pending`; 자동 후보 생성은 읽기/큐 적재만 수행.
- `[선택 건 송금요청 등록]` 전 최종 확인 모달에 벤더별 건수, 통화별 합계, invoice 목록, 지급요청일을 표시.
- 승인된 행만 connector가 POST하며, 송금요청 등록 뒤에 자동으로 `지급완료`를 바꾸지 않음.
- 같은 invoice가 다른 활성 요청에 있으면 차단. `idempotency_key = vendor + invoice_no + payment_request_date`.
- 실패/타임아웃은 성공으로 표시하지 않으며, 외부 request_id가 없으면 재시도 전 reconciliation 확인.
- 외부 API key/쿠키/비밀번호는 `~/.openclaw/secrets/` 로컬에만 보관하고 로그 마스킹.

## 6. 단계별 구현

### Phase 1 — 목업/읽기 검증

- 아래 목업으로 사용자 흐름 확정.
- SVMS Fund Request + Invoice My Vessel 목록을 읽고, `RMT_YN`/Vendor 이력으로 지급상태를 분류한 뒤 `PAY_DT` 기준 자동체크·수동선택·중복검사를 구현.
- 외부 POST는 호출하지 않는 dry-run으로 payload와 그룹핑 결과 검증.

### Phase 2 — connector 파일럿

- 테스트용 1개 Vendor, 1~2건으로 외부 POST 성공/실패/타임아웃 계약 확인.
- 실제 요청 등록은 형이 확인한 테스트 데이터로만 수행.
- 외부 API가 공식 지원되지 않으면 API 호출 대신 전용 브라우저 세션 자동화로 전환하되, DOM 변경 감지와 수동 중단을 포함.

### Phase 3 — TRMT web/iOS 확대

- 웹 큐와 connector가 안정화된 뒤 iOS는 큐 조회·승인·결과 확인만 제공.
- 실제 외부 등록은 서버나 iOS가 아니라 맥 connector 한 곳에서만 실행.

## 7. 결론

기술적으로는 현재 외부 시스템이 제공하는 JSON API가 명확해 API 연동이 1순위다. 다만 외부 POST는 Outlook 초안과 요청 이력을 생성하는 외부 상태 변경이므로, TRMT 안에 2단 게이트(`pending → approved → submitted`)를 두는 것이 적절하다. 우선 Phase 1 목업과 dry-run부터 진행하고, 실제 POST는 테스트 건 확인 후 파일럿으로 여는 것을 권장한다.
