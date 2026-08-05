#!/usr/bin/env bash
set -Eeuo pipefail

APP=ddnswatch
RUNTIME=/usr/local/lib/ddnswatch/ddnswatch.sh
MANAGER=/usr/local/sbin/ddnswatch
SERVICE=/etc/systemd/system/ddnswatch@.service
CONFIG_DIR=/etc/ddnswatch
STATE_DIR=/var/lib/ddnswatch
LOG_DIR=/var/log/ddnswatch

if (( EUID != 0 )); then
  echo "错误：请使用 root 运行（curl ... | sudo bash）" >&2
  exit 1
fi
command -v systemctl >/dev/null || { echo "错误：系统不支持 systemd" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "错误：仅支持 Debian/Ubuntu（apt）" >&2; exit 1; }

echo "[1/4] 安装依赖..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends curl dnsutils netcat-openbsd ca-certificates
install -d -m 755 /usr/local/lib/ddnswatch "$STATE_DIR" "$LOG_DIR"
install -d -m 700 "$CONFIG_DIR"

echo "[2/4] 安装监控程序..."
cat > "$RUNTIME" <<'RUNTIME_EOF'
#!/usr/bin/env bash
set -u
INSTANCE=${1:-}
CONFIG_DIR=/etc/ddnswatch
STATE_DIR=/var/lib/ddnswatch
LOG_DIR=/var/log/ddnswatch
[[ "$INSTANCE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]] || { echo "无效实例名"; exit 1; }
CONF="$CONFIG_DIR/$INSTANCE.conf"
[[ -r "$CONF" ]] || { echo "配置不存在：$CONF"; exit 1; }
# 配置由 root 管理器使用 printf %q 生成
source "$CONF"
: "${TARGET_INPUT:?缺少 TARGET_INPUT}" "${TARGET_MODE:?缺少 TARGET_MODE}" "${PORT:?缺少 PORT}"
DNS_SERVER=${DNS_SERVER:-223.5.5.5}; TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}; TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-}
SOCKS5_HOST=${SOCKS5_HOST:-}; SOCKS5_PORT=${SOCKS5_PORT:-}; SOCKS5_USER=${SOCKS5_USER:-}; SOCKS5_PASS=${SOCKS5_PASS:-}
HOST_TAG=${HOST_TAG:-$(hostname)}; INTERVAL=${INTERVAL:-60}
STATE="$STATE_DIR/$INSTANCE.state"
mkdir -p "$STATE_DIR" "$LOG_DIR"

