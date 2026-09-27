/* ═══════════════════════════════════════════════════════════════
   InlineEdit — 화면 값 클릭 → 그 자리에서 수정 (모든 탭 공용)
   ───────────────────────────────────────────────────────────────
   2026-09-27 손유석 지시: "모달로 편집하는 부분은 모달 유지 + 화면 클릭으로도 수정".
   모달은 그대로 두고, 표시값에 InlineEdit.bind(...) 만 걸면 클릭 편집이 붙는다.

   InlineEdit.bind(el, {
     kind:  'text' | 'textarea' | 'date' | 'number' | 'select' | 'datetime',
     value: 현재값(원본; 표시 텍스트 아님),
     options: [[value,label], ...]          // select 전용
     placeholder, min, max, step,
     allowEmpty: true(기본) — false면 빈 값 저장 거부
     save: async (newValue) => {...}        // throw 하면 원복 + 오류 토스트
     onDone: () => {...}                    // 저장 성공 후 (재렌더 등)
   })

   키: text/date/number/select = Enter 저장 · Esc 취소 · 바깥 클릭 저장
       textarea = ⌘/Ctrl+Enter 저장 · Esc 취소 · [저장]/[취소] 버튼 (blur 자동저장 없음 — 긴 글 유실 방지)
   ═══════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  if (window.InlineEdit) return;

  let active = null;   // 동시에 한 칸만 편집

  function toast(msg, kind) {
    let box = document.getElementById('ie-toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'ie-toast';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    box.textContent = msg;
    box.className = 'ie-toast show' + (kind === 'error' ? ' is-error' : '');
    clearTimeout(box._t);
    box._t = setTimeout(() => { box.className = 'ie-toast'; }, kind === 'error' ? 4200 : 1800);
  }

  function norm(kind, v) {
    if (v === undefined || v === null) return '';
    if (kind === 'datetime' && typeof v === 'string') return v.replace(' ', 'T').slice(0, 16);
    return String(v);
  }

  function makeInput(o) {
    const kind = o.kind || 'text';
    let input;
    if (kind === 'textarea') {
      input = document.createElement('textarea');
      input.rows = Math.min(12, Math.max(3, (norm(kind, o.value).match(/\n/g) || []).length + 2));
    } else if (kind === 'select') {
      input = document.createElement('select');
      for (const [v, label] of (o.options || [])) {
        const opt = document.createElement('option');
        opt.value = v == null ? '' : String(v);
        opt.textContent = label;
        input.appendChild(opt);
      }
    } else {
      input = document.createElement('input');
      input.type = kind === 'datetime' ? 'datetime-local' : (kind === 'number' ? 'number' : (kind === 'date' ? 'date' : 'text'));
      if (kind === 'number') {
        input.inputMode = 'decimal';
        if (o.step != null) input.step = o.step; else input.step = 'any';
        if (o.min != null) input.min = o.min;
        if (o.max != null) input.max = o.max;
      }
    }
    input.className = 'ie-input ie-' + kind;
    if (o.placeholder) input.placeholder = o.placeholder;
    input.value = norm(kind, o.value);
    return input;
  }

  function start(el, o) {
    if (active) {
      if (active.el === el) return;
      // 긴 글(textarea)은 바깥 클릭으로 확정·취소하지 않는다 → 새 칸을 열지 않고 먼저 끝내도록 안내(초안 유실 방지)
      if (active.kind === 'textarea') {
        toast('작성 중인 칸을 먼저 저장하거나 취소하세요', 'error');
        const pending = active.el.querySelector('textarea');
        if (pending) pending.focus();
        return;
      }
      active.commit();          // 한 줄 입력은 먼저 확정
    }
    const kind = o.kind || 'text';
    const orig = norm(kind, o.value);
    const prevHTML = el.innerHTML;
    const input = makeInput(o);
    let done = false;

    el.classList.add('ie-editing');
    el.innerHTML = '';
    const wrap = document.createElement('span');
    wrap.className = 'ie-wrap' + (kind === 'textarea' ? ' ie-wrap-block' : '');
    wrap.appendChild(input);

    const restore = () => { el.innerHTML = prevHTML; el.classList.remove('ie-editing', 'ie-saving'); };

    async function finish(save) {
      if (done) return;
      done = true;
      if (active && active.session === session) active = null;
      let val = input.value;
      if (kind === 'text' || kind === 'textarea') val = val.replace(/\s+$/, '');
      if (!save || val === orig) { restore(); return; }
      if (o.allowEmpty === false && !String(val).trim()) {
        restore(); toast('빈 값은 저장할 수 없습니다', 'error'); return;
      }
      let out = val;
      if (kind === 'number') out = val === '' ? null : Number(val);
      else if ((kind === 'date' || kind === 'datetime') && val === '') out = null;
      el.classList.add('ie-saving');
      input.disabled = true;
      try {
        await o.save(out);
        // 표시 모드 복구 — onDone 이 재렌더하지 않는 호출부도 입력칸이 남지 않게 새 값을 그대로 보여준다
        o.value = out;
        el.classList.remove('ie-editing', 'ie-saving');
        el.textContent = (out === null || out === '') ? (o.emptyText || o.placeholder || '—') : String(out);
        toast('저장됨');
        if (o.onDone) o.onDone(out);
      } catch (err) {
        restore();
        toast('저장 실패: ' + ((err && err.message) || err), 'error');
      }
    }

    const session = {};
    active = { el, kind, session, commit: () => finish(true) };

    if (kind === 'textarea') {
      const btns = document.createElement('span');
      btns.className = 'ie-btns';
      const ok = document.createElement('button');
      ok.type = 'button'; ok.className = 'ie-btn ie-ok'; ok.textContent = '저장';
      const no = document.createElement('button');
      no.type = 'button'; no.className = 'ie-btn'; no.textContent = '취소';
      const hint = document.createElement('span');
      hint.className = 'ie-hint'; hint.textContent = '⌘/Ctrl+Enter 저장 · Esc 취소';
      ok.addEventListener('click', (e) => { e.stopPropagation(); finish(true); });
      no.addEventListener('click', (e) => { e.stopPropagation(); finish(false); });
      btns.append(ok, no, hint);
      wrap.appendChild(btns);
    } else {
      input.addEventListener('blur', () => setTimeout(() => finish(true), 60));
      if (kind === 'select') input.addEventListener('change', () => finish(true));
    }

    input.addEventListener('keydown', (e) => {
      if (e.isComposing || e.keyCode === 229) return;   // 한글 조합 중 Enter 무시
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); return; }
      if (e.key === 'Enter') {
        if (kind === 'textarea' && !(e.metaKey || e.ctrlKey)) return;
        e.preventDefault(); finish(true);
      }
    });
    ['click', 'mousedown', 'dblclick'].forEach(t => wrap.addEventListener(t, (e) => e.stopPropagation()));

    el.appendChild(wrap);
    input.focus();
    if (input.select && kind !== 'select' && kind !== 'date' && kind !== 'datetime') input.select();
  }

  function bind(el, o) {
    if (!el || !o || typeof o.save !== 'function') return el;
    el.classList.add('ie-target');
    if (!el.hasAttribute('tabindex')) el.tabIndex = 0;
    if (!el.title) el.title = '클릭해서 수정';
    el._ieOpts = o;
    if (el._ieBound) return el;
    el._ieBound = true;
    el.addEventListener('click', (e) => {
      if (el.classList.contains('ie-editing')) return;
      if (e.target.closest('a,button,input,select,textarea,.ie-skip')) return;
      e.stopPropagation();
      start(el, el._ieOpts);
    });
    el.addEventListener('keydown', (e) => {
      if (el.classList.contains('ie-editing')) return;
      if (e.key === 'Enter' || e.key === 'F2') { e.preventDefault(); start(el, el._ieOpts); }
    });
    return el;
  }

  /* JSON 저장 헬퍼 — CSRF 헤더는 페이지 공용 fetch 래퍼(_csrf.html)가 붙인다 */
  async function send(url, method, body) {
    const r = await fetch(url, {
      method: method || 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body),
    });
    let j = null;
    try { j = await r.json(); } catch (_) { /* 본문 없음 */ }
    if (!r.ok || (j && j.ok === false)) throw new Error((j && (j.error || j.message)) || ('HTTP ' + r.status));
    return j;
  }

  /* 자동 새로고침 타이머용: 지금 사용자가 뭔가 입력 중이면 true → 그 틱은 건너뛴다(입력 유실 방지) */
  function busy() {
    if (active || document.querySelector('.ie-editing')) return true;
    const a = document.activeElement;
    if (!a || a === document.body) return false;
    if (a.isContentEditable || a.tagName === 'TEXTAREA') return true;
    // 체크박스·버튼은 클릭 후에도 포커스가 남으므로 제외(그대로 두면 새로고침이 영영 멈춤)
    if (a.tagName === 'INPUT') return !/^(checkbox|radio|button|submit|reset|file|range|color)$/i.test(a.type);
    return false;
  }

  window.InlineEdit = { bind, start, toast, send, busy };
})();
