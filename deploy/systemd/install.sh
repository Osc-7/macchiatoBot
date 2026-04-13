#!/usr/bin/env bash
# 将模板 unit 安装到 /etc/systemd/system/ 并 daemon-reload。
# 用法:
#   ./deploy/systemd/install.sh /path/to/macchiatoBot [运行用户]
#   ./deploy/systemd/install.sh --dry-run /path/to/macchiatoBot ubuntu
#
# 安装前请在项目根执行: uv sync（或 source init.sh），确保 .venv 存在。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

ROOT="${1:?用法: $0 [--dry-run] <MACCHIATO_ROOT> [USER]}"
ROOT="$(cd "$ROOT" && pwd)"
RUN_USER="${2:-${SUDO_USER:-$USER}}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "缺少命令: $1" >&2
    exit 1
  }
}

need sed
if [[ "$DRY_RUN" -eq 0 ]]; then
  need sudo
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "未找到 $ROOT/.venv/bin/python；请先在项目根执行: uv sync" >&2
  exit 1
fi

render() {
  local name="$1"
  sed -e "s|__MACCHIATO_ROOT__|${ROOT}|g" -e "s|__MACCHIATO_USER__|${RUN_USER}|g" \
    "$SCRIPT_DIR/${name}.service.in"
}

install_service() {
  local name="$1"
  local out="/etc/systemd/system/${name}.service"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "===== $out ====="
    render "$name"
    echo
  else
    render "$name" | sudo tee "$out" >/dev/null
    echo "已写入 $out"
  fi
}

for s in macchiato-automation macchiato-feishu-gateway macchiato-shuiyuan-connector; do
  install_service "$s"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "===== /etc/systemd/system/macchiato.target (copy) ====="
  cat "$SCRIPT_DIR/macchiato.target"
else
  sudo cp "$SCRIPT_DIR/macchiato.target" /etc/systemd/system/macchiato.target
  echo "已写入 /etc/systemd/system/macchiato.target"
  sudo systemctl daemon-reload
fi

echo
echo "安装完成。首次启用示例:"
echo "  sudo systemctl enable --now macchiato-automation.service"
echo "  sudo systemctl enable --now macchiato-feishu-gateway.service"
echo "  sudo systemctl enable --now macchiato-shuiyuan-connector.service"
echo "或一次性启动（不写入开机）:"
echo "  sudo systemctl start macchiato.target"
echo "注意: restart macchiato.target 一般不会重启上述 .service；更新代码后请:"
echo "  sudo systemctl restart macchiato-automation.service macchiato-feishu-gateway.service macchiato-shuiyuan-connector.service"
echo "查看日志:"
echo "  journalctl -u macchiato-automation.service -f"
