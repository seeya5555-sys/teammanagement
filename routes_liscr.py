"""routes_liscr — 인보이스 PDF 업로드 → SVMS 신규 인보이스 생성(Case 2).

기존 `/invoice` 탭(invoice_draft)과 헷갈리지 말 것:
  · `/invoice`  = 이미 SVMS에 있는 인보이스를 컨펌하는 자동화 (Case 1). 이 모듈은 거기 손대지 않는다.
  · `/liscr`    = SVMS에 아직 없는 인보이스를 PDF 한 장으로 만들어내는 자동화 (Case 2).
생성이 끝나면 SVMS 상태가 'S'(상신)이 되고, 그 순간부터 Case 1 러너가 그 인보이스를
평소처럼 집어간다. 두 파이프라인은 코드로 엮이지 않고 SVMS 상태로만 이어진다.

왜 서버가 SVMS를 직접 부르지 않는가:
  SVMS 자격증명은 맥 로컬(`~/.openclaw/secrets/`)에만 두는 게 확정 방침이고, 인보이스
  생성은 금전 행위다. 그래서 이 서버는 **큐와 화면만** 갖고, 실제 SVMS 쓰기는 맥 러너가
  `/api/ext/liscr/*` 로 claim 해서 수행한다. 서버가 털려도 SVMS 쓰기 권한은 새지 않는다.

사람 승인 게이트:
  파싱(러너) → 사람이 화면에서 값 확인·수정 → [승인] → 그때서야 러너가 SVMS에 만든다.
  승인 없이는 어떤 인보이스도 생성되지 않는다.

등록 유형(프리셋) — 2026-08-19 형 지시로 기국 전용에서 범용으로 확장:
  · `liscr`   기국(LISCR). Vendor V25081 · Expense 070205 · USD 가 **고정**. 지금까지 그대로.
  · `generic` 기타 인보이스. Vendor·Expense·통화를 올릴 때 사람이 지정한다.

  🔴 **고정값을 강제하는 곳은 이 서버가 아니라 맥 러너(`profiles.py`)다.** 프리셋의 정본은
     맥에 있고, 서버는 러너가 밀어준 목록(`liscr_master`)을 화면에 보여줄 뿐이다.
     그래서 서버가 vendor/expense 를 뭐라고 보내든 기국 건에는 고정값이 박힌다 —
     잠금이 화면 로직이 아니라 **쓰기가 일어나는 지점**에 있다는 뜻이다.
  🔴 러너가 아직 마스터를 안 밀었으면 `generic` 업로드를 받지 않는다(=오늘까지의 동작).
     목록 없이 코드만 받으면 형이 뭘 고르는지 화면에서 볼 수 없다 — fail-closed.

gate 3단(기존 2단에서 확장):
  READY 승인만 하면 그대로 쓴다
  FIX   못 읽은 값이 있다. **사람이 채우면 승인 가능**(기타 인보이스에서만 나온다)
  HOLD  승인 불가. 사람이 채워서 해결되는 종류가 아니다(번호중복·마스터 조회실패 등)
"""
from flask import Blueprint

import hashlib
import json
import os
import re
import uuid
from datetime import date

from flask import abort, jsonify, render_template, request, send_file, session

from app_core import LISCR_PDF_DIR, app, execute, execute_rc, query
from helpers_shared import admin_required, api_key_required

bp = Blueprint('routes_liscr', __name__)

# 러너가 claim 한 뒤 죽어버린 건을 되살리는 lease. 파싱은 읽기라 짧게 회수해도 안전하다.
LISCR_PARSE_LEASE_SEC = 600
# 🔴 creating(=SVMS 쓰기 진행중)은 자동 회수하지 않는다. 러너가 SVMS 저장을 마치고 결과를
#    돌려주기 직전에 죽으면, 회수해서 재시도하는 순간 인보이스가 두 번 만들어진다.
#    금전 행위는 미탐(사람이 손으로 확인)이 중복실행보다 낫다. 그래서 lease 자체를 두지 않는다.

LISCR_MAX_BYTES = 20 * 1024 * 1024

# 화면에서 사람이 고칠 수 있는 필드 → (컬럼, 헤더키). 여기 없는 필드는 어떤 요청이 와도 안 바뀐다.
# 🔴 `inv_user_id`(Invoice PIC) 는 일부러 뺐다 — 형 지시로 SS0059 고정이고, 화면에서 고칠 수
#    있게 두면 "고정"이 사실상 기본값으로 내려앉는다. Oversea 도 같은 이유로 없다.
# 🔴 통화(`cur_cd`)·Vendor(`vndr_cd`)·Expense(`exp_cd`) 는 **프리셋이 잠그지 않은 경우에 한해**
#    아래 approve 에서 열린다(그래서 이 표가 아니라 approve 안에서 붙인다).
#    · 통화 : 자동판독 실패로 빈 FIX 카드를 여기서 못 채우면 그 건은 살릴 방법이 없다.
#    · Vendor·Expense : 2026-08-19 형 지시. 업로드 폼에서 하나로 정하면 **벤더가 섞인 묶음을
#      한 번에 못 올린다** — 자유서식에서는 그게 정상 사용법이라 카드마다 고르게 한다.
#      🔴 대신 벤더를 바꾸면 Pay Date 가 조용히 틀려질 수 있다(PAY_TERM 이 벤더마다 다르고
#         그 계산은 러너만 한다) → 아래 approve 에서 **PAY_TERM 을 버리고 Remit 재확인을 요구**한다.
_EDITABLE = {
    'inv_no':      ('inv_no', 'INV_NO'),
    'inv_dt':      ('inv_dt', 'INV_DT'),
    'amt':         ('amt', 'AMT'),
    'pay_dt':      ('pay_dt', 'PAY_DT'),
    'subject':     ('subject', None),      # 라인 적요 — 헤더가 아니라 lines[0].SUBJ 로 간다
    'sup_user_id': ('sup_user_id', 'SUP_USER_ID'),   # 담당 감독(사람) — 과거 인보이스에서 추정하므로 수정 여지를 남긴다
}
# 선박은 기국에서는 못 고친다(PDF 가 권위). 자유서식은 파서가 권위가 없어 사람이 지정해야 한다.
_EDITABLE_VESSEL_PROFILES = ('generic',)

# 상신이 성립하려면 반드시 채워져 있어야 하는 헤더 필드 → 사람이 읽는 라벨.
# 🔴 이건 **UX 게이트**다(빈 채로 승인 눌러 러너에서 터지는 걸 막는다). 최종 강제선은
#    러너 `create_invoice.REQUIRED_HEADER` 이고, 쓰기 직전에 한 번 더 본다.
_REQUIRED_HEADER = (('VSL_CD', '선박'), ('VNDR_CD', 'Vendor'), ('SUP_USER_ID', 'Superintendent'),
                    ('INV_NO', 'Invoice No'), ('INV_DT', 'Invoice Date'), ('CUR_CD', '통화'),
                    ('AMT', '금액'), ('PAY_DT', 'Pay Date'))


