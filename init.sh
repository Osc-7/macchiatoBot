#!/bin/bash
# =============================================================================
# init.sh - Schedule Agent 环境初始化脚本 (Sandbox 优化版)
# =============================================================================

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

echo -e "${BLUE}>>> 正在初始化 Sandbox 开发环境...${NC}"

# 1. 确保 Python 路径正确 (Sandbox 通常直接使用 python3)
export PYTHONUNBUFFERED=1
PYTHON_BIN=$(which python3 || which python)

if [ -z "$PYTHON_BIN" ]; then
    echo -e "${RED}[ERROR]${NC} 未找到 Python 环境"
    return 1 2>/dev/null || exit 1
fi

# 2. 同步依赖（pyproject.toml + uv.lock → uv sync → .venv）
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    log_info "未检测到 uv，正在通过 pip 安装..."
    if ! $PYTHON_BIN -m pip install -q uv; then
        echo -e "${RED}[ERROR]${NC} 无法安装 uv，请先安装: https://docs.astral.sh/uv/getting-started/installation/"
        return 1
    fi
}

if [ -f "pyproject.toml" ]; then
    ensure_uv || return 1 2>/dev/null || exit 1

    log_info "正在同步依赖 (uv sync，含 dev 组)..."
    uv sync --all-groups -q
    export PATH="$(pwd)/.venv/bin:$PATH"
    PYTHON_BIN="$(pwd)/.venv/bin/python"
    log_success "依赖检查完成"
fi

# 3. 设置 PYTHONPATH (frontend / system / agent_core 三个一级包)
export PYTHONPATH=$(pwd)/src:$PYTHONPATH
log_info "PYTHONPATH 已设置: $PYTHONPATH"

# 4. 加载本地环境变量（可选）
# 若项目根目录存在 .env，则 source 进当前 shell，便于配置 API Key 等。
# 使用 Tavily 联网搜索时，在 .env 中加入一行即可：
#   export TAVILY_API_KEY="tvly-你的密钥"
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
    log_success "已加载 .env"
fi

# 5. 启动 dev-terminal server（如已运行则跳过）
start_dev_terminal_server() {
    local DEV_TERM_DIR="${HOME}/.agents/skills/dev-terminal"
    local HEALTH_URL_ROOT="http://localhost:9333"
    local HEALTH_URL_PAGE="${HEALTH_URL_ROOT}/"

    # 若未安装 curl，则跳过自动启动（不影响主流程）
    if ! command -v curl >/dev/null 2>&1; then
        log_warn "未找到 curl，跳过 dev-terminal 自动启动"
        return 0
    fi

    # 已有 server 在跑就直接返回（能连到 9333 且返回任意 HTTP 状态即可）
    if curl -sS -o /dev/null "$HEALTH_URL_ROOT" >/dev/null 2>&1; then
        log_info "dev-terminal server 已在运行，跳过启动"
        return 0
    fi

    # 没有安装 skill 就不尝试启动
    if [ ! -d "$DEV_TERM_DIR" ]; then
        log_warn "未检测到 dev-terminal 技能目录（$DEV_TERM_DIR），跳过自动启动"
        return 0
    fi

    log_info "正在后台启动 dev-terminal server..."
    (
        cd "$DEV_TERM_DIR" && \
        ./server.sh >/tmp/dev-terminal-server.log 2>&1 &
    )

    # 简单等一下再做一次健康检查
    sleep 1
    if curl -sS -o /dev/null "$HEALTH_URL_PAGE" >/dev/null 2>&1; then
        log_success "dev-terminal server 已启动"
    else
        log_warn "尝试启动 dev-terminal server 但健康检查失败，请手动检查 ~/.agents/skills/dev-terminal"
    fi
}

start_dev_terminal_server || log_warn "dev-terminal server 自动启动失败，可手动运行：cd ~/.agents/skills/dev-terminal && ./server.sh &"

echo -e "${GREEN}✅ 环境就绪 (${PYTHON_BIN})${NC}"