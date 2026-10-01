/* upload-accel.js — 웹 전 탭 공용 업로드 가속(TRMT + /drydock).
 *
 * 왜: 업로드 지연의 실측 병목은 서버가 아니라 회선이다(서버 내부 8MB 0.13s,
 * 느린 회선에서는 50KB/s대). 그래서 할 수 있는 건 두 가지뿐이다.
 *   ① 보내는 바이트를 줄인다 — 큰 JPEG 사진을 긴 변 2560px / q0.85 로 줄여 보낸다.
 *      PDF·Office·PNG 등 나머지 파일은 원본 그대로다.
 *   ② 진행률을 보여준다 — fetch 는 업로드 진행을 못 알려주므로, 큰 multipart
 *      업로드만 XHR 로 보내고 하단에 진행률 바를 띄운다.
 *
 * 대상: same-origin POST/PUT/PATCH + FormData body 만. 그 외 요청은 손대지 않는다.
 * 순서: `_csrf.html` 의 CSRF 래퍼보다 **먼저** 로드한다. CSRF 래퍼가 이 래퍼를
 * native 로 잡아야 토큰 헤더가 붙은 요청이 여기 XHR 경로를 탄다.
 */
(function () {
  if (window.__trmtUploadAccel) return;
  window.__trmtUploadAccel = true;

  var MAX_EDGE = 2560;              // 축소 상한(긴 변)
  var JPEG_Q = 0.85;
  var SHRINK_MIN_BYTES = 1200 * 1024; // 이보다 작은 사진은 그대로
  var PROGRESS_MIN_BYTES = 256 * 1024;
  var UNSAFE = { POST: 1, PUT: 1, PATCH: 1 };
  var inner = window.fetch.bind(window);

  function sameOrigin(url) {
    try { return new URL(url, location.href).origin === location.origin; }
    catch (e) { return false; }
  }

  function eligible(input, init) {
    if (!init || !(init.body instanceof FormData)) return false;
    if (typeof input !== 'string' && !(window.URL && input instanceof URL)) return false;
    var method = String(init.method || 'GET').toUpperCase();
    if (!UNSAFE[method] || !sameOrigin(String(input))) return false;
    // XHR 로 흉내낼 수 없는 fetch 옵션이 있으면 원래 경로로 보낸다.
    if (init.redirect && init.redirect !== 'follow') return false;
    if (init.keepalive || (init.mode && init.mode !== 'cors' && init.mode !== 'same-origin')) return false;
    if (init.credentials === 'omit') return false;
    return true;
  }

  function canShrink() {
    return typeof createImageBitmap === 'function' &&
      (typeof OffscreenCanvas === 'function' || !!document.createElement('canvas').toBlob);
  }

  function encodeJpeg(bitmap, w, h) {
    if (typeof OffscreenCanvas === 'function') {
      var oc = new OffscreenCanvas(w, h);
      oc.getContext('2d').drawImage(bitmap, 0, 0, w, h);
      return oc.convertToBlob({ type: 'image/jpeg', quality: JPEG_Q });
    }
    return new Promise(function (resolve) {
      var c = document.createElement('canvas');
      c.width = w; c.height = h;
      c.getContext('2d').drawImage(bitmap, 0, 0, w, h);
      c.toBlob(resolve, 'image/jpeg', JPEG_Q);
    });
  }

  // 실패하면 언제나 원본을 돌려준다 — 축소는 최적화일 뿐 업로드를 막으면 안 된다.
  function shrink(file) {
    if (!(file instanceof File) || file.type !== 'image/jpeg' || file.size < SHRINK_MIN_BYTES) {
      return Promise.resolve(file);
    }
    return createImageBitmap(file, { imageOrientation: 'from-image' }).then(function (bmp) {
      var w = bmp.width, h = bmp.height, edge = Math.max(w, h);
      var s = edge > MAX_EDGE ? MAX_EDGE / edge : 1;
      var tw = Math.max(1, Math.round(w * s)), th = Math.max(1, Math.round(h * s));
      function release() { if (bmp.close) bmp.close(); }
      return Promise.resolve().then(function () { return encodeJpeg(bmp, tw, th); }).then(function (blob) {
        release();
        if (!blob || blob.size >= file.size * 0.9) return file;   // 이득 없으면 원본
        return new File([blob], file.name, { type: 'image/jpeg', lastModified: file.lastModified });
      }, function (err) { release(); throw err; });
    }).catch(function () { return file; });
  }

  function shrinkForm(fd) {
    if (!canShrink()) return Promise.resolve(fd);
    var entries = [];
    fd.forEach(function (v, k) { entries.push([k, v]); });
    var touched = entries.some(function (e) {
      return e[1] instanceof File && e[1].type === 'image/jpeg' && e[1].size >= SHRINK_MIN_BYTES;
    });
    if (!touched) return Promise.resolve(fd);
    // 한 장씩 순차 처리 — 12MP 사진 10장을 동시에 디코딩하면 비트맵만 수백 MB 다.
    var vals = [];
    return entries.reduce(function (p, e) {
      return p.then(function () {
        return (e[1] instanceof File ? shrink(e[1]) : Promise.resolve(e[1])).then(function (v) { vals.push(v); });
      });
    }, Promise.resolve()).then(function () {
      var out = new FormData();
      entries.forEach(function (e, i) {
        if (vals[i] instanceof File) out.append(e[0], vals[i], vals[i].name);
        else out.append(e[0], vals[i]);
      });
      return out;
    });
  }

  function formBytes(fd) {
    var n = 0;
    fd.forEach(function (v) { n += (v instanceof Blob) ? v.size : String(v).length; });
    return n;
  }

  // ── 진행률 바(동시 업로드는 합산) ──────────────────────────────
  var jobs = {}, seq = 0, bar = null, hideTimer = null;
  function fmtMB(b) { return (b / 1048576).toFixed(1) + 'MB'; }
  function render() {
    var loaded = 0, total = 0, n = 0;
    for (var id in jobs) { loaded += jobs[id].loaded; total += jobs[id].total; n++; }
    if (!n) {
      if (bar) { clearTimeout(hideTimer); hideTimer = setTimeout(function () { if (bar) bar.style.display = 'none'; }, 600); }
      return;
    }
    if (!bar) {
      bar = document.createElement('div');
      bar.setAttribute('role', 'status');
      bar.setAttribute('aria-live', 'polite');
      bar.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:2147483000;' +
        'min-width:240px;max-width:90vw;padding:8px 12px;border-radius:8px;background:rgba(40,36,32,.92);' +
        'color:#fff;font:12px/1.4 system-ui,-apple-system,sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.25)';
      bar.innerHTML = '<div class="ua-t"></div><div style="height:4px;margin-top:6px;border-radius:2px;background:rgba(255,255,255,.2)">' +
        '<div class="ua-f" style="height:100%;width:0;border-radius:2px;background:#e8a54b;transition:width .2s"></div></div>';
      (document.body || document.documentElement).appendChild(bar);
    }
    clearTimeout(hideTimer);
    bar.style.display = '';
    var pct = total ? Math.min(100, Math.floor(loaded * 100 / total)) : 0;
    bar.querySelector('.ua-t').textContent = pct >= 100
      ? '업로드 완료 · 서버 처리 중…'
      : '업로드 중 ' + pct + '% · ' + fmtMB(loaded) + ' / ' + fmtMB(total);
    bar.querySelector('.ua-f').style.width = pct + '%';
  }

  function parseHeaders(raw) {
    var h = new Headers();
    (raw || '').trim().split(/[\r\n]+/).forEach(function (line) {
      var i = line.indexOf(':');
      if (i > 0) { try { h.append(line.slice(0, i).trim(), line.slice(i + 1).trim()); } catch (e) {} }
    });
    return h;
  }

  function xhrFetch(url, init, body) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      var id = ++seq, total = formBytes(body);
      var signal = init.signal;
      if (signal && signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return; }
      xhr.open(String(init.method).toUpperCase(), String(url), true);
      xhr.responseType = 'blob';
      new Headers(init.headers || undefined).forEach(function (v, k) {
        if (k.toLowerCase() !== 'content-type') xhr.setRequestHeader(k, v);  // boundary 는 브라우저가
      });
      function done() { delete jobs[id]; render(); if (signal) signal.removeEventListener('abort', onAbort); }
      function onAbort() { xhr.abort(); }
      if (signal) signal.addEventListener('abort', onAbort);
      jobs[id] = { loaded: 0, total: total };
      render();
      xhr.upload.onprogress = function (e) {
        if (!jobs[id]) return;
        jobs[id].loaded = e.loaded;
        if (e.lengthComputable) jobs[id].total = e.total;
        render();
      };
      xhr.upload.onload = function () { if (jobs[id]) { jobs[id].loaded = jobs[id].total; render(); } };
      xhr.onload = function () {
        done();
        var st = xhr.status;
        var nullBody = st === 101 || st === 204 || st === 205 || st === 304;
        try {
          resolve(new Response(nullBody ? null : xhr.response,
            { status: st, statusText: xhr.statusText, headers: parseHeaders(xhr.getAllResponseHeaders()) }));
        } catch (e) { reject(new TypeError('Failed to fetch')); }
      };
      xhr.onerror = function () { done(); reject(new TypeError('Failed to fetch')); };
      xhr.ontimeout = xhr.onerror;
      xhr.onabort = function () { done(); reject(new DOMException('Aborted', 'AbortError')); };
      xhr.send(body);
    });
  }

  window.fetch = function (input, init) {
    if (!eligible(input, init)) return inner(input, init);
    return shrinkForm(init.body).then(function (body) {
      if (formBytes(body) < PROGRESS_MIN_BYTES || typeof XMLHttpRequest !== 'function') {
        var next = {}; for (var k in init) next[k] = init[k];
        next.body = body;
        return inner(input, next);
      }
      return xhrFetch(input, init, body);
    });
  };
})();