def _missing_labels(header, lines):
    """상신에 필요한데 안 채워진 항목의 라벨 목록. 비어 있으면 값은 다 갖춰졌다는 뜻.

    🔴 단건 승인(`api_liscr_approve`)과 일괄 승인(`api_liscr_approve_bulk`)이 **이 함수
       하나를** 부른다. 같은 판정을 두 벌로 적어두면 한쪽만 고쳐지는 날이 오고, 그날
       일괄 버튼이 단건보다 느슨해진다 — 한 장씩 눌렀을 때 막히던 건이 한 번에 통과하는
       것이 이 기능에서 제일 위험한 종류의 차이다(`tests/test_liscr_bulk_reparse.py` §3 이
       두 경로를 같은 입력으로 대조한다).
    """
    empty = [lab for k, lab in _REQUIRED_HEADER if header.get(k) in (None, '')]
    # 🔴 빈 값만 보면 모자란다. 날짜는 **채워져 있어도** 형식이 어긋나면(`2026-08-12`,
    #    달력에 없는 `20260231`) 러너가 SVMS 앞에서 터진다 — 그땐 이미 사람 손을 떠났다.
    #    검사를 승인 요청의 편집값이 아니라 **합쳐진 최종 헤더**에 걸어야, 이번에 안 고친
    #    필드나 러너가 올려둔 값도 같이 걸린다(2026-08-19 올마이트 지적).
    #    일괄 승인도 이 함수를 부르므로 두 경로가 같은 강도를 갖는다.
    for k, lab in (('INV_DT', 'Invoice Date'), ('PAY_DT', 'Pay Date')):
        if header.get(k) in (None, ''):
            continue
        _, derr = _check_date(str(header[k]).strip(), lab)
        if derr:
            empty.append(derr)
    if not (lines and (lines[0].get('SUBJ') or '').strip()):
        empty.append('적요(Subject)')
    # 🔴 Expense 는 헤더가 아니라 라인에 있다. 카드에서 고르게 열어둔 값이라(2026-08-19),
    #    안 고른 채 승인되면 러너가 Invoice List 행을 코드 없이 SVMS 로 보낸다.
    if not (lines and (str(lines[0].get('EXP_CD') or '')).strip()):
        empty.append('Expense')
    return empty


_MASTER_KINDS = ('profiles', 'vessels', 'expenses', 'vendors', 'currencies')
# SVMS 코드 형식 — 화면에서 직접 칠 수 있는 값이라 서버에서도 형태를 본다.
_CODE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,29}$')


def _master(kind):
    """마스터 스냅샷 1종. 아직 안 밀렸으면 (None, []) — 호출측이 fail-closed 판단."""
    r = query("SELECT payload, updated_at FROM liscr_master WHERE kind=?", (kind,), one=True)
    if not r:
        return None, []
    try:
        return r['updated_at'], json.loads(r['payload']) or []
    except Exception:
        app.logger.exception('liscr-master-parse')
        return r['updated_at'], []


def _profile_registry():
    """러너가 밀어준 프리셋 목록. 없으면 기국 1종만 — 오늘까지의 동작 그대로다.

    🔴 여기서 기타 인보이스를 임의로 만들어내지 않는다. Vendor/Expense 목록 없이 코드만
       받으면 형이 화면에서 뭘 고르는지 볼 수가 없다.
    """
    _, rows = _master('profiles')
    known = [p for p in rows if isinstance(p, dict) and p.get('key')]
    # 🔴 프리셋만 밀려 있고 나머지 마스터가 비어 있으면 **열지 않는다**. `profiles` 는
    #    러너가 SVMS 호출 없이 만드는 순수 데이터라, SVMS 조회가 통째로 실패한 회차에도
    #    혼자 저장될 수 있다. 그 상태로 '기타 인보이스' 를 열면 Vendor/Expense 목록이 빈
    #    화면에서 코드를 손으로 쳐야 하고, 형이 뭘 고르는지 확인할 방법이 없다.
    if known and all(_master(k)[1] for k in ('vessels', 'expenses', 'vendors', 'currencies')):
        return {p['key']: p for p in known}
    return {'liscr': {'key': 'liscr', 'label': '기국 (LISCR)',
                      'locked': ['vendor', 'expense', 'currency'], 'soft_fill': False,
                      'hint': '러너가 아직 마스터를 밀지 않음 — 기국만 등록 가능'}}

def _check_currency(v, allow_auto=False):
    """통화 코드 검증 → (정규화값, 에러문구). 형식 + **마스터 실재** 둘 다 본다.

    allow_auto : 업로드 시점에만 True. 'AUTO' = "인보이스에서 읽어라" 라는 지시어지
                 통화가 아니다. 승인(=SVMS 로 나갈 값)에서는 절대 통과시키지 않는다.

    🔴 형식만 보면 `USF` 같은 오타 3자리가 그대로 SVMS 통화칸에 실린다 — 금액의 의미가
       통째로 바뀌는 값이라 자유입력으로 두지 않는다. 마스터는 러너가 **최근 실사용
       인보이스에서 집계**한 목록이라, 여기 없는 코드는 우리 회사가 안 쓰는 통화다.
    🔴 마스터가 아직 안 밀렸으면 형식만 본다(fail-open). 여기서 막으면 마스터 없는
       상태에서 기국 건까지 못 올리는데, 기국은 통화가 고정이라 이 경로를 아예 안 탄다.
    """
    v = (v or '').strip().upper()
    if allow_auto and v == 'AUTO':
        return v, None
    if not re.match(r'^[A-Z]{3}$', v):
        return None, '통화는 ISO 3자리 코드여야 함 (예: USD)'
    _, curs = _master('currencies')
    known = {(c.get('cd') or '').upper() for c in curs if isinstance(c, dict)}
    if known and v not in known:
        return None, '통화 %s 는 SVMS 최근 사용 목록에 없음 (%s)' % (v, ', '.join(sorted(known)))
    return v, None


# 금액 형식 — 부호 없는 십진수, 소수 2자리까지. float() 만 쓰면 'nan'/'inf' 가 통과해
# 금액칸에 NaN 이 실린다(비교·합계가 전부 조용히 무너짐).
_AMT_RE = re.compile(r'^\d{1,15}(\.\d{1,2})?$')
# INV_DT/PAY_DT 저장형(YYYYMMDD).
# 🔴 화면은 input[type=date] 라 형식이 보장되지만 승인은 API 로도 부를 수 있고, 무엇보다
#    2026-08-19 부터 자유서식에서는 **날짜를 사람이 직접 치는 것이 정상 경로**가 됐다
#    (벤더 마스터에 PAY_TERM 이 없으면 Pay Date 를 자동으로 못 만든다). 달력에 없는 날
#    (20260231) 까지 걸러야 러너가 SVMS 앞에서 터지지 않는다 — 그때는 이미 사람 손을 떠났다.
_DATE_RE = re.compile(r'^\d{8}$')
_DATE_LABEL = {'inv_dt': 'Invoice Date', 'pay_dt': 'Pay Date'}


def _check_date(val, label):
    """(정규화값, 오류문구). 형식과 **실재하는 날짜인지**를 같이 본다."""
    if not _DATE_RE.match(val):
        return None, '%s 형식이 잘못됨 (YYYYMMDD)' % label
    try:
        date(int(val[:4]), int(val[4:6]), int(val[6:]))
    except ValueError:
        return None, '%s %s 는 달력에 없는 날짜임' % (label, val)
    return val, None


def _liscr_pdf_path(jid):
    """업로드 PDF 경로. 파일명이 id 고정이라 경로주입이 성립하지 않는다."""
    return os.path.join(LISCR_PDF_DIR, '%d.pdf' % int(jid))


