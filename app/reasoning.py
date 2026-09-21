"""
思考控制与语义参数策略
======================
本模块只做纯函数，不碰网络、不读全局状态 —— 这样能离线测。

三件事：

1. **thinking → reasoning_effort 映射**
   上游（copilot.tencent.com /v2/chat/completions）顶层吃 `reasoning_effort`，
   取值 none/minimal/low/medium/high/xhigh/max。实测 2026-09-21 于 glm-5.3-flash
   （canDisableThinking=true，带区分度的对照）：

       effort=none -> 思考   0 字 / 正文 115 字
       effort=max  -> 思考 547 字 / 正文  31 字
       不传        -> 思考 636 字 / 正文  40 字

   而 hy3（canDisableThinking=false）的 low/high 差异无区分度（low 反而思考更多），
   所以**验证 effort 是否生效必须挑能关思考的模型**，拿 hy3 做判据会得出「不生效」的错误结论。

   Anthropic 侧客户端给的是 `thinking: {type, budget_tokens}`，要分档映射。
   档位边界照 Anthropic 官方语义取（1024 是官方最小 budget）：

       disabled            -> none
       enabled < 2048      -> low
       enabled >= 2048     -> medium
       enabled >= 8192     -> high
       enabled >= 32768    -> xhigh
       adaptive / 无 budget -> fallback（默认 high）

2. **语义参数策略**
   实测上游对这批参数**静默忽略**（不报错、也不生效），2026-09-21：

       response_format={"type":"json_object"} -> 返回散文，不是 JSON
       n=2                                    -> 只回 choices[0] 一个
       seed / presence_penalty / logprobs     -> 不报错不生效

   静默忽略比报错更坏：客户端以为要到了 JSON，拿到散文还不知道为什么。
   所以这些键一律 400 拒绝，让调用方当场知道。

3. **context_window 档位校验**
   上游对非法档位（如 12345）同样静默接受，不校验也不报错，
   所以必须由我们按模型的 supportedLengths 收敛。
"""
from __future__ import annotations

from typing import Any

# 上游认的 effort 取值（顺序 = 强度递增，用于 clamp）
EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_SET = frozenset(EFFORTS)

DEFAULT_FALLBACK_EFFORT = "high"

# budget_tokens 分档表：(下界, effort)，从大到小匹配
_BUDGET_TIERS: tuple[tuple[int, str], ...] = (
    (32768, "xhigh"),
    (8192, "high"),
    (2048, "medium"),
    (0, "low"),
)

# 会静默改变语义的参数：上游不认，装作支持等于骗客户端
# 不列 metadata / service_tier：它们是 Anthropic 官方参数，Claude Code 每个请求
# 都带 metadata.user_id，且二者只是元信息、不改变回复语义，拒了只会把客户端挡在门外
# （2026-09-21 生产实测：带 metadata 的请求被 400）。
# 也不列 store：它只是 OpenAI 的「是否在服务端留存本次对话」开关，不影响回复内容；
# pi-ai 的 openai-completions 适配器（dsh 在用）默认每个请求都带 store=false，
# 拒了会把 dsh 整条 OpenAI 口打死（2026-09-21 生产实测 400）。
UNSUPPORTED_PARAMS: frozenset[str] = frozenset({
    "response_format", "n", "seed", "logprobs", "top_logprobs",
    "presence_penalty", "frequency_penalty", "logit_bias",
    "functions", "function_call",
    "modalities", "audio", "prediction", "web_search_options",
})

# 上游 chat 请求允许出现的顶层键（白名单之外的自定义键不往上游发）
UPSTREAM_ALLOWED: frozenset[str] = frozenset({
    "model", "messages", "stream", "temperature", "top_p", "max_tokens",
    "stop", "tools", "tool_choice", "parallel_tool_calls",
    "reasoning_effort", "context_window",
})


class ParamRejected(ValueError):
    """客户端传了会静默失真的参数。调用方据此回 400。"""

    def __init__(self, param: str, message: str):
        super().__init__(message)
        self.param = param
        self.message = message


def normalize_effort(value: Any, fallback: str = DEFAULT_FALLBACK_EFFORT) -> str | None:
    """把任意输入收敛成合法 effort；不认识的返回 None（调用方决定丢弃还是报错）。"""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in _EFFORT_SET:
        return v
    return None if v else None


