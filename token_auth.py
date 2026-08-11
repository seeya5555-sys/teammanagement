"""Bearer-token primitives shared by the Flask app and API blueprints.

The serializer is intentionally lazy: ``app_core`` gives Flask an ephemeral
key at import time, and the explicit runtime initializer replaces it with the
persisted key during startup.  Constructing the serializer on first use (and
again if the configured key changes) keeps those two phases correct and makes
test configuration deterministic.
"""
import hashlib
import hmac
import threading
import time

from itsdangerous import URLSafeTimedSerializer
from werkzeug.security import generate_password_hash

from app_core import app


_TOKEN_SALT = 'trmt-mobile-bearer-v1'
_TOKEN_MAXAGE = 60 * 60 * 24 * 30          # 30일

_serializer_lock = threading.Lock()
_serializer_key = None
_serializer = None


def _get_token_serializer():
    """Return a serializer bound to the app's current SECRET_KEY."""
    global _serializer_key, _serializer
    key = app.config['SECRET_KEY']
    if isinstance(key, str):
        key = key.encode()
    with _serializer_lock:
        if _serializer is None or _serializer_key != key:
            _serializer = URLSafeTimedSerializer(key, salt=_TOKEN_SALT)
            _serializer_key = key
        return _serializer


def _pw_fingerprint(pw_hash):
    """Return a keyed fingerprint that invalidates tokens on password change."""
    key = app.config['SECRET_KEY']
    if isinstance(key, str):
        key = key.encode()
    msg = b'trmt-pv-v1 ' + (pw_hash or '').encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _issue_token(u):
    return _get_token_serializer().dumps({
        'uid': u['id'],
        'pv': _pw_fingerprint(u['password_hash']),
    })


def _load_token(token):
    """Load a token and return ``(payload, issued_at)``.

    ``itsdangerous.BadData`` is intentionally allowed through so the caller
    can preserve its existing invalid-token response path.
    """
    return _get_token_serializer().loads(
        token, max_age=_TOKEN_MAXAGE, return_timestamp=True)


# Token endpoint brute-force defense (in-memory, canonical user_id key,
# thread-safe, hard-bounded).
_TOKEN_FAILS = {}           # user_id(int) -> [실패 epoch, ...]
_TOKEN_FAIL_LOCK = threading.Lock()
_TOKEN_FAIL_WINDOW = 15 * 60
_TOKEN_FAIL_MAX = 10

# Timing-oracle equalizer for nonexistent usernames.
_DUMMY_PW_HASH = generate_password_hash('trmt-mobile-timing-equalizer')


def _token_rate_limited(key):
    """Return whether this user bucket is currently blocked."""
    now = time.time()
    with _TOKEN_FAIL_LOCK:
        fails = [t for t in _TOKEN_FAILS.get(key, []) if now - t < _TOKEN_FAIL_WINDOW]
        if fails:
            _TOKEN_FAILS[key] = fails
        else:
            _TOKEN_FAILS.pop(key, None)
        return len(fails) >= _TOKEN_FAIL_MAX


def _token_note_fail(key):
    now = time.time()
    with _TOKEN_FAIL_LOCK:
        for k in [k for k, v in list(_TOKEN_FAILS.items())
                  if all(now - t >= _TOKEN_FAIL_WINDOW for t in v)]:
            _TOKEN_FAILS.pop(k, None)
        fails = [t for t in _TOKEN_FAILS.get(key, []) if now - t < _TOKEN_FAIL_WINDOW]
        fails.append(now)
        _TOKEN_FAILS[key] = fails


def _token_reset_fails(key):
    with _TOKEN_FAIL_LOCK:
        _TOKEN_FAILS.pop(key, None)