def _remove_pdf(jid):
    """업로드 PDF 삭제. 지워졌으면(원래 없었어도) True, 남아 있으면 False.

    🔴 성공 여부를 **반드시 돌려준다.** 화면은 "업로드한 PDF도 함께 삭제됩니다" 라고
       약속하는데, 여기서 예외를 삼키고 성공만 답하면 그 약속이 거짓말이 되고
       디스크엔 주인 없는 PDF 가 남는다. 행 삭제 자체는 이미 끝났으므로 되돌리지 않고,
       못 지웠다는 사실을 호출자에게 올려 보낸다.
    """
    try:
        p = _liscr_pdf_path(jid)
        if os.path.exists(p):
            os.remove(p)
        return True
    except Exception:
        app.logger.exception('liscr-pdf-delete')
        return False


def _job_dict(r):
    d = dict(r)
    for k in ('reasons', 'hard_json', 'header_json', 'lines_json', 'parsed_json', 'edited_json'):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = None
    d['has_pdf'] = os.path.exists(_liscr_pdf_path(d['id']))
    return d


# ───────────────────────────── 사람용 화면/API ─────────────────────────────

@bp.route('/liscr')
@admin_required
def liscr_page():
    return render_template('liscr.html')


@bp.route('/api/liscr/jobs')
@admin_required
def api_liscr_jobs():
    rows = query("SELECT * FROM liscr_job ORDER BY CASE status "
                 "WHEN 'parsed' THEN 0 WHEN 'hold' THEN 1 WHEN 'queued' THEN 2 "
                 "WHEN 'parsing' THEN 3 WHEN 'approved' THEN 4 WHEN 'creating' THEN 5 "
                 "ELSE 6 END, id DESC LIMIT 200")
    waiting = query("SELECT COUNT(*) c FROM liscr_job WHERE status='parsed'", one=True)
    return jsonify({'jobs': [_job_dict(r) for r in rows], 'waiting': waiting['c']})


@bp.route('/api/liscr/upload', methods=['POST'])
@admin_required
def api_liscr_upload():
    """PDF 업로드 → 큐 적재. 여기서는 파싱도 SVMS 호출도 하지 않는다(맥 러너 담당).

    등록 유형(프리셋)은 **올릴 때** 고른다. 파싱 뒤에 고르게 하면 기국 건까지 한 번 더
    손이 가고, 무엇보다 파서를 무엇으로 태울지를 파싱 전에 알아야 한다.
    """
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': '파일이 없음'}), 400

    # ── 등록 유형과 그에 딸린 값들 ──────────────────────────────────────
    reg = _profile_registry()
    pkey = (request.form.get('profile') or 'liscr').strip().lower()
    prof = reg.get(pkey)
    if not prof:
        return jsonify({'error': '등록 유형 %r 을 쓸 수 없음 (사용 가능: %s)'
                                 % (pkey, ', '.join(reg))}), 400
    locked = set(prof.get('locked') or ())

    def choice(field, form_key, required, label):
        """고정 필드는 사람이 뭘 보내든 무시한다(고정의 정본은 맥 러너)."""
        if field in locked:
            return None, None
        v = (request.form.get(form_key) or '').strip()
        if not v:
            return (None, None) if not required else (None, '%s 를 골라야 함' % label)
        if not _CODE_RE.match(v):
            return None, '%s 코드 형식이 잘못됨' % label
        return v, None

    # 🔴 Vendor·Expense 는 비워둘 수 있다(2026-08-19 형 지시). 비우면 러너가 FIX 로 내리고
    #    **카드마다** 고른다 — 벤더가 섞인 인보이스 묶음을 한 번에 올리려면 이 길뿐이다.
    #    (여기서 required 로 되돌리면 그 사용법이 다시 막힌다.)
    vndr_cd, err = choice('vendor', 'vendor_cd', False, 'Vendor')
    if err:
        return jsonify({'error': err}), 400
    exp_cd, err = choice('expense', 'exp_cd', False, 'Expense')
    if err:
        return jsonify({'error': err}), 400
    # 통화는 비워둘 수 있다 — 비면 러너가 인보이스에서 읽는다(못 읽으면 FIX 로 떨어져 형이 채운다).
    cur_cd, err = choice('currency', 'cur_cd', False, '통화')
    if err:
        return jsonify({'error': err}), 400
    if cur_cd:
        cur_cd, err = _check_currency(cur_cd, allow_auto=True)
        if err:
            return jsonify({'error': err}), 400
    vsl_cd = (request.form.get('vsl_cd') or '').strip() or None
    if vsl_cd and not _CODE_RE.match(vsl_cd):
        return jsonify({'error': '선박 코드 형식이 잘못됨'}), 400
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'PDF 파일만 업로드 가능'}), 400
    data = f.read(LISCR_MAX_BYTES + 1)
    if len(data) > LISCR_MAX_BYTES:
        return jsonify({'error': '파일이 20MB를 넘음'}), 400
    if not data.startswith(b'%PDF'):
        return jsonify({'error': 'PDF 형식이 아님'}), 400
    sha = hashlib.sha256(data).hexdigest()

    # 같은 PDF가 아직 처리중이면 새 잡을 만들지 않는다(같은 인보이스 두 번 생성 방지의 1차선).
    # IN 목록은 리터럴로 적는다 — 상태 집합은 이 파일이 정하는 고정값이고, 문자열로 조립하면
    # `test_sql_construction_contract` 의 검토 대상이 되기 때문이다.
    dup = query("SELECT id, status FROM liscr_job WHERE sha256=? AND status IN "
                "('uploading','queued','parsing','parsed','hold','approved','creating') "
                "ORDER BY id DESC LIMIT 1", (sha,), one=True)
    if dup:
        return jsonify({'error': '같은 PDF가 이미 처리중임 (#%d, %s)' % (dup['id'], dup['status']),
                        'dup_id': dup['id']}), 409
    created = query("SELECT id, inv_cd FROM liscr_job WHERE sha256=? AND status='created' "
                    "ORDER BY id DESC LIMIT 1", (sha,), one=True)
    if created:
        return jsonify({'error': '같은 PDF로 이미 SVMS 인보이스를 만들었음 (%s)' % created['inv_cd'],
                        'dup_id': created['id']}), 409

    # 🔴 'uploading' 으로 넣고 파일을 쓴 뒤에 'queued' 로 올린다. 곧바로 'queued' 로 넣으면
    #    러너가 파일이 다 쓰이기 전에 claim 해 반쯤 쓰인 PDF 를 파싱할 수 있다.
    #    claim 은 'queued' 만 보므로, 이 순서가 파일 완성과 큐 진입을 묶어준다.
    jid = execute("INSERT INTO liscr_job (filename, sha256, status, profile, vndr_cd, exp_cd, "
                  "cur_cd, vsl_cd) VALUES (?,?,'uploading',?,?,?,?,?)",
                  (os.path.basename(f.filename)[:200], sha, pkey,
                   vndr_cd, exp_cd, cur_cd, vsl_cd))
    try:
        with open(_liscr_pdf_path(jid), 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        app.logger.exception('liscr-pdf-save')
        execute("UPDATE liscr_job SET status='failed', error='PDF 저장 실패' WHERE id=?", (jid,))
        return jsonify({'error': 'PDF 저장 실패'}), 500
    execute("UPDATE liscr_job SET status='queued' WHERE id=? AND status='uploading'", (jid,))
    return jsonify({'id': jid, 'status': 'queued', 'profile': pkey})


@bp.route('/api/liscr/master')
@admin_required
def api_liscr_master():
    """화면이 쓰는 마스터 스냅샷(등록 유형·선박·Vendor·Expense·통화).

    러너가 밀어준 것 그대로다. 이 서버는 SVMS 를 못 부르므로 여기 없는 값은 화면에도 없다.
    """
    out, oldest = {}, None
    for kind in _MASTER_KINDS:
        ts, rows = _master(kind)
        out[kind] = rows
        if ts and (oldest is None or ts < oldest):
            oldest = ts
    out['profiles'] = list(_profile_registry().values())
    out['updated_at'] = oldest
    return jsonify(out)


@bp.route('/api/liscr/jobs/<int:jid>/pdf')
@admin_required
def api_liscr_pdf(jid):
    p = _liscr_pdf_path(jid)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype='application/pdf', as_attachment=False,
                     download_name='liscr_%d.pdf' % jid, conditional=True)


