#!/usr/bin/env bash
# 卸载 Jev × Codex 自动路由。
#
#   ./uninstall.sh             停止代理 + 卸载 launchd + 删软链（保留配置与脚本）
#   ./uninstall.sh --purge     再删掉安装目录、env 文件，并把 config.toml 改回原生 provider
set -euo pipefail

PREFIX="${JEV_ROUTER_HOME:-$HOME/.codex/jev-router}"
BIN_DIR="${JEV_BIN_DIR:-$HOME/.local/bin}"
ENV_FILE="${JEV_ENV_FILE:-$HOME/.codex/jev-router.env}"
LABEL="${JEVPROXY_LAUNCHD_LABEL:-}"
PLIST=""

PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '  %s\n' "$1"; }

# 自动找 label（哪个 plist 跑了 jev_proxy.py）
if [ -z "$LABEL" ]; then
  for p in "$HOME/Library/LaunchAgents"/*.plist; do
    [ -f "$p" ] || continue
    if grep -q "jev_proxy.py" "$p" 2>/dev/null; then
      PLIST="$p"
      LABEL="$(basename "$p" .plist)"
      break
    fi
  done
fi
[ -n "$LABEL" ] && [ -z "$PLIST" ] && PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> 停止代理"
if [ -n "$LABEL" ]; then
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    say "已卸载 launchd 服务 $LABEL"
  fi
fi
pkill -f "jev_proxy.py" 2>/dev/null && say "已结束残留代理进程" || true
rm -f "$PREFIX/proxy.pid" 2>/dev/null || true

echo "==> 删除命令软链"
for n in jcodex jcodex-live jcodex-doctor; do
  if [ -L "$BIN_DIR/$n" ]; then rm -f "$BIN_DIR/$n"; say "$BIN_DIR/$n"; fi
done

if [ "$PURGE" = "1" ]; then
  echo "==> 把关掉 config.toml 的路由接线"
  if [ -x "$PREFIX/jcodex-doctor" ]; then
    "$PREFIX/jcodex-doctor" --off || true
  fi
  echo "==> 删除文件"
  if [ -n "$PLIST" ] && [ -f "$PLIST" ]; then rm -f "$PLIST"; say "$PLIST"; fi
  rm -rf "$PREFIX"
  say "$PREFIX"
  if [ -f "$ENV_FILE" ]; then
    rm -f "$ENV_FILE"
    say "$ENV_FILE（含你的 Jev key）"
  fi
  echo
  echo "已彻底卸载。config.toml 的备份还在 ~/.codex/config.toml.bak-jev-*"
else
  echo
  echo "已停止并摘掉命令入口（配置与脚本保留在 $PREFIX）。"
  echo "想彻底清干净：./uninstall.sh --purge"
fi
