#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jev 决策器：把「这个任务有多难」交给 TypeSafe 的 Jev(System One) 判断，
然后由本文件的策略代码决定 Codex 用哪一档模型
（默认对应 gpt-5.6-luna / gpt-5.6-terra / gpt-5.6-sol / gpt-6-astra）。

设计原则（照 TypeSafe 官方 "code in control" 的用法）：
  * Jev 只回答窄问题（难度评分 / 是否需要深推理 / 是否模糊 / 是否含图片 / 预计工具轮数）
  * 阈值、加权、置信度门控、硬约束全部写死在下面的代码里，随时可改
  * 难度用**概率分布的期望值**而不是 argmax，避免「把握不大就被顶到最强档」
  * 任何失败都 fail-open，绝不阻塞 Codex

仅用 Python 3.9 标准库，无需 pip 安装任何东西。
"""
from __future__ import print_function

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ENV_FILE = os.path.expanduser("~/.codex/jev-router.env")


def _bootstrap_env():
    """把 ~/.codex/jev-router.env 里的所有 KEY=VALUE 注入环境（已存在的环境变量优先，
    方便临时用 JEV_T_FAST=0.2 jcodex ... 这样覆盖）。必须在读下面的配置常量之前执行。"""
    try:
        if not os.path.exists(ENV_FILE):
            return
        with open(ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_bootstrap_env()

# ---------------------------------------------------------------- 配置

API_URL = os.environ.get("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
API_MODEL = os.environ.get("JEV_MODEL", "jev-latest")
TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "8"))

# 四档：模型 / profile（留空=用 -m 直接指定模型）/ reasoning effort
TIERS = {
    "fast": {
        "model": os.environ.get("JEV_CODEX_FAST_MODEL", "gpt-5.6-luna"),
        "profile": os.environ.get("JEV_CODEX_FAST_PROFILE", ""),
        "effort": os.environ.get("JEV_EFFORT_FAST", "low"),
    },
    "balanced": {
        "model": os.environ.get("JEV_CODEX_BALANCED_MODEL", "gpt-5.6-terra"),
        "profile": os.environ.get("JEV_CODEX_BALANCED_PROFILE", ""),
        "effort": os.environ.get("JEV_EFFORT_BALANCED", "medium"),
    },
    "strong": {
        "model": os.environ.get("JEV_CODEX_STRONG_MODEL", "gpt-5.6-sol"),
        "profile": os.environ.get("JEV_CODEX_STRONG_PROFILE", ""),
        "effort": os.environ.get("JEV_EFFORT_STRONG", "high"),
    },
    "heavy": {
        "model": os.environ.get("JEV_CODEX_HEAVY_MODEL", "gpt-6-astra"),
        "profile": os.environ.get("JEV_CODEX_HEAVY_PROFILE", ""),
        "effort": os.environ.get("JEV_EFFORT_HEAVY", "high"),
    },
}
ORDER = ["fast", "balanced", "strong", "heavy"]

# 阈值：综合分 < T_FAST -> fast，< T_BALANCED -> balanced，< T_STRONG -> strong，否则 heavy
T_FAST = float(os.environ.get("JEV_T_FAST", "0.30"))
T_BALANCED = float(os.environ.get("JEV_T_BALANCED", "0.60"))
T_STRONG = float(os.environ.get("JEV_T_STRONG", "0.85"))
ALLOW_HEAVY = os.environ.get("JEV_ALLOW_HEAVY", "1") not in ("0", "false", "no")

# 置信度门控：Jev 拿不准、且综合分本来就不算低时，往上抬一档（不是直接抬到顶）
MIN_CONFIDENCE = float(os.environ.get("JEV_MIN_CONFIDENCE", "0.60"))
CONF_FLOOR = float(os.environ.get("JEV_CONF_FLOOR", "0.30"))
LOW_CONF_ESCALATE = os.environ.get("JEV_LOW_CONF_ESCALATE", "1") not in ("0", "false", "no")

# 图片处理：留空 = 不干预（当前 GPT 目录四个模型都能读图）。
# 如果你的目录里有"不支持视觉"的模型（例如之前的 deepseek-v4-pro），就设成对应的档，例如
#   JEV_IMAGE_FORCE_TIER=fast
IMAGE_FORCE_TIER = (os.environ.get("JEV_IMAGE_FORCE_TIER", "") or "").strip()
IMAGE_THRESHOLD = float(os.environ.get("JEV_IMAGE_THRESHOLD", "0.5"))
FALLBACK = os.environ.get("JEV_FALLBACK", "strong")   # fast | balanced | strong | heavy | keep
LOG_PATH = os.path.expanduser(os.environ.get("JEV_LOG", "~/.codex/jev-router.log"))

# 加权（和 = 1.0）。模糊度权重故意给得小：简短指令天然「信息不全」，
# 权重给大就会把所有一句话任务都顶上去（实测过这个坑）
W_DIFFICULTY = 0.60
W_REASONING = 0.25
W_AMBIGUITY = 0.05
W_TOOLSTEPS = 0.10
TOOLSTEP_WEIGHT = {"few": 0.0, "some": 0.5, "many": 1.0}

QUESTIONS = {
    "difficulty": {
        "type": "score",
        "instructions": "完成这个编码任务的整体难度（只看任务本身，不考虑模型能力）",
        "criteria": [
            "无需推理：改文案/改名/格式化/执行一条明确命令",
            "单点修改：单个文件内的小改动，需求清楚",
            "多点改动：需要改多个文件，或需要读代码/调试才能定位",
            "困难：架构设计、复杂 bug、跨模块重构、性能并发或长链路规划",
        ],
    },
    "reasoning_need": {
        "type": "noul",
        "instructions": "完成该任务是否需要对代码库做深入推理、权衡多种方案、或多阶段规划",
    },
    "ambiguity": {
        "type": "noul",
        "instructions": "任务描述是否缺少关键信息，必须先探索或澄清才能动手",
    },
    "image_input": {
        "type": "noul",
        "instructions": "这个任务是否包含或依赖图片、截图等多模态输入",
    },
    "tool_steps": {
        "type": "choice",
        "instructions": "预计需要多少轮工具调用（读文件、改文件、跑命令）",
        "criteria": {
            "few": "1-3 轮，直接改完就收工",
            "some": "4-10 轮，要读写多个文件",
            "many": "10 轮以上，长链路排查或多阶段推进",
        },
    },
}

IMAGE_EXT = re.compile(r"\.(png|jpe?g|webp|gif|bmp|tiff?|heic)\b", re.I)


# ---------------------------------------------------------------- 小工具

def _log(record):
    try:
        d = os.path.dirname(LOG_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _api_key():
    for name in ("JEV_API_KEY", "TYPESAFE_API_KEY", "TYPESAFE_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    env_file = os.path.expanduser("~/.codex/jev-router.env")
    if os.path.exists(env_file):
        try:
            with open(env_file) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
                        v = v.strip().strip('"').strip("'")
                        if v:
                            return v
        except Exception:
            pass
    return None


def _post(payload, key):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "jev-codex-router/1.1",
        },
        method="POST",
    )
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last = "HTTP %s %s" % (exc.code, body)
            if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(1.0)
                continue
            break
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
            if attempt == 0:
                time.sleep(0.5)
                continue
            break
    raise RuntimeError(last or "unknown error")


# ---------------------------------------------------------------- 决策核心

def mock_answers(kind):
    """离线演示 / 自测用的假回答，形状与真实 API 完全一致。"""
    table = {
        "easy": {
            "difficulty": {"type": "score", "score": 0.0, "confidence": 0.95,
                           "legend": {}, "probabilities": {"0": 0.93, "1": 0.07}},
            "reasoning_need": {"type": "noul", "noul": 0.03},
            "ambiguity": {"type": "noul", "noul": 0.70},
            "image_input": {"type": "noul", "noul": 0.01},
            "tool_steps": {"type": "choice", "choice": "few", "confidence": 0.9,
                           "probabilities": {"few": 0.9, "some": 0.1}},
        },
        "hard": {
            "difficulty": {"type": "score", "score": 2.99, "confidence": 0.99,
                           "legend": {}, "probabilities": {"2": 0.01, "3": 0.99}},
            "reasoning_need": {"type": "noul", "noul": 0.89},
            "ambiguity": {"type": "noul", "noul": 0.88},
            "image_input": {"type": "noul", "noul": 0.04},
            "tool_steps": {"type": "choice", "choice": "many", "confidence": 0.68,
                           "probabilities": {"some": 0.21, "many": 0.79}},
        },
        "image": {
            "difficulty": {"type": "score", "score": 1.0, "confidence": 0.8,
                           "legend": {}, "probabilities": {"1": 0.7, "2": 0.3}},
            "reasoning_need": {"type": "noul", "noul": 0.2},
            "ambiguity": {"type": "noul", "noul": 0.2},
            "image_input": {"type": "noul", "noul": 0.97},
            "tool_steps": {"type": "choice", "choice": "some", "confidence": 0.7,
                           "probabilities": {"some": 0.6, "many": 0.3}},
        },
        "unsure": {
            "difficulty": {"type": "score", "score": 1.0, "confidence": 0.31,
                           "legend": {}, "probabilities": {"1": 0.34, "2": 0.31}},
            "reasoning_need": {"type": "noul", "noul": 0.45},
            "ambiguity": {"type": "noul", "noul": 0.50},
            "image_input": {"type": "noul", "noul": 0.05},
            "tool_steps": {"type": "choice", "choice": "some", "confidence": 0.4,
                           "probabilities": {"some": 0.40}},
        },
    }
    return table[kind]


def ask_jev(task_text, mock=None, debug=False):
    if mock:
        return mock_answers(mock), {"mock": True}
    key = _api_key()
    if not key:
        raise RuntimeError(
            "没有 TypeSafe API key：请设 JEV_API_KEY（在 https://console.typesafe.ai/keys 申请），"
            "或写入 ~/.codex/jev-router.env"
        )
    payload = {"state": task_text, "model": API_MODEL, "questions": QUESTIONS}
    if debug:
        sys.stderr.write("[jev] request: " + json.dumps(payload, ensure_ascii=False)[:400] + "\n")
    resp = _post(payload, key)
    if debug:
        sys.stderr.write("[jev] response: " + json.dumps(resp, ensure_ascii=False)[:800] + "\n")
    answers = resp.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise RuntimeError("响应里没有 answers: %s" % json.dumps(resp, ensure_ascii=False)[:200])
    return answers, resp.get("usage") or {}


def _expected_index(answer):
    """用 probabilities 算期望档位；拿不到概率就退回 score。"""
    if not isinstance(answer, dict):
        return None
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and probs:
        total, acc = 0.0, 0.0
        for k, p in probs.items():
            try:
                idx = float(k)
                w = float(p)
            except (TypeError, ValueError):
                continue
            total += w
            acc += idx * w
        if total > 0:
            return acc / total
    if "score" in answer:
        try:
            return float(answer["score"])
        except (TypeError, ValueError):
            return None
    return None


def _expected_toolsteps(answer):
    if not isinstance(answer, dict):
        return 0.0, "?"
    choice = answer.get("choice") or "?"
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and probs:
        total, acc = 0.0, 0.0
        for k, p in probs.items():
            if k in TOOLSTEP_WEIGHT:
                try:
                    w = float(p)
                except (TypeError, ValueError):
                    continue
                total += w
                acc += TOOLSTEP_WEIGHT[k] * w
        if total > 0:
            return acc / total, choice
    return TOOLSTEP_WEIGHT.get(answer.get("choice"), 0.0), choice


def _tier_for(score):
    if score < T_FAST:
        return "fast"
    if score < T_BALANCED:
        return "balanced"
    if score < T_STRONG:
        return "strong"
    return "heavy" if ALLOW_HEAVY else "strong"


def decide(task_text, mock=None, debug=False, hints=None):
    """返回决策 dict（choice/model/profile/effort/reason/factors/confidence/answers/source）"""
    factors, answers, conf, usage = {}, {}, None, {}
    img_hit = None
    for text in (hints or []):
        if text and IMAGE_EXT.search(text):
            img_hit = text
            break

    try:
        answers, usage = ask_jev(task_text, mock=mock, debug=debug)
        source = "mock" if mock else "jev"
    except Exception as exc:
        return _finish(FALLBACK, "Jev 不可用（%s）→ 回退 %s" % (str(exc)[:160], FALLBACK),
                       "fallback", {}, None, {}, {})

    d = answers.get("difficulty") or {}
    n_levels = len(QUESTIONS["difficulty"]["criteria"])
    ev = _expected_index(d)
    factors["difficulty"] = 0.0 if ev is None else max(0.0, min(1.0, ev / (n_levels - 1)))
    factors["difficulty_level"] = int(round(factors["difficulty"] * (n_levels - 1)))
    if isinstance(d.get("confidence"), (int, float)):
        conf = float(d["confidence"])

    factors["reasoning_need"] = float((answers.get("reasoning_need") or {}).get("noul") or 0.0)
    factors["ambiguity"] = float((answers.get("ambiguity") or {}).get("noul") or 0.0)
    factors["image_input"] = float((answers.get("image_input") or {}).get("noul") or 0.0)
    ts_ev, ts_choice = _expected_toolsteps(answers.get("tool_steps"))
    factors["tool_steps"] = ts_choice
    factors["tool_steps_weight"] = round(ts_ev, 3)

    # 图片：只有在你显式配置了 JEV_IMAGE_FORCE_TIER 时才强制换档
    # （例如目录里有不支持视觉的模型）。否则只记录为因子，正常走评分。
    if IMAGE_FORCE_TIER in TIERS and (factors["image_input"] >= IMAGE_THRESHOLD or img_hit):
        why = ("Jev 判定含图片输入" if factors["image_input"] >= IMAGE_THRESHOLD
               else "参数里出现图片 %s" % img_hit)
        return _finish(IMAGE_FORCE_TIER,
                       "硬约束：%s，按配置强制 %s 档 → %s" % (
                           why, IMAGE_FORCE_TIER, TIERS[IMAGE_FORCE_TIER]["model"]),
                       "hard-rule", factors, conf, answers, usage)

    score = (W_DIFFICULTY * factors["difficulty"]
             + W_REASONING * factors["reasoning_need"]
             + W_AMBIGUITY * factors["ambiguity"]
             + W_TOOLSTEPS * factors["tool_steps_weight"])
    factors["composite"] = round(score, 4)
    tier = _tier_for(score)
    reason = "综合分 %.2f → %s 档" % (score, tier)

    # 低置信度：只在「本来就不算简单」时往上抬一档
    if conf is not None and conf < MIN_CONFIDENCE and LOW_CONF_ESCALATE and score >= CONF_FLOOR:
        idx = ORDER.index(tier)
        if idx < len(ORDER) - 1:
            new_tier = ORDER[idx + 1]
            if ALLOW_HEAVY or new_tier != "heavy":
                reason = ("Jev 置信度 %.2f < %.2f，综合分 %.2f 不算低 → %s 上抬一档到 %s"
                          % (conf, MIN_CONFIDENCE, score, tier, new_tier))
                tier = new_tier

    return _finish(tier, reason, source, factors, conf, answers, usage)


def _finish(choice, reason, source, factors, conf, answers, usage):
    t = TIERS.get(choice)
    if t:
        model, profile, effort = t["model"], (t["profile"] or None), t["effort"]
    else:
        choice, model, profile, effort = "keep", None, None, None
        if not reason:
            reason = "不干预，沿用 Codex 默认模型"
    return {
        "choice": choice,
        "model": model,
        "profile": profile,
        "effort": effort,
        "reason": reason,
        "source": source,
        "confidence": conf,
        "factors": factors,
        "answers": answers,
        "usage": usage,
    }


def reason_line(decision):
    if decision["choice"] != "keep":
        head = "[Jev] 这一轮用 %s（%s 档%s，依据：%s）" % (
            decision["model"], decision["choice"],
            " + effort=" + decision["effort"] if decision.get("effort") else "",
            decision["reason"])
    else:
        head = "[Jev] 不干预：%s" % decision["reason"]
    f = decision["factors"] or {}
    if f:
        head += " | 难度 %.2f/深推理 %.2f/模糊 %.2f/图片 %.2f/轮数 %s" % (
            f.get("difficulty", 0), f.get("reasoning_need", 0),
            f.get("ambiguity", 0), f.get("image_input", 0), f.get("tool_steps", "?"))
        if decision.get("confidence") is not None:
            head += " | Jev 置信度 %.2f" % decision["confidence"]
    return head


# ---------------------------------------------------------------- CLI（调试用）

def _selftest():
    cases = [
        ("easy", "把 README 里的错别字改一下", ("fast",)),
        ("hard", "重构整个鉴权模块，改成无状态 token 鉴权，并保证现有测试全绿", ("strong", "heavy")),
        ("image", "按这张截图 /tmp/shot.png 里的报错修一下", ("balanced", "strong")),
        ("unsure", "看看这个项目", ("strong", "heavy")),
    ]
    ok = True
    for mock, text, expect in cases:
        d = decide(text, mock=mock)
        good = d["choice"] in expect
        ok = ok and good
        print("%s mock=%-6s -> %-8s %-14s (%s)" % (
            "OK " if good else "FAIL", mock, d["choice"], d["model"], d["reason"][:70]))
    d = decide("随便什么任务", mock=None)
    print("   没 key fail-open -> %s %s (%s)" % (d["choice"], d["model"], d["reason"][:50]))
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("用法: jev_router.py \"任务描述\" [--mock easy|hard|image|unsure] [--debug]")
        return 0
    if argv[0] == "--selftest":
        return _selftest()
    text = argv[0]
    mock = argv[argv.index("--mock") + 1] if "--mock" in argv else None
    d = decide(text, mock=mock, debug="--debug" in argv)
    print(reason_line(d))
    print(json.dumps({k: v for k, v in d.items() if k != "answers"}, ensure_ascii=False, indent=2))
    _log({"task": text[:200], "choice": d["choice"], "model": d["model"],
          "reason": d["reason"], "source": d["source"], "factors": d["factors"]})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
