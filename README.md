# TRMT3 — Ship Management System

Tanker Management Team 3 (Sinokor Shipmanagement) 내부 업무관리 시스템.

웹 애플리케이션의 구조·경계·의존성은 [ARCHITECTURE.md](ARCHITECTURE.md)를 정본으로 삼는다. `app.py`는 Flask 앱/WSGI 호환 표면과 공통 DB·인증 기반을 소유하며, 모든 기능 라우트가 `app.py`에 직접 들어 있는 단일 파일 구조는 현재 설명이 아니다.

## 주요 기능과 모듈

- `routes_core.py` — 로그인·대시보드·이슈·선박·사용자·감독자·Condition Survey 페이지/API
- `ai_gemini.py` — Vetting CRUD, findings/attachments, report extraction
- `routes_calendar_dock.py` — `/api/ext/*` 워커, 캘린더·리포트·비용·출장·STT 및 Invoice/Fund Request/AOR/Reqgen API
- `calendar_service.py` — 캘린더 조회·CRUD 데이터 연산(HTTP/auth 계약은 기존 Blueprint에 유지)
- `dock_report_projection.py` — Dry Dock Report 목록·상세·내보내기용 읽기 투영
- `boarding_report_projection.py` — Boarding Report 내보내기용 읽기 투영
- `report_export_service.py` — Report DOCX/PDF 생성 응답·변환 공통 기반
- `migration_steps.py` — 부팅 시 additive migration의 순서·독립 실패 경계
- `routes_dock_submit.py` — Dock procurement/inquiry/submit/yard workflow 및 ShipWiki 카드
- `routes_dock_daily.py` — Dock Daily Report·SVMS 동기화 API
- `routes_repair_request.py` — Repair Request 생성·수정·정규화 API
- `routes_liscr.py` — LISCR job/profile API
- `routes_tail.py` — Class Status, fleet map, iOS/push delivery, ShipWiki push callback, classic dashboard
- `helpers_shared.py` — 여러 경계가 공유하는 인증·Gemini·dock/SOA·fleet/push·automation helper
- `app.py` — Flask 인스턴스, 설정, SQLite primitive, authentication hooks, Blueprint 등록 및 역사적 public helper 이름
- `wsgi.py` — gunicorn 통합 진입점(`wsgi:application`, `/drydock` 서브마운트 포함)

라우트 수와 경계 규칙, `app.py` 로더 동작, 데이터 경계는 [ARCHITECTURE.md](ARCHITECTURE.md)에서 확인한다.

## 스택

- Python 3.10+ (개발/CI 기준 버전은 `.python-version`에 정의)
- Flask 3 · SQLite 3 · gunicorn
- Vanilla JS · CSS (빌드 도구 없음)
- ORM 없음, `sqlite3` 기반 순수 SQL

## 폴더 구조

```
teammanagement/
├── instance/trmt.db             # 운영 SQLite DB (자동 생성, 커밋 금지)
├── static/uploads/               # 첨부 파일 저장소 (운영 상태)
├── templates/                   # Flask HTML templates
├── app.py                        # Flask 앱·공통 기반·Blueprint 등록
├── routes_*.py / ai_gemini.py   # 기능별 라우트 경계
├── calendar_service.py           # 캘린더 데이터 서비스
├── *_report_projection.py        # Report 읽기 투영
├── report_export_service.py      # DOCX/PDF 응답 공통 기반
├── helpers_shared.py             # 공유 helper 경계
├── wsgi.py                       # gunicorn 통합 진입점
├── schema.sql / seed.sql         # 스키마·초기 데이터
├── tests/                        # 파일별 격리 실행 unittest
├── run_tests.sh                  # 표준 테스트 러너
├── requirements*.txt             # 실행/개발 의존성
├── .github/workflows/tests.yml   # CI
└── deploy/                       # 설치·배포·백업·복구·롤백 도구
```

## 로컬 실행

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\\Scripts\\activate
pip install -r requirements.txt
python app.py                  # 개발 서버: http://localhost:5000
```

DB가 없으면 `python app.py`가 자동 초기화한다. 빈 DB에서 admin 계정이 생성되면 비밀번호는 실행 로그에 1회 표시된다. 초기 비밀번호를 미리 지정하려면 `TRMT_ADMIN_INIT_PW=... python app.py`로 실행한다.

```bash
python app.py --init-db        # DB 완전 재초기화
TRMT_DEBUG=1 python app.py     # 개발 서버 debug mode 명시적 활성화
```

프로덕션은 `wsgi:application`을 gunicorn으로 실행하며, 실제 systemd 명령은 [`deploy/trmt.service`](deploy/trmt.service)에 있다.

## 테스트

표준 명령은 저장소 루트에서 실행하는 `./run_tests.sh`다. 테스트 파일마다 별도 프로세스로 실행해 각 파일의 격리 DB와 전역 상태가 서로 오염되지 않게 한다. 기본 러너는 `.venv-test`를 만들고 `.python-version` 및 `requirements-dev.txt`를 사용한다.

```bash
./run_tests.sh
TRMT_SKIP_VENV=1 ./run_tests.sh  # CI 등 이미 준비된 환경
```

GitHub Actions의 실제 설정은 [`.github/workflows/tests.yml`](.github/workflows/tests.yml)이며 Python 3.12 테스트와 정보용 Python 3.9 production-parity job을 실행한다.

## SQL 조립 컨벤션 (F8)

SQL의 **값**은 항상 SQLite placeholder와 `params`로 전달한다. 사용자 입력을 SQL 문자열에 직접 이어 붙이거나 f-string/`str.format()`으로 값 자리에 넣지 않는다.

```python
# 올바름: 값은 placeholder/params
query("SELECT * FROM vessels WHERE name=?", (vessel_name,))

