#!/usr/bin/env bash
# 安装 / 更新 Jev × Codex 自动路由。
#
#   ./install.sh --key=apikey_xxx            安装脚本 + 写入 key
#   ./install.sh --key=apikey_xxx --wire     再自动接线 config.toml（会备份）
#   ./install.sh --key=apikey_xxx --wire --launchd
#                                            再装成 launchd 常驻（推荐）
#   ./install.sh --launchd                   只重装 launchd（用已有 env）
#
# 可重复执行（幂等），用来升级脚本。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${JEV_ROUTER_HOME:-$HOME/.codex/jev-router}"
BIN_DIR="${JEV_BIN_DIR:-$HOME/.local/bin}"
ENV_FILE="${JEV_ENV_FILE:-$HOME/.codex/jev-router.env}"
LABEL="${JEVPROXY_LAUNCHD_LABEL:-com.jevrouter.proxy}"
AGENTS_DIR="$HOME/Library/LaunchAgents"

KEY=""
WIRE=0
LAUNCHD=0
NO_BIN=0

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --key=*)   KEY="${1#*=}" ;;
    --key)     shift; KEY="${1:-}" ;;
    --wire)    WIRE=1 ;;
    --launchd) LAUNCHD=1 ;;
    --no-bin)  NO_BIN=1 ;;
    --prefix=*) PREFIX="${1#*=}" ;;
    -h|--help) usage ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '  %s\n' "$1"; }

echo "==> 环境检查"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "找不到 python3（需要 >= 3.8，只用标准库）" >&2; exit 1; }
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
case "$PYVER" in
  2.*) echo "python3 版本过低：$PYVER" >&2; exit 1 ;;
esac
say "python3: $PY ($PYVER)"
CX=""
for c in "${CODEX_CLI_PATH:-}" "$HOME/.local/bin/codex" /opt/homebrew/bin/codex /usr/local/bin/codex \
         "/Applications/ChatGPT.app/Contents/Resources/codex"; do
  if [ -n "$c" ] && [ -x "$c" ]; then CX="$c"; break; fi
done
if [ -n "$CX" ]; then say "codex:   $CX"; else
  say "codex:   没找到（装好后用 CODEX_CLI_PATH 指定，或把 codex 放进 PATH）"
fi

echo "==> 安装脚本到 $PREFIX"
mkdir -p "$PREFIX"
for f in jev_router.py jev_proxy.py jcodex jcodex-live jcodex-doctor; do
  cp -f "$HERE/$f" "$PREFIX/$f"
  chmod 755 "$PREFIX/$f"
done
mkdir -p "$PREFIX/test"
cp -f "$HERE"/test/*.py "$HERE"/test/*.sh "$PREFIX/test/" 2>/dev/null || true
chmod 755 "$PREFIX"/test/*.py "$PREFIX"/test/*.sh 2>/dev/null || true
say "已安装 5 个脚本 + 测试"

echo "==> 配置文件 $ENV_FILE"
mkdir -p "$(dirname "$ENV_FILE")"
if [ -f "$ENV_FILE" ]; then
  say "已存在，保留不动（要改请直接编辑）"
else
  cp -f "$HERE/config/jev-router.env.example" "$ENV_FILE"
  if [ -n "$KEY" ]; then
    "$PY" - "$ENV_FILE" "$KEY" <<'PYEOF'
import sys
path, key = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
out = []
for l in lines:
    out.append("JEV_API_KEY=%s" % key if l.startswith("JEV_API_KEY=") else l)
open(path, "w").write("\n".join(out) + "\n")
PYEOF
    say "已写入 JEV_API_KEY（${KEY:0:12}…）"
  else
    say "⚠️  还没填 JEV_API_KEY：编辑 $ENV_FILE（或重跑 install.sh --key=...）"
  fi
  chmod 600 "$ENV_FILE"
  say "权限已设为 600"
fi
[ "$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || echo 600)" = "600" ] || chmod 600 "$ENV_FILE"

if [ "$NO_BIN" = "0" ]; then
  echo "==> 命令软链到 $BIN_DIR"
  mkdir -p "$BIN_DIR"
  for n in jcodex jcodex-live jcodex-doctor; do
    ln -sf "$PREFIX/$n" "$BIN_DIR/$n"
    say "$BIN_DIR/$n"
  done
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "⚠️  $BIN_DIR 不在 PATH 里，往 ~/.zshrc 加一行：export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
fi

if [ "$WIRE" = "1" ]; then
  echo "==> 接线 ~/.codex/config.toml（会先备份）"
  cp -p "$HOME/.codex/config.toml" "$HOME/.codex/config.toml.bak-jev-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
  "$PY" "$PREFIX/jcodex-doctor" --on
fi

if [ "$LAUNCHD" = "1" ]; then
  echo "==> 安装 launchd 常驻（$LABEL）"
  mkdir -p "$AGENTS_DIR"
  PLIST="$AGENTS_DIR/$LABEL.plist"
  sed -e "s|__LABEL__|$LABEL|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__PREFIX__|$PREFIX|g" \
      -e "s|__PYTHON__|$PY|g" \
      "$HERE/launchd/com.jevrouter.proxy.plist.template" > "$PLIST"
  say "已写入 $PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    say "已加载并启动"
  else
    say "⚠️  bootstrap 失败（受限环境/沙箱常见）。请在普通终端执行："
    say "    launchctl bootstrap gui/$(id -u) \"$PLIST\""
  fi
fi

echo
echo "==> 完成。下一步："
echo "  1) 编辑 $ENV_FILE —— 把四档模型改成你 Codex 目录里真实存在的名字："
echo "       grep -o '\"slug\": \"[^\"]*\"' ~/.codex/models_cache.json"
echo "  2) jcodex-doctor --deep      # 全链路体检（会真调一次 Jev）"
echo "  3) jcodex-doctor --fix       # 缺什么自动修什么"
echo "  4) 直接敲 codex              # 每轮按难度自动选模型"
echo
echo "  想彻底关掉：jcodex-doctor --off       卸载：./uninstall.sh"