log(){ printf '[%s] %s\n' "$(date '+%F %T')" "$1" | tee -a "$LOG_DIR/${INSTANCE}_$(date +%F).log"; }
duration(){ local n=$1; printf '%d天 %d小时 %d分钟 %d秒' "$((n/86400))" "$(((n%86400)/3600))" "$(((n%3600)/60))" "$((n%60))"; }
resolve_ip(){
  if [[ $TARGET_MODE == ip ]]; then printf '%s\n' "$TARGET_INPUT";
  else dig +time=5 +tries=1 @"$DNS_SERVER" "$TARGET_INPUT" A +short 2>/dev/null | awk '/^([0-9]{1,3}\.){3}[0-9]{1,3}$/{print;exit}'; fi
}
test_port(){ nc -z -w 5 "$1" "$PORT" >/dev/null 2>&1; }
send_tg(){
  [[ -n $TELEGRAM_BOT_TOKEN && -n $TELEGRAM_CHAT_ID ]] || return 0
  local args=(-sS --fail --max-time 20)
  if [[ -n $SOCKS5_HOST && -n $SOCKS5_PORT ]]; then
    args+=(--proxy "socks5h://$SOCKS5_HOST:$SOCKS5_PORT")
    [[ -n $SOCKS5_USER || -n $SOCKS5_PASS ]] && args+=(--proxy-user "$SOCKS5_USER:$SOCKS5_PASS")
  fi
  curl "${args[@]}" -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" --data-urlencode "text=$1" >/dev/null 2>&1 || log "Telegram 通知发送失败"
}
init_state(){
  [[ -f $STATE ]] && return
  umask 077
  cat > "$STATE" <<'EOF'
CURRENT_IP=''
STATUS='INIT'
STREAK_START_TS=0
CURRENT_STREAK_SECONDS=0
MAX_STREAK_SECONDS=0
MAX_STREAK_IP=''
FAIL_NOTIFIED=0
DNS_FAIL_NOTIFIED=0
DOWN_START_TS=0
LAST_GOOD_IP=''
LAST_GOOD_STREAK_SECONDS=0
EOF
}
save_state(){
  local tmp="$STATE.tmp"
  umask 077
  {
    printf 'CURRENT_IP=%q\n' "$CURRENT_IP"; printf 'STATUS=%q\n' "$STATUS"
    printf 'STREAK_START_TS=%q\n' "$STREAK_START_TS"; printf 'CURRENT_STREAK_SECONDS=%q\n' "$CURRENT_STREAK_SECONDS"
    printf 'MAX_STREAK_SECONDS=%q\n' "$MAX_STREAK_SECONDS"; printf 'MAX_STREAK_IP=%q\n' "$MAX_STREAK_IP"
    printf 'FAIL_NOTIFIED=%q\n' "$FAIL_NOTIFIED"; printf 'DNS_FAIL_NOTIFIED=%q\n' "$DNS_FAIL_NOTIFIED"
    printf 'DOWN_START_TS=%q\n' "$DOWN_START_TS"; printf 'LAST_GOOD_IP=%q\n' "$LAST_GOOD_IP"
    printf 'LAST_GOOD_STREAK_SECONDS=%q\n' "$LAST_GOOD_STREAK_SECONDS"
  } > "$tmp"; mv -f "$tmp" "$STATE"
}