def effort_from_budget(budget: Any, fallback: str = DEFAULT_FALLBACK_EFFORT) -> str:
    """Anthropic budget_tokens -> effort 分档。非数字/非正数回落 fallback。"""
    try:
        n = int(budget)
    except (TypeError, ValueError):
        return fallback
    if n <= 0:
        return fallback
    for lo, eff in _BUDGET_TIERS:
        if n >= lo:
            return eff
    return fallback


def map_thinking(thinking: Any, fallback: str = DEFAULT_FALLBACK_EFFORT) -> str | None:
    """
    Anthropic `thinking` -> 上游 reasoning_effort。

    支持这几种形态（各家客户端写法不统一，都得认）：
        "high"                                  裸字符串
        {"type": "disabled"}                    -> none
        {"type": "enabled", "budget_tokens": N} -> 分档
        {"type": "adaptive"}                    -> fallback
        {"effort": "xhigh"}                     -> 直接取
    认不出来返回 None（= 不往上游发这个字段，用模型默认行为）。
    """
    if thinking is None:
        return None
    if isinstance(thinking, str):
        return normalize_effort(thinking, fallback)
    if not isinstance(thinking, dict):
        return None

    # {"effort": "..."} 优先：最直接的表达
    if thinking.get("effort") is not None:
        eff = normalize_effort(thinking.get("effort"), fallback)
        if eff:
            return eff

    ttype = str(thinking.get("type") or "").strip().lower()
    if ttype == "disabled":
        return "none"
    if ttype == "adaptive":
        return fallback
    if ttype == "enabled":
        if thinking.get("budget_tokens") is None:
            return fallback
        return effort_from_budget(thinking.get("budget_tokens"), fallback)
    # 没有 type 但有 budget_tokens 的宽松写法
    if thinking.get("budget_tokens") is not None:
        return effort_from_budget(thinking.get("budget_tokens"), fallback)
    return None


def supported_efforts(meta: dict[str, Any] | None) -> list[str]:
    """模型声明支持的 effort 列表；没声明就返回空（= 不限制）。"""
    r = ((meta or {}).get("reasoning") or {}) if isinstance(meta, dict) else {}
    if not isinstance(r, dict):
        return []
    got = r.get("supported_efforts") or r.get("supportedEfforts") or []
    if not isinstance(got, (list, tuple)):
        return []
    return [e for e in (str(x).strip().lower() for x in got) if e in _EFFORT_SET]


def clamp_effort(effort: str | None, meta: dict[str, Any] | None) -> str | None:
    """
    把 effort 收敛到该模型真支持的档位。

    模型没声明 supportedEfforts 时原样透传（上游自己会忽略不认的值）。
    声明了但请求的档位不在列表里时，取**最接近的可用档**而不是报错 ——
    客户端要 medium 而模型只有 [low, high]，给 low 比 400 更有用。

    另一条硬规则：canDisableThinking=false 的模型不能接受 none，
    强行发 none 上游会当没看见，客户端却以为关掉了思考。
    """
    if not effort:
        return None
    allowed = supported_efforts(meta)
    r = ((meta or {}).get("reasoning") or {}) if isinstance(meta, dict) else {}
    can_disable = bool(r.get("can_disable_thinking") or r.get("canDisableThinking")) \
        if isinstance(r, dict) else False

    if effort == "none" and not can_disable:
        # 关不掉思考的模型：退回它的默认档，别假装关掉了
        if isinstance(r, dict):
            dflt = normalize_effort(r.get("default_effort") or r.get("defaultEffort")
                                    or r.get("effort"))
            if dflt:
                return dflt
        return allowed[0] if allowed else None

    # canDisableThinking=true 的模型：上游的 supportedEfforts 只列正向档位，
    # 从不把 none 写进去（实测 gpt-6-astra 与 glm-5.3 的 supportedEfforts 均无 none，
    # 但两者 canDisableThinking 都是 true）。所以 none 必须在这里显式放行，
    # 否则会被下面的「最接近档位」逻辑折成 low：
    # 客户端明确要求关闭思考，实际却拿到 low 强度的思考。
    if effort == "none" and can_disable:
        return "none"

    if not allowed or effort in allowed:
        return effort

    # 取强度最接近的可用档
    want = EFFORTS.index(effort)
    return min(allowed, key=lambda e: (abs(EFFORTS.index(e) - want), EFFORTS.index(e)))


