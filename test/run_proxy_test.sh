#!/bin/bash
# 端到端验证代理：不需要任何 API key（JEVPROXY_MOCK=auto 用本地关键词替身）
set -u
D="$HOME/.codex/jev-router"
T="$D/test"
cd "$T" || exit 1
rm -f seen.jsonl proxy_test.log jev_test.log

python3 fake_upstream.py 9999 & UP=$!
sleep 0.5
JEVPROXY_PORT=8788 JEVPROXY_UPSTREAM=http://127.0.0.1:9999 JEVPROXY_MOCK=auto \
JEVPROXY_PIDFILE="$T/proxy_test.pid" JEVPROXY_LOG="$T/proxy_test.log" \
JEV_LOG="$T/jev_test.log" python3 "$D/jev_proxy.py" >"$T/proxy_stdout.log" 2>&1 & PX=$!
sleep 1.0

post() {
  curl -s -N --max-time 10 -X POST "http://127.0.0.1:8788/responses" \
    -H "Content-Type: application/json" -H "Authorization: Bearer fake-key" \
    -d "$1" | head -c 90
  echo
}

echo "=== 0) healthz"; curl -s --max-time 5 http://127.0.0.1:8788/healthz; echo
echo "=== 1) 简单任务 → 期望 flash"
post '{"model":"deepseek-v4-pro","input":[{"role":"user","content":[{"type":"input_text","text":"把 README 的错别字改一下"}]}]}'
echo "=== 2) 困难任务 → 期望 pro"
post '{"model":"deepseek-v4-pro","input":[{"role":"user","content":[{"type":"input_text","text":"重构整个鉴权模块，改成无状态 token 鉴权，并保证现有测试全绿"}]}]}'
echo "=== 3) 带图片 → 期望 flash（硬约束）"
post '{"model":"deepseek-v4-pro","input":[{"role":"user","content":[{"type":"input_text","text":"按这个报错改"},{"type":"input_image","image_url":"data:image/png;base64,AAA"}]}]}'
echo "=== 4) 重复第 2 条（同一轮）→ 应复用判断，不重复问 Jev"
post '{"model":"deepseek-v4-pro","input":[{"role":"user","content":[{"type":"input_text","text":"重构整个鉴权模块，改成无状态 token 鉴权，并保证现有测试全绿"}]}]}'
echo "=== 5) 上游实际收到的 model 序列（应为 flash, pro, flash, pro）"
cat seen.jsonl
echo "=== 6) 决策条数（应为 3，第 4 个请求复用）"
wc -l < jev_test.log
echo "=== 7) 代理状态里的最近一次决策"
curl -s --max-time 5 http://127.0.0.1:8788/healthz; echo

kill $PX $UP 2>/dev/null
wait $PX $UP 2>/dev/null
echo "=== 完成"
