#!/usr/bin/env bash
# TRMT 자동배포 1회 설치 (git 불필요). 전제: ~/app 에 앱+venv, systemd 서비스 'trmt' 동작 중.
set -euo pipefail
APP_DIR="$HOME/app"
REPO="seeya5555-sys/teammanagement"
BRANCH="main"

echo "[1/5] 최신 코드 받아 ~/app 갱신 (curl/unzip)"
curl -sL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip" -o /tmp/trmt_u.zip
cd /tmp && rm -rf "teammanagement-${BRANCH}" && unzip -oq trmt_u.zip
cp -rf "teammanagement-${BRANCH}/." "$APP_DIR/"
rm -rf "teammanagement-${BRANCH}" trmt_u.zip

echo "[2/5] autodeploy.sh 실행권한"
chmod +x "$APP_DIR/deploy/autodeploy.sh"

echo "[3/5] sudoers: restart 만 무인 허용"
SCTL=$(command -v systemctl)
echo "opc ALL=(ALL) NOPASSWD: $SCTL restart trmt" | sudo tee /etc/sudoers.d/trmt-autodeploy >/dev/null
sudo chmod 440 /etc/sudoers.d/trmt-autodeploy

echo "[4/5] systemd service + timer 설치"
sudo cp "$APP_DIR/deploy/trmt-autodeploy.service" /etc/systemd/system/
sudo cp "$APP_DIR/deploy/trmt-autodeploy.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trmt-autodeploy.timer

echo "[5/5] 완료. 타이머 상태:"
systemctl status trmt-autodeploy.timer --no-pager | head -6 || true
echo ""
echo "수동 1회 실행: bash ~/app/deploy/autodeploy.sh"
echo "로그:        journalctl -u trmt-autodeploy.service -n 30 --no-pager"
