# TRMT3 — Ship Management System

Tanker Management Team 3 (Sinokor Shipmanagement) 내부 업무관리 시스템.

현재 제공 모듈:
- **Daily 업무관리** — 감독별 탭으로 이슈 등록/추적 (제목·상세·조치·우선순위·상태·마감일·첨부)
- Condition Survey, Vetting Status 는 추후 추가

## 스택

- Python 3.10+ · Flask 3 · SQLite 3
- Vanilla JS · CSS (빌드 도구 없음)
- DD Manager 스타일 단일 파일 구조 (ORM 없음, 순수 SQL)

## 폴더 구조

```
trmt/
├── instance/
│   └── trmt.db            # SQLite DB (자동 생성)
├── static/
│   ├── css/main.css
│   ├── js/app.js
│   ├── templates/         # fetch 용 HTML 조각 (필요 시)
│   └── uploads/           # 첨부 파일 저장소
├── templates/
│   ├── base.html
│   ├── index.html
│   └── login.html
├── app.py                 # Flask 메인 (모든 라우트)
├── schema.sql             # DB 스키마
├── seed.sql               # 초기 데이터
├── requirements.txt
├── .gitignore
└── README.md
```

## 로컬 실행

```bash
# 1) 가상환경
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate

# 2) 의존성
pip install -r requirements.txt

# 3) 실행 (DB 자동 초기화)
python app.py

# → http://localhost:5000
# 기본 관리자 계정: admin / admin0424
```

DB 완전 재초기화가 필요한 경우:
```bash
python app.py --init-db
```

## Oracle Cloud 배포 (기존 TRMT3 패턴)

```bash
# 1) 로컬에서 zip
zip -r trmt.zip trmt -x "trmt/instance/*" "trmt/static/uploads/*" "trmt/venv/*"

# 2) 서버로 복사
scp trmt.zip opc@152.67.202.26:~/

# 3) 서버에서
ssh opc@152.67.202.26
unzip -o trmt.zip
cd trmt
pip install --user -r requirements.txt

# 4) 기존 프로세스 종료 후 재기동
pkill -f "python.*app.py" || true
nohup python3 app.py > app.log 2>&1 &
```

프로덕션에서는 `app.run(debug=False)` 로 변경하거나 gunicorn 사용 권장:
```bash
pip install --user gunicorn
nohup gunicorn -w 2 -b 0.0.0.0:5000 app:app > app.log 2>&1 &
```

## 감독 / 선박 변경

초기 감독·선박은 `seed.sql` 에 임시로 박혀 있습니다. 실제 운영 전에 관리자 계정으로
로그인 후 DB 에서 직접 수정하거나, 추후 `/admin/users` 페이지를 통해 관리 예정.

현재 임시 시드:
- 감독: 손차장 · 김과장 · 이과장
- 선박: KUWAIT PROSPERITY · KUWAIT GLORY · SAUDI EXPORT · ATLANTIC PIONEER

## 키보드 단축키

- `Esc`  — 모달 닫기
- 행 클릭  — 해당 이슈 수정 모달 열기

## 보안 주의

- `instance/.secret_key` 파일은 절대 공유/커밋 금지 (세션 서명에 사용)
- `admin / admin0424` 기본 비밀번호는 **첫 로그인 후 즉시 변경** 하세요
- 외부 공개 시 HTTPS 리버스 프록시 (nginx/Caddy) 필수