@bp.route('/api/liscr/jobs/<int:jid>/approve', methods=['POST'])
@admin_required
def api_liscr_approve(jid):
    """사람 승인 = SVMS 생성 허가. 화면에서 고친 값이 있으면 여기서 헤더/라인에 반영한다."""
    row = query("SELECT * FROM liscr_job WHERE id=?", (jid,), one=True)
    if not row:
        abort(404)
    if row['status'] != 'parsed':
        # 보류 건이면 **왜** 보류인지까지 답한다. 상태만 돌려주면 형이 카드를 다시 열어
        # 사유를 찾아야 하고, 승인이 왜 막혔는지 화면에서 바로 안 보인다.
        why = (json.loads(row['hard_json'] or '[]') or json.loads(row['reasons'] or '[]')
               ) if row['status'] == 'hold' else []
        return jsonify({'error': "승인 가능한 상태가 아님 (현재 %s)%s"
                                 % (row['status'], (' — %s' % '; '.join(why)) if why else '')}), 409
    # 🔴 HOLD 는 사람이 값을 채워서 풀리는 종류가 아니다(번호중복·마스터 조회실패 등).
    #    FIX 는 "못 읽었으니 채워라" 라서 채우면 승인된다 — 이 둘을 한 코드로 뭉치면
    #    중복 인보이스가 사람 손으로 통과된다.
    if (row['gate'] or '') not in ('READY', 'FIX'):
        return jsonify({'error': 'HOLD 상태는 승인할 수 없음 (사유: %s)'
                                 % '; '.join(json.loads(row['hard_json'] or '[]')
                                             or json.loads(row['reasons'] or '[]'))}), 409

    d = request.get_json(silent=True) or {}
    header = json.loads(row['header_json'] or '{}')
    lines = json.loads(row['lines_json'] or '[]')
    if not lines:
        return jsonify({'error': '명세 라인이 없어 승인할 수 없음 — 삭제 후 다시 올릴 것'}), 409

    pkey = row['profile'] or 'liscr'
    locked = set((_profile_registry().get(pkey) or {}).get('locked') or ())
    editable = dict(_EDITABLE)
    if pkey in _EDITABLE_VESSEL_PROFILES:
        editable['vsl_cd'] = ('vsl_cd', 'VSL_CD')
    # 🔴 통화가 고정이 아닌 프리셋에서는 카드에서 고칠 수 있어야 한다. 자동판독이 실패하면
    #    CUR_CD 가 비는데(=FIX), 여기서 못 고치면 그 건은 삭제 후 재업로드 말고 길이 없다.
    #    고정 프리셋(기국)에서는 절대 열지 않는다 — 고정의 의미가 사라진다.
    if 'currency' not in locked:
        editable['cur_cd'] = ('cur_cd', 'CUR_CD')
    # 🔴 Vendor·Expense 도 잠기지 않은 프리셋에서만 연다(2026-08-19 형 지시 — 벤더가 섞인
    #    묶음을 한 번에 올리려면 카드마다 골라야 한다). 고정 프리셋에서는 절대 열지 않는다.
    #    Expense 는 헤더가 아니라 **라인** 값이라 헤더키가 없다(아래에서 lines 에 실어준다).
    if 'vendor' not in locked:
        editable['vndr_cd'] = ('vndr_cd', 'VNDR_CD')
    if 'expense' not in locked:
        editable['exp_cd'] = ('exp_cd', None)

    edits, cols = {}, {}
    for key, (col, hkey) in editable.items():
        if key not in d:
            continue
        val = d[key]
        # 🔴 빈칸은 "안 고침" 이 아니라 **비움**이다(2026-08-19 올마이트 지적, 금전 경로).
        #    화면에서 Vendor 를 지우고 새로 안 고른 채 승인을 누르면, 예전에는 그 필드가
        #    요청에서 빠져 서버가 **저장된 옛 벤더**로 승인해버렸다 — 카드도 확인창도
        #    비어 보이는데 형이 본 적 없는 벤더로 돈이 나가는 경로다. 여기서 지우고,
        #    아래 완결성 검사(`_missing_labels`)가 "아직 안 채운 값" 으로 되돌려준다.
        if val is None or not str(val).strip():
            val = None
        elif key == 'amt':
            if not _AMT_RE.match(str(val).strip()):
                return jsonify({'error': '금액 형식이 잘못됨 (숫자, 소수 2자리까지)'}), 400
            val = float(val)
            if val <= 0:
                return jsonify({'error': '금액은 0보다 커야 함'}), 400
        else:
            val = str(val).strip()
            if key == 'cur_cd':
                # 형식 + 마스터 실재. 'AUTO' 나 오타가 그대로 SVMS 통화칸에 실리면
                # 금액의 의미가 바뀐다(업로드 때와 같은 검사를 승인에서도 한다).
                val, cerr = _check_currency(val)
                if cerr:
                    return jsonify({'error': cerr}), 400
            elif key in _DATE_LABEL:
                val, derr = _check_date(val, _DATE_LABEL[key])
                if derr:
                    return jsonify({'error': derr}), 400
        # 빈 값끼리(None ↔ '')는 같은 것으로 본다 — 비어 있던 칸을 비운 채로 둔 것은
        # '수정' 이 아니다. 아니면 승인할 때마다 빈 수정이력이 쌓이고, "벤더가 바뀌었으니
        # Remit 을 다시 받으라" 같은 후속 처리가 바뀐 것도 없는데 헛돈다.
        if val == row[col] or (val is None and row[col] in (None, '')):
            continue
        edits[key] = {'from': row[col], 'to': val}
        cols[col] = val
        if hkey:
            header[hkey] = val

    # 선박을 바꿨으면 이름도 마스터에서 같이 가져온다. 🔴 코드만 믿고 이름을 비워두면
    # 카드에 배 이름이 안 뜨고, 형이 어느 배로 끊는지 못 보고 승인하게 된다.
    # 🔴 비운 경우(`cols['vsl_cd'] is None`)는 마스터를 뒤지지 않는다 — 이름도 같이 지운다.
    #    코드는 비었는데 앞 배 이름이 남아 있으면 카드에 그 배가 계속 떠 있다.
    if cols.get('vsl_cd'):
        _, vessels = _master('vessels')
        hit = next((v for v in vessels if v.get('cd') == cols['vsl_cd']), None)
        if not hit:
            return jsonify({'error': '선박 코드 %s 가 마스터에 없음' % cols['vsl_cd']}), 400
        cols['vsl_nm'] = hit.get('nm')
        header['VSL_NM'] = hit.get('nm')
    elif 'vsl_cd' in cols:
        cols['vsl_nm'] = None
        header['VSL_NM'] = None

    # 벤더를 카드에서 골랐거나 바꿨으면 이름도 마스터에서 같이 가져온다(선박과 같은 이유).
    if cols.get('vndr_cd'):
        _, vendors = _master('vendors')
        hit = next((v for v in vendors if v.get('cd') == cols['vndr_cd']), None)
        if not hit:
            return jsonify({'error': 'Vendor 코드 %s 가 마스터에 없음' % cols['vndr_cd']}), 400
        cols['vndr_nm'] = hit.get('nm')
        header['VNDR_NM'] = hit.get('nm')
        # 🔴 PAY_TERM 은 **벤더 마스터 값**이고, Pay Date 는 거기서 나온다. 벤더가 바뀌면
        #    앞 벤더 기준으로 계산된 PAY_TERM/Pay Date 는 더는 근거가 없다 — 그대로 두면
        #    형이 화면에서 본 적도 없는 공식으로 만들어진 날짜가 SVMS 로 나간다.
        #    서버는 PAY_TERM 을 모르므로(마스터 스냅샷엔 코드·이름뿐) 지어내지 않고 버리고,
        #    Remit 을 이 요청에서 **다시 확인받는다**(러너가 그 날짜에서 PAY_TERM 을 역산한다).
        header.pop('PAY_TERM', None)
        if 'pay_dt' not in d:
            return jsonify({'error': '벤더를 바꾸면 Pay Date(Remit)를 다시 확인해야 함 '
                                     '— 벤더마다 결제조건이 달라 앞 벤더 기준 날짜는 쓸 수 없음'}), 400
    elif 'vndr_cd' in cols:
        # 벤더를 지운 경우. 이름·PAY_TERM 도 같이 근거를 잃는다. 승인 자체는 아래
        # 완결성 검사가 'Vendor' 로 막지만, 여기서 안 지우면 옛 이름이 카드에 남는다.
        cols['vndr_nm'] = None
        header['VNDR_NM'] = None
        header.pop('PAY_TERM', None)

    # Expense 는 헤더가 아니라 Invoice List 행에 실린다. 우리 라인은 전부 같은 Expense 라
    # (러너 `build_lines`) 행 전체에 같은 값을 박는다 — 한 행만 바꾸면 나머지가 옛 코드로 남는다.
    if cols.get('exp_cd'):
        _, expenses = _master('expenses')
        hit = next((x for x in expenses if x.get('cd') == cols['exp_cd']), None)
        if not hit:
            return jsonify({'error': 'Expense 코드 %s 가 마스터에 없음' % cols['exp_cd']}), 400
        cols['exp_nm'] = hit.get('nm')
        for ln in lines:
            ln['EXP_CD'] = cols['exp_cd']
            ln['EXP_NM'] = hit.get('nm')
    elif 'exp_cd' in cols:
        cols['exp_nm'] = None
        for ln in lines:
            ln['EXP_CD'] = None
            ln['EXP_NM'] = None

    if 'subject' in cols and lines:
        lines[0]['SUBJ'] = cols['subject']
    # 금액을 **비운** 경우는 라인을 건드리지 않는다 — 아래 완결성 검사가 '금액' 으로 막는다.
    # (여기서 2줄 이상 거부에 걸리면 "라인이 2줄이라 못 고친다" 는 엉뚱한 사유가 나간다.)
    if cols.get('amt') is not None:
        # 금액을 고쳤는데 라인이 1줄이면 라인 금액도 같이 맞춘다. 2줄 이상이면 어느 줄을
        # 고쳐야 하는지 알 수 없으므로 건드리지 않고 거부한다(헤더-라인 합 불일치 방지).
        if len(lines) == 1:
            lines[0]['AMT'] = cols['amt']
        else:
            return jsonify({'error': '라인이 2줄 이상이라 금액 수정은 SVMS 화면에서 해야 함'}), 400

    # 🔴 여기까지 와서야 "다 채워졌나" 를 본다. FIX 카드는 값이 비어 있는 게 정상이고,
    #    비운 채로 승인되면 러너가 SVMS 앞에서 터진다(그때는 이미 사람 손을 떠났다).
    #    빈 라인 적요도 같이 본다 — 헤더만 보면 적요 없는 인보이스가 통과한다.
    empty = _missing_labels(header, lines)
    if empty:
        return jsonify({'error': '아직 안 채운 값이 있음: %s' % ', '.join(empty),
                        'missing': empty}), 400

    # 컬럼 목록은 고정 SQL 로 적는다(수정 안 된 필드는 기존 값을 그대로 다시 쓴다).
    # 조립식 SET 절이면 편집 가능한 컬럼이 요청 내용에 따라 달라져 검토가 어려워진다.
    rc = execute_rc(
        "UPDATE liscr_job SET inv_no=?, inv_dt=?, amt=?, pay_dt=?, subject=?, "
        "sup_user_id=?, inv_user_id=?, vsl_cd=?, vsl_nm=?, cur_cd=?, "
        "vndr_cd=?, vndr_nm=?, exp_cd=?, exp_nm=?, "
        "header_json=?, lines_json=?, edited_json=?, "
        "status='approved', decided_at=datetime('now','localtime'), decided_by=? "
        "WHERE id=? AND status='parsed'",
        (cols.get('inv_no', row['inv_no']), cols.get('inv_dt', row['inv_dt']),
         cols.get('amt', row['amt']), cols.get('pay_dt', row['pay_dt']),
         cols.get('subject', row['subject']), cols.get('sup_user_id', row['sup_user_id']),
         cols.get('inv_user_id', row['inv_user_id']),
         cols.get('vsl_cd', row['vsl_cd']), cols.get('vsl_nm', row['vsl_nm']),
         cols.get('cur_cd', row['cur_cd']),
         # 🔴 카드에서 고른 Vendor/Expense 는 **컬럼에도** 적는다. header_json 에만 남기면
         #    카드 화면(=컬럼을 읽는다)은 계속 빈칸으로 보이고, 러너 재파싱이 옛 값으로
         #    되돌린다 — 형이 고른 값이 조용히 사라지는 경로다.
         cols.get('vndr_cd', row['vndr_cd']), cols.get('vndr_nm', row['vndr_nm']),
         cols.get('exp_cd', row['exp_cd']), cols.get('exp_nm', row['exp_nm']),
         json.dumps(header, ensure_ascii=False), json.dumps(lines, ensure_ascii=False),
         json.dumps(edits, ensure_ascii=False) if edits else None,
         _actor(), jid))
    if not rc:
        return jsonify({'error': '다른 처리와 겹쳐 승인되지 않음'}), 409
    return jsonify({'id': jid, 'status': 'approved', 'edited': list(edits)})


