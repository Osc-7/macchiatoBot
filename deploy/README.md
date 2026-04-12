# 部署说明（systemd）

本目录提供在 Linux 上使用 **systemd** 常驻运行 macchiatoBot 相关进程的单元文件与安装脚本。

## 目录结构

| 路径 | 说明 |
|------|------|
| `systemd/*.service.in` | 服务单元**模板**（含占位符，勿直接复制到 `/etc`） |
| `systemd/macchiato.target` | 聚合目标：一键启动/停止三个服务 |
| `systemd/install.sh` | 将模板展开为真实路径与用户，并安装到 `/etc/systemd/system/` |

## 版本库

**建议将 `deploy/` 整目录提交到 Git。**

- 提交的是**模板与脚本**，不包含机器上的 `config.yaml`、`.env` 或密钥。
- 安装后生成的文件在 `/etc/systemd/system/`，由本机 `install.sh` 写入，**不要**把 `/etc` 里的内容提交进仓库。

首次纳入版本控制示例：

```bash
git add deploy/
git status
git commit -m "Add systemd deploy units and install script"
```

## 前置条件

1. 已克隆仓库，并在**项目根**完成依赖同步（保证存在 `.venv`）：

   ```bash
   cd /path/to/macchiatoBot
   uv sync --all-groups
   # 或: source init.sh
   ```

2. 按 `config.example.yaml` 准备好本机的 `config.yaml`（及所需环境变量；应用按现有逻辑读取配置）。

3. 运行用户（如 `ubuntu`）对项目目录、`data/`、`logs/` 等有读写权限。

## 安装 systemd 单元

```bash
cd /path/to/macchiatoBot
sudo ./deploy/systemd/install.sh "$(pwd)" 你的系统用户名
```

仅查看生成内容、不写系统目录：

```bash
./deploy/systemd/install.sh --dry-run "$(pwd)" 你的系统用户名
```

安装完成后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now macchiato-automation.service
sudo systemctl enable --now macchiato-feishu-gateway.service
sudo systemctl enable --now macchiato-shuiyuan-connector.service
```

若暂时不需要飞书或水源，**不要**对对应 unit 执行 `enable`；不必改模板。

一次性启动三个服务（不写入开机自启）：

```bash
sudo systemctl start macchiato.target
```

## 日志与排障

```bash
journalctl -u macchiato-automation.service -f
journalctl -u macchiato-feishu-gateway.service -f
journalctl -u macchiato-shuiyuan-connector.service -f
```

`automation_daemon` 另会将日志写入项目下 `logs/automation_daemon.log`（见仓库内 daemon 实现）。

## 额外环境变量

单元文件**未**绑定 `EnvironmentFile=.env`，以免 `.env` 中使用 `export` 等 shell 语法导致 systemd 解析失败。若需为服务注入环境变量，推荐：

```bash
sudo systemctl edit macchiato-automation.service
```

在打开的 override 中于 `[Service]` 下添加 `Environment=KEY=value`，或使用 **仅含 `KEY=value` 行** 的文件并在 override 里 `EnvironmentFile=` 指向该文件。

## 卸载（本机）

```bash
sudo systemctl disable --now macchiato-automation.service macchiato-feishu-gateway.service macchiato-shuiyuan-connector.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/macchiato-*.service /etc/systemd/system/macchiato.target
sudo systemctl daemon-reload
```

（若曾使用 `systemctl edit` 生成 drop-in，需自行删除 `/etc/systemd/system/*.d/` 下对应目录。）