log "监控启动：$TARGET_INPUT:$PORT，模式=$TARGET_MODE，间隔=${INTERVAL}s"
while true; do
  init_state; source "$STATE"; now=$(date +%s)
  if [[ -z $CURRENT_IP ]]; then
    ip=$(resolve_ip || true)
    if [[ -z $ip ]]; then
      log "初始化解析失败：$TARGET_INPUT"
      if [[ $TARGET_MODE == domain && $DNS_FAIL_NOTIFIED -eq 0 ]]; then send_tg "⚠️ DNS解析失败\n主机：$HOST_TAG\n实例：$INSTANCE\n目标：$TARGET_INPUT:$PORT\nDNS：$DNS_SERVER\n时间：$(date '+%F %T')"; DNS_FAIL_NOTIFIED=1; fi
      STATUS=DOWN; save_state; sleep "$INTERVAL"; continue
    fi
    CURRENT_IP=$ip; DNS_FAIL_NOTIFIED=0; log "初始化 IP=$CURRENT_IP"
  fi
  if test_port "$CURRENT_IP"; then
    if (( STREAK_START_TS == 0 )); then STREAK_START_TS=$now; CURRENT_STREAK_SECONDS=0; else CURRENT_STREAK_SECONDS=$((now-STREAK_START_TS)); fi
    if (( CURRENT_STREAK_SECONDS > MAX_STREAK_SECONDS )); then MAX_STREAK_SECONDS=$CURRENT_STREAK_SECONDS; MAX_STREAK_IP=$CURRENT_IP; fi
    STATUS=UP; FAIL_NOTIFIED=0; DNS_FAIL_NOTIFIED=0; DOWN_START_TS=0
    LAST_GOOD_IP=$CURRENT_IP; LAST_GOOD_STREAK_SECONDS=$CURRENT_STREAK_SECONDS
    log "联通正常 IP=$CURRENT_IP 当前连续=$(duration "$CURRENT_STREAK_SECONDS") 历史最长=$(duration "$MAX_STREAK_SECONDS")"
    save_state; sleep "$INTERVAL"; continue
  fi
  old_streak=$CURRENT_STREAK_SECONDS
  if (( FAIL_NOTIFIED == 0 )); then
    log "TCP 不通 IP=$CURRENT_IP"
    send_tg "🚨 TCP异常\n主机：$HOST_TAG\n实例：$INSTANCE\n目标：$TARGET_INPUT:$PORT\nIP：$CURRENT_IP\n此前连续可用：$((old_streak/60)) 分钟\n时间：$(date '+%F %T')"
    FAIL_NOTIFIED=1; (( DOWN_START_TS == 0 )) && DOWN_START_TS=$now
    LAST_GOOD_IP=$CURRENT_IP; LAST_GOOD_STREAK_SECONDS=$old_streak
  else log "TCP 仍不通 IP=$CURRENT_IP"; (( DOWN_START_TS == 0 )) && DOWN_START_TS=$now; fi
  if [[ $TARGET_MODE == domain ]]; then
    ip=$(resolve_ip || true)
    if [[ -z $ip ]]; then
      log "故障后重新解析失败"
      if (( DNS_FAIL_NOTIFIED == 0 )); then send_tg "⚠️ DNS解析失败\n主机：$HOST_TAG\n实例：$INSTANCE\n目标：$TARGET_INPUT:$PORT\nDNS：$DNS_SERVER\n时间：$(date '+%F %T')"; DNS_FAIL_NOTIFIED=1; fi
    elif [[ $ip != "$CURRENT_IP" ]] && test_port "$ip"; then
      down=$((now-DOWN_START_TS)); old=$CURRENT_IP
      log "IP 切换成功：$old -> $ip"
      send_tg "🔄 IP切换完成\n主机：$HOST_TAG\n实例：$INSTANCE\n目标：$TARGET_INPUT:$PORT\nIP：$old → $ip\n旧IP持续可用：$((LAST_GOOD_STREAK_SECONDS/60)) 分钟\n失效时长：$((down/60)) 分钟\n时间：$(date '+%F %T')"
      CURRENT_IP=$ip; STATUS=UP; STREAK_START_TS=$now; CURRENT_STREAK_SECONDS=0; FAIL_NOTIFIED=0; DNS_FAIL_NOTIFIED=0; DOWN_START_TS=0
      LAST_GOOD_IP=$ip; LAST_GOOD_STREAK_SECONDS=0; save_state; sleep "$INTERVAL"; continue
    fi
  fi
  STATUS=DOWN; STREAK_START_TS=0; CURRENT_STREAK_SECONDS=0; save_state; sleep "$INTERVAL"
done
RUNTIME_EOF
chmod 755 "$RUNTIME"