# 제한적 조립: 식별자는 내부 allowlist를 통과한 값만 사용
allowed_order = {"name": "name", "updated": "updated_at"}
order_sql = allowed_order[requested_order]
query(f"SELECT * FROM vessels ORDER BY {order_sql}")
```

테이블명, 컬럼명, `ORDER BY`처럼 placeholder로 바인딩할 수 없는 **식별자**를 f-string/format으로 조립해야 할 때만, 입력을 내부 고정 allowlist 또는 별도 식별자 검증으로 먼저 제한한다. `IN (...)` 목록도 값은 항목별 `?` placeholder를 생성하고 값은 `params`로 전달한다. 사용자 입력은 어떤 경우에도 SQL 문자열에 직접 삽입하지 않는다.

새 SQL을 추가할 때는 테스트와 리뷰에서 다음을 확인한다.

1. 값이 전부 placeholder/`params`로 전달되는가.
2. 동적 식별자가 있다면 내부 allowlist/검증을 거치는가.
3. 사용자 입력이 SQL 문자열에 들어가지 않는가.

현재 f-string SQL을 기계적으로 치환하라는 뜻은 아니다. 기존 호출부는 각 allowlist와 `params` 사용을 확인해 판단하고, 새 코드에서 같은 패턴을 무검토로 복제하지 않는다.

## 운영 배포·복구

서버는 Git checkout이 아니라 커밋 archive를 받아 [`deploy/autodeploy.sh`](deploy/autodeploy.sh)가 반영한다. systemd 설치/서비스/타이머 정의는 [`deploy/install.sh`](deploy/install.sh), [`deploy/trmt.service`](deploy/trmt.service), [`deploy/trmt-autodeploy.timer`](deploy/trmt-autodeploy.timer)에 있다.

```bash
# 서버 최초 설치/타이머 설정
bash deploy/install.sh

# 자동배포 수동 1회 실행
bash deploy/autodeploy.sh

# 보관 릴리스 목록 또는 롤백
bash deploy/rollback.sh --list
bash deploy/rollback.sh
bash deploy/rollback.sh --to <40자리 SHA>
```

배포 완료 판단에는 서비스 재시작만으로 충분하지 않다. `deploy/autodeploy.sh`는 archive 반영 후 배포 SHA를 기록하고, 롤백 도구는 `/login` 라이브 응답을 검증한다. 일반 배포도 라이브 SHA와 health 응답을 별도로 확인해야 한다. DB 온라인 백업은 [`deploy/backup.sh`](deploy/backup.sh)와 `trmt-backup.timer`, 복구 점검은 [`deploy/restore-check.sh`](deploy/restore-check.sh)와 `trmt-restore-check.timer`에 정의되어 있다. 운영 데이터인 `instance/`와 `static/uploads/`는 disposable 파일로 취급하지 않는다.

## 감독 / 선박 변경

초기 감독·선박은 `seed.sql`에 임시로 박혀 있습니다. 실제 운영에서는 관리자 계정으로 로그인 후 DB에서 수정하거나 관리 화면을 사용한다.

현재 임시 시드:
- 감독: 손차장 · 김과장 · 이과장
- 선박: KUWAIT PROSPERITY · KUWAIT GLORY · SAUDI EXPORT · ATLANTIC PIONEER

## 키보드 단축키

- `Esc` — 모달 닫기
- 행 클릭 — 해당 이슈 수정 모달 열기

## 보안 주의

- `instance/.secret_key` 파일은 절대 공유/커밋 금지 (세션 서명에 사용)
- 최초 생성되는 admin 비밀번호는 난수이며 **기동 로그에 1회만** 표시된다 → 확인 후 즉시 변경 (`TRMT_ADMIN_INIT_PW`로 지정 가능. 기본 비밀번호를 코드/문서에 적지 않는다)
- 외부 공개 시 HTTPS 리버스 프록시 (nginx/Caddy) 필수
