#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jev_proxy —— Codex 每轮都问一次 Jev 的本地代理。

  Codex ──POST /responses──> 本代理(127.0.0.1:8787) ──改写 model──> 上游(默认 api.deepseek.com)

* 只看**最新一条 user 消息**：它变了就重新判一次（这就是「新一轮」），工具循环里的后续请求
  复用同一轮的判断，所以不会来回换模型、也不会浪费 prompt cache。
* 本轮消息里带图片 → 强制 flash（deepseek-v4-pro 不支持图片输入）。
* Jev 失败/超时/没 key → 按 JEVPROXY_FALLBACK（默认 keep）原样放行，绝不阻塞。
* 流式(SSE)原样透传；用 chunked 编码回给 Codex。

只用标准库，Python 3.9+ 可跑。启动方式见 jcodex-live。
"""
from __future__ import print_function

import collections
import hashlib
import http.client
import http.server
import json
import os
import re
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import jev_router as R  # noqa: E402

HOST = os.environ.get("JEVPROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("JEVPROXY_PORT", "8787"))
UPSTREAM = os.environ.get("JEVPROXY_UPSTREAM", "https://api.deepseek.com")
FALLBACK = os.environ.get("JEVPROXY_FALLBACK", "keep")       # keep | strong | fast
MOCK = os.environ.get("JEVPROXY_MOCK")                       # easy|hard|image|unsure（离线演示）
# 哨兵模型：只有请求里的 model == 它（默认同 config.toml 的 model）时才由 Jev 改写。
# 这样你在 /model 里显式选别的模型时，代理不会覆盖你的选择。
SENTINEL = (os.environ.get("JEVPROXY_SENTINEL", "") or "").strip()
# 「Jev 自动」这个虚拟模型：注入到 /models 列表里，于是 Codex App 的模型选择器
# 里会多出一项，选中它就等于「让 Jev 每轮自己决定」——不用改 App 包、升级也不会失效。
AUTO_MODEL = (os.environ.get("JEVPROXY_AUTO_MODEL", "jev-auto") or "jev-auto").strip()
AUTO_NAME = os.environ.get("JEVPROXY_AUTO_NAME", "Jev 自动（按难度选模型）")
INJECT_MODELS = (os.environ.get("JEVPROXY_INJECT_MODELS", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")
SENTINEL_SET = {s.strip().lower() for s in SENTINEL.split(",") if s.strip()}
if INJECT_MODELS and AUTO_MODEL:
    SENTINEL_SET.add(AUTO_MODEL.lower())
# 决策完发一条 macOS 通知，让你立刻看到这轮用了哪个模型（默认关）
NOTIFY = (os.environ.get("JEVPROXY_NOTIFY", "0") or "0").strip().lower() not in ("0", "false", "no", "off")
SET_EFFORT = os.environ.get("JEVPROXY_SET_EFFORT", "0") not in ("0", "false", "no")
DEBUG = os.environ.get("JEVPROXY_DEBUG", "0") not in ("0", "false", "no")
UP_TIMEOUT = float(os.environ.get("JEVPROXY_TIMEOUT", "900"))   # 上游单次请求最长等待（秒）
PIDFILE = os.path.expanduser(os.environ.get("JEVPROXY_PIDFILE", "~/.codex/jev-router/proxy.pid"))
PLOG = os.path.expanduser(os.environ.get("JEVPROXY_LOG", "~/.codex/jev-router/proxy.log"))

_u = urllib.parse.urlsplit(UPSTREAM)
UP_HOST = _u.hostname
UP_PORT = _u.port or (443 if _u.scheme == "https" else 80)
UP_TLS = _u.scheme == "https"
UP_PREFIX = (_u.path or "").rstrip("/")

HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection",
               "keep-alive", "proxy-connection", "upgrade"}
DROP_RESP_HEADERS = HOP_HEADERS | {"server", "date"}

_state = {"key": None, "decision": None, "hits": 0, "misses": 0}
_lock = threading.Lock()
_cache = collections.OrderedDict()      # 轮指纹 -> 决策（同一轮/重复轮直接复用）
CACHE_MAX = 64


def plog(msg):
    try:
        line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
        with open(PLOG, "a") as fh:
            fh.write(line)
    except Exception:
        pass
    if DEBUG:
        sys.stderr.write("[jevproxy] " + msg + "\n")


# ------------------------------------------------------------------ 取「最新一轮」的用户消息

def _text_of(content):
    """Responses API 的 content 数组 或 Chat 风格的 content。返回 (文本, 是否含图片)"""
    texts, img = [], False
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                if isinstance(part, str):
                    texts.append(part)
                continue
            ptype = (part.get("type") or "").lower()
            if ptype in ("input_image", "image_url", "image"):
                img = True
            for k in ("text", "input_text", "output_text"):
                v = part.get(k)
                if isinstance(v, str):
                    texts.append(v)
    return "\n".join(t for t in texts if t), img


def newest_user_turn(body):
    """返回 (最新一条 user 消息的文本, 是否含图片, 会话指纹)"""
    items = body.get("input")
    if isinstance(items, str):
        return items, False, hashlib.sha1(items.encode("utf-8", "ignore")).hexdigest()[:12]
    if not isinstance(items, list):
        items = body.get("messages") if isinstance(body.get("messages"), list) else []
    texts, img, idx_last = [], False, None
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        if (it.get("role") or "").lower() == "user":
            idx_last = i
    if idx_last is None:
        return "", False, ""
    it = items[idx_last]
    t, img = _text_of(it.get("content"))
    # 同一轮里可能有连续的 user item（少数客户端会拆开），往前合并
    j = idx_last - 1
    while j >= 0 and isinstance(items[j], dict) and (items[j].get("role") or "").lower() == "user":
        t2, img2 = _text_of(items[j].get("content"))
        t = (t2 + "\n" + t).strip()
        img = img or img2
        j -= 1
    t = clean_user_text(t)
    fp = hashlib.sha1((("IMG" if img else "") + t).encode("utf-8", "ignore")).hexdigest()[:12]
    return t, img, fp


# ------------------------------------------------------------------ 决策（每轮一次）

HARD_HINTS = ("重构", "架构", "设计", "并发", "性能", "排查", "为什么", "分析", "多个文件",
              "整个", "迁移", "调试", "测试全绿", "优化", "安全", "鉴权", "部署", "跨模块")

# Codex 会在 user 消息里塞环境元数据块，判断前先剥掉，别让它稀释「这轮任务有多难」
META_BLOCKS = re.compile(
    r"(?is)<(environment_context|environment_details|user_instructions|system_reminder)>.*?</\1>")


def clean_user_text(t):
    if not t:
        return ""
    t = META_BLOCKS.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def auto_mock(text):
    """JEVPROXY_MOCK=auto 时的本地替身：纯关键词模拟，只在没 key 的演示/自测里用。"""
    t = text or ""
    if len(t) >= 120 or any(h in t for h in HARD_HINTS):
        return "hard"
    return "easy"


def decide_for_turn(body):
    text, has_image, fp = newest_user_turn(body)
    if not fp:
        # 没有 user 消息的请求（历史压缩、内部续写等）：沿用最近一次决策，
        # 而不是另挑一个模型——一轮之内换模型会让上游前缀缓存全部失效。
        with _lock:
            last = _state.get("decision")
        if last and last.get("model"):
            if DEBUG:
                plog("无 user 消息的内部请求 → 沿用最近决策 %s（保持整轮一致）" % last["model"])
            return last
        return None
    with _lock:
        hit = _cache.get(fp)
        if hit is not None:
            # 同一轮的后续请求（工具循环）或回头重复的一轮：直接复用，不重复问 Jev
            _cache.move_to_end(fp)
            _state["key"], _state["decision"] = fp, hit
            _state["hits"] += 1
            if DEBUG:
                plog("turn %s -> %s（复用本轮已有决策，未重新问 Jev）" % (fp, hit["model"]))
            return hit
        _state["misses"] += 1

    # 图片：现在四个 GPT 模型都支持读图，所以默认交给 Jev 正常判断；
    # 只有显式配了 JEV_IMAGE_FORCE_TIER 才强制换档（目录里出现不支持视觉的模型时才需要）。
    force = R.IMAGE_FORCE_TIER if (has_image and R.IMAGE_FORCE_TIER in R.TIERS) else None
    if force:
        d = R._finish(force, "本轮带图片 → 强制 %s 档（JEV_IMAGE_FORCE_TIER）" % force,
                      "hard-rule", {}, None, {}, {})
    else:
        mock = MOCK
        if mock == "auto":
            mock = auto_mock(text)
        d = R.decide((text or "")[:8000], mock=mock, hints=[text])
        if d["source"] == "fallback" and FALLBACK == "keep":
            d = R._finish("keep", "Jev 不可用，原样放行（%s）" % d["reason"][:80],
                          "fallback", {}, None, {}, {})
        elif d["source"] == "fallback" and FALLBACK in R.TIERS:
            d = R._finish(FALLBACK, "Jev 不可用 → 回退 %s" % FALLBACK, "fallback",
                          {}, None, {}, {})

    with _lock:
        _cache[fp] = d
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
        _state["key"], _state["decision"] = fp, d
    R._log({"where": "proxy", "turn": fp, "task": (text or "")[:200], "image": has_image,
            "choice": d["choice"], "model": d["model"], "reason": d["reason"],
            "source": d["source"], "factors": d["factors"]})
    plog("turn %s -> %s | %s" % (fp, d["model"] or "(不改)", d["reason"]))
    notify("Jev 自动选模型",
           "%.2f → %s｜%s" % (d.get("score") or 0.0, d["model"] or "不改", d["reason"][:60]))
    return d


def notify(title, msg):
    """决策完弹一条 macOS 通知（JEVPROXY_NOTIFY=1 时启用）。"""
    if not NOTIFY:
        return

    def run():
        try:
            subprocess.run(
                ["osascript", "-e",
                 "display notification %s with title %s" % (json.dumps(msg), json.dumps(title))],
                timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def inject_models(raw):
    """往 /models 响应里塞一个「Jev 自动」条目。

    Codex App / CLI 的模型选择器就是拿这个接口的结果渲染的，所以塞进去以后，
    App 的选择器里会直接多出「Jev 自动（按难度选模型）」——那就是我们要的"按钮"，
    而且完全不用改 App 包（改了会破坏签名、一升级就失效）。
    任何异常都原样放行，绝不把列表搞坏。
    """
    if not (INJECT_MODELS and AUTO_MODEL) or not raw:
        return raw, False
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw, False
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        items = data["models"]
    elif isinstance(data, list):
        items = data
    else:
        return raw, False
    if not items:
        return raw, False
    if any(isinstance(m, dict) and str(m.get("slug", "")).lower() == AUTO_MODEL.lower()
           for m in items):
        return raw, False

    # 克隆一份已存在的条目（保证字段 schema 完全一致），再改掉标识字段
    tmpl = None
    for want in (os.environ.get("JEVPROXY_AUTO_TEMPLATE", ""), "gpt-5.6-terra",
                 "gpt-5.6-luna", "gpt-6-astra"):
        if not want:
            continue
        tmpl = next((m for m in items if isinstance(m, dict) and m.get("slug") == want), None)
        if tmpl:
            break
    if tmpl is None:
        tmpl = next((m for m in items if isinstance(m, dict)), None)
    if tmpl is None:
        return raw, False
    try:
        entry = json.loads(json.dumps(tmpl))
    except Exception:
        return raw, False
    entry["slug"] = AUTO_MODEL
    entry["display_name"] = AUTO_NAME
    entry["description"] = ("由 Jev 判断每轮任务难度，自动在 luna / terra / sol / astra 之间切换。"
                            "选中它就等于把选模型这件事交给 Jev。")
    if isinstance(entry.get("priority"), int):
        entry["priority"] = 0            # 排在最前面，方便一眼看到
    items.insert(0, entry)
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8"), True
    except Exception:
        return raw, False


def rewrite(body_bytes):
    """按需改写请求体。返回 (新字节, 决策)"""
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return body_bytes, None
    if not isinstance(body, dict):
        return body_bytes, None

    # 哨兵检查：用户显式选了别的模型 → 尊重选择，不改写、也不问 Jev
    req_model = body.get("model")
    is_auto = isinstance(req_model, str) and req_model.strip().lower() == AUTO_MODEL.lower()
    if SENTINEL_SET and isinstance(req_model, str) and req_model.strip().lower() not in SENTINEL_SET:
        d = R._finish("keep", "请求里是用户显式选择的模型 %s，不改写" % req_model,
                      "manual", {}, None, {}, {})
        plog("pass through: model=%s（显式选择，不在哨兵集合 %s）"
             % (req_model, ",".join(sorted(SENTINEL_SET))))
        return body_bytes, d

    d = decide_for_turn(body)
    if is_auto and (not d or d["choice"] == "keep" or not d.get("model")):
        # 「Jev 自动」必须落到真实模型上：Jev 挂了也得换掉，不能把虚拟名字发给上游
        d = R._finish("strong", "Jev 不可用 → 自动挡兜底 strong 档", "fallback", {}, None, {}, {})
    if not d or d["choice"] == "keep" or not d.get("model"):
        return body_bytes, d
    body["model"] = d["model"]
    if SET_EFFORT and d.get("effort"):
        eff = d["effort"]
        r = body.get("reasoning")
        if isinstance(r, dict):
            r["effort"] = eff
        else:
            body["reasoning"] = {"effort": eff}
    out = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if DEBUG:
        plog("rewrite model -> %s (%d -> %d bytes)" % (d["model"], len(body_bytes), len(out)))
    return out, d


# ------------------------------------------------------------------ HTTP 服务

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jevproxy/1.0"

    def log_message(self, fmt, *args):
        plog("http " + (fmt % args))

    # ---- 读请求体（支持 Content-Length 与 chunked）
    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        if length:
            try:
                return self.rfile.read(int(length))
            except Exception:
                return b""
        return b""

    def _health(self):
        d = _state["decision"]
        payload = {
            "ok": True,
            "upstream": UPSTREAM,
            "mock": MOCK or None,
            "fallback": FALLBACK,
            "sentinel": SENTINEL or None,
            "sentinels": sorted(SENTINEL_SET) or None,
            "auto_model": AUTO_MODEL if INJECT_MODELS else None,
            "notify": NOTIFY,
            "set_effort": SET_EFFORT,
            "turns_decided": _state["misses"],
            "turns_reused": _state["hits"],
            "cached_turns": len(_cache),
            "last_decision": None if not d else {
                "choice": d["choice"], "model": d["model"], "source": d["source"],
                "reason": d["reason"],
            },
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("healthz"):
            return self._health()
        if self._is_upgrade():
            return self._upgrade_relay()
        return self._proxy(b"")

    def do_POST(self):
        if self._is_upgrade():
            return self._upgrade_relay()
        body = self._read_body()
        if self.path.rstrip("/").endswith("healthz"):
            return self._health()
        is_responses = "responses" in self.path or "chat/completions" in self.path
        if is_responses and body:
            body, _ = rewrite(body)
        return self._proxy(body)

    # ---- WebSocket / 其它协议升级：原样 TCP 隧道（App 的 realtime /live 走这里）
    def _is_upgrade(self):
        conn = (self.headers.get("Connection") or "").lower()
        return "upgrade" in conn and bool((self.headers.get("Upgrade") or "").strip())

    def _upgrade_relay(self):
        path = UP_PREFIX + self.path
        raw = None
        try:
            raw = socket.create_connection((UP_HOST, UP_PORT), timeout=30)
            if UP_TLS:
                raw = ssl.create_default_context().wrap_socket(raw, server_hostname=UP_HOST)
            head = "%s %s HTTP/1.1\r\n" % (self.command, path)
            for k, v in self.headers.items():
                if k.lower() == "host":
                    continue
                head += "%s: %s\r\n" % (k, v)
            head += "Host: %s\r\n\r\n" % (UP_HOST if UP_PORT in (80, 443) else "%s:%d" % (UP_HOST, UP_PORT))
            raw.sendall(head.encode("latin-1"))
        except Exception as exc:
            plog("upgrade relay: 连不上上游 %s: %s" % (UP_HOST, exc))
            try:
                if raw:
                    raw.close()
            except Exception:
                pass
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True
            return

        plog("upgrade passthrough %s -> %s" % (self.path, UP_HOST))
        client = self.connection
        peers = {client: raw, raw: client}
        idle = 0.0
        try:
            while True:
                ready, _, _ = select.select([client, raw], [], [], 30)
                if not ready:                    # 空闲：realtime 连接会长时间静默，继续等
                    idle += 30
                    if idle > float(os.environ.get("JEVPROXY_IDLE_LIMIT", "3600")):
                        break
                    continue
                idle = 0.0
                for sock in ready:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionResetError("peer closed")
                    peers[sock].sendall(chunk)
        except Exception:
            pass
        finally:
            for s in (client, raw):
                try:
                    s.close()
                except Exception:
                    pass
            self.close_connection = True

    def _proxy(self, body):
        path = UP_PREFIX + self.path
        is_models = self.command == "GET" and "/models" in self.path
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_HEADERS or k.lower() == "accept-encoding":
                continue
            if is_models and k.lower() in ("if-none-match", "if-modified-since"):
                continue          # 别让上游回 304（那样就没法注入「Jev 自动」了）
            headers[k] = v
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        if DEBUG:
            def _mask(k, v):
                return (v[:10] + "…") if k.lower() in ("authorization", "cookie",
                                                       "chatgpt-account-id") else v
            plog("-> %s %s  headers=%s" % (
                self.command, path,
                json.dumps({k: _mask(k, v) for k, v in headers.items()}, ensure_ascii=False)[:700]))

        cls = http.client.HTTPSConnection if UP_TLS else http.client.HTTPConnection
        conn = cls(UP_HOST, UP_PORT, timeout=UP_TIMEOUT)
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            plog("upstream error: %s" % exc)
            msg = json.dumps({"error": {"message": "jevproxy upstream error: %s" % exc,
                                        "type": "proxy_error"}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            try:
                conn.close()
            except Exception:
                pass
            return

        # /models：整段读下来，注入「Jev 自动」后按 Content-Length 回（列表很小，不需要流式）
        if is_models and INJECT_MODELS and resp.status == 200:
            raw = None
            try:
                raw = resp.read()
                new, injected = inject_models(raw)
            except Exception as exc:
                plog("models 注入失败，按原样转发：%s" % exc)
                new, injected = (raw if raw is not None else b""), False
            if raw is not None:
                try:
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() in DROP_RESP_HEADERS or k.lower() in ("etag", "content-length"):
                            continue
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(new)))
                    self.end_headers()
                    self.wfile.write(new)
                    self.wfile.flush()
                    if injected:
                        plog("models: 已注入「%s」(%s)，App/CLI 的选择器里会多这一项"
                             % (AUTO_MODEL, AUTO_NAME))
                except Exception as exc:
                    plog("models 回写失败：%s" % exc)
                try:
                    conn.close()
                except Exception:
                    pass
                return

        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in DROP_RESP_HEADERS:
                    continue
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return

        total = 0
        try:
            while True:
                chunk = resp.read(1024)
                if not chunk:
                    break
                total += len(chunk)
                self.wfile.write(("%X\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            plog("client went away after %d bytes (normal for cancelled turns)" % total)
        except Exception as exc:
            plog("relay error after %d bytes: %s" % (total, exc))
        finally:
            try:
                conn.close()
            except Exception:
                pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """客户端提前断开（codex 取消/切轮）是常态，不要往日志里打堆栈。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        plog("unhandled error from %s: %r" % (client_address, exc))


