# 部署说明（systemd）

本目录提供在 Linux 上使用 **systemd** 常驻运行 macchiatoBot 相关进程的单元文件与安装脚本。

## 目录结构


| 路径                         | 说明                                        |
| -------------------------- | ----------------------------------------- |
| `systemd/*.service.in`     | 服务单元**模板**（含占位符，勿直接复制到 `/etc`）            |
| `systemd/macchiato.target` | 聚合目标：一键启动/停止三个服务                          |
| `systemd/install.sh`       | 将模板展开为真实路径与用户，并安装到 `/etc/systemd/system/`（可选 `--automation-root`） |
| `systemd/50-macchiato-proxy.conf` | 可选 drop-in：HTTP(S) 代理 + `NO_PROXY`（与 `--with-proxy` 配套） |


## 版本库

**建议将 `deploy/` 整目录提交到 Git。**

- 提交的是**模板与脚本**，不包含机器上的 `config/config.yaml`、`.env` 或密钥。
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
2. 按 `config/config.example.yaml` 准备好本机的 `config/config.yaml`（及所需环境变量；应用按现有逻辑读取配置）。
3. 运行用户（如 `ubuntu`）对项目目录、`data/`、`logs/` 等有读写权限。

## 安装 systemd 单元

```bash
cd /path/to/macchiatoBot
sudo ./deploy/systemd/install.sh "$(pwd)" 你的系统用户名
```

若 `config/config.yaml` 中 `command_tools.bash_os_user_enabled: true`（Linux 下用 `runuser` 隔离 bash），**automation daemon 必须以 root 运行**，安装时追加 **`--automation-root`**（飞书 / 水源连接器仍为普通用户，不变）：

```bash
sudo ./deploy/systemd/install.sh "$(pwd)" 你的系统用户名 --automation-root
```

请先为 `bash_os_admin_system_users` 创建对应 Linux 用户（如 `useradd -r -M macchiato_bash_admin`），并安装 `util-linux`（提供 `/sbin/runuser`）。

本机使用 **Clash / 7890** 等 HTTP 代理访问境外 LLM（Gemini、OpenAI）时，**shell 有代理而 systemd 没有**会导致 daemon 内请求超时。安装单元时一并注入代理与直连名单：

```bash
sudo ./deploy/systemd/install.sh "$(pwd)" 你的系统用户名 --with-proxy
sudo systemctl daemon-reload
sudo systemctl restart macchiato-automation.service macchiato-feishu-gateway.service macchiato-shuiyuan-connector.service
```

代理与 `NO_PROXY` 内容见 `deploy/systemd/50-macchiato-proxy.conf`（端口非 `7890` 时请编辑该文件后重装 `--with-proxy`，或改 `/etc/systemd/system/macchiato-*.service.d/50-macchiato-proxy.conf`）。

仅查看生成内容、不写系统目录：

```bash
./deploy/systemd/install.sh --dry-run "$(pwd)" 你的系统用户名
./deploy/systemd/install.sh --dry-run "$(pwd)" 你的系统用户名 --automation-root
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

更新代码后请**直接重启需要的服务**，例如三个一起：

```bash
sudo systemctl restart macchiato-automation.service macchiato-feishu-gateway.service macchiato-shuiyuan-connector.service
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

**HTTP 代理（境外 LLM）**：优先使用仓库内 `50-macchiato-proxy.conf` + `install.sh --with-proxy`（见上文），避免国内 API 误走代理时可编辑该文件中的 `NO_PROXY` 列表。可与 `--automation-root` 同一条命令指定：`install.sh "$(pwd)" ubuntu --automation-root --with-proxy`。

## 卸载（本机）

```bash
sudo systemctl disable --now macchiato-automation.service macchiato-feishu-gateway.service macchiato-shuiyuan-connector.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/macchiato-*.service /etc/systemd/system/macchiato.target
sudo systemctl daemon-reload
```

（若曾使用 `systemctl edit` 生成 drop-in，需自行删除 `/etc/systemd/system/*.d/` 下对应目录。）