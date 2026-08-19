#!/usr/bin/env python3
"""일회성 그룹 키 병합 — `vsl_nm` 표기가 갈라진 선박을 하나로 접는다.

⚠️ 왜 일회성 스크립트인가: `vsl_nm` 은 여러 테이블이 **문자열로** 참조하는 그룹 키(FK 아님)다.
   이름 재매핑은 그 전부를 UNIQUE 충돌 없이 옮기는 파괴적 작업이라 `init_db` 부팅 마이그레이션에
   넣지 않는다(같은 코드의 두 엔트리가 서로를 지우는 순환이 성립 — 2026-08-15 올마이트 지적).
   재발 예방은 `routes_repair_request._reserve_rows` 의 표기 재사용이 담당한다.

설계 원칙 = **fail-closed**. 아래 중 하나라도 걸리면 아무것도 바꾸지 않는다.
  · 참조 테이블 목록을 스키마에서 파생하고(`vsl_nm` 컬럼 보유 테이블 전수) 하드코딩 기대목록과
    교차검증 — 미등재 테이블이 있으면 중단(fail-open 금지)
  · UNIQUE 동반 컬럼도 스키마(`PRAGMA index_list/index_info`)에서 파생 — 나중에 UNIQUE 가 추가돼도 잡힌다
  · 대상 테이블에 trigger 가 있으면 중단(부수효과를 예측할 수 없다)
  · 두 엔트리의 메타가 충돌하거나 출처에만 값이 있으면 중단(`--allow-meta-loss` 로만 강행)
  · 예상 이동 행수(`--expect-rows`)를 인자로 못박아야 실행된다 — 우발 실행·드리프트 방지
  · 백업 → `BEGIN IMMEDIATE` → preflight 재수행 → UPDATE(rowcount 검사) → **COMMIT 전** 잔여검사
    → 이상 있으면 rollback

    python3 deploy/oneoff/merge_vsl_nm.py --db instance/trmt.db \
        --from 'Belgium B' --to 'BELGIUM B' --expect-rows 2
    ... 위 dry-run 이 예상과 같으면 `--apply` 추가.
"""
import argparse
import os
import sqlite3
import sys
import time

# `vsl_nm` 을 그룹 키로 갖는다고 **알고 있는** 테이블. 실제 목록은 스키마에서 파생하며, 이 집합은
# 교차검증용이다 — 스키마에 이보다 많으면(새 테이블이 생겼으면) 중단한다.
KNOWN_REFS = {
    'aor_draft', 'dock_inquiry_draft', 'dock_procure', 'dock_submit_draft', 'dock_yard',
    'fundreq_draft', 'invoice_draft', 'liscr_job', 'repair_request', 'reqgen_draft',
    'soa_review_case',
}
OWNER = 'dock_procure_vessel'          # PK = vsl_nm. 대상 행이 이미 있으면 출처 행은 삭제한다.
META_SKIP = {'vsl_nm', 'updated_at', 'origin'}   # origin=출처 태그라 shim 과 함께 사라지는 게 정상


def die(msg, code=2):
    print(f'⛔ {msg}', file=sys.stderr)
    sys.exit(code)


def tables_with_vsl_nm(db):
    """`vsl_nm` 컬럼을 가진 테이블 전수 — 하드코딩이 아니라 실제 스키마에서 파생한다."""
    out = []
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if any(r[1] == 'vsl_nm' for r in db.execute(f'PRAGMA table_info("{t}")')):
            out.append(t)
    return out


def unique_partners(db, tab):
    """`vsl_nm` 이 포함된 UNIQUE 인덱스의 나머지 컬럼들 — 충돌검사 대상을 스키마에서 도출한다."""
    groups = []
    for idx in db.execute(f'PRAGMA index_list("{tab}")'):
        if not idx['unique']:
            continue
        cols = [r['name'] for r in db.execute(f'PRAGMA index_info("{idx["name"]}")')]
        if 'vsl_nm' in cols:
            groups.append([c for c in cols if c != 'vsl_nm'])
    return groups


