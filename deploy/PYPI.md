# PyPI 发布说明

## 包

| 包 | PyPI | 安装 |
|----|------|------|
| `macchiato-remote` | https://pypi.org/project/macchiato-remote/ | `uv tool install macchiato-remote` |
| `macchiato-bot` | https://pypi.org/project/macchiato-bot/ | `uv tool install macchiato-bot` → `macchiato`, `macchiato-daemon`, `macchiato-remote` |

发版流程见 **[RELEASING.md](./RELEASING.md)**（打 tag、改版本号）。

## 手动发布

```bash
export UV_PUBLISH_TOKEN='pypi-...'   # 勿提交到 git
./deploy/release-pypi.sh
```

## GitHub Actions

推送 `v*` tag 时 workflow `.github/workflows/release.yml` 会：

1. 构建两个 wheel 并上传 GitHub Release
2. 使用 `PYPI_API_TOKEN` 发布到 PyPI（`--skip-existing`）

需在仓库 **Settings → Secrets and variables → Actions** 添加 `PYPI_API_TOKEN`，并创建 **Environment** `pypi`。

## 安全

- 切勿把 token 写入仓库或发到聊天
- token 泄露后应在 PyPI 上 **Revoke** 并重新生成
