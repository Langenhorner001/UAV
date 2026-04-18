#!/bin/bash

# ==============================================================
#  URL Auto Visitor Bot — AWS EC2 1-Click Deployment Script
#  Tested on: Ubuntu 22.04 LTS / 24.04 LTS
#
#  METHOD 1 — SSH Deploy (interactive):
#    chmod +x setup.sh && sudo ./setup.sh
#
#  METHOD 2 — AWS User Data (auto on first boot):
#    Use userdata.sh instead (see same folder)
# ==============================================================

set -e

# ── Colors ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
die()     { echo -e "${RED}[FAIL]${RESET}  $*"; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}── $* ──${RESET}\n"; }

# ── Banner ────────────────────────────────────────────────────
clear
echo -e "${BOLD}${CYAN}"
cat << 'BANNER'
 ██╗   ██╗██████╗ ██╗      █████╗ ██╗   ██╗████████╗ ██████╗
 ██║   ██║██╔══██╗██║     ██╔══██╗██║   ██║╚══██╔══╝██╔═══██╗
 ██║   ██║██████╔╝██║     ███████║██║   ██║   ██║   ██║   ██║
 ██║   ██║██╔══██╗██║     ██╔══██║╚██╗ ██╔╝   ██║   ██║   ██║
 ╚██████╔╝██║  ██║███████╗██║  ██║ ╚████╔╝    ██║   ╚██████╔╝
  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝  ╚═══╝     ╚═╝    ╚═════╝
              URL Auto Visitor Bot — AWS Deployer
BANNER
echo -e "${RESET}"

# ── Root check ────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Run as root: sudo ./setup.sh"

# ── Detect paths ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$SCRIPT_DIR"
BOT_USER="${SUDO_USER:-ubuntu}"
VENV_DIR="$BOT_DIR/venv"
ENV_FILE="$BOT_DIR/.env"

info "Bot directory : $BOT_DIR"
info "Running as    : $BOT_USER"

# ── BOT_TOKEN ─────────────────────────────────────────────────
step "Telegram Bot Token"
if [[ -f "$ENV_FILE" ]] && grep -q "BOT_TOKEN=" "$ENV_FILE" 2>/dev/null; then
    warn ".env already exists — token not overwritten"
    warn "To update: nano $ENV_FILE && sudo systemctl restart visitor-bot"
else
    echo -e "${BOLD}Get your token from @BotFather on Telegram${RESET}"
    while true; do
        read -rp "Enter BOT_TOKEN: " BOT_TOKEN_INPUT
        [[ -n "$BOT_TOKEN_INPUT" ]] && break
        warn "Token cannot be empty. Try again."
    done
    printf "BOT_TOKEN=%s\n" "$BOT_TOKEN_INPUT" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown "$BOT_USER":"$BOT_USER" "$ENV_FILE"
    success ".env saved (permissions: 600)"
fi

# ── 1. System Update ──────────────────────────────────────────
step "1/7  System Update"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
success "System up to date"

# ── 2. Core tools ─────────────────────────────────────────────
step "2/7  Core Tools (git, curl, screen)"
apt-get install -y -qq git curl wget unzip screen net-tools
success "Core tools ready"

# ── 3. Python 3.11 ────────────────────────────────────────────
step "3/7  Python 3.11"
if ! command -v python3.11 &>/dev/null; then
    info "Adding deadsnakes PPA..."
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null
    apt-get update -qq
fi
apt-get install -y -qq python3.11 python3.11-venv python3.11-dev python3-pip
success "Python $(python3.11 --version) installed"

# ── 4. Tor ────────────────────────────────────────────────────
step "4/7  Tor"
apt-get install -y -qq tor
systemctl stop tor 2>/dev/null || true
systemctl disable tor 2>/dev/null || true
success "Tor installed (bot manages its own daemon)"

# ── 5. Chromium + ChromeDriver ────────────────────────────────
step "5/7  Chromium + ChromeDriver"

CHROMIUM_BIN=""
CHROMEDRIVER_BIN=""