def _actor():
    """결재 흔적에 남길 사람 이름.

    🔴 세션 키는 `username` 이다(`routes_core.py` 로그인). 여기서 `user` 를 읽고 있었는데
       그런 키는 없어서, 누가 승인/취소했든 `decided_by` 에 항상 'admin' 이 박혔다 =
       돈경로 감사 흔적이 통째로 무의미했다(2026-08-19 실측, 일괄승인 테스트에서 발견).
    """
    return session.get('username') or 'admin'


def _approve_blockers(row):
    """저장된 값만으로 승인 가능한가 → 막는 사유 목록(비면 승인 가능).

    🔴 값 판정은 단건 승인과 **같은 함수**(`_missing_labels`)를 부른다. 일괄이 더 느슨하면
       한 장씩 눌렀을 때 막히던 건이 일괄로는 통과한다 — 그게 제일 위험한 종류의 차이다.
       단건과 다른 점은 딱 하나, 일괄은 화면에서 고친 값을 받지 않는다(저장된 값 그대로).
    🔴 `header_json`/`lines_json` 이 깨져 있어도 여기서 예외를 내면 안 된다. 배치 도중
       500 이 나면 앞의 몇 건은 이미 승인된 채 화면은 실패로 보인다 = 형이 무엇이
       승인됐는지 모르게 된다. 깨진 건은 '읽을 수 없음' 으로 건너뛴다.
    """
    if row['status'] != 'parsed':
        return ['승인 가능한 상태가 아님 (현재 %s)' % row['status']]
    if (row['gate'] or '') not in ('READY', 'FIX'):
        return ['HOLD 는 승인 불가']
    try:
        header = json.loads(row['header_json'] or '{}')
        lines = json.loads(row['lines_json'] or '[]')
    except ValueError:
        return ['저장된 파싱 결과를 읽을 수 없음 — 다시 읽기 후 확인할 것']
    if not isinstance(header, dict) or not isinstance(lines, list):
        return ['저장된 파싱 결과 형식이 잘못됨 — 다시 읽기 후 확인할 것']
    if not lines:
        return ['명세 라인이 없음']
    empty = _missing_labels(header, lines)
    return ['안 채운 값: %s' % ', '.join(empty)] if empty else []


