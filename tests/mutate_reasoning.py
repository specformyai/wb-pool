
"""\u7a81\u53d8\u5bf9\u7167\uff1a\u6545\u610f\u7834\u574f\u5b9e\u73b0\uff0c\u9a8c\u8bc1\u5355\u6d4b\u771f\u7684\u4f1a\u53d8\u7ea2\u3002
\u4e0d\u53d8\u7ea2\u7684\u65ad\u8a00 = \u5047\u7eff\u3002
"""
import subprocess
import sys
import tempfile
from pathlib import Path

# 路径一律相对本文件定位，不写死部署路径。
# 写死绝对路径的后果实测过：脚本在仓库副本里跑、却去读生产那份源码，
# 于是「改了仓库代码，突变对照仍全绿」—— 测的根本不是你改的文件。
_ROOT = Path(__file__).resolve().parent.parent
SRC = _ROOT / "app" / "reasoning.py"
TEST = _ROOT / "tests" / "test_reasoning.py"
# 优先用项目自带 venv，没有就用当前解释器（仓库副本上通常没有 .venv）
_VENV = _ROOT / ".venv" / "bin" / "python"
PY = str(_VENV) if _VENV.exists() else sys.executable

MUTATIONS = [
    ("\u5206\u6863\u8fb9\u754c 2048->4096", "(2048, \"medium\")", "(4096, \"medium\")"),
    ("\u5206\u6863\u8fb9\u754c 8192->9999", "(8192, \"high\")", "(9999, \"high\")"),
    ("canDisable=True \u65f6\u4e0d\u653e\u884c none",
     'if effort == "none" and can_disable:\n        return "none"',
     'if effort == "none" and can_disable and False:\n        return "none"'),
    ("canDisable=False \u4e0d\u62e6 none",
     'if effort == "none" and not can_disable:',
     'if effort == "none" and not can_disable and False:'),
    ("\u62d2\u7edd\u540d\u5355\u53bb\u6389 n", '\"response_format\", \"n\",', '\"response_format\",'),
    ("\u62d2\u7edd\u540d\u5355\u53bb\u6389 response_format", '\"response_format\", \"n\",', '\"n\",'),
    ("check_unsupported \u76f4\u63a5\u4e0d\u62db",
     "        if key in UNSUPPORTED_PARAMS:", "        if False:"),
    ("ctx \u4e0d\u6821\u9a8c\u975e\u6cd5\u6863\u4f4d",
     "        if n in valid:\n            return n", "        return n"),
    ("ctx \u56de\u843d\u6539\u6210\u53d6\u6700\u5c0f", "    return max(valid)", "    return min(valid)"),
    ("strip \u6539\u6210\u5168\u900f\u4f20",
     "    return {k: v for k, v in payload.items() if k in UPSTREAM_ALLOWED}",
     "    return dict(payload)"),
    ("\u5e73\u5c40\u53d6\u9ad8\u6863\uff08\u800c\u975e\u4f4e\u6863\uff09",
     "    return min(allowed, key=lambda e: (abs(EFFORTS.index(e) - want), EFFORTS.index(e)))",
     "    return min(allowed, key=lambda e: (abs(EFFORTS.index(e) - want), -EFFORTS.index(e)))"),
    ("adaptive \u4e0d\u8bfb fallback",
     '    if ttype == "adaptive":\n        return fallback',
     '    if ttype == "adaptive":\n        return "low"'),
]

src = SRC.read_text(encoding="utf-8")
survived, killed = [], []

with tempfile.TemporaryDirectory() as td:
    for label, old, new in MUTATIONS:
        if old not in src:
            survived.append(f"{label}  [\u951a\u70b9\u672a\u547d\u4e2d\uff0c\u7a81\u53d8\u672a\u6ce8\u5165]")
            continue
        mutated = src.replace(old, new, 1)
        mp = Path(td) / "reasoning_mut.py"
        mp.write_text(mutated, encoding="utf-8")
        r = subprocess.run([PY, str(TEST), "--module", str(mp)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            survived.append(label)
        else:
            killed.append(label)

print(f"== \u7a81\u53d8\u5bf9\u7167: {len(killed)}/{len(MUTATIONS)} \u88ab\u6740\u6b7b")
for k in killed:
    print("  \u2713 \u53d8\u7ea2  ", k)
for s in survived:
    print("  \u2717 \u672a\u53d8\u7ea2(\u65ad\u8a00\u662f\u5047\u7684!)  ", s)
sys.exit(1 if survived else 0)
