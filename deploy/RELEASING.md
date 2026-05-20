# 发版与打 Tag

仓库里有两个可发布到 PyPI 的包，**版本号各自维护**，打 git tag 触发 CI 构建并上传。

| 包 | 版本文件 | PyPI |
|----|----------|------|
| `macchiato-bot` | 根目录 `pyproject.toml` → `[project].version` | https://pypi.org/project/macchiato-bot/ |
| `macchiato-remote` | `packages/macchiato-remote/pyproject.toml` → `version` | https://pypi.org/project/macchiato-remote/ |

Tag 名（如 `v0.2.1`）仅用于 **GitHub Release 与 CI 触发**，不必与 PyPI 版本相同，但建议一致便于对照。

## 发版步骤

### 1. 改版本号

```bash
# 示例：发 0.2.1
# 编辑根 pyproject.toml
# version = "0.2.1"

# 编辑 packages/macchiato-remote/pyproject.toml
# version = "0.2.1"
```

至少改你要重新上传 PyPI 的那个包；**已存在的版本不能覆盖上传**。

若 `uv publish` 报 `Local file and index file do not match`：说明该版本已在 PyPI 上，但本地重新 build 的 wheel 与当时上传的内容不一致（代码又改了）。只能 **升版本号** 再发，不能覆盖 0.2.0。

### 2. 提交

```bash
git add pyproject.toml packages/macchiato-remote/pyproject.toml
git commit -m "chore(release): bump versions to 0.2.1"
```

### 3. 打 tag 并推送

```bash
git tag v0.2.1
git push origin main          # 或你的默认分支
git push origin v0.2.1
```

推送 `v*` tag 后会自动：

1. 构建两个 `.whl`
2. 创建 GitHub Release（附件含 wheel）
3. 在配置了 `PYPI_API_TOKEN` 时，将 wheel 发布到 PyPI（`--check-url` 跳过已存在版本）

### 4. 首次 / 本地手动发 PyPI（可选）

```bash
export UV_PUBLISH_TOKEN='pypi-...'   # 勿提交、勿发到聊天
./deploy/release-pypi.sh
```

## GitHub 配置（一次性）

1. **Secrets → Actions** → `PYPI_API_TOKEN`（PyPI API token）
2. **Environments** → 新建 `pypi`（workflow 使用；可不设审批人）

## 安装（用户侧）

```bash
# 仅远程 worker
uv tool install macchiato-remote

# 完整 bot 库（含 macchiato-remote 命令；daemon/CLI 仍建议从仓库 uv run）
uv pip install macchiato-bot
# 或
uv tool install macchiato-bot
```

PyPI / `uv tool install macchiato-bot` 会安装：

- `macchiato` — CLI（连 daemon）
- `macchiato-daemon` — automation daemon
- `macchiato-remote` — 远程 worker

仓库内仍可用 `uv run main.py` / `uv run automation_daemon.py`（根目录 shim）。

## 删除错误 tag（仅本地未推送时）

```bash
git tag -d v0.2.1
```

已推送且 Release 已创建时，不要在 PyPI 上覆盖同版本，应 **升版本号** 再打新 tag。
