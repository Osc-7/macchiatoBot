# 发版与 Tag 规范

仓库里有两个可发布到 PyPI 的包，版本号分别维护。Git tag 用来触发 GitHub Release 与 CI，不等同于必须和每个 PyPI 包版本完全一致，但建议在 release note 中写清楚对应关系。

| 包 | 版本文件 | PyPI |
|---|---|---|
| `macchiato-bot` | 根目录 `pyproject.toml` 的 `[project].version` | https://pypi.org/project/macchiato-bot/ |
| `macchiato-remote` | `packages/macchiato-remote/pyproject.toml` 的 `[project].version` | https://pypi.org/project/macchiato-remote/ |

## Release 纪律

- 已推送的 `v*` tag 视为不可变；如果 PyPI / GitHub Release 已经发布，不要移动或重打同名 tag。
- Release commit 应从稳定基线产生，通常是 `master` 或专门的 `release/vX.Y.x` 维护分支。
- 功能分支不要混入最终 release commit。需要运行时代码改动时，先以 feature/fix 提交合入稳定基线，再做小的 release commit。
- `release/vX.Y.Z` 只作为临时发版分支。发布完成后可以删除；需要长期补丁线时使用 `release/vX.Y.x`。
- PyPI 不允许覆盖同版本文件。若同版本已上传但内容变了，只能升版本号再发布。
- Release commit 只处理版本号、包元数据、CI/workflow、安装说明和 release 文档，不顺手重构架构。

## 标准流程

### 1. 确认基线

```bash
git fetch --prune --tags
git switch master
git pull --ff-only
git status --short --branch
```

工作区必须干净。若要从功能分支发版，先把功能分支通过 PR / merge / fast-forward 整合到稳定基线，再继续。

### 2. 运行验证

```bash
uv sync --all-groups
uv run pytest tests/ -v --tb=short
black --check src/ tests/
isort --check-only src/ tests/
```

全量测试若遇到已知状态泄漏问题，记录失败用例，并单独复跑确认是否为 pre-existing issue。

### 3. 改版本号

只改需要发布到 PyPI 的包版本。例如发布 `macchiato-remote 0.2.2` 时：

```toml
# packages/macchiato-remote/pyproject.toml
[project]
version = "0.2.2"
```

如果完整 bot 也要发布，再改根 `pyproject.toml`：

```toml
# pyproject.toml
[project]
version = "0.1.2"
```

### 4. 本地构建检查

```bash
rm -rf dist build packages/macchiato-remote/build
uv build --wheel -o dist/
uv build --wheel -o dist/ packages/macchiato-remote
ls -la dist/*.whl
```

不要提交 `dist/`、`build/`、`*.egg-info/` 或运行日志。

### 5. 提交 release commit

```bash
git add pyproject.toml packages/macchiato-remote/pyproject.toml deploy/RELEASING.md
git commit -m "chore(release): v0.2.2"
```

如果只改了一个包，只 add 对应版本文件即可。提交信息里写清楚 tag 与包版本对应关系，尤其当两个包版本不同步时。

### 6. 打 tag 并推送

```bash
git tag -a v0.2.2 -m "Release v0.2.2: macchiato-remote 0.2.2"
git push origin master
git push origin v0.2.2
```

推送 `v*` tag 后 CI 会：

1. 构建 wheel
2. 创建 GitHub Release 并上传 wheel
3. 如配置了 `PYPI_API_TOKEN`，发布到 PyPI；已存在版本会被跳过

## 手动发布 PyPI

仅在需要本地手动发版时使用：

```bash
export UV_PUBLISH_TOKEN='pypi-...'
./deploy/release-pypi.sh
```

不要把 token 提交或发到聊天。

## GitHub 一次性配置

1. Repository Secrets / Actions: `PYPI_API_TOKEN`
2. Environments: `pypi`，供 workflow 使用；是否加审批人按项目需要决定

## 用户安装

```bash
# 仅远程 worker
uv tool install macchiato-remote

# 完整 bot，包含 daemon / CLI / remote worker 命令
uv tool install macchiato-bot
```

安装 `macchiato-bot` 后会得到：

- `macchiato`：CLI，连接 daemon
- `macchiato-daemon`：automation daemon
- `macchiato-remote`：远程 worker

仓库内开发仍可使用：

```bash
uv run main.py
uv run automation_daemon.py
uv run macchiato-remote status
```

## 错误 tag 处理

仅本地未推送时可以删除：

```bash
git tag -d v0.2.2
```

如果 tag 已推送，或 PyPI / GitHub Release 已经创建，不要覆盖同版本。应升 patch 版本，重新提交并打新 tag。