def main():
    if "--selftest" in sys.argv:
        return _selftest()
    d = os.path.dirname(PIDFILE)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    try:
        with open(PIDFILE, "w") as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass
    srv = Server((HOST, PORT), Handler)
    plog("listening on http://%s:%d -> %s (mock=%s fallback=%s)" % (HOST, PORT, UPSTREAM, MOCK, FALLBACK))
    print("jev_proxy listening on http://%s:%d -> %s" % (HOST, PORT, UPSTREAM))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(PIDFILE)
        except Exception:
            pass
    return 0


def _selftest():
    """不联网：只验解析「最新一轮」+ 图片识别 + 上一轮指纹变化。"""
    easy = {"input": [
        {"role": "user", "content": [{"type": "input_text", "text": "把错别字改一下"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "好的"}]},
    ]}
    hard = {"input": [
        {"role": "user", "content": [{"type": "input_text", "text": "把错别字改一下"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "好的"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "现在重构整个鉴权模块，改成无状态 token 鉴权并保证测试全绿"}]},
    ]}
    img = {"input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "按这个报错修一下"},
            {"type": "input_image", "image_url": "data:image/png;base64,xxx"}]},
    ]}
    t1, i1, f1 = newest_user_turn(easy)
    t2, i2, f2 = newest_user_turn(hard)
    t3, i3, f3 = newest_user_turn(img)
    ok = True
    print("1) 最新 user 文本:", repr(t1), "| 图片:", i1)
    ok &= t1 == "把错别字改一下" and not i1
    print("2) 新一轮文本:  ", repr(t2), "| 图片:", i2)
    ok &= t2.startswith("现在重构整个鉴权模块") and not i2
    print("3) 指纹变化:    ", f1, "!=", f2, "->", f1 != f2)
    ok &= f1 != f2
    print("4) 图片识别:    ", repr(t3), "| 图片:", i3)
    ok &= i3 is True
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
