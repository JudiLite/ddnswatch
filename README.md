# DDNS Watch Telegram Bot

通过 Telegram Bot 交互添加、查看、暂停、检测和删除 DDNS/TCP 监测目标。支持域名与 IPv4、指定 TCP 端口、自定义 DNS、检测间隔、状态持久化和自动告警。

## 功能

- Telegram 按钮和对话式添加监测
- 支持 IPv4、域名及 TCP 端口
- 域名支持自定义 DNS 服务器
- 两种 DDNS 模式：故障后重新解析、持续检测 DNS 变化
- TCP 故障、DNS 故障、服务恢复和 IP 切换通知
- 目标列表、状态总览、最近事件、立即检测
- 暂停/启用、修改检测间隔、删除目标
- SQLite 持久化，避免重复告警
- Telegram 管理员 ID 白名单
- 可选 SOCKS5 代理
- systemd 开机启动及崩溃重启
- 一键安装、升级、备份、卸载

> 本项目负责监测，不会主动修改 DNS 记录。目前检测 IPv4 A 记录和 TCP 端口。

## 支持环境

- Debian 11/12/13
- Ubuntu 20.04/22.04/24.04
- systemd
- root 权限

## 安装前准备

1. 在 Telegram 打开 `@BotFather`，创建 Bot 并取得 Token。
2. 获取你的 Telegram 数字 User ID。可以先向 `@userinfobot` 查询。
3. 仓库须为 Public，公开 Raw 安装命令才能直接使用。

## 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/JudiLite/ddnswatch/main/install.sh | sudo bash
```

安装时输入：

- Telegram Bot Token（不回显）
- 管理员 Telegram User ID；多个管理员用逗号分隔
- 可选 SOCKS5 URL，例如 `socks5h://127.0.0.1:1080`

安装完成后，给 Bot 发送：

```text
/start
```

然后在 Bot 菜单中点击“➕ 添加监测”，按提示输入名称、域名/IP、端口、DNS、模式和检测间隔。

## 默认目录

项目数据统一存放在 `/etc/ddnswatch`：

```text
/etc/ddnswatch/
├── bot.py
├── config.env
├── ddnswatch.db
├── logs/
│   └── ddnswatch.log
└── backups/
```

systemd 服务位于：

```text
/etc/systemd/system/ddnswatch-bot.service
```

目录权限默认为 `700`，配置和数据库默认仅 root 可读。备份时请妥善保管 `config.env`，其中包含 Bot Token。

## Bot 菜单

- ➕ 添加监测
- 📋 监测列表
- 📊 状态总览
- 🔄 全部检测
- 📝 最近事件
- ℹ️ 帮助

目标详情支持立即检测、暂停/启用、修改检测间隔和删除。

## 两种 DDNS 模式

### 故障后重新解析

正常时检测当前 IP 的 TCP 端口；端口故障后才重新解析域名并尝试新 IP。适合统计 DDNS 故障切换时间。

### 持续检测 DNS

每次检测前重新解析域名，即使旧 IP 仍可访问，也能发现 A 记录变化。

## 管理命令

```bash
systemctl status ddnswatch-bot
journalctl -u ddnswatch-bot -f
systemctl restart ddnswatch-bot
```

升级：

```bash
curl -fsSL https://raw.githubusercontent.com/JudiLite/ddnswatch/main/install.sh | sudo bash -s -- upgrade
```

备份：

```bash
curl -fsSL https://raw.githubusercontent.com/JudiLite/ddnswatch/main/install.sh | sudo bash -s -- backup
```

卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/JudiLite/ddnswatch/main/install.sh | sudo bash -s -- uninstall
```

## 手动修改配置

```bash
sudo nano /etc/ddnswatch/config.env
sudo systemctl restart ddnswatch-bot
```

配置示例：

```bash
TELEGRAM_BOT_TOKEN=123456:ABCDEF
TELEGRAM_ADMIN_IDS=123456789,987654321
SOCKS5_PROXY=socks5h://127.0.0.1:1080
```

不要把真实 Token 提交到 GitHub。

## 更新来源

升级命令默认从 `main` 分支获取 `bot.py`。生产环境建议固定版本标签，例如：

```bash
DDNSWATCH_RAW_BASE=https://raw.githubusercontent.com/JudiLite/ddnswatch/v2.0.0 \
  curl -fsSL https://raw.githubusercontent.com/JudiLite/ddnswatch/v2.0.0/install.sh | sudo -E bash
```

## License

MIT
