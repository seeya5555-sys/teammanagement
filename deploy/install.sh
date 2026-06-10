#!/usr/bin/env bash
# TRMT 자동배포 1회 설치 스크립트 (서버에서 1번만 실행).
# 전제: ~/app 에 앱이 있고, venv 와 systemd 서비스 'trmt' 가 이미 동작 중.
set -euo pipefail
APP_DIR="$HOME/app"
REPO="https://github.com/seeya5555-sys/teammanagement.git"
cd "$APP_DIR"

echo "[1/5] ~/app 을 git 체크아웃으로 전환"
if [ ! -d .git ]; then
  git init -q
  git remote add origin "$REPO"
  git fetch -q origin main
  git reset --hard origin/main      # tracked 파일 갱신, instance/ & static/uploads(untracked) 보존
  git branch -M main
  git branch --set-upstream-to=origin/main main
else
  echo "    이미 git 저장소 → 건너뜀"
fi

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
echo "수동 1회 실행 테스트: bash ~/app/deploy/autodeploy.sh"
echo "로그 보기:           journalctl -u trmt-autodeploy.service -n 30 --no-pager"
