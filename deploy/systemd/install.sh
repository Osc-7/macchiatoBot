#!/usr/bin/env bash
# 将模板 unit 安装到 /etc/systemd/system/ 并 daemon-reload。
# 用法:
#   ./deploy/systemd/install.sh /path/to/macchiatoBot [运行用户]
#   ./deploy/systemd/install.sh /path/to/macchiatoBot ubuntu --with-proxy
#   ./deploy/systemd/install.sh /path/to/macchiatoBot ubuntu --automation-root
#   ./deploy/systemd/install.sh --dry-run /path/to/macchiatoBot ubuntu
#
# --with-proxy       同时安装 50-macchiato-proxy.conf（本机 Clash 等 HTTP 代理 + NO_PROXY 直连国内域名）
# --automation-root  仅 macchiato-automation 以 root 运行（command_tools.bash_os_user_enabled 时使用 runuser/useradd）
#
# 安装前请在项目根执行: uv sync（或 source init.sh），确保 .venv 存在。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
WITH_PROXY=0
AUTOMATION_ROOT=0
RAW_ARGS=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --with-proxy) WITH_PROXY=1 ;;
    --automation-root) AUTOMATION_ROOT=1 ;;
    *) RAW_ARGS+=("$a") ;;
  esac
done
set -- "${RAW_ARGS[@]}"

ROOT="${1:?用法: $0 [--dry-run] [--with-proxy] [--automation-root] <MACCHIATO_ROOT> [USER]}"
ROOT="$(cd "$ROOT" && pwd)"
RUN_USER="${2:-${SUDO_USER:-$USER}}"

if [[ "$AUTOMATION_ROOT" -eq 1 ]]; then
  AUTOMATION_USER=root
  AUTOMATION_GROUP=root
else
  AUTOMATION_USER="$RUN_USER"
  AUTOMATION_GROUP="$RUN_USER"
fi

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
    -e "s|__MACCHIATO_AUTOMATION_USER__|${AUTOMATION_USER}|g" \
    -e "s|__MACCHIATO_AUTOMATION_GROUP__|${AUTOMATION_GROUP}|g" \
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

install_proxy_dropins() {
  local src="$SCRIPT_DIR/50-macchiato-proxy.conf"
  if [[ ! -f "$src" ]]; then
    echo "缺少 $src" >&2
    exit 1
  fi
  for s in macchiato-automation macchiato-feishu-gateway macchiato-shuiyuan-connector; do
    local dir="/etc/systemd/system/${s}.service.d"
    local dst="${dir}/50-macchiato-proxy.conf"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "===== $dst (copy from 50-macchiato-proxy.conf) ====="
      cat "$src"
      echo
    else
      sudo mkdir -p "$dir"
      sudo cp "$src" "$dst"
      echo "已写入 $dst"
    fi
  done
}

if [[ "$WITH_PROXY" -eq 1 ]]; then
  install_proxy_dropins
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "===== /etc/systemd/system/macchiato.target (copy) ====="
  cat "$SCRIPT_DIR/macchiato.target"
else
  sudo cp "$SCRIPT_DIR/macchiato.target" /etc/systemd/system/macchiato.target
  echo "已写入 /etc/systemd/system/macchiato.target"
  sudo systemctl daemon-reload
fi

echo
if [[ "$WITH_PROXY" -eq 0 ]]; then
  echo "提示: 若 systemd 内访问 Gemini/OpenAI 超时而 shell 里正常，多半是未继承本机代理；可重装并加 --with-proxy"
fi
if [[ "$AUTOMATION_ROOT" -eq 1 ]]; then
  echo "提示: macchiato-automation.service 已设为 User=root（command_tools.bash_os_user_enabled）；飞书/水源单元仍为 ${RUN_USER}。root 进程面较大，请收紧机器与密钥访问。"
elif [[ "$AUTOMATION_ROOT" -eq 0 ]]; then
  echo "提示: 若 config 中 bash_os_user_enabled: true，请重装本脚本并加 --automation-root，否则 runuser/useradd 会失败。"
fi
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
