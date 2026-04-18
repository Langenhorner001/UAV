#!/bin/bash

# ==============================================================
#  URL Auto Visitor Bot — AWS EC2 User Data Script
#
#  HOW TO USE (AWS Console):
#  1. Go to EC2 → Launch Instance
#  2. Choose: Ubuntu Server 22.04 LTS (64-bit x86)
#  3. Instance type: t3.small (or larger)
#  4. Scroll to "Advanced Details" → "User data"
#  5. Paste THIS entire script
#  6. Fill in your BOT_TOKEN below (line 20)
#  7. Launch → Bot starts automatically on first boot!
#
#  View logs after boot:
#    ssh ubuntu@<your-ec2-ip>
#    sudo journalctl -u visitor-bot -f
# ==============================================================

# ╔══════════════════════════════════════╗
# ║  FILL THIS IN BEFORE PASTING        ║
# ╚══════════════════════════════════════╝
BOT_TOKEN="PASTE_YOUR_BOT_TOKEN_HERE"

# ── Repo to clone ─────────────────────────────────────────────
REPO_URL="https://github.com/hiaistudent-jpg/Auto-TGBot-Clicker.git"
INSTALL_DIR="/home/ubuntu/Auto-TGBot-Clicker"
BOT_DIR="$INSTALL_DIR"
BOT_USER="ubuntu"
VENV_DIR="$BOT_DIR/venv"
LOG_FILE="/var/log/visitor-bot-setup.log"

# ── Log everything ────────────────────────────────────────────
exec > >(tee -a "$LOG_FILE") 2>&1
echo "========================================"
echo " Visitor Bot Setup — $(date)"
echo "========================================"

# Validate token
if [[ "$BOT_TOKEN" == "PASTE_YOUR_BOT_TOKEN_HERE" || -z "$BOT_TOKEN" ]]; then
    echo "[FAIL] BOT_TOKEN not set! Edit line 20 before using."
    exit 1
fi

# ── 1. System update ──────────────────────────────────────────
echo "[1/8] System update..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq

# ── 2. Core tools ─────────────────────────────────────────────
echo "[2/8] Core tools..."
apt-get install -y -qq git curl wget unzip screen net-tools software-properties-common

# ── 3. Clone repo ─────────────────────────────────────────────
echo "[3/8] Cloning repo..."
if [[ -d "$INSTALL_DIR" ]]; then
    echo "  Repo already exists — pulling latest..."
    sudo -u "$BOT_USER" git -C "$INSTALL_DIR" pull
else
    sudo -u "$BOT_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi
echo "  Repo ready at $INSTALL_DIR"

# ── 4. Python 3.11 ────────────────────────────────────────────
echo "[4/8] Python 3.11..."
if ! command -v python3.11 &>/dev/null; then
    add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null
    apt-get update -qq
fi
apt-get install -y -qq python3.11 python3.11-venv python3.11-dev python3-pip

# ── 5. Tor ────────────────────────────────────────────────────
echo "[5/8] Tor..."
apt-get install -y -qq tor
systemctl stop tor 2>/dev/null || true
systemctl disable tor 2>/dev/null || true

# ── 6. Chromium + ChromeDriver ────────────────────────────────
echo "[6/8] Chromium..."
CHROMIUM_BIN=""
CHROMEDRIVER_BIN=""

if apt-cache show chromium-browser &>/dev/null 2>&1; then
    apt-get install -y -qq chromium-browser chromium-chromedriver
    CHROMIUM_BIN="/usr/bin/chromium-browser"
    CHROMEDRIVER_BIN="$(command -v chromedriver 2>/dev/null || echo /usr/lib/chromium-browser/chromedriver)"
elif apt-cache show chromium &>/dev/null 2>&1; then
    apt-get install -y -qq chromium chromium-driver
    CHROMIUM_BIN="/usr/bin/chromium"
    CHROMEDRIVER_BIN="$(command -v chromedriver 2>/dev/null || echo /usr/bin/chromedriver)"
else
    snap install chromium 2>/dev/null || true
    CHROMIUM_BIN="/snap/bin/chromium"
    if [ -f "/snap/bin/chromium.chromedriver" ]; then
        CHROMEDRIVER_BIN="/snap/bin/chromium.chromedriver"
    else
        CHROMEDRIVER_BIN="/snap/bin/chromedriver"
    fi