def _bulk_items(d):
    """요청 본문 → [(jid, inv_no, amt)]. 형식이 틀리면 (None, 사유).

    화면은 **자기가 보여준 값**(Invoice No·금액)을 같이 보낸다. 그 값이 지금 DB 와 다르면
    승인하지 않는다 → 아래 UPDATE 의 WHERE 참조.
    """
    items = d.get('items')
    if not isinstance(items, list) or not items:
        return None, '승인할 건을 지정해야 함 (화면을 새로고침한 뒤 다시 시도)'
    if len(items) > 100:
        return None, '한 번에 100건까지'
    out = []
    for it in items:
        if not isinstance(it, dict):
            return None, 'items 는 {id, inv_no, amt} 목록'
        jid = it.get('id')
        # 🔴 `int()` 로 넘기면 1.9→1, True→1 이 조용히 통과한다. 금전 경로라 형태를 그대로 본다.
        if not isinstance(jid, int) or isinstance(jid, bool):
            return None, 'id 는 정수여야 함'
        amt = it.get('amt')
        if amt is not None and (isinstance(amt, bool) or not isinstance(amt, (int, float))):
            return None, 'amt 는 숫자여야 함'
        inv_no = it.get('inv_no')
        if inv_no is not None and not isinstance(inv_no, str):
            return None, 'inv_no 는 문자열이어야 함'
        out.append((jid, inv_no, float(amt) if amt is not None else None))
    return out, None


@bp.route('/api/liscr/jobs/approve-bulk', methods=['POST'])
@admin_required
def api_liscr_approve_bulk():
    """체크한 건 일괄 승인 = SVMS 생성 허가를 여러 장 한 번에.

    🔴 화면이 본 건만, 화면이 본 내용 그대로 승인한다. id 만 받으면 안 된다 —
       [다시 읽기]/러너 재파싱으로 같은 id 의 **금액·Invoice No 가 바뀔 수 있고**,
       그러면 형이 확인한 1,490 대신 바뀐 값이 승인된다(TOCTOU). 그래서 화면이 보여준
       `inv_no`·`amt` 를 같이 받아 UPDATE 의 WHERE 에 넣는다. 어긋나면 승인하지 않고
       "화면과 달라졌다" 고 돌려준다.
    🔴 값은 **고치지 않는다.** 일괄 버튼은 "이미 확인한 것들을 한꺼번에 통과시키는" 용도지
       빈 칸을 대신 채워주는 용도가 아니다. 덜 채워진 건은 승인하지 않고 사유를 돌려준다.
    🔴 상태 게이트도 UPDATE 의 WHERE 안에 있다(`status='parsed'`). 이미 approved/creating
       인 건이 다시 승인되면 인보이스가 두 번 만들어진다 = 이중집행.
    """
    items, err = _bulk_items(request.get_json(silent=True) or {})
    if err:
        return jsonify({'error': err}), 400

    who = _actor()
    approved, skipped = [], []
    seen = set()
    for jid, inv_no, amt in items:
        if jid in seen:                     # 같은 id 가 두 번 와도 한 번만 본다
            continue
        seen.add(jid)
        row = query("SELECT * FROM liscr_job WHERE id=?", (jid,), one=True)
        if not row:
            skipped.append({'id': jid, 'reason': '없는 건'})
            continue
        why = _approve_blockers(row)
        if why:
            skipped.append({'id': jid, 'reason': '; '.join(why)})
            continue
        # `IS` 는 SQLite 의 NULL 안전 비교다(`=` 는 NULL 이 끼면 무조건 거짓이라
        # 값이 비어 있던 카드를 영영 승인 못 하게 된다).
        rc = execute_rc(
            "UPDATE liscr_job SET status='approved', decided_at=datetime('now','localtime'), "
            "decided_by=? WHERE id=? AND status='parsed' AND inv_no IS ? AND amt IS ?",
            (who, jid, inv_no, amt))
        if rc:
            approved.append(jid)
        else:
            cur = query("SELECT status, inv_no, amt FROM liscr_job WHERE id=?", (jid,), one=True)
            if cur and cur['status'] == 'parsed':
                skipped.append({'id': jid, 'reason':
                                '화면에 뜬 내용과 달라짐(지금 %s / %s) — 새로고침 후 확인할 것'
                                % (cur['inv_no'], cur['amt'])})
            else:
                skipped.append({'id': jid, 'reason': '다른 처리와 겹쳐 승인되지 않음'})
    return jsonify({'approved': approved, 'skipped': skipped, 'n': len(approved)})


@bp.route('/api/liscr/jobs/<int:jid>/reparse', methods=['POST'])
@admin_required
def api_liscr_reparse(jid):
    """올려둔 PDF 를 그대로 다시 파싱한다(삭제 후 재업로드 대신).

    파서를 고치고 나면 이미 HOLD 로 앉은 카드들이 그 개선을 못 받는다. 지웠다 다시 올리면
    되지만, 그러면 형이 화면에서 파일을 다시 찾아야 하고 원본 파일명도 바뀐다.

    🔴 되돌릴 수 있는 상태는 `hold`·`parsed` 뿐이다.
       · `approved`/`creating` — 러너가 잡고 있거나 곧 SVMS 에 쓴다.
       · `created`/`failed`   — 이미 SVMS 를 건드린 뒤다. 다시 파싱해 승인하면 같은
                                 인보이스를 두 번 만들 수 있다(failed 도 저장까지는 됐을 수
                                 있어 INV_CD 가 남아 있다).
    """
    row = query("SELECT id, status FROM liscr_job WHERE id=?", (jid,), one=True)
    if not row:
        return jsonify({'error': '없는 건'}), 404
    if not os.path.exists(_liscr_pdf_path(jid)):
        return jsonify({'error': '업로드 PDF 가 없어 다시 파싱할 수 없음'}), 409
    rc = execute_rc(
        "UPDATE liscr_job SET status='queued', gate=NULL, reasons=NULL, hard_json=NULL, "
        "error=NULL, claim_token=NULL, claimed_at=NULL, decided_at=NULL, decided_by=NULL "
        "WHERE id=? AND status IN ('hold','parsed')", (jid,))
    if not rc:
        return jsonify({'error': '다시 파싱할 수 있는 상태가 아님 (현재 %s)' % row['status']}), 409
    return jsonify({'id': jid, 'status': 'queued'})


