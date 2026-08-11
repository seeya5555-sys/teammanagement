#!/usr/bin/env python3
"""Runtime side-effect, key persistence, and API 413 regression checks."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class RuntimeHardeningTests(unittest.TestCase):
    def test_clean_app_core_import_has_no_runtime_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            shutil.copy2(os.path.join(ROOT, 'app_core.py'), os.path.join(td, 'app_core.py'))
            probe = subprocess.run(
                [sys.executable, '-c', (
                    'import app_core, json, os; '
                    'print(json.dumps({"instance": os.path.exists(app_core.INSTANCE_DIR), '
                    '"secret": os.path.exists(app_core.SECRET_KEY_FILE)}))'
                )],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': td},
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual({'instance': False, 'secret': False}, json.loads(probe.stdout))

    def test_runtime_initializer_creates_dirs_and_stable_key(self):
        import app_core

        with tempfile.TemporaryDirectory() as td:
            old_dirs = app_core._RUNTIME_DIRS
            old_secret_file = app_core.SECRET_KEY_FILE
            old_key = app_core.app.config['SECRET_KEY']
            paths = tuple(os.path.join(td, name) for name in (
                'instance', 'uploads', 'invoice_pdfs', 'jeonja_pdfs', 'aor_pdfs',
                'fundreq_files', 'soa_review_pdfs', 'dockproc_files', 'stt_audio',
            ))
            app_core.SECRET_KEY_FILE = os.path.join(paths[0], '.secret_key')
            app_core._RUNTIME_DIRS = paths
            try:
                first = app_core.init_runtime()
                second = app_core.init_runtime()
                self.assertEqual(first, second)
                self.assertEqual(first, app_core.app.config['SECRET_KEY'])
                self.assertTrue(all(os.path.isdir(path) for path in paths))
                with open(app_core.SECRET_KEY_FILE, 'rb') as fh:
                    self.assertEqual(first, fh.read())
                self.assertEqual(0o600, os.stat(app_core.SECRET_KEY_FILE).st_mode & 0o777)
                self.assertFalse(any(name.startswith('.secret_key.')
                                     for name in os.listdir(paths[0])))
            finally:
                app_core._RUNTIME_DIRS = old_dirs
                app_core.SECRET_KEY_FILE = old_secret_file
                app_core.app.config['SECRET_KEY'] = old_key

    def test_concurrent_secret_creation_publishes_one_complete_key(self):
        import app_core

        with tempfile.TemporaryDirectory() as td:
            old_secret_file = app_core.SECRET_KEY_FILE
            app_core.SECRET_KEY_FILE = os.path.join(td, '.secret_key')
            barrier = threading.Barrier(12)
            keys = []
            errors = []

            def create_key():
                try:
                    barrier.wait()
                    keys.append(app_core._load_or_create_secret_key())
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=create_key) for _ in range(12)]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual([], errors)
                self.assertEqual(12, len(keys))
                self.assertEqual(1, len(set(keys)))
                self.assertEqual(32, len(keys[0]))
                with open(app_core.SECRET_KEY_FILE, 'rb') as fh:
                    self.assertEqual(keys[0], fh.read())
                self.assertFalse(any(name.startswith('.secret_key.')
                                     for name in os.listdir(td)))
            finally:
                app_core.SECRET_KEY_FILE = old_secret_file

    def test_token_serializer_tracks_runtime_key_changes(self):
        from itsdangerous import BadData
        import app_core
        import token_auth

        old_key = app_core.app.config['SECRET_KEY']
        user = {'id': 7, 'password_hash': 'hash'}
        try:
            app_core.app.config['SECRET_KEY'] = b'a' * 32
            old_token = token_auth._issue_token(user)
            self.assertEqual(7, token_auth._load_token(old_token)[0]['uid'])

            app_core.app.config['SECRET_KEY'] = b'b' * 32
            new_token = token_auth._issue_token(user)
            self.assertEqual(7, token_auth._load_token(new_token)[0]['uid'])
            with self.assertRaises(BadData):
                token_auth._load_token(old_token)
        finally:
            app_core.app.config['SECRET_KEY'] = old_key

    def test_api_oversize_uses_json_413_handler(self):
        import app

        response = app.app.test_client().post(
            '/api/auth/token',
            data=b'x' * (app._NON_STT_UPLOAD_MAX + 1),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(413, response.status_code)
        self.assertEqual({'error': '파일 크기는 20MB 이하여야 합니다.'}, response.get_json())


if __name__ == '__main__':
    unittest.main()