echo "[3/4] 安装管理器..."
cat > "$MANAGER" <<'MANAGER_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
CONFIG_DIR=/etc/ddnswatch; STATE_DIR=/var/lib/ddnswatch; LOG_DIR=/var/log/ddnswatch
[[ $EUID -eq 0 ]] || { echo "请使用 sudo ddnswatch"; exit 1; }
TTY=/dev/tty; [[ -r $TTY && -w $TTY ]] || { echo "需要交互终端"; exit 1; }
exec 3<>"$TTY"
ask(){ local __v=$1 __p=$2 __d=${3-} x; printf '%s' "$__p" >&3; IFS= read -r x <&3 || exit 1; [[ -n $x ]] || x=$__d; printf -v "$__v" '%s' "$x"; }
secret(){ local __v=$1 __p=$2 x; printf '%s' "$__p" >&3; IFS= read -rs x <&3 || exit 1; printf '\n' >&3; printf -v "$__v" '%s' "$x"; }
pause(){ printf '按回车继续...' >&3; IFS= read -r _ <&3 || true; }
valid_name(){ [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; }
valid_ipv4(){ local IFS=. a b c d; read -r a b c d <<<"$1"; [[ $a =~ ^[0-9]+$ && $b =~ ^[0-9]+$ && $c =~ ^[0-9]+$ && $d =~ ^[0-9]+$ ]] && ((a<=255&&b<=255&&c<=255&&d<=255)); }
valid_domain(){ [[ ${#1} -le 253 && $1 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && $1 == *.* ]]; }
write_conf(){
  local f="$CONFIG_DIR/$INSTANCE.conf" tmp="$f.tmp"; umask 077
  { for v in TARGET_INPUT TARGET_MODE PORT DNS_SERVER TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID SOCKS5_HOST SOCKS5_PORT SOCKS5_USER SOCKS5_PASS HOST_TAG INTERVAL; do printf '%s=%q\n' "$v" "${!v}"; done; } > "$tmp"
  chmod 600 "$tmp"; mv -f "$tmp" "$f"
}
collect(){
  while :; do ask INSTANCE '实例名（字母/数字/_/-，最多32位）: '; valid_name "$INSTANCE" && break; echo '实例名无效' >&3; done
  while :; do ask TARGET_INPUT '监测域名或 IPv4: '; if valid_ipv4 "$TARGET_INPUT"; then TARGET_MODE=ip; break; elif valid_domain "$TARGET_INPUT"; then TARGET_MODE=domain; break; else echo '域名或 IPv4 无效' >&3; fi; done
  if [[ $TARGET_MODE == ip ]]; then ask PORT '端口 [80]: ' 80; DNS_SERVER=''; else ask PORT '端口 [443]: ' 443; ask DNS_SERVER 'DNS服务器 [223.5.5.5]: ' 223.5.5.5; fi
  [[ $PORT =~ ^[0-9]+$ ]] && ((PORT>=1&&PORT<=65535)) || { echo '端口无效' >&3; return 1; }
  echo 'Telegram 留空即关闭通知。' >&3
  secret TELEGRAM_BOT_TOKEN 'Telegram Bot Token（输入不显示）: '
  ask TELEGRAM_CHAT_ID 'Telegram Chat ID: '
  ask SOCKS5_HOST 'SOCKS5 主机/IP（留空不用）: '
  if [[ -n $SOCKS5_HOST ]]; then ask SOCKS5_PORT 'SOCKS5 端口: '; [[ $SOCKS5_PORT =~ ^[0-9]+$ ]] && ((SOCKS5_PORT>=1&&SOCKS5_PORT<=65535)) || { echo '代理端口无效' >&3; return 1; }; ask SOCKS5_USER 'SOCKS5 用户名（可空）: '; secret SOCKS5_PASS 'SOCKS5 密码（输入不显示，可空）: '; else SOCKS5_PORT=''; SOCKS5_USER=''; SOCKS5_PASS=''; fi
  ask HOST_TAG "主机标识 [$(hostname)]: " "$(hostname)"
  ask INTERVAL '检测间隔秒数 [60]: ' 60
  [[ $INTERVAL =~ ^[0-9]+$ ]] && ((INTERVAL>=10&&INTERVAL<=86400)) || { echo '间隔必须为 10-86400 秒' >&3; return 1; }
}
add(){ collect || return; [[ ! -e $CONFIG_DIR/$INSTANCE.conf ]] || { echo '实例已存在' >&3; return; }; write_conf; systemctl enable --now "ddnswatch@$INSTANCE.service"; echo "已创建并启动：$INSTANCE" >&3; }
list(){ echo '实例        状态      目标' >&3; shopt -s nullglob; local f i st target; for f in "$CONFIG_DIR"/*.conf; do i=$(basename "$f" .conf); st=$(systemctl is-active "ddnswatch@$i.service" 2>/dev/null || true); target=$(bash -c 'source "$1"; printf "%s:%s" "$TARGET_INPUT" "$PORT"' _ "$f"); printf '%-12s %-9s %s\n' "$i" "$st" "$target" >&3; done; }
choose(){ list; ask INSTANCE '实例名: '; valid_name "$INSTANCE" && [[ -f $CONFIG_DIR/$INSTANCE.conf ]] || { echo '实例不存在' >&3; return 1; }; }
delete_one(){ choose || return; ask yn "确认删除 $INSTANCE？[y/N]: " N; [[ $yn =~ ^[Yy]$ ]] || return; systemctl disable --now "ddnswatch@$INSTANCE.service" >/dev/null 2>&1 || true; rm -f "$CONFIG_DIR/$INSTANCE.conf" "$STATE_DIR/$INSTANCE.state" "$LOG_DIR/${INSTANCE}_"*.log; echo '已删除' >&3; }
restart_one(){ choose || return; systemctl restart "ddnswatch@$INSTANCE.service"; echo '已重启' >&3; }
status_one(){ choose || return; systemctl --no-pager --full status "ddnswatch@$INSTANCE.service" >&3 || true; }
logs_one(){ choose || return; echo '按 Ctrl+C 退出日志' >&3; journalctl -u "ddnswatch@$INSTANCE.service" -f; }
test_tg(){
  choose || return; source "$CONFIG_DIR/$INSTANCE.conf"; [[ -n ${TELEGRAM_BOT_TOKEN:-} && -n ${TELEGRAM_CHAT_ID:-} ]] || { echo '该实例未配置 Telegram' >&3; return; }
  local args=(-sS --fail --max-time 20); if [[ -n ${SOCKS5_HOST:-} && -n ${SOCKS5_PORT:-} ]]; then args+=(--proxy "socks5h://$SOCKS5_HOST:$SOCKS5_PORT"); [[ -n ${SOCKS5_USER:-} || -n ${SOCKS5_PASS:-} ]] && args+=(--proxy-user "$SOCKS5_USER:$SOCKS5_PASS"); fi
  if curl "${args[@]}" -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" --data-urlencode "text=✅ DDNS Watch 测试成功\n实例：$INSTANCE\n主机：$HOST_TAG\n时间：$(date '+%F %T')" >/dev/null; then echo '测试消息发送成功' >&3; else echo '发送失败，请检查 Token、Chat ID、网络或代理' >&3; fi
}
uninstall_all(){ ask yn '确认彻底卸载 DDNS Watch？[y/N]: ' N; [[ $yn =~ ^[Yy]$ ]] || return; shopt -s nullglob; local f i; for f in "$CONFIG_DIR"/*.conf; do i=$(basename "$f" .conf); systemctl disable --now "ddnswatch@$i.service" >/dev/null 2>&1 || true; done; rm -rf /etc/ddnswatch /var/lib/ddnswatch /var/log/ddnswatch /usr/local/lib/ddnswatch; rm -f /etc/systemd/system/ddnswatch@.service /usr/local/sbin/ddnswatch; systemctl daemon-reload; echo '已彻底卸载' >&3; exit 0; }
while :; do
  printf '\n===== DDNS Watch 管理器 =====\n1) 添加监测目标\n2) 查看实例列表\n3) 查看服务状态\n4) 查看实时日志\n5) 重启实例\n6) 测试 Telegram\n7) 删除实例\n8) 彻底卸载\n0) 退出\n' >&3
  ask c '请选择: '
  case $c in 1)add;;2)list;;3)status_one;;4)logs_one;;5)restart_one;;6)test_tg;;7)delete_one;;8)uninstall_all;;0)exit;;*)echo '无效选项' >&3;; esac
  [[ $c == 4 || $c == 0 ]] || pause
done
MANAGER_EOF
chmod 750 "$MANAGER"

cat > "$SERVICE" <<EOF
[Unit]
Description=DDNS Watch (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=$RUNTIME %i
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$STATE_DIR $LOG_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo "[4/4] 安装完成"
echo "以后运行管理器：sudo ddnswatch"
if [[ -r /dev/tty && -w /dev/tty ]]; then
  echo "正在打开交互配置..."
  "$MANAGER" </dev/tty >/dev/tty 2>/dev/tty
else
  echo "当前没有交互终端，请稍后运行：sudo ddnswatch"
fi