@bp.route('/api/liscr/jobs/<int:jid>/reject', methods=['POST'])
@admin_required
def api_liscr_reject(jid):
    """사람이 취소. 아직 SVMS에 안 만든 것만 취소할 수 있다."""
    rc = execute_rc("UPDATE liscr_job SET status='rejected', decided_at=datetime('now','localtime'), "
                    "decided_by=? WHERE id=? AND status IN ('parsed','hold','queued')",
                    (_actor(), jid))
    if not rc:
        return jsonify({'error': '취소 가능한 상태가 아님'}), 409
    return jsonify({'id': jid, 'status': 'rejected'})


@bp.route('/api/liscr/jobs/<int:jid>', methods=['DELETE'])
@admin_required
def api_liscr_delete(jid):
    """행 삭제(= 목록에서 치우기). SVMS 인보이스는 지우지 않는다 — 여기 큐 행만 지운다.

    끝난 건은 전부 지울 수 있다. 러너가 잡고 있거나 곧 잡을 상태만 막는다.

    🔴 한때 `inv_cd IS NULL` 조건으로 created 삭제를 막아뒀는데, 실측으로 근거가 틀렸다.
       "행의 sha256 이 재업로드를 막는 유일한 표식" 이라는 게 전제였지만, 진짜 방어선은
       SVMS 자체 중복검사(`PKG_CO.SP_GET_CHK_INV_NO`)다. 행을 지우고 같은 PDF 를 다시
       올려도 파싱 단계에서 `HOLD — 인보이스 번호 중복(<INV_CD>, <INV_NO>)` 로 막힌다
       (2026-08-19 실제 생성건 3개로 확인). sha256 표식은 큐 안 중복을 걸러주는 1차선일
       뿐이고, 그 역할은 아직 처리중인 행들이 계속 한다.
    """
    # 없는 번호를 "처리중이라 못 지움"으로 답하면 원인을 못 가린다 — 404 로 갈라준다.
    if not query("SELECT id FROM liscr_job WHERE id=?", (jid,), one=True):
        return jsonify({'error': '없는 건'}), 404
    rc = execute_rc("DELETE FROM liscr_job WHERE id=? AND status IN "
                    "('created','failed','rejected','hold','parsed')", (jid,))
    if not rc:
        return jsonify({'error': '러너가 처리중인 건은 삭제할 수 없음 '
                                 '(대기·읽는 중·승인됨·SVMS 생성 중)'}), 409
    return jsonify({'id': jid, 'deleted': True, 'pdf_removed': _remove_pdf(jid)})


@bp.route('/api/liscr/jobs/clear-done', methods=['POST'])
@admin_required
def api_liscr_clear_done():
    """끝난 건 일괄 삭제 — 생성 완료/취소된 카드만. 목록 청소용.

    🔴 `failed`·`hold`·`parsed` 는 일부러 뺐다. 실패는 사람이 사유를 보고 손봐야 할
       건이고, hold/parsed 는 아직 형이 판단을 안 한 건이다. 한 번에 쓸어버리는 버튼이
       그것들까지 먹으면 "확인해야 할 것" 이 조용히 사라진다. 그 둘은 카드별 [삭제]로.
    """
    rows = query("SELECT id FROM liscr_job WHERE status IN ('created','rejected')")
    ids = [r['id'] for r in rows]
    if not ids:
        return jsonify({'deleted': 0})
    n, stuck = 0, []
    for jid in ids:
        if execute_rc("DELETE FROM liscr_job WHERE id=? AND status IN ('created','rejected')",
                      (jid,)):
            n += 1
            if not _remove_pdf(jid):
                stuck.append(jid)
    return jsonify({'deleted': n, 'pdf_failed': stuck})


# ───────────────────────────── 맥 러너용 API ─────────────────────────────

@bp.route('/api/ext/liscr/pending')
@api_key_required
def api_ext_liscr_pending():
    """러너: 단계별 대기 건수(읽기 전용).

    🔴 이게 있어야 러너가 **claim 하기 전에** SVMS 로그인을 끝낼 수 있다. 로그인을 뒤로
       미루면 create 를 claim 해놓고 로그인에서 죽는 회차가 생기는데, create 단계는
       중복생성을 막으려고 lease 회수를 일부러 안 넣었으므로 그 잡이 'SVMS 생성 중'으로
       영구히 굳는다. 큐가 빌 땐 매 회차 로그인하지 않으려는 목적도 겸한다.
    """
    p = query("SELECT COUNT(*) c FROM liscr_job WHERE status='queued' OR (status='parsing' "
              "AND (claimed_at IS NULL OR claimed_at < datetime('now','localtime',?)))",
              ('-%d seconds' % LISCR_PARSE_LEASE_SEC,), one=True)
    c = query("SELECT COUNT(*) c FROM liscr_job WHERE status='approved'", one=True)
    return jsonify({'parse': p['c'], 'create': c['c']})


@bp.route('/api/ext/liscr/claim', methods=['POST'])
@api_key_required
def api_ext_liscr_claim():
    """러너: 처리할 잡 1건 claim.

    stage="parse"  → queued(또는 lease 만료된 parsing) 1건 → parsing
    stage="create" → approved 1건 → creating   (사람이 승인한 것만. lease 회수 없음)
    """
    stage = ((request.get_json(silent=True) or {}).get('stage') or '').strip()
    if stage == 'parse':
        row = query("SELECT id, status, claim_token FROM liscr_job WHERE status='queued' "
                    "OR (status='parsing' AND (claimed_at IS NULL OR claimed_at < "
                    "datetime('now','localtime',?))) ORDER BY id ASC LIMIT 1",
                    ('-%d seconds' % LISCR_PARSE_LEASE_SEC,), one=True)
        nxt = 'parsing'
    elif stage == 'create':
        row = query("SELECT id, status, claim_token FROM liscr_job WHERE status='approved' "
                    "ORDER BY id ASC LIMIT 1", one=True)
        nxt = 'creating'
    else:
        return jsonify({'error': 'stage 는 parse 또는 create'}), 400
    if not row:
        return jsonify({'job': None})

    token = uuid.uuid4().hex
    # CAS — 두 러너가 동시에 붙어도 한쪽만 이긴다. 토큰 비교까지 넣어 회수 경합도 막는다.
    rc = execute_rc("UPDATE liscr_job SET status=?, claim_token=?, "
                    "claimed_at=datetime('now','localtime') WHERE id=? AND status=? "
                    "AND ((claim_token IS ?) OR (claim_token = ?))",
                    (nxt, token, row['id'], row['status'], row['claim_token'], row['claim_token']))
    if not rc:
        return jsonify({'job': None})
    r = query("SELECT * FROM liscr_job WHERE id=?", (row['id'],), one=True)
    job = _job_dict(r)
    job['claim_token'] = token
    return jsonify({'job': job})