def preflight(db, src, dst, verbose=False):
    """이동 계획을 계산한다. 부적합하면 die(). (트랜잭션 안에서 한 번 더 호출된다)"""
    refs = tables_with_vsl_nm(db)
    unknown = set(refs) - KNOWN_REFS - {OWNER}
    if unknown:
        die(f'`vsl_nm` 을 가진 미등재 테이블 {sorted(unknown)} — 이 스크립트가 모르는 참조가 있어 중단. '
            f'KNOWN_REFS 를 갱신하고 영향을 검토할 것')
    missing = KNOWN_REFS - set(refs)
    if missing:
        die(f'기대한 참조 테이블 {sorted(missing)} 이 스키마에 없다 — DB 가 예상과 다르므로 중단')
    if OWNER not in refs:
        die(f'{OWNER} 이 없다')

    o_dst = db.execute(f'SELECT * FROM {OWNER} WHERE vsl_nm=?', (dst,)).fetchone()
    o_src = db.execute(f'SELECT * FROM {OWNER} WHERE vsl_nm=?', (src,)).fetchone()
    if not o_src:
        die(f'{OWNER} 에 {src!r} 행이 없다 — 이미 정리됐거나 이름이 틀렸다')
    if not o_dst:
        die(f'{OWNER} 에 정본 {dst!r} 행이 없다 — 이 스크립트는 병합만 하고 개명은 하지 않는다')
    sc, dc = (o_src['vsl_cd'] or '').strip(), (o_dst['vsl_cd'] or '').strip()
    if not sc or not dc:
        die(f'vsl_cd 가 비어 있다({sc!r}/{dc!r}) — 같은 배임을 확인할 수 없어 중단')
    if sc.upper() != dc.upper():
        die(f'vsl_cd 가 다르다({sc!r} vs {dc!r}) — 다른 배일 수 있어 중단')

    # 메타 충돌: 출처에만 값이 있거나(병합 시 소실), 양쪽 값이 서로 다르면 사람 판단이 필요하다.
    loss, clash = [], []
    for k in o_src.keys():
        if k in META_SKIP or k == 'vsl_cd':
            continue
        sv, dv = (o_src[k] or ''), (o_dst[k] or '')
        if sv and not dv:
            loss.append(k)
        elif sv and dv and sv != dv:
            clash.append(f'{k}({sv!r} vs {dv!r})')

    plan, conflicts = [], []
    for tab in refs:
        if tab == OWNER:
            continue
        rows = db.execute(f'SELECT rowid AS _rid,* FROM "{tab}" WHERE vsl_nm=?', (src,)).fetchall()
        if not rows:
            continue
        groups = unique_partners(db, tab)
        for r in rows:
            bad = None
            for cols in groups:
                # `IS ?` 는 NULL==NULL 을 매치시킨다 — SQLite UNIQUE 는 NULL 을 충돌로 보지 않으므로
                # 이건 **보수적 오탐**(중단) 쪽으로 기운다. 조용히 통과시키는 것보다 안전하다.
                where = ' AND '.join(f'"{c}" IS ?' for c in cols)
                hit = db.execute(f'SELECT rowid FROM "{tab}" WHERE vsl_nm=? AND {where}',
                                 (dst, *[r[c] for c in cols])).fetchone()
                if hit:
                    bad = f'{tab} rowid={r["_rid"]} {({c: r[c] for c in cols})} → {dst!r} 에 이미 존재'
                    break
            if bad:
                conflicts.append(bad)
            else:
                plan.append((tab, r['_rid']))
        if verbose:
            print(f'  {tab:20} {len(rows)}행 (UNIQUE 검사 {groups or "없음"})')

    # trigger 검사는 **실제로 손대는 테이블**로 한정한다. 참조 테이블 전체로 넓히면 무관한 trigger
    # (예: `trg_aor_draft_absorbing` = `BEFORE UPDATE OF status, aor_cd ON aor_draft`, 이동 0행)
    # 때문에 스크립트가 영구히 못 돈다 — 실측으로 확인함(2026-08-15).
    touched = sorted({t for t, _ in plan} | {OWNER})
    trg = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
        f"({','.join('?' * len(touched))})", touched)]
    if trg:
        die(f'손댈 테이블 {touched} 에 trigger {trg} 가 있다 — 부수효과를 예측할 수 없어 중단')
    return plan, conflicts, loss, clash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--from', dest='src', required=True, help='접을 표기(사라진다)')
    ap.add_argument('--to', dest='dst', required=True, help='정본 표기(남는다)')
    ap.add_argument('--expect-rows', type=int, required=True,
                    help='예상 이동 행수. 실측과 다르면 중단(드리프트·우발 실행 방지)')
    ap.add_argument('--apply', action='store_true', help='실제 반영. 없으면 dry-run')
    ap.add_argument('--allow-meta-loss', action='store_true',
                    help='선박 메타가 소실/충돌해도 강행(기본은 중단)')
    a = ap.parse_args()
    if a.src == a.dst:
        die('--from 과 --to 가 같다')
    if not os.path.exists(a.db):
        die(f'DB 가 없다: {a.db}')

    db = sqlite3.connect(a.db, isolation_level=None)   # 트랜잭션을 직접 통제한다
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')

    plan, conflicts, loss, clash = preflight(db, a.src, a.dst, verbose=True)
    if conflicts:
        print('\n⛔ UNIQUE 충돌 — 아무것도 바꾸지 않고 중단(수동 판단 필요):', file=sys.stderr)
        for c in conflicts:
            print('   -', c, file=sys.stderr)
        sys.exit(2)
    if (loss or clash) and not a.allow_meta_loss:
        die(f'선박 메타 소실 {loss} / 충돌 {clash} — 정본에 먼저 반영하거나 --allow-meta-loss 를 명시할 것')
    if len(plan) != a.expect_rows:
        die(f'이동 행수가 예상과 다르다: 실측 {len(plan)} vs --expect-rows {a.expect_rows}')

    print(f'\n총 {len(plan)}행 이동 + {OWNER} {a.src!r} 행 삭제')
    if not a.apply:
        print('dry-run 이므로 아무것도 바꾸지 않았다. 반영하려면 --apply')
        return

    bak = f'{a.db}.premerge-{time.strftime("%Y%m%d%H%M%S")}'
    if os.path.exists(bak):
        die(f'백업 파일이 이미 있다: {bak} — 동시 실행 의심, 중단')
    with sqlite3.connect(bak) as out:      # sqlite 온라인 백업 — 서비스 중이어도 일관 스냅샷
        db.backup(out)
    if sqlite3.connect(bak).execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        die(f'백업 integrity_check 실패: {bak}')
    print(f'백업: {bak} ({os.path.getsize(bak)} bytes, integrity ok)')

    db.execute('BEGIN IMMEDIATE')           # 쓰기 잠금 확보 후 계획을 재계산(TOCTOU 차단)
    try:
        plan2, conflicts2, loss2, clash2 = preflight(db, a.src, a.dst)
        if conflicts2 or (loss2 or clash2) and not a.allow_meta_loss:
            raise RuntimeError(f'잠금 후 재검사에서 상태가 달라졌다: {conflicts2 or (loss2, clash2)}')
        if len(plan2) != a.expect_rows:
            raise RuntimeError(f'잠금 후 이동 행수 변동: {len(plan2)} vs {a.expect_rows}')
        for tab, rowid in plan2:
            # rowid 재사용/동시 write 대비 — `vsl_nm` 까지 조건에 걸고 1행 정확히 바뀌었는지 본다.
            cur = db.execute(f'UPDATE "{tab}" SET vsl_nm=? WHERE rowid=? AND vsl_nm=?',
                             (a.dst, rowid, a.src))
            if cur.rowcount != 1:
                raise RuntimeError(f'{tab} rowid={rowid} UPDATE rowcount={cur.rowcount} (1 이어야 함)')
        cur = db.execute(f'DELETE FROM {OWNER} WHERE vsl_nm=?', (a.src,))
        if cur.rowcount != 1:
            raise RuntimeError(f'{OWNER} DELETE rowcount={cur.rowcount} (1 이어야 함)')

        refs = tables_with_vsl_nm(db)       # postcondition 을 **COMMIT 전에** 검사한다
        left = sum(db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE vsl_nm=?', (a.src,)).fetchone()[0]
                   for t in refs)
        if left:
            raise RuntimeError(f'{a.src!r} 잔여 참조 {left}건 — 커밋하지 않는다')
        if db.execute(f'SELECT COUNT(*) FROM {OWNER} WHERE vsl_nm=?', (a.dst,)).fetchone()[0] != 1:
            raise RuntimeError('정본 엔트리가 1행이 아니다')
        db.execute('COMMIT')
    except Exception as e:
        db.execute('ROLLBACK')
        die(f'{e} → 전량 롤백함. 백업 보존: {bak}', 3)

    print(f"완료 — {a.src!r} 잔여 참조 0건. 백업 {bak}")
    # 백업은 DB 옆에 만든다(경로를 하나만 알면 되므로).
    # 🔴 이 사본은 **어디에 두든 정기백업에 안 들어간다** — `deploy/backup.sh` 의 files tar 는
    #    `--exclude='instance/trmt.db*'` 이고, DB 백업 경로는 `instance/trmt.db` 하나만 뜬다.
    #    즉 이건 로컬 서버에만 존재하는 일회성 사본이다. 검증 끝나면 앱 데이터 디렉토리 밖으로
    #    옮겨 두고(혼동 방지), 오래 보관할 거면 off-host 로 따로 챙길 것.
    print('  ↳ 이 백업은 정기백업 대상이 아니다(backup.sh 가 instance/trmt.db* 를 제외). '
          '검증 후 앱 디렉토리 밖으로 옮기고, 장기보관은 off-host 로 따로 챙길 것')


if __name__ == '__main__':
    main()
