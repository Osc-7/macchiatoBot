# macchiato-remote

轻量 **远程互联模块**：在本机或集群节点上暴露授权目录，供云端 macchiatoBot agent 执行 bash / 读写文件。

## 安装

```bash
# 推荐：从 PyPI 安装（pipx 亦可）
uv tool install macchiato-remote

# 指定版本
uv tool install macchiato-remote==0.2.1
```

PyPI: https://pypi.org/project/macchiato-remote/

```bash
# 从 GitHub Release wheel（锁版本、内网镜像）
pip install https://github.com/Osc-7/macchiatoBot/releases/download/v0.2.1/macchiato_remote-0.2.1-py3-none-any.whl
```

## 与完整 macchiato-bot 的关系

| 安装包 | 场景 |
|--------|------|
| **macchiato-bot** | 云服务器 / Mac 上需要本地对话、飞书、daemon |
| **macchiato-remote**（本包） | 仅作远程 worker：集群节点、或不想装完整依赖的机器 |

Mac 上若已安装完整 **macchiato-bot**，通常已包含 `macchiato-remote` 命令，无需再装本包。

## 常用命令

```bash
macchiato-remote login --server http://HOST:9380 --login personal --token '<token>'
macchiato-remote start --background
macchiato-remote status
```

完整说明见仓库根目录 [README.md](../../README.md#remote-workspaces)。