@bp.route('/api/ext/liscr/jobs/<int:jid>/pdf')
@api_key_required
def api_ext_liscr_pdf(jid):
    """러너: 원본 PDF 다운로드(파싱 + SVMS 첨부용)."""
    p = _liscr_pdf_path(jid)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype='application/pdf', as_attachment=True,
                     download_name='liscr_%d.pdf' % jid, conditional=True)


@bp.route('/api/ext/liscr/jobs/<int:jid>/parsed', methods=['POST'])
@api_key_required
def api_ext_liscr_parsed(jid):
    """러너: 파싱 결과 적재.

    gate=READY 사람 승인 대기(parsed)
    gate=FIX   사람이 못 읽은 값을 채우면 승인 가능 → 같은 대기줄(parsed)에 세운다
    gate=HOLD  보류(hold). 승인 버튼 자체가 막힌다
    """
    d = request.get_json(silent=True) or {}
    token = (d.get('claim_token') or '').strip()
    gate = d.get('gate') if d.get('gate') in ('READY', 'FIX') else 'HOLD'
    header = d.get('header') or {}
    lines = d.get('lines') or []
    # READY/FIX = "사람 손만 거치면 SVMS 에 그대로 쓴다"는 뜻이다. 라인이 없으면 승인 화면에
    # 금액/적요가 안 뜨고 생성 단계에서야 터진다 — 여기서 HOLD 로 떨어뜨리는 게 맞다.
    if gate != 'HOLD' and (not header or not lines):
        return jsonify({'error': '%s 인데 header 또는 lines 가 비었음' % gate}), 400
    line0 = lines[0] if lines else {}
    rc = execute_rc(
        "UPDATE liscr_job SET status=?, gate=?, reasons=?, hard_json=?, vsl_cd=?, vsl_nm=?, "
        "inv_no=?, inv_dt=?, cur_cd=?, amt=?, pay_dt=?, vndr_cd=?, vndr_nm=?, exp_cd=?, exp_nm=?, "
        "subject=?, sup_user_id=?, sup_user_nm=?, "
        "inv_user_id=?, oversea_tp=?, header_json=?, lines_json=?, parsed_json=?, error=NULL "
        "WHERE id=? AND status='parsing' AND claim_token=?",
        ('hold' if gate == 'HOLD' else 'parsed', gate,
         json.dumps(d.get('reasons') or [], ensure_ascii=False),
         json.dumps(d.get('hard') or [], ensure_ascii=False),
         header.get('VSL_CD'), header.get('VSL_NM'), header.get('INV_NO'), header.get('INV_DT'),
         header.get('CUR_CD'), header.get('AMT'), header.get('PAY_DT'),
         header.get('VNDR_CD'), header.get('VNDR_NM'),
         line0.get('EXP_CD'), line0.get('EXP_NM'), line0.get('SUBJ'),
         header.get('SUP_USER_ID'), header.get('SUP_USER_NM'), header.get('INV_USER_ID'),
         header.get('OVERSEA_TP'),
         json.dumps(header, ensure_ascii=False), json.dumps(lines, ensure_ascii=False),
         json.dumps(d.get('parsed') or {}, ensure_ascii=False), jid, token))
    if not rc:
        return jsonify({'error': 'claim 불일치 또는 상태 변경됨'}), 409
    return jsonify({'id': jid, 'gate': gate})


@bp.route('/api/ext/liscr/master', methods=['POST'])
@api_key_required
def api_ext_liscr_master():
    """러너: SVMS 마스터 스냅샷 적재(선박·Expense·Vendor·통화·등록유형).

    🔴 **빈 목록은 받지 않는다.** SVMS 조회가 반쯤 실패한 회차가 멀쩡한 스냅샷을 0건으로
       덮어쓰면, 화면에서 고를 게 사라져 기능이 조용히 죽는다(원인은 아무데도 안 남는다).
       빈 종류는 건너뛰고 어느 것을 건너뛰었는지 응답에 적어 러너 로그에 남긴다.
    """
    d = request.get_json(silent=True) or {}
    saved, skipped = {}, []
    for kind in _MASTER_KINDS:
        rows = d.get(kind)
        if not isinstance(rows, list) or not rows:
            skipped.append(kind)
            continue
        execute("INSERT INTO liscr_master (kind, payload, n, updated_at) "
                "VALUES (?,?,?,datetime('now','localtime')) "
                "ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload, n=excluded.n, "
                "updated_at=excluded.updated_at",
                (kind, json.dumps(rows, ensure_ascii=False), len(rows)))
        saved[kind] = len(rows)
    return jsonify({'saved': saved, 'skipped': skipped})


@bp.route('/api/ext/liscr/master/age')
@api_key_required
def api_ext_liscr_master_age():
    """러너: 마스터가 얼마나 오래됐나(초). 매 회차 무겁게 다시 뜨지 않으려는 용도.

    한 종류라도 없으면 age=None → 러너가 무조건 새로 뜬다.
    """
    rows = query("SELECT kind, updated_at, (julianday('now','localtime') - "
                 "julianday(updated_at)) * 86400 AS age FROM liscr_master")
    got = {r['kind']: r['age'] for r in rows}
    if any(k not in got for k in _MASTER_KINDS):
        return jsonify({'age': None, 'have': sorted(got)})
    return jsonify({'age': max(got.values()), 'have': sorted(got)})


@bp.route('/api/ext/liscr/jobs/<int:jid>/result', methods=['POST'])
@api_key_required
def api_ext_liscr_result(jid):
    """러너: SVMS 생성 결과 반영.

    🔴 inv_cd 가 왔으면 ok 여부와 무관하게 반드시 기록한다. 저장은 됐는데 상신에서
       실패한 경우 inv_cd 를 버리면 SVMS에 주인 없는 인보이스가 남고, 사람이 그걸
       찾을 단서가 사라진다.
    """
    d = request.get_json(silent=True) or {}
    token = (d.get('claim_token') or '').strip()
    inv_cd = (d.get('inv_cd') or '').strip() or None
    ok = bool(d.get('ok'))
    err = (d.get('error') or '알 수 없는 실패')
    # 🔴 ok=true 인데 INV_CD 가 없으면 '생성 완료'로 찍지 않는다. 화면상 초록으로 끝나버리면
    #    아무도 SVMS 를 확인하지 않는데, 실제로는 인보이스가 생겼는지조차 모르는 상태다.
    if ok and not inv_cd:
        ok, err = False, 'SVMS 가 INV_CD 를 주지 않음 — 생성 여부 직접 확인 필요'
    rc = execute_rc("UPDATE liscr_job SET status=?, inv_cd=?, error=?, "
                    "done_at=datetime('now','localtime') WHERE id=? AND status='creating' "
                    "AND claim_token=?",
                    ('created' if ok else 'failed', inv_cd,
                     None if ok else err[:2000], jid, token))
    if not rc:
        return jsonify({'error': 'claim 불일치 또는 상태 변경됨'}), 409
    return jsonify({'id': jid, 'ok': ok, 'inv_cd': inv_cd})
