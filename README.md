# DDNS Watch

监测域名或 IPv4 的 TCP 端口。当端口失效、DNS 解析失败，或域名切换到可用的新 IP 时，通过 Telegram Bot 通知。

## 支持环境

- Debian 11/12/13
- Ubuntu 20.04/22.04/24.04
- systemd
- root 权限

## 真正一键安装

运行以下命令即可安装：

```bash
curl -fsSL https://raw.githubusercontent.com/JudiLite/ddnswatch/main/install.sh | sudo bash
```

安装程序会自动安装依赖，然后直接进入交互管理界面。Telegram Bot Token、Chat ID、SOCKS5 代理、DNS、端口和检测间隔都在界面中设置。

> GitHub 仓库需要设为 Public；否则 Raw 文件需要认证，无法使用上述公开命令。

## 管理

```bash
sudo ddnswatch
```

管理菜单支持：

1. 添加监测目标
2. 查看实例列表
3. 查看服务状态
4. 查看实时日志
5. 重启实例
6. 测试 Telegram
7. 删除实例
8. 彻底卸载

## Telegram 配置

1. 在 Telegram 中通过 `@BotFather` 创建 Bot，取得 Bot Token。
2. 给 Bot 发送一条消息。
3. 获取自己的 Chat ID。
4. 添加实例时输入 Token 和 Chat ID；Token 输入过程不会回显。
5. 在管理菜单选择“测试 Telegram”。

Telegram 配置保存在 `/etc/ddnswatch/<实例名>.conf`，文件权限为 `600`，仅 root 可读。

## 常用命令

```bash
systemctl status ddnswatch@实例名
journalctl -u ddnswatch@实例名 -f
systemctl restart ddnswatch@实例名
```

文件日志位于：

```text
/var/log/ddnswatch/实例名_YYYY-MM-DD.log
```

## GitHub 发布步骤

```bash
git init
git add install.sh README.md
git commit -m "Initial release"
git branch -M main
git remote add origin https://github.com/JudiLite/ddnswatch.git
git push -u origin main
```

建议创建版本标签，并在正式环境固定版本安装：

```bash
curl -fsSL https://raw.githubusercontent.com/JudiLite/ddnswatch/v1.0.0/install.sh | sudo bash
```

## 注意

- 本项目监控 DDNS 切换，但不会主动修改 DNS 记录。
- 当前仅检测 IPv4 A 记录和 TCP 端口。
- 多 A 记录域名只取第一个 IPv4 地址。
- 检测间隔最小为 10 秒。
