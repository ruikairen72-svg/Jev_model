# Jev × Codex — 让模型按任务难度自动切换

用 [TypeSafe **Jev**（System One）](https://typesafe.ai) 判断"这一轮任务有多难"，然后让 Codex
自动在便宜模型和强模型之间切换。**你只管写提示词**——不用手动选模型，也不改 Codex 源码。

```
你 / Codex App / CLI
      │   codex 的 provider 指向本地代理（model = jev-auto）
      ▼
127.0.0.1:8787  ← 常驻代理（launchd / 后台进程）
      │   每一轮：取最新一条 user 消息 → 问 Jev → 改写 model + reasoning.effort
      ▼
ChatGPT 后端（或任何 OpenAI 兼容 base_url）
```

> **English TL;DR** — A local proxy that asks TypeSafe's System One decision model
> ("Jev") how hard each turn is, then picks the right Codex model for that turn
> (cheap → strong) and rewrites `model` + `reasoning.effort` on the fly.
> It also injects a virtual `jev-auto` entry into `GET /models`, so the Codex app's
> model picker itself becomes the on/off switch. Stdlib-only Python, macOS-oriented.

---

## 它解决什么

手动选模型的两难：选弱的做难题会翻车，选强的做删错别字是浪费。
Jev 是专门做决策的模型，用它给每一轮任务打分，映射到四档：

| 档位 | 默认模型 | reasoning | 触发（综合分） | 典型任务 |
| --- | --- | --- | --- | --- |
| fast | `gpt-5.6-luna` | low | `< 0.30` | 改错别字、解释名词、格式化 |
| balanced | `gpt-5.6-terra` | medium | `< 0.60` | 加个字段、写单测、小重构 |
| strong | `gpt-5.6-sol` | high | `< 0.85` | 跨文件重构、设计接口 |
| heavy | `gpt-6-astra` | high | `>= 0.85` | 生产事故排查、分布式方案设计 |

实测（真 Jev，非 mock）：

```
"把 README 里的错别字改一下"                 综合分 0.17 → fast     gpt-5.6-luna
"给用户列表接口加分页并补单元测试"            综合分 0.50 → balanced gpt-5.6-terra
"排查生产环境偶发的订单重复扣款…"             综合分 0.96 → heavy    gpt-6-astra
```

同一轮的工具循环 / 重试会复用同一次决策（按"最新一条 user 消息"的指纹做 LRU 缓存），
所以不会因为来回调用而反复问 Jev。

---

## 快速开始

需要：macOS（代理与 launchd 部分）、Python ≥ 3.8（**只用标准库**）、
Codex CLI / Codex 桌面 App，以及一个 [TypeSafe API key](https://console.typesafe.ai/keys)。

```bash
git clone git@github.com:ruikairen72-svg/Jev_model.git
cd Jev_model
./install.sh --key=apikey_你的key --wire --launchd
```

`install.sh` 做四件事（可重复执行，用来升级）：

1. 把脚本装到 `~/.codex/jev-router/`
2. 生成 `~/.codex/jev-router.env`（`chmod 600`）并写入 key
3. 软链 `jcodex` / `jcodex-live` / `jcodex-doctor` 到 `~/.local/bin`
4. `--wire` 接线 `~/.codex/config.toml`（先备份）；`--launchd` 装成常驻服务

然后：

```bash
jcodex-doctor --deep     # 体检：真调一次 Jev，验证 key / 接线 / 代理 / 目录
jcodex-doctor --fix      # 缺什么补什么（幂等）
codex "帮我把这个函数改成 async"      # 就这样，什么都不用加
```

> 四档模型名要改成**你** Codex 目录里真实存在的模型：
> `grep -o '"slug": "[^"]*"' ~/.codex/models_cache.json`

---

## 日常使用

```bash
codex                   # 交互式：每发一条新消息都重新判一次难度
codex "重构鉴权模块"     # 一句话任务也照样判
codex exec "改个错别字"  # 非交互
```

| 想做什么 | 怎么做 |
| --- | --- |
| 自己指定模型 | 在 `/model` 或 App 的选择器里选具体模型 → 代理**不覆盖**你的选择 |
| 完全绕过路由 | `codex -c model_provider=openai "…"` |
| 临时关掉 / 打开 | `jcodex-doctor --off` / `jcodex-doctor --on` |
| 换一档阈值 | 改 `~/.codex/jev-router.env` 里的 `JEV_T_*`，重启代理 |
| 看它每轮选了啥 | `grep turn ~/.codex/jev-router/proxy.log`，或开 `JEVPROXY_NOTIFY=1` 弹通知 |

---

## Codex 桌面 App：选择器里会多一项「Jev 自动」

不用改 App 包（改包会破坏签名、一升级就失效）。App / CLI 的模型选择器是拉
`GET /models` 渲染的，所以代理往这个响应里**注入一个虚拟模型**：

```
jev-auto    Jev 自动（按难度选模型）      ← 列表第一位
```

选中它 = 每轮交给 Jev。实现上是克隆一份已有条目（字段 schema 完全一致），
幂等且防御式——任何解析异常都原样放行，绝不会把你的模型列表搞坏
（`JEVPROXY_INJECT_MODELS=0` 可关闭）。

**安装后重启一次 App** 才会看到新列表。想让 App/CLI 启动时就是它，把顶层
`model` 设成 `jev-auto`（`install.sh --wire` 已这么做）——原因见下面那个坑。

---

## Jev 被问了什么

一次请求并行问完，答案带概率分布，再用**期望值**（不是 argmax）合成综合分：

| 问题 | 类型 | 权重 | 作用 |
| --- | --- | --- | --- |
| 任务整体难度（4 档） | Score | 0.60 | 主判据，用概率分布算期望 |
| 是否需要深推理 / 多方案权衡 | Noul | 0.25 | 区分"难"和"烦" |
| 需求是否缺关键信息 | Noul | 0.05 | 权重故意小：一句话指令天然信息不全 |
| 是否需要图片输入 | Noul | — | 默认只记录不干预（见 `JEV_IMAGE_FORCE_TIER`） |
| 预计工具调用轮数 | Choice | 0.10 | 用分布期望值 |

```
综合分 = 0.60*难度 + 0.25*深推理 + 0.05*缺信息 + 0.10*轮数期望
< 0.30 / 0.60 / 0.85 / 更高 → fast / balanced / strong / heavy
Jev 置信度 < 0.60 且综合分 ≥ 0.30 → 只上抬一档（不直接顶到最强）
Jev 报错 / 超时 / 没 key → 照旧放行（代理）或走 JEV_FALLBACK（启动器）
```

设计取舍、踩过的坑、为什么用期望值而不是取最高档，见 [`docs/DESIGN.md`](docs/DESIGN.md)。

---

## 配置

全部配置集中在 `~/.codex/jev-router.env`（模板见 [`config/jev-router.env.example`](config/jev-router.env.example)），
临时覆盖直接走环境变量，例如 `JEV_T_FAST=0.2 codex "…"`。

关键几项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JEV_API_KEY` | — | TypeSafe key |
| `JEV_CODEX_{FAST,BALANCED,STRONG,HEAVY}_MODEL` | luna/terra/sol/astra | 四档模型名 |
| `JEV_T_{FAST,BALANCED,STRONG}` | 0.30 / 0.60 / 0.85 | 档位阈值 |
| `JEV_ALLOW_HEAVY` | 1 | 设 0 则最高只到 strong |
| `JEV_FALLBACK` | strong | `jcodex` 启动器在 Jev 不可用时的兜底档 |
| `JEVPROXY_UPSTREAM` | ChatGPT Codex 后端 | 转发目标（可换成任何 OpenAI 兼容服务） |
| `JEVPROXY_SENTINEL` | `gpt-6-astra` | 哨兵：请求模型命中才改写（逗号分隔可多个） |
| `JEVPROXY_INJECT_MODELS` / `JEVPROXY_AUTO_MODEL` | 1 / `jev-auto` | 「Jev 自动」注入开关与 slug |
| `JEVPROXY_SET_EFFORT` | 1 | 是否按档改写 `reasoning.effort` |
| `JEVPROXY_FALLBACK` | keep | 代理兜底：`keep` = 原样放行 |
| `JEVPROXY_NOTIFY` | 0 | 每轮弹 macOS 通知，直接看到用了哪个模型 |
| `JEVPROXY_DEBUG` | 0 | 记录转发头与改写细节（凭据自动打码） |

---

## 体检与自测

```bash
jcodex-doctor              # 体检：key / 接线 / launchd / 代理 / codex / 模型目录
jcodex-doctor --deep       # 上面 + 真调一次 Jev
jcodex-doctor --fix        # 自动修复（幂等）
jcodex-live --jev-status   # 代理状态与累计决策数
jcodex-live --jev-stop     # 停代理（会连同 launchd 一起卸载，避免 KeepAlive 打架）
```

离线自测（**不需要 key、不联网**）：

```bash
python3 jev_router.py --selftest     # 评分 / 门控 / fail-open
python3 jev_proxy.py  --selftest     # 「最新一轮」解析 / 图片识别
bash    test/run_proxy_test.sh       # 端到端：假上游 + 真代理（含流式 SSE）
python3 test/test_ws.py              # WebSocket 透传（101 握手 + 双向数据）
python3 jev_router.py --mock --task "排查线上偶发超时"   # 离线看决策结果
```

---

## 已知边界与坑

- **顶层 `model` 必须是哨兵（`jev-auto`）**。App / CLI 的选择器会把选择**写回**
  `~/.codex/config.toml`；如果那里是某个具体模型，代理会认为"你自己选的"而
  **整条路由静默失效**（没有报错，只是不再切换）。`jcodex-doctor` 会把这种情况判成 ❌
  并给出 `--fix`。（这就是把默认值设成 `jev-auto` 的原因：两边永远一致。）
- **CC Switch 之类会覆写 `config.toml`** 的工具会冲掉接线 → `jcodex-doctor --fix`。
- **代理是单点**：它没在跑时 Codex 会连不上。launchd 的 `KeepAlive` 会在几秒内拉起；
  应急用 `codex -c model_provider=openai` 或 `jcodex-doctor --off`。
- **云端执行的任务**（在服务端跑的那种）不受本地代理影响，只有本地会话会路由。
- **协议升级已处理**：需要 WebSocket 的端点（如 App 的实时语音 `/live`）走 TCP 隧道透传，
  不会因为 hop-by-hop 头被剥掉而失败。
- **隐私**：只有"最新一条 user 消息"（剥掉 `<environment_context>` 等元数据、截断到 8000 字）
  会发给 TypeSafe 判断难度；其余内容都不出本机。日志里凭据自动打码。
- **Codex 的请求格式不是公开契约**：上游一变就用 `JEVPROXY_DEBUG=1` 看日志定位。
- 只在 macOS 上验证过 launchd / 通知部分；路由与代理本身是纯标准库 HTTP，跨平台可用。

---

## 目录

```
jev_router.py            决策引擎：问 Jev + 评分 / 门控策略（想改策略只改这里）
jev_proxy.py             逐轮改写 model/effort 的本地代理（含 /models 注入、WS 透传）
jcodex                   一次性决策启动器（算好后 -m 启动 codex）
jcodex-live              代理生命周期管理（launchd 感知）
jcodex-doctor            体检 / --fix / --off / --on
install.sh uninstall.sh  安装与卸载
config/                  env、独立 profile、config.toml 接线片段
launchd/                 LaunchAgent 模板
test/                    离线端到端测试（假上游，无需 key）
docs/DESIGN.md           设计取舍与踩坑记录
```

运行时产物（都在 `~/.codex/`）：`jev-router.env`（配置，600）、`jev-router.log`
（每次决策一行 JSON）、`jev-router/proxy.log`（代理日志）、`config.toml.bak-jev-*`（备份）。

---

## License

MIT —— 见 [LICENSE](LICENSE)。
