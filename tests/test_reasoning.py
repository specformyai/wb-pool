
"""app/reasoning.py 离线单测 —— 不打上游、不烧积分。

运行: .venv/bin/python tests/test_reasoning.py
     .venv/bin/python tests/test_reasoning.py --module /path/to/reasoning.py   (突变测试用)
"""
import importlib.util
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "app" / "reasoning.py"


def load(path):
    """按显式路径加载，避免 sys.path 里的生产副本盖住被测文件。"""
    spec = importlib.util.spec_from_file_location("_reasoning_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


target = DEFAULT
if "--module" in sys.argv:
    target = Path(sys.argv[sys.argv.index("--module") + 1])
R = load(target)

FAILS = []
N = 0


def eq(got, want, label):
    global N
    N += 1
    if got != want:
        FAILS.append(f"{label}: got={got!r} want={want!r}")


def raises(fn, exc, label):
    global N
    N += 1
    try:
        fn()
    except exc:
        return
    except Exception as e:
        FAILS.append(f"{label}: \u629b\u4e86 {type(e).__name__} \u800c\u975e {exc.__name__}")
        return
    FAILS.append(f"{label}: \u6ca1\u629b\u5f02\u5e38")


# ---------------- map_thinking ----------------
eq(R.map_thinking("high"), "high", "\u88f8\u5b57\u7b26\u4e32 high")
eq(R.map_thinking("XHIGH"), "xhigh", "\u5927\u5199\u5f52\u4e00")
eq(R.map_thinking("\u80e1\u8bf4"), None, "\u4e0d\u8ba4\u8bc6\u7684\u5b57\u7b26\u4e32 -> None")
eq(R.map_thinking(None), None, "None -> None")
eq(R.map_thinking({"type": "disabled"}), "none", "disabled -> none")
eq(R.map_thinking({"type": "adaptive"}), "high", "adaptive -> fallback")
eq(R.map_thinking({"type": "adaptive"}, fallback="low"), "low", "adaptive \u5c0a\u91cd\u81ea\u5b9a fallback")
eq(R.map_thinking({"effort": "max"}), "max", "{effort} \u76f4\u53d6")
eq(R.map_thinking({"type": "enabled"}), "high", "enabled \u65e0 budget -> fallback")
eq(R.map_thinking({"type": "enabled", "budget_tokens": 1024}), "low", "budget 1024 -> low")
eq(R.map_thinking({"type": "enabled", "budget_tokens": 2048}), "medium", "budget 2048 -> medium (\u8fb9\u754c)")
eq(R.map_thinking({"type": "enabled", "budget_tokens": 2047}), "low", "budget 2047 -> low (\u8fb9\u754c\u4e0b\u4fa7)")
eq(R.map_thinking({"type": "enabled", "budget_tokens": 8192}), "high", "budget 8192 -> high (\u8fb9\u754c)")
eq(R.map_thinking({"type": "enabled", "budget_tokens": 8191}), "medium", "budget 8191 -> medium")
eq(R.map_thinking({"type": "enabled", "budget_tokens": 32768}), "xhigh", "budget 32768 -> xhigh (\u8fb9\u754c)")
eq(R.map_thinking({"type": "enabled", "budget_tokens": 32767}), "high", "budget 32767 -> high")
eq(R.map_thinking({"budget_tokens": 40000}), "xhigh", "\u65e0 type \u4f46\u6709 budget \u7684\u5bbd\u677e\u5199\u6cd5")
eq(R.map_thinking({}), None, "\u7a7a dict -> None")
eq(R.map_thinking(123), None, "\u975e dict/str -> None")
eq(R.effort_from_budget("abc"), "high", "\u975e\u6570\u5b57 budget -> fallback")
eq(R.effort_from_budget(-5), "high", "\u8d1f budget -> fallback")

# ---------------- clamp_effort ----------------
eq(R.clamp_effort("medium", None), "medium", "\u65e0 meta \u539f\u6837\u900f\u4f20")
eq(R.clamp_effort("medium", {}), "medium", "\u7a7a meta \u539f\u6837\u900f\u4f20")
eq(R.clamp_effort(None, {}), None, "None \u8fd4\u56de None")
eq(R.clamp_effort("medium", {"reasoning": {"supportedEfforts": ["low", "high"]}}),
   "low", "medium \u4e0d\u5728\u8868\u91cc -> \u5e73\u5c40\u53d6\u4f4e\u6863 low")
eq(R.clamp_effort("high", {"reasoning": {"supportedEfforts": ["low", "high"]}}),
   "high", "high \u5728\u8868\u91cc\u539f\u6837\u7ed9")
# \u5e73\u5c40\u89c4\u5219\uff1axhigh \u8ddd high/max \u90fd\u662f 1\uff0c\u53d6\u4f4e\u6863 = \u7edd\u4e0d\u8d85\u989d\u6d88\u8017\u79ef\u5206
eq(R.clamp_effort("xhigh", {"reasoning": {"supportedEfforts": ["low", "high", "max"]}}),
   "high", "\u5e73\u5c40\u53d6\u4f4e\u6863\uff08\u4e0d\u8d85\u989d\u70e7\u5206\uff09")
eq(R.clamp_effort("none", {"reasoning": {"canDisableThinking": True,
                                        "supportedEfforts": ["low", "high", "max"]}}),
   "none", "canDisable=True \u5141\u8bb8 none\uff08\u5373\u4f7f supportedEfforts \u4e0d\u5217 none\uff09")
eq(R.clamp_effort("none", {"reasoning": {"canDisableThinking": False,
                                        "defaultEffort": "high",
                                        "supportedEfforts": ["low", "high"]}}),
   "high", "canDisable=False \u62d2\u7ed8 none -> \u9ed8\u8ba4\u6863")
eq(R.clamp_effort("none", {"reasoning": {"canDisableThinking": False,
                                        "supportedEfforts": ["low", "high"]}}),
   "low", "canDisable=False \u4e14\u65e0\u9ed8\u8ba4\u6863 -> \u6700\u4f4e\u53ef\u7528\u6863")
# snake_case \u4e0e camelCase \u4e24\u79cd\u5199\u6cd5\u90fd\u8ba4\uff08\u7f13\u5b58\u843d\u76d8\u540e\u662f snake\uff09
eq(R.clamp_effort("none", {"reasoning": {"can_disable_thinking": True,
                                        "supported_efforts": ["low", "high"]}}),
   "none", "snake_case \u5b57\u6bb5\u540d\u4e5f\u8ba4")

# ---------------- check_unsupported ----------------
raises(lambda: R.check_unsupported({"model": "hy3", "response_format": {"type": "json_object"}}),
       R.ParamRejected, "response_format \u88ab\u62d2")
raises(lambda: R.check_unsupported({"model": "hy3", "n": 2}), R.ParamRejected, "n \u88ab\u62d2")
raises(lambda: R.check_unsupported({"seed": 1}), R.ParamRejected, "seed \u88ab\u62d2")
raises(lambda: R.check_unsupported({"logprobs": True}), R.ParamRejected, "logprobs \u88ab\u62d2")
N += 1
try:
    R.check_unsupported({"model": "hy3", "messages": [], "temperature": 0.7,
                         "max_tokens": 100, "tools": [], "stream": True})
except Exception as e:
    FAILS.append(f"\u6b63\u5e38\u53c2\u6570\u8bef\u62d2: {e}")

# ---------------- resolve_context_window ----------------
META_CW = {"context_window": {"defaultLength": 300000,
                              "supportedLengths": [300000, 1000000]}}
eq(R.resolve_context_window(None, None), None, "\u6a21\u578b\u65e0\u6863\u4f4d -> None")
eq(R.resolve_context_window(None, {}), None, "\u7a7a meta -> None")
eq(R.resolve_context_window(1000000, META_CW), 1000000, "\u5408\u6cd5\u8bf7\u6c42\u6863\u4f4d\u76f4\u53d6")
eq(R.resolve_context_window(12345, META_CW), 300000,
   "\u975e\u6cd5\u6863\u4f4d\u56de\u843d\u9ed8\u8ba4\uff08\u4e0a\u6e38\u9759\u9ed8\u63a5\u53d7\uff0c\u5fc5\u987b\u6211\u4eec\u6536\u655b\uff09")
eq(R.resolve_context_window(None, META_CW), 300000, "\u4e0d\u6307\u5b9a -> \u9ed8\u8ba4\u6863")
eq(R.resolve_context_window(None, META_CW, configured=1000000), 1000000,
   "\u9762\u677f\u914d\u7f6e\u751f\u6548")
eq(R.resolve_context_window(300000, META_CW, configured=1000000), 300000,
   "\u8bf7\u6c42\u4f18\u5148\u4e8e\u9762\u677f\u914d\u7f6e")
eq(R.resolve_context_window(None, META_CW, configured=999), 300000,
   "\u9762\u677f\u914d\u7f6e\u975e\u6cd5\u4e5f\u56de\u843d")
eq(R.resolve_context_window(None, {"context_window": {"supportedLengths": [200000, 500000]}}),
   500000, "\u65e0 defaultLength -> \u53d6\u6700\u5927\u6863")

# ---------------- strip_for_upstream ----------------
got = R.strip_for_upstream({"model": "hy3", "messages": [], "thinking": {"type": "enabled"},
                            "reasoning_effort": "high", "context_window": 300000,
                            "anthropic_version": "2023-06-01", "system": "x"})
eq(sorted(got), ["context_window", "messages", "model", "reasoning_effort"],
   "\u81ea\u5b9a\u4e49\u5b57\u6bb5\u4e0d\u5f80\u4e0a\u6e38\u53d1")

print(f"== reasoning \u5355\u6d4b: {N - len(FAILS)}/{N} \u901a\u8fc7  (module={target})")
if FAILS:
    print(f"=== {len(FAILS)} \u5904\u5931\u8d25 ===")
    for f in FAILS:
        print("  FAIL", f)
    sys.exit(1)
sys.exit(0)
