"""Cross-site request forgery defense for the cookie-authenticated surface.

Scope was measured, not assumed.  Three things authorize a state-changing
request in this app:

  * the session cookie (browser),
  * ``Authorization: Bearer`` (native app, injected into the session by
    ``app._bearer_auth``),
  * ``X-API-Key`` on ``/api/ext/`` (the Mac runners).

Only the first is attached by the browser on its own, so only the first needs a
token.  Demanding one from the other two would break the app and every runner
without closing a hole: a cross-site page cannot make a browser send either
header, and neither travels with an ambient credential.

The exemptions therefore key off the credential presented, never off the path.
``/api/ext/`` is not blanket-exempt: two of its routes are ``@admin_required``
(cookie), one of which regenerates the live API key.

The three hooks that run before this one were read for side effects, since
anything they do happens before a forged request is refused.
``_require_runtime_initialization`` only raises, ``_limit_non_stt_upload`` only
aborts 413 and sets a per-request size cap, and ``_bearer_auth`` touches the
session only on Bearer requests -- which are exempt, and whose session write is
discarded by ``_suppress_bearer_session_cookie`` anyway.  Nothing persists.

``SESSION_COOKIE_SAMESITE='Lax'`` was the only thing standing here before.  It
is real but partial: it is a hint the browser may honour, it does nothing about
a sibling ``*.duckdns.org`` origin that the public suffix list treats as the
same site, and it is not a control this app owns.

Not covered, deliberately: the Dock Manager app mounted at its own WSGI root
(``wsgi.py``, ``DispatcherMiddleware``).  That is a different Flask instance
with its own hooks and this module never sees its requests -- calling this
protection "app-wide" would be wrong by one whole application.
"""
import hmac
import secrets

from flask import current_app, g, jsonify, redirect, request, session, url_for

#: Session key holding the per-session token.
SESSION_KEY = '_csrf_token'
#: Header used by the browser fetch wrapper.
HEADER = 'X-CSRF-Token'
#: Hidden field used by the one classic HTML form (login).
FIELD = '_csrf'
#: Marks a rejection as CSRF so the wrapper can refresh and replay once.
#: A plain 403 must stay indistinguishable from a permission failure.
FAIL_HEADER = 'X-CSRF-Fail'

_UNSAFE = ('POST', 'PUT', 'PATCH', 'DELETE')
_FORM_TYPES = ('application/x-www-form-urlencoded', 'multipart/form-data')

#: Header the Mac runners authenticate with.
API_KEY_HEADER = 'X-API-Key'

#: Paths that need a token even with no session.  Login is the one that matters:
#: unchecked, a cross-site page can log the victim into an account the attacker
#: controls, and every action afterwards is attributed to the wrong user.  A real
#: submitter always has a token because rendering the login page mints one.
_ENFORCED_ANONYMOUS = ('/login',)


def csrf_token():
    """Return this session's token, minting one on first use.

    Minted lazily rather than on every request: a Bearer call also reaches the
    session (``_bearer_auth`` injects the user into it), and
    ``_suppress_bearer_session_cookie`` then throws the session write away.
    Minting eagerly would hand out tokens that were never stored, so the mint
    happens only where a template actually asks for one -- which is a cookie
    page render by construction.
    """
    token = session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def _submitted():
    """The token the caller sent, or None."""
    header = request.headers.get(HEADER)
    if header:
        return header
    # Only look at the form when the body actually is one.  Touching
    # ``request.form`` on a JSON body is harmless but on a file upload it
    # forces the multipart parse before the view has decided anything.
    if request.mimetype in _FORM_TYPES:
        return request.form.get(FIELD)
    return None


def _refuse():
    # A classic form submit is a navigation: answering it with JSON would put
    # raw ``{"error": "csrf"}`` on screen.  Send those back to the login page
    # instead -- that render mints a fresh token, so a real person just retries,
    # while a forged submit gets a redirect and no session.
    if request.path in _ENFORCED_ANONYMOUS and request.mimetype in _FORM_TYPES:
        response = redirect(url_for('routes_core.login', csrf='1'))
        response.headers[FAIL_HEADER] = '1'
        return response
    response = jsonify({
        'error': 'csrf',
        'message': '보안 토큰이 만료됐습니다. 페이지를 새로고침한 뒤 다시 시도하세요.',
    })
    response.status_code = 403
    response.headers[FAIL_HEADER] = '1'
    return response


def enforce():
    """Reject unsafe cookie-authenticated requests that carry no valid token.

    Returns a response to abort with, or None to let the request through.
    """
    # The default follows ``app.testing``: on everywhere real, off under the
    # test client, which authenticates by writing the session directly
    # (``client.session_transaction()``) and so never holds a token.  ``TESTING``
    # is set in 40 test files and nowhere in the shipped code or wsgi entry
    # point, so this cannot be flipped by anything an attacker controls -- and
    # tests/test_csrf.py sets ``CSRF_PROTECT=True`` to exercise the real path.
    if not current_app.config.get('CSRF_PROTECT', not current_app.testing):
        return None
    if request.method not in _UNSAFE:
        return None
    if getattr(g, '_token_auth', False):
        return None                      # Bearer: not an ambient credential
    # Runner exemption keys off the credential being *presented*, not off the
    # path.  Measured reason: two ``/api/ext/`` routes -- ``/api/ext/key`` and
    # ``/api/ext/key/regenerate`` -- are ``@admin_required``, i.e. cookie-
    # authenticated, and the second one destroys the live automation key.  A
    # path-prefix exemption would have left that forgeable.  A cross-site page
    # cannot add this header (a custom header forces a CORS preflight this app
    # never approves), so its presence is a safe signal.
    if request.headers.get(API_KEY_HEADER) and request.path.startswith('/api/ext/'):
        return None
    if 'user_id' not in session and request.path not in _ENFORCED_ANONYMOUS:
        # Nothing to forge with.  An unauthenticated write is the view's own
        # 401/redirect to answer, and turning it into 403 csrf here would
        # mislabel every logged-out click as a security failure.  The exception
        # is login: see ``_ENFORCED_ANONYMOUS``.
        return None
    expected = session.get(SESSION_KEY)
    got = _submitted()
    if not expected or not got:
        return _refuse()
    if not hmac.compare_digest(str(expected), str(got)):
        return _refuse()
    return None


def init_app(app):
    """Wire the token into templates and the check into the request pipeline.

    🔴 The call site in ``app.py`` is a contract, not a style choice: this must
    be registered **after** ``_bearer_auth`` (which sets ``g._token_auth``, the
    signal that skips the check) and **before** ``_idem_replay`` (so a forged
    request cannot claim an idempotency key on its way to being rejected).
    ``before_request`` hooks run in registration order.

    Deliberately no ``config.setdefault`` here: leaving the key absent is what
    lets ``enforce`` fall back to ``not app.testing``.  Writing True at init
    would freeze the value before a test file has said it is a test.
    """
    app.jinja_env.globals['csrf_token'] = csrf_token
    app.before_request(enforce)
    return app