def check_unsupported(body: dict[str, Any]) -> None:
    """客户端传了静默失真的参数就抛 ParamRejected（调用方回 400）。"""
    for key in body:
        if key in UNSUPPORTED_PARAMS:
            raise ParamRejected(
                key,
                f"参数 {key} 不被上游支持：上游会静默忽略它，"
                f"返回的结果与该参数无关。已拒绝以免结果失真。",
            )


def drop_unsupported(payload: dict[str, Any]) -> list[str]:
    """从待发上游的请求体里剥掉静默失真的参数，返回剥掉的键名（按名单顺序稳定）。

    默认策略（2026-09-21 用户决定）：不 400、直接丢。上游本来就会忽略它们，
    在我们这一层剥掉只是让请求体干净，并通过 X-WB-Dropped-Params 头告诉客户端
    「你要的这个语义没生效」——排障时能看见，又不打断正常对话。
    """
    dropped = [k for k in sorted(UNSUPPORTED_PARAMS) if k in payload]
    for k in dropped:
        payload.pop(k, None)
    return dropped


def resolve_context_window(requested: Any, meta: dict[str, Any] | None,
                           configured: Any = None) -> int | None:
    """
    定 context_window。上游对非法档位静默接受，所以必须我们收敛。

    优先级：请求里显式指定 > 面板按模型配置 > 模型默认档。
    非法档位（不在 supportedLengths 里）一律丢弃并回落，不报错 ——
    这是能力协商而非用户错误。
    """
    cw = ((meta or {}).get("context_window") or {}) if isinstance(meta, dict) else {}
    if not isinstance(cw, dict):
        cw = {}
    lengths = cw.get("supported_lengths") or cw.get("supportedLengths") or []
    if not isinstance(lengths, (list, tuple)):
        lengths = []
    valid: list[int] = []
    for x in lengths:
        try:
            valid.append(int(x))
        except (TypeError, ValueError):
            continue
    if not valid:
        return None

    for cand in (requested, configured):
        if cand is None:
            continue
        try:
            n = int(cand)
        except (TypeError, ValueError):
            continue
        if n in valid:
            return n

    dflt = cw.get("default_length") or cw.get("defaultLength")
    try:
        d = int(dflt)
        if d in valid:
            return d
    except (TypeError, ValueError):
        pass
    return max(valid)


def wants_thinking(body: dict[str, Any], default: bool = False,
                   header: Any = None) -> bool:
    """客户端要不要回传 thinking 块。

    为什么是请求级而不是服务端全局：Anthropic 协议里 `thinking` 参数
    就是客户端表达「我要思考内容」的官方方式。做成服务端开关会让同一
    实例上的不同客户端没法各取所需 —— 严格客户端（会校验 signature 的）
    需要不发，宽松客户端想看思考。所以以客户端意图为准。

    优先级：X-WB-Thinking 头 > 请求体 thinking > 服务端默认。

    注意 `signature`：官方 thinking 块带签名供多轮回传时验签，上游只给
    OpenAI 口径的 reasoning_content 纯文本，拿不到真签名。所以默认不发，
    由客户端显式要求时才发 —— 谁要谁负责自己的客户端能不能吃下。
    """
    # 1) 显式头覆盖：给那些不发 thinking 参数、但想看思考的客户端
    if header is not None:
        h = str(header).strip().lower()
        if h in ("on", "1", "true", "yes", "enabled"):
            return True
        if h in ("off", "0", "false", "no", "disabled"):
            return False

    # 2) 请求体的 Anthropic 官方语义
    t = body.get("thinking")
    if isinstance(t, dict):
        ttype = str(t.get("type") or "").strip().lower()
        if ttype == "enabled":
            return True
        if ttype == "disabled":
            return False
        if ttype == "adaptive":
            return True
        # 没写 type 但给了强度/预算 = 明确要思考
        if t.get("effort") is not None:
            return normalize_effort(t.get("effort")) != "none"
        if t.get("budget_tokens") is not None:
            try:
                return int(t.get("budget_tokens")) > 0
            except (TypeError, ValueError):
                return bool(default)
    elif isinstance(t, str) and t.strip():
        # 裸字符串就是 effort，"none" = 不要思考
        return normalize_effort(t) != "none"

    # 3) 客户端完全没表态
    return bool(default)


def strip_for_upstream(payload: dict[str, Any]) -> dict[str, Any]:
    """只保留上游认的顶层键，避免把自定义字段（如 thinking）原样发上去。"""
    return {k: v for k, v in payload.items() if k in UPSTREAM_ALLOWED}
