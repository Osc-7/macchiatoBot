# macchiato-remote

轻量远程 worker：在本机或集群节点上暴露一个用户授权目录，供云端 macchiatoBot daemon 执行 bash / 文件读写。

## 安装

```bash
uv tool install macchiato-remote
macchiato-remote --version
```

指定版本：

```bash
uv tool install macchiato-remote==0.2.1
```

PyPI: https://pypi.org/project/macchiato-remote/

## 与 macchiato-bot 的关系

| 包 | 场景 |
|---|---|
| `macchiato-bot` | 完整 bot：daemon、CLI、飞书、调度、LLM、记忆 |
| `macchiato-remote` | 仅 worker：暴露本机授权目录给远端 daemon |

如果一台机器已经安装完整 `macchiato-bot`，通常也已有 `macchiato-remote` 命令，不需要再装本包。

## 常用命令

```bash
macchiato-remote login --server http://HOST:9380 --login personal --token '<token>'
macchiato-remote start --background
macchiato-remote status
macchiato-remote stop
```

完整说明见仓库文档：

- 中文：https://github.com/Osc-7/macchiatoBot/blob/master/docs/remote-workspace_zh.md
- English: https://github.com/Osc-7/macchiatoBot/blob/master/docs/remote-workspace.md