# Try apt first (Ubuntu 22.04)
if apt-cache show chromium-browser &>/dev/null 2>&1; then
    apt-get install -y -qq chromium-browser chromium-chromedriver
    CHROMIUM_BIN="$(command -v chromium-browser 2>/dev/null || echo /usr/bin/chromium-browser)"
    CHROMEDRIVER_BIN="$(command -v chromedriver 2>/dev/null || echo /usr/lib/chromium-browser/chromedriver)"
    success "Chromium installed via apt"
elif apt-cache show chromium &>/dev/null 2>&1; then
    apt-get install -y -qq chromium chromium-driver
    CHROMIUM_BIN="$(command -v chromium 2>/dev/null || echo /usr/bin/chromium)"
    CHROMEDRIVER_BIN="$(command -v chromedriver 2>/dev/null || echo /usr/bin/chromedriver)"
    success "Chromium installed via apt (chromium pkg)"
else
    # Fallback: snap
    info "Installing Chromium via snap (may take a minute)..."
    snap install chromium 2>/dev/null || true
    CHROMIUM_BIN="/snap/bin/chromium"
    # snap chromium ships chromedriver as chromium.chromedriver
    if [ -f "/snap/bin/chromium.chromedriver" ]; then
        CHROMEDRIVER_BIN="/snap/bin/chromium.chromedriver"
    else
        CHROMEDRIVER_BIN="/snap/bin/chromedriver"
    fi
    success "Chromium installed via snap"
fi

# ── 6. Xvfb + libraries ───────────────────────────────────────
step "6/7  Xvfb + Fonts + Libraries"
apt-get install -y -qq \
    xvfb \
    fonts-liberation fonts-noto fonts-noto-cjk xfonts-base \
    libgbm1 libnss3 libxss1 libasound2 \
    libatk-bridge2.0-0 libgtk-3-0 libx11-xcb1 \
    libdrm2 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 \
    2>/dev/null || true
success "Xvfb + fonts + libs installed"

# ── 7. Python venv + packages ─────────────────────────────────
step "7/7  Python Virtual Environment"
sudo -u "$BOT_USER" python3.11 -m venv "$VENV_DIR"
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -r "$BOT_DIR/requirements.txt" -q
success "Python venv ready: $VENV_DIR"

# ── Detect actual binary paths ────────────────────────────────
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

# ── Xvfb systemd service ──────────────────────────────────────
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

# ── Bot systemd service ───────────────────────────────────────
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
sleep 3
systemctl enable visitor-bot.service
systemctl start visitor-bot.service

# ── SCREEN shortcut (optional) ────────────────────────────────
# Alternative: run in screen without systemd
# sudo -u "$BOT_USER" bash -c "
#   source $VENV_DIR/bin/activate
#   DISPLAY=:99 CHROMIUM_PATH=$REAL_CHROMIUM CHROMEDRIVER_PATH=$REAL_CHROMEDRIVER \
#   screen -dmS visitor-bot python3 $BOT_DIR/bot.py
# "

# ── Final summary ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║           DEPLOYMENT COMPLETE ✓                      ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Paths:${RESET}"
echo -e "    Bot dir       : $BOT_DIR"
echo -e "    Venv          : $VENV_DIR"
echo -e "    Chromium      : $REAL_CHROMIUM"
echo -e "    Chromedriver  : $REAL_CHROMEDRIVER"
echo -e "    Service file  : /etc/systemd/system/visitor-bot.service"
echo ""
echo -e "  ${BOLD}Useful Commands:${RESET}"
echo ""
echo -e "  ${CYAN}# Stream live logs${RESET}"
echo -e "  sudo journalctl -u visitor-bot -f"
echo ""
echo -e "  ${CYAN}# Check status${RESET}"
echo -e "  sudo systemctl status visitor-bot"
echo ""
echo -e "  ${CYAN}# Restart / Stop${RESET}"
echo -e "  sudo systemctl restart visitor-bot"
echo -e "  sudo systemctl stop visitor-bot"
echo ""
echo -e "  ${CYAN}# Change BOT_TOKEN${RESET}"
echo -e "  nano $ENV_FILE"
echo -e "  sudo systemctl restart visitor-bot"
echo ""
sleep 2
echo -e "${BOLD}Current bot status:${RESET}"
systemctl status visitor-bot --no-pager -l 2>&1 | head -25 || true
