"""Backup script contract tests (real subprocesses, no mocks of sqlite/tar output)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "deploy" / "backup.sh"


class BackupContractTest(unittest.TestCase):
    def make_app(self, td: Path) -> Path:
        app = td / "app"
        (app / "instance").mkdir(parents=True)
        (app / "static" / "uploads").mkdir(parents=True)
        (app / "venv" / "bin").mkdir(parents=True)
        (app / "venv" / "bin" / "python3").symlink_to(sys.executable)

        db = sqlite3.connect(app / "instance" / "trmt.db")
        required = ["users", "vessels", "issues", "dock_procure", "aor_draft", "attachments", "api_settings"]
        for table in required:
            if table == "attachments":
                db.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY, stored_name TEXT NOT NULL UNIQUE)")
            else:
                db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
        for i in range(33):
            db.execute(f'CREATE TABLE "dummy_{i}" (id INTEGER PRIMARY KEY)')
        db.execute("INSERT INTO attachments(stored_name) VALUES ('proof.txt')")
        db.commit()
        db.close()
        (app / "static" / "uploads" / "proof.txt").write_text("proof", encoding="utf-8")
        (app / "instance" / ".secret_key").write_text("fixture", encoding="utf-8")
        return app

    def test_tar_rc1_is_retried_and_files_manifest_pairs_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            app = self.make_app(td)
            dest = td / "backups"
            state = td / "tar-attempts"
            fake_tar = td / "tar-retry-once.sh"
            fake_tar.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                f"state={state!s}\n"
                "if [[ ${1:-} == -czf ]]; then\n"
                "  n=$(cat \"$state\" 2>/dev/null || echo 0); n=$((n+1)); echo $n > \"$state\"\n"
                "  if [[ $n -eq 1 ]]; then exit 1; fi\n"
                "fi\n"
                "exec /usr/bin/tar \"$@\"\n",
                encoding="utf-8",
            )
            fake_tar.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "TRMT_APP_DIR": str(app),
                "TRMT_BACKUP_DIR": str(dest),
                "TRMT_TAR_BIN": str(fake_tar),
                "TRMT_TAR_RETRIES": "2",
                "TRMT_FLOCK_BIN": "/usr/bin/true",
            })
            p = subprocess.run(["bash", str(BACKUP)], env=env, text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue(state.exists(), p.stdout + p.stderr)
            self.assertEqual(state.read_text().strip(), "2", "tar rc=1 must cause a retry")

            archives = list((dest / "files").glob("files-*.tar.gz"))
            self.assertEqual(len(archives), 1)
            archive = archives[0]
            mf_path = Path(str(archive) + ".manifest.json")
            self.assertTrue(mf_path.is_file(), "files archive needs a paired manifest")
            mf = json.loads(mf_path.read_text(encoding="utf-8"))
            self.assertRegex(mf["db_backup"], r"^trmt-.*\.db\.gz$")
            self.assertEqual(mf["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            with tarfile.open(archive, "r:gz") as tf:
                names = set(tf.getnames())
            self.assertIn("static/uploads/proof.txt", names)
            self.assertIn("static/uploads/proof.txt", set(mf["members"]))

            db_archive = dest / "db" / mf["db_backup"]
            self.assertTrue(db_archive.is_file())
            with gzip.open(db_archive, "rb") as f:
                restored = td / "paired.db"
                data = f.read()
                assert isinstance(data, bytes)
                restored.write_bytes(data)
            c = sqlite3.connect(restored)
            self.assertEqual(c.execute("SELECT stored_name FROM attachments").fetchone()[0], "proof.txt")
            c.close()

    def test_restore_member_check_ignores_directory_entries(self) -> None:
        text = (ROOT / "deploy" / "restore-check.sh").read_text(encoding="utf-8")
        self.assertIn("m.isfile()", text, "restore member comparison must use regular files only")

    def test_offsite_pull_script_has_atomic_verified_files_copy(self) -> None:
        script = ROOT / "deploy" / "offsite-pull-macos.sh"
        self.assertTrue(script.is_file(), "off-host pull script must be versioned")
        text = script.read_text(encoding="utf-8")
        for required in (".partial", "sha256", "tar -tzf", "files-*.tar.gz.manifest.json"):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
