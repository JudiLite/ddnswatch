#!/usr/bin/env python3
import json, logging, os, re, socket, sqlite3, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = "/etc/ddnswatch"
DB_PATH = os.getenv("DDNSWATCH_DB", f"{BASE}/ddnswatch.db")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMINS = {int(x) for x in re.split(r"[, ]+", os.getenv("TELEGRAM_ADMIN_IDS", "")) if x.isdigit()}
PROXY = os.getenv("SOCKS5_PROXY", "").strip()
LOG_PATH = os.getenv("DDNSWATCH_LOG", f"{BASE}/logs/ddnswatch.log")
API = f"https://api.telegram.org/bot{TOKEN}"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()])
log = logging.getLogger("ddnswatch")
DB_LOCK = threading.RLock()
SESSIONS = {}
PANELS = {}
RUNNING = set()
POOL = ThreadPoolExecutor(max_workers=20)

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, target TEXT NOT NULL,
 target_type TEXT NOT NULL, port INTEGER NOT NULL, dns_server TEXT NOT NULL DEFAULT '',
 interval_seconds INTEGER NOT NULL DEFAULT 60, dns_mode TEXT NOT NULL DEFAULT 'failure',
 enabled INTEGER NOT NULL DEFAULT 1, notify_chat_id INTEGER NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS states(
 target_id INTEGER PRIMARY KEY, current_ip TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'INIT',
 last_check_at INTEGER NOT NULL DEFAULT 0, next_check_at INTEGER NOT NULL DEFAULT 0,
 streak_started_at INTEGER NOT NULL DEFAULT 0, max_streak_seconds INTEGER NOT NULL DEFAULT 0,
 down_since INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
 last_dns_ip TEXT NOT NULL DEFAULT '', FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, target_id INTEGER NOT NULL, event_type TEXT NOT NULL,
 message TEXT NOT NULL, created_at INTEGER NOT NULL);
