#!/usr/bin/env bash
set -Eeuo pipefail
BASE=/etc/ddnswatch
SERVICE=/etc/systemd/system/ddnswatch-bot.service
RAW_BASE=${DDNSWATCH_RAW_BASE:-https://raw.githubusercontent.com/JudiLite/ddnswatch/main}

[[ $EUID -eq 0 ]] || { echo '错误：请使用 root 运行'; exit 1; }
command -v apt-get >/dev/null || { echo '错误：仅支持 Debian/Ubuntu'; exit 1; }
command -v systemctl >/dev/null || { echo '错误：系统不支持 systemd'; exit 1; }

install_app(){
  echo '[1/4] 安装依赖...'
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends python3 curl dnsutils ca-certificates sqlite3
  echo '[2/4] 安装程序...'
  install -d -m 700 "$BASE" "$BASE/logs" "$BASE/backups"
  if [[ -f ${SOURCE_DIR:-}/bot.py ]]; then install -m 700 "$SOURCE_DIR/bot.py" "$BASE/bot.py"; else curl -fsSL "$RAW_BASE/bot.py" -o "$BASE/bot.py"; chmod 700 "$BASE/bot.py"; fi
  if [[ -f $BASE/config.env ]]; then
    echo '保留现有配置：/etc/ddnswatch/config.env'
  else
    read -rsp 'Telegram Bot Token: ' token </dev/tty; echo >/dev/tty
    read -rp '管理员 Telegram User ID（多个用逗号分隔）: ' admins </dev/tty
    [[ -n $token && -n $admins ]] || { echo 'Token 和管理员 ID 不能为空'; exit 1; }
    read -rp 'SOCKS5 代理 URL（可留空，例如 socks5h://127.0.0.1:1080）: ' proxy </dev/tty
    umask 077
    { printf 'TELEGRAM_BOT_TOKEN=%q\n' "$token"; printf 'TELEGRAM_ADMIN_IDS=%q\n' "$admins"; printf 'SOCKS5_PROXY=%q\n' "$proxy"; } > "$BASE/config.env"
  fi
  echo '[3/4] 安装 systemd 服务...'
  cat > "$SERVICE" <<'EOF'
[Unit]
Description=DDNS Watch Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/etc/ddnswatch
EnvironmentFile=/etc/ddnswatch/config.env
ExecStart=/usr/bin/python3 /etc/ddnswatch/bot.py
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/etc/ddnswatch

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now ddnswatch-bot
  echo '[4/4] 安装完成。请在 Telegram 给 Bot 发送 /start'
  systemctl --no-pager --full status ddnswatch-bot || true
}

upgrade(){
  cp -a "$BASE/bot.py" "$BASE/backups/bot.py.$(date +%Y%m%d-%H%M%S)"
  curl -fsSL "$RAW_BASE/bot.py" -o "$BASE/bot.py.new"
  python3 -m py_compile "$BASE/bot.py.new"
  install -m 700 "$BASE/bot.py.new" "$BASE/bot.py"; rm -f "$BASE/bot.py.new"
  systemctl restart ddnswatch-bot
  echo '升级完成。'
}

backup(){
  local f="$BASE/backups/ddnswatch-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar -czf "$f" --exclude="$BASE/backups" "$BASE/config.env" "$BASE/ddnswatch.db" 2>/dev/null || true
  echo "备份完成：$f"
}

uninstall(){
  read -rp '确认彻底卸载并删除数据库？[y/N]: ' yn </dev/tty
  [[ $yn =~ ^[Yy]$ ]] || exit 0
  systemctl disable --now ddnswatch-bot >/dev/null 2>&1 || true
  rm -f "$SERVICE"; rm -rf "$BASE"; systemctl daemon-reload
  echo '已卸载。'
}

case ${1:-install} in
  install) install_app;;
  upgrade) upgrade;;
  backup) backup;;
  uninstall) uninstall;;
  *) echo "用法：$0 [install|upgrade|backup|uninstall]"; exit 1;;
esac