fi

# ── 7. Xvfb + fonts + libs ────────────────────────────────────
echo "[7/8] Xvfb + libraries..."
apt-get install -y -qq \
    xvfb fonts-liberation fonts-noto fonts-noto-cjk xfonts-base \
    libgbm1 libnss3 libxss1 libasound2 \
    libatk-bridge2.0-0 libgtk-3-0 libx11-xcb1 \
    libdrm2 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 \
    2>/dev/null || true

# ── 8. Python venv + packages ─────────────────────────────────
echo "[8/8] Python venv..."
sudo -u "$BOT_USER" python3.11 -m venv "$VENV_DIR"
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -r "$BOT_DIR/requirements.txt" -q

# ── Save .env ─────────────────────────────────────────────────
ENV_FILE="$BOT_DIR/.env"
printf "BOT_TOKEN=%s\n" "$BOT_TOKEN" > "$ENV_FILE"
chmod 600 "$ENV_FILE"
chown "$BOT_USER":"$BOT_USER" "$ENV_FILE"
echo "  .env saved"

# ── Detect real binary paths ──────────────────────────────────
REAL_CHROMIUM=$(sudo -u "$BOT_USER" "$VENV_DIR/bin/python3" -c "
import shutil, os
for c in ['$CHROMIUM_BIN','/usr/bin/chromium-browser','/usr/bin/chromium','/snap/bin/chromium']:
    if c and os.path.isfile(c): print(c); raise SystemExit
print(shutil.which('chromium-browser') or shutil.which('chromium') or '$CHROMIUM_BIN')
" 2>/dev/null || echo "$CHROMIUM_BIN")

REAL_CHROMEDRIVER=$(sudo -u "$BOT_USER" "$VENV_DIR/bin/python3" -c "
import shutil, os
chromium = '$REAL_CHROMIUM'
if 'snap' in chromium:
    for c in ['/snap/bin/chromium.chromedriver', '/snap/bin/chromedriver']:
        if os.path.isfile(c): print(c); raise SystemExit
for c in ['$CHROMEDRIVER_BIN','/usr/lib/chromium-browser/chromedriver','/usr/bin/chromedriver','/snap/bin/chromium.chromedriver','/snap/bin/chromedriver']:
    if c and os.path.isfile(c): print(c); raise SystemExit
found = shutil.which('chromium.chromedriver') or shutil.which('chromedriver')
print(found or '$CHROMEDRIVER_BIN')
" 2>/dev/null || echo "$CHROMEDRIVER_BIN")

# ── Xvfb service ──────────────────────────────────────────────
cat > /etc/systemd/system/xvfb.service << 'XVFB'
[Unit]
Description=Xvfb Virtual Display :99
After=network.target

[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1280x720x24 -nolisten tcp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
XVFB

# ── Bot service ───────────────────────────────────────────────
cat > /etc/systemd/system/visitor-bot.service << SYSDSVC
[Unit]
Description=URL Auto Visitor Telegram Bot
After=network-online.target xvfb.service
Wants=network-online.target
Requires=xvfb.service

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$BOT_DIR
EnvironmentFile=$ENV_FILE
Environment=DISPLAY=:99
Environment=CHROMIUM_PATH=$REAL_CHROMIUM
Environment=CHROMEDRIVER_PATH=$REAL_CHROMEDRIVER
ExecStart=$VENV_DIR/bin/python3 $BOT_DIR/bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=visitor-bot

[Install]
WantedBy=multi-user.target
SYSDSVC

systemctl daemon-reload
systemctl enable xvfb.service
systemctl start xvfb.service
sleep 2
systemctl enable visitor-bot.service
systemctl start visitor-bot.service

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " SETUP COMPLETE — Bot is starting..."
echo "========================================"
echo ""
echo "  Bot dir     : $BOT_DIR"
echo "  Chromium    : $REAL_CHROMIUM"
echo "  Venv        : $VENV_DIR"
echo ""
echo "  SSH in and check logs:"
echo "  sudo journalctl -u visitor-bot -f"
echo ""
echo "  Setup log saved at: $LOG_FILE"
echo "========================================"