"""

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with DB_LOCK, db() as c: c.executescript(SCHEMA)
    try: os.chmod(DB_PATH, 0o600)
    except OSError: pass

def api(method, data=None, timeout=35):
    cmd = ["curl", "-sS", "--fail", "--max-time", str(timeout)]
    if PROXY: cmd += ["--proxy", PROXY]
    cmd += [f"{API}/{method}"]
    for k, v in (data or {}).items():
        if isinstance(v, (dict, list)): v = json.dumps(v, ensure_ascii=False)
        cmd += ["--data-urlencode", f"{k}={v}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
        if p.returncode: raise RuntimeError(p.stderr.strip() or f"curl {p.returncode}")
        r = json.loads(p.stdout)
        if not r.get("ok"): raise RuntimeError(str(r))
        return r.get("result")
    except Exception as e:
        log.warning("Telegram API %s 失败: %s", method, e)
        return None

def send(chat, text, keyboard=None):
    d = {"chat_id": chat, "text": text}
    if keyboard: d["reply_markup"] = {"inline_keyboard": keyboard}
    return api("sendMessage", d)

def edit(chat, mid, text, keyboard=None):
    d = {"chat_id": chat, "message_id": mid, "text": text}
    if keyboard: d["reply_markup"] = {"inline_keyboard": keyboard}
    return api("editMessageText", d)

def delete_message(chat, mid):
    if mid: api("deleteMessage", {"chat_id": chat, "message_id": mid})

def panel(chat, text, keyboard=None, mid=None):
    """交互界面始终复用一条 Bot 消息，无法编辑时才重建。"""
    mid = mid or PANELS.get(chat)
    if mid:
        result = edit(chat, mid, text, keyboard)
        if result:
            PANELS[chat] = mid
            return result
    result = send(chat, text, keyboard)
    if isinstance(result, dict) and result.get("message_id"):
        old = PANELS.get(chat)
        PANELS[chat] = result["message_id"]
        if old and old != PANELS[chat]: delete_message(chat, old)
    return result

def answer(cid, text=""):
    api("answerCallbackQuery", {"callback_query_id": cid, "text": text})

def main_keyboard():
    return [[{"text":"➕ 添加监测","callback_data":"add"},{"text":"📋 监测列表","callback_data":"list"}],
            [{"text":"📊 状态总览","callback_data":"summary"},{"text":"🔄 全部检测","callback_data":"checkall"}],
            [{"text":"📝 最近事件","callback_data":"events"},{"text":"ℹ️ 帮助","callback_data":"help"}]]

def menu(chat, text="🖥️ DDNS Watch\n\n请选择操作：", mid=None):
    return panel(chat, text, main_keyboard(), mid)

def valid_target(s):
    try: socket.inet_aton(s); return "ip"
    except OSError: pass
    if len(s) <= 253 and "." in s and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", s): return "domain"
    return None

def duration(n):
    n=max(0,int(n)); d,n=divmod(n,86400); h,n=divmod(n,3600); m,s=divmod(n,60)
    parts=[]
    if d: parts.append(f"{d}天")
    if h: parts.append(f"{h}小时")
    if m: parts.append(f"{m}分钟")
    if not parts or s: parts.append(f"{s}秒")
    return " ".join(parts)

def resolve_doh(host):
    """传统 DNS 不可用时，通过 HTTPS 443 查询 Google DNS。"""
    cmd = ["curl", "-sS", "--fail", "--max-time", "10", "-G",
           "https://dns.google/resolve", "--data-urlencode", f"name={host}",
           "--data-urlencode", "type=A"]
    if PROXY: cmd[1:1] = ["--proxy", PROXY]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=13)
        if p.returncode: return "", (p.stderr or "DoH 请求失败").strip()
        result = json.loads(p.stdout)
        for answer in result.get("Answer", []):
            if answer.get("type") == 1:
                ip = str(answer.get("data", "")).strip()
                try:
                    socket.inet_aton(ip)
                    if ip.count(".") == 3: return ip, ""
                except OSError: pass
        return "", f"DoH 未返回 IPv4 A 记录（状态 {result.get('Status', '未知')}）"
    except Exception as e: return "", f"DoH 解析失败：{e}"

def resolve(host, server):
    """优先查询指定 DNS；失败后自动回退到 DNS-over-HTTPS。"""
    name = host.rstrip(".")
    seen = set()
    dns_error = ""
    try:
        for _ in range(6):
            if name.lower() in seen:
                dns_error = "DNS CNAME 循环"; break
            seen.add(name.lower())
            p = subprocess.run(
                ["dig", "+time=4", "+tries=1", f"@{server}", name, "A", "+short"],
                capture_output=True, text=True, timeout=7)
            lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
            for x in lines:
                try:
                    socket.inet_aton(x)
                    if x.count(".") == 3: return x, ""
                except OSError: pass
            cname = next((x.rstrip(".") for x in lines
                          if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.?", x)), "")
            if cname and cname.lower() != name.lower():
                name = cname; continue
            detail = (p.stderr or "").strip()
            dns_error = (f"DNS 查询失败（{server}）：{detail or 'dig 退出码 '+str(p.returncode)}"
                         if p.returncode else f"DNS 未返回 IPv4 A 记录（{server}）")
            break
        else: dns_error = "DNS CNAME 层级超过限制"
    except subprocess.TimeoutExpired:
        dns_error = f"DNS 查询超时（{server}:53）"
    except Exception as e:
        dns_error = f"DNS 解析失败：{e}"
    ip, doh_error = resolve_doh(host)
    if ip:
        log.warning("%s；已通过 DoH 成功解析 %s -> %s", dns_error, host, ip)
        return ip, ""
    return "", f"{dns_error}；DoH 回退失败：{doh_error}"

def tcp_test(ip, port):
    try:
        with socket.create_connection((ip, int(port)), timeout=5): return True, ""
    except socket.timeout: return False, "连接超时"
    except Exception as e: return False, str(e)

def event(c, tid, typ, msg):
    c.execute("INSERT INTO events(target_id,event_type,message,created_at) VALUES(?,?,?,?)",(tid,typ,msg,int(time.time())))

def notify(chat, text):
    send(chat, text)

def check_target(tid, manual=False):
    with DB_LOCK, db() as c:
        t=c.execute("SELECT * FROM targets WHERE id=?",(tid,)).fetchone()
        if not t or (not t["enabled"] and not manual): return
        s=c.execute("SELECT * FROM states WHERE target_id=?",(tid,)).fetchone()
        if not s:
            c.execute("INSERT INTO states(target_id) VALUES(?)",(tid,)); s=c.execute("SELECT * FROM states WHERE target_id=?",(tid,)).fetchone()
        old=dict(s); now=int(time.time()); current=old["current_ip"]
    dns_ip=""; dns_err=""
    if t["target_type"]=="ip": current=t["target"]
    elif not current or t["dns_mode"]=="continuous":
        dns_ip,dns_err=resolve(t["target"],t["dns_server"])
        if dns_ip and (not current or t["dns_mode"]=="continuous"): current=dns_ip
    ok=False; err=dns_err
    if current: ok,err=tcp_test(current,t["port"])
    # 故障触发模式：旧 IP 不通后解析并尝试新 IP
    switched=False; old_ip=old["current_ip"]
    if not ok and t["target_type"]=="domain" and t["dns_mode"]=="failure":
        dns_ip,dns_err=resolve(t["target"],t["dns_server"])
        if dns_ip and dns_ip != current:
            new_ok,new_err=tcp_test(dns_ip,t["port"])
            if new_ok: current=dns_ip; ok=True; err=""; switched=True
            else: err=new_err
        elif dns_err: err=dns_err
    if t["target_type"]=="domain" and t["dns_mode"]=="continuous" and old_ip and current!=old_ip and ok: switched=True
    notices=[]
    with DB_LOCK, db() as c:
        status="UP" if ok else "DOWN"; streak=old["streak_started_at"]; down=old["down_since"]; maxs=old["max_streak_seconds"]
        if ok:
            if old["status"]!="UP" or switched: streak=now
            if streak<=0: streak=now
            maxs=max(maxs,now-streak)
            if switched:
                msg=f"🔄 DDNS IP 切换完成\n\n名称：{t['name']}\n目标：{t['target']}:{t['port']}\n旧 IP：{old_ip or '未知'}\n新 IP：{current}\n中断时长：{duration(now-down) if down else '0秒'}\n时间：{datetime.now():%F %T}"
                notices.append(msg); event(c,tid,"IP_SWITCH",msg)
            elif old["status"]=="DOWN":
                msg=f"✅ 服务已恢复\n\n名称：{t['name']}\n目标：{t['target']}:{t['port']}\nIP：{current}\n故障持续：{duration(now-down) if down else '未知'}\n时间：{datetime.now():%F %T}"
                notices.append(msg); event(c,tid,"RECOVERY",msg)
            down=0
        else:
            streak=0
            if old["status"]!="DOWN":
                down=now
                typ="DNS_FAILURE" if not current or dns_err else "TCP_DOWN"
                title="⚠️ DNS 解析失败" if typ=="DNS_FAILURE" else "🚨 TCP 连接异常"
                msg=f"{title}\n\n名称：{t['name']}\n目标：{t['target']}:{t['port']}\nIP：{current or '无'}\n错误：{err or '不可达'}\n时间：{datetime.now():%F %T}"
                notices.append(msg); event(c,tid,typ,msg)
            elif down<=0: down=now
        c.execute("""UPDATE states SET current_ip=?,status=?,last_check_at=?,next_check_at=?,streak_started_at=?,max_streak_seconds=?,down_since=?,last_error=?,last_dns_ip=? WHERE target_id=?""",
                  (current,status,now,now+t["interval_seconds"],streak,maxs,down,err,"" if t["target_type"]=="ip" else (dns_ip or old["last_dns_ip"]),tid))
    for msg in notices: notify(t["notify_chat_id"],msg)

def scheduled_check(tid):
    try: check_target(tid)
    except Exception: log.exception("检测目标 %s 失败",tid)
    finally:
        with DB_LOCK: RUNNING.discard(tid)

def scheduler():
    while True:
        try:
            now=int(time.time())
            with DB_LOCK, db() as c:
                rows=c.execute("SELECT t.id FROM targets t LEFT JOIN states s ON s.target_id=t.id WHERE t.enabled=1 AND COALESCE(s.next_check_at,0)<=?",(now,)).fetchall()
                for r in rows:
                    tid=r["id"]
                    if tid not in RUNNING: RUNNING.add(tid); POOL.submit(scheduled_check,tid)
        except Exception: log.exception("调度器异常")
        time.sleep(2)

def get_target(tid):
    with DB_LOCK, db() as c: return c.execute("SELECT t.*,s.* FROM targets t LEFT JOIN states s ON s.target_id=t.id WHERE t.id=?",(tid,)).fetchone()

def target_text(r):
    icons={"UP":"🟢","DOWN":"🔴","INIT":"⚪"}; st=r["status"] or "INIT"; now=int(time.time())
    since=""
    if st=="UP" and r["streak_started_at"]: since=f"\n连续正常：{duration(now-r['streak_started_at'])}"
    if st=="DOWN" and r["down_since"]: since=f"\n故障持续：{duration(now-r['down_since'])}"
    mode="持续检测 DNS" if r["dns_mode"]=="continuous" else "故障后解析"
    return f"{icons.get(st,'⚪')} {r['name']}\n目标：{r['target']}:{r['port']}\n当前 IP：{r['current_ip'] or '未检测'}\n状态：{st}{since}\n间隔：{r['interval_seconds']}秒\n模式：{mode}\n启用：{'是' if r['enabled'] else '否'}\n最后错误：{r['last_error'] or '无'}"

def detail_keyboard(r):
    tid=r["id"]; toggle="暂停" if r["enabled"] else "启用"
    return [[{"text":"🔄 立即检测","callback_data":f"check:{tid}"},{"text":toggle,"callback_data":f"toggle:{tid}"}],
            [{"text":"✏️ 修改间隔","callback_data":f"editint:{tid}"},{"text":"🗑 删除","callback_data":f"delask:{tid}"}],
            [{"text":"⬅️ 列表","callback_data":"list"}]]

def check_one_ui(tid, chat, mid):
    try:
        check_target(tid, True)
        r=get_target(tid)
        if not r:
            panel(chat,"检测目标已不存在。",main_keyboard(),mid); return
        icon="✅" if r["status"]=="UP" else "❌"
        panel(chat,f"{icon} 检测完成\n\n"+target_text(r),detail_keyboard(r),mid)
    except Exception as e:
        log.exception("手动检测目标 %s 失败",tid)
        panel(chat,f"❌ 检测执行失败\n\n错误：{e}",[[{"text":"返回详情","callback_data":f"view:{tid}"}]],mid)

def summary_text(prefix="📊 状态总览"):
    with DB_LOCK, db() as c:
        rows=c.execute("SELECT COALESCE(s.status,'INIT') status,COUNT(*) n FROM targets t LEFT JOIN states s ON s.target_id=t.id GROUP BY status").fetchall()
    z={r["status"]:r["n"] for r in rows}
    return f"{prefix}\n\n🟢 正常：{z.get('UP',0)}\n🔴 故障：{z.get('DOWN',0)}\n⚪ 待检测：{z.get('INIT',0)}"

def check_all_ui(ids, chat, mid):
    total=len(ids)
    try:
        for index,tid in enumerate(ids,1):
            check_target(tid,True)
            panel(chat,f"🔄 正在检测全部目标…\n\n进度：{index}/{total}",[[{"text":"请稍候…","callback_data":"noop"}]],mid)
        panel(chat,summary_text("✅ 全部检测完成"),[[{"text":"📋 查看列表","callback_data":"list"},{"text":"🏠 主菜单","callback_data":"menu"}]],mid)
    except Exception as e:
        log.exception("全部检测失败")
        panel(chat,f"❌ 全部检测未完成\n\n错误：{e}",[[{"text":"🏠 主菜单","callback_data":"menu"}]],mid)

def show_list(chat, mid=None):
    with DB_LOCK, db() as c: rows=c.execute("SELECT t.*,COALESCE(s.status,'INIT') status FROM targets t LEFT JOIN states s ON s.target_id=t.id ORDER BY t.id").fetchall()
    if not rows: return panel(chat,"📋 暂无监测目标。",[[{"text":"➕ 添加","callback_data":"add"}],[{"text":"🏠 主菜单","callback_data":"menu"}]],mid)
    kb=[[{"text":f"{'🟢' if r['status']=='UP' else '🔴' if r['status']=='DOWN' else '⚪'} {r['name']}","callback_data":f"view:{r['id']}"}] for r in rows]
    kb.append([{"text":"🏠 主菜单","callback_data":"menu"}])
    panel(chat,f"📋 监测列表（{len(rows)}）\n点击目标查看详情：",kb,mid)

def add_prompt(chat, step, text=None, mid=None):
    prompts={"name":"请输入监测名称，例如：日本家宽","target":"请输入域名或 IPv4，例如：jp.example.com","port":"请输入 TCP 端口（1-65535）","dns":"请输入 DNS 服务器 IPv4，发送 /default 使用 223.5.5.5","interval":"请输入检测间隔秒数（10-86400）"}
    panel(chat,text or prompts[step],[[{"text":"❌ 取消","callback_data":"cancel"}]],mid)

def handle_message(m):
    chat=m["chat"]["id"]; uid=m.get("from",{}).get("id",0); text=m.get("text","").strip()
    if uid not in ADMINS: send(chat,f"⛔ 你没有权限使用此 Bot。\n你的 Telegram ID：{uid}"); return
    if text in ("/start","/menu","/cancel"): SESSIONS.pop(uid,None); menu(chat); return
    s=SESSIONS.get(uid)
    if not s: menu(chat,"无法识别命令，请使用菜单："); return
    step=s["step"]; d=s["data"]
    if step=="name":
        if not (1<=len(text)<=40): add_prompt(chat,step,"名称长度应为 1-40 个字符。"); return
        d["name"]=text; s["step"]="target"; add_prompt(chat,"target")
    elif step=="target":
        typ=valid_target(text)
        if not typ: add_prompt(chat,step,"请输入有效域名或 IPv4 地址。"); return
        d.update(target=text,target_type=typ); s["step"]="port"; add_prompt(chat,"port")
    elif step=="port":
        if not text.isdigit() or not 1<=int(text)<=65535: add_prompt(chat,step,"端口必须为 1-65535。"); return
        d["port"]=int(text)
        if d["target_type"]=="domain": s["step"]="dns"; add_prompt(chat,"dns")
        else: d["dns_server"]=""; ask_interval(chat,s)
    elif step=="dns":
        text="223.5.5.5" if text=="/default" else text
        try: socket.inet_aton(text)
        except OSError: add_prompt(chat,step,"DNS 必须是有效 IPv4，或发送 /default。"); return
        d["dns_server"]=text; ask_mode(chat,s)
    elif step=="interval":
        if not text.isdigit() or not 10<=int(text)<=86400: add_prompt(chat,step,"间隔必须为 10-86400 秒。"); return
        d["interval_seconds"]=int(text); confirm_add(chat,uid)

def ask_mode(chat,s):
    s["step"]="mode"
    panel(chat,"请选择 DDNS 监测方式：",[[{"text":"故障后重新解析","callback_data":"mode:failure"}],[{"text":"持续检测 DNS 变化","callback_data":"mode:continuous"}],[{"text":"❌ 取消","callback_data":"cancel"}]])

def ask_interval(chat,s): s["step"]="interval"; add_prompt(chat,"interval")

def confirm_add(chat,uid):
    d=SESSIONS[uid]["data"]; mode="持续检测 DNS" if d.get("dns_mode")=="continuous" else "故障后解析"
    text=f"请确认监测配置：\n\n名称：{d['name']}\n目标：{d['target']}:{d['port']}\n类型：{'域名' if d['target_type']=='domain' else 'IPv4'}\nDNS：{d.get('dns_server') or '不适用'}\n间隔：{d['interval_seconds']}秒\n模式：{mode}"
    panel(chat,text,[[{"text":"✅ 确认添加","callback_data":"addconfirm"},{"text":"❌ 取消","callback_data":"cancel"}]])
    SESSIONS[uid]["step"]="confirm"

def callback(q):
    uid=q["from"]["id"]; chat=q["message"]["chat"]["id"]; mid=q["message"]["message_id"]; data=q.get("data",""); PANELS[chat]=mid
    if not (data.startswith("check:") or data in ("checkall","noop")): answer(q["id"])
    if uid not in ADMINS: send(chat,f"⛔ 无权限。你的 Telegram ID：{uid}"); return
    # 离开添加/编辑流程时清除旧会话，避免删除后仍提示输入间隔。
    if not (data in ("add", "addconfirm") or data.startswith("mode:") or data.startswith("editint:")):
        SESSIONS.pop(uid, None)
    if data=="menu": SESSIONS.pop(uid,None); menu(chat,mid=mid)
    elif data=="add": SESSIONS[uid]={"step":"name","data":{}}; add_prompt(chat,"name")
    elif data=="cancel": SESSIONS.pop(uid,None); menu(chat,"操作已取消。")
    elif data.startswith("mode:"):
        s=SESSIONS.get(uid)
        if s and s["step"]=="mode": s["data"]["dns_mode"]=data.split(":",1)[1]; ask_interval(chat,s)
    elif data=="addconfirm":
        s=SESSIONS.get(uid)
        if not s or s["step"]!="confirm": send(chat,"会话已过期，请重新添加。"); return
        d=s["data"]; now=int(time.time())
        with DB_LOCK, db() as c:
            cur=c.execute("INSERT INTO targets(name,target,target_type,port,dns_server,interval_seconds,dns_mode,notify_chat_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(d["name"],d["target"],d["target_type"],d["port"],d.get("dns_server",""),d["interval_seconds"],d.get("dns_mode","failure"),chat,now,now)); tid=cur.lastrowid
            c.execute("INSERT INTO states(target_id,next_check_at) VALUES(?,0)",(tid,))
        SESSIONS.pop(uid,None); panel(chat,"✅ 已添加，监测将在数秒内开始。",[[{"text":"查看详情","callback_data":f"view:{tid}"},{"text":"🏠 主菜单","callback_data":"menu"}]],mid)
    elif data=="list": show_list(chat,mid)
    elif data.startswith("view:"):
        tid=int(data.split(":")[1]); r=get_target(tid)
        if not r: panel(chat,"目标不存在。",main_keyboard(),mid); return
        panel(chat,target_text(r),detail_keyboard(r),mid)
    elif data.startswith("check:"):
        tid=int(data.split(":")[1]); r=get_target(tid)
        if not r: panel(chat,"目标不存在。",main_keyboard(),mid); return
        answer(q["id"],"正在检测，请稍候…")
        panel(chat,f"🔄 正在检测\n\n名称：{r['name']}\n目标：{r['target']}:{r['port']}\n\n通常会在数秒内完成。",[[{"text":"请稍候…","callback_data":"noop"}]],mid)
        POOL.submit(check_one_ui,tid,chat,mid)
    elif data.startswith("toggle:"):
        tid=int(data.split(":")[1])
        with DB_LOCK, db() as c: c.execute("UPDATE targets SET enabled=1-enabled,updated_at=? WHERE id=?",(int(time.time()),tid)); c.execute("UPDATE states SET next_check_at=0 WHERE target_id=?",(tid,))
        r=get_target(tid)
        panel(chat,target_text(r),detail_keyboard(r),mid)
    elif data.startswith("editint:"):
        tid=int(data.split(":")[1]); SESSIONS[uid]={"step":"edit_interval","data":{"id":tid}}; panel(chat,"请输入新的检测间隔秒数（10-86400）：",[[{"text":"❌ 取消","callback_data":f"view:{tid}"}]],mid)
    elif data.startswith("delask:"):
        tid=int(data.split(":")[1]); r=get_target(tid)
        if r: panel(chat,f"确认删除监测目标“{r['name']}”？此操作不可恢复。",[[{"text":"确认删除","callback_data":f"del:{tid}"},{"text":"取消","callback_data":f"view:{tid}"}]],mid)
    elif data.startswith("del:"):
        tid=int(data.split(":")[1])
        with DB_LOCK, db() as c: c.execute("DELETE FROM targets WHERE id=?",(tid,))
        panel(chat,"✅ 已删除监测目标。",main_keyboard(),mid)
    elif data=="summary":
        panel(chat,summary_text(),[[{"text":"🏠 主菜单","callback_data":"menu"}]],mid)
    elif data=="checkall":
        with DB_LOCK, db() as c: ids=[r[0] for r in c.execute("SELECT id FROM targets WHERE enabled=1")]
        if not ids:
            answer(q["id"],"没有已启用的监测目标")
            panel(chat,"⚠️ 没有已启用的监测目标。",[[{"text":"➕ 添加监测","callback_data":"add"},{"text":"🏠 主菜单","callback_data":"menu"}]],mid)
        else:
            answer(q["id"],f"开始检测 {len(ids)} 个目标")
            panel(chat,f"🔄 正在检测全部目标…\n\n进度：0/{len(ids)}",[[{"text":"请稍候…","callback_data":"noop"}]],mid)
            POOL.submit(check_all_ui,ids,chat,mid)
    elif data=="noop":
        answer(q["id"],"检测正在进行，请稍候…")
    elif data=="events":
        with DB_LOCK, db() as c: rows=c.execute("SELECT e.*,t.name FROM events e LEFT JOIN targets t ON t.id=e.target_id ORDER BY e.id DESC LIMIT 10").fetchall()
        text="📝 最近事件\n\n"+("\n\n".join(f"{datetime.fromtimestamp(r['created_at']):%m-%d %H:%M} · {r['name'] or '已删除'} · {r['event_type']}" for r in rows) if rows else "暂无事件")
        panel(chat,text,[[{"text":"🏠 主菜单","callback_data":"menu"}]],mid)
    elif data=="help": panel(chat,"ℹ️ 使用说明\n\n/start 打开菜单\n/cancel 取消当前操作\n\nBot 检测 IPv4 TCP 端口。域名支持指定 DNS，并可选择故障后解析或持续监测 DNS。告警自动发送到创建目标的会话。",[[{"text":"🏠 主菜单","callback_data":"menu"}]],mid)

def handle_edit_interval(uid,chat,text):
    s=SESSIONS.get(uid)
    if not s or s["step"]!="edit_interval": return False
    if not text.isdigit() or not 10<=int(text)<=86400: panel(chat,"间隔必须为 10-86400 秒，请重新输入：",[[{"text":"❌ 取消","callback_data":f"view:{s['data']['id']}"}]]); return True
    tid=s["data"]["id"]
    with DB_LOCK, db() as c: c.execute("UPDATE targets SET interval_seconds=?,updated_at=? WHERE id=?",(int(text),int(time.time()),tid))
    SESSIONS.pop(uid,None); r=get_target(tid)
    panel(chat,"✅ 检测间隔已修改。\n\n"+target_text(r),detail_keyboard(r)); return True

def run():
    if not TOKEN or not ADMINS: raise SystemExit("必须配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_ADMIN_IDS")
    init_db(); threading.Thread(target=scheduler,daemon=True).start(); offset=0
    api("setMyCommands",{"commands":[{"command":"start","description":"打开主菜单"},{"command":"menu","description":"打开主菜单"},{"command":"cancel","description":"取消当前操作"}]})
    log.info("DDNS Watch Bot 已启动，管理员数量=%d",len(ADMINS))
    while True:
        updates=api("getUpdates",{"offset":offset,"timeout":25,"allowed_updates":["message","callback_query"]},timeout=32)
        if updates is None: time.sleep(3); continue
        for u in updates:
            offset=u["update_id"]+1
            try:
                if "callback_query" in u: callback(u["callback_query"])
                elif "message" in u:
                    m=u["message"]; uid=m.get("from",{}).get("id",0); text=m.get("text","").strip(); chat=m["chat"]["id"]
                    if not handle_edit_interval(uid,chat,text): handle_message(m)
            except Exception: log.exception("处理更新失败")

if __name__=="__main__": run()
