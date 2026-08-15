#!/usr/bin/env python3
"""
离线验证 upstream_sync 的三层降级逻辑（不碰线上 data/models_cache.json）。
1. 缓存不存在      -> resolve_models 落静态兜底（console 11 + 漏网 3 = 14）
2. 老格式 list[str] -> 自动升级成 list[dict]
3. 失败写入        -> last_fail 生效、5min 冷却内 in_fail_cooldown=True，且旧 models 不丢
4. TTL             -> 1h 前的时间戳判过期
5. to_openai_data  -> 字段齐全，id 不丢
6. 损坏缓存        -> 不抛异常，落静态兜底
7. 漏网模型合并    -> merge_unlisted 幂等、不覆盖同 id、缓存命中路径也补
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import upstream_sync as us

TMP = Path(tempfile.mkdtemp(prefix="us_test_"))
fails = []

N_CONSOLE = len(us.STATIC_MODELS)          # 11
N_UNLISTED = len(us.UNLISTED_MODELS)       # 3
N_ALL = N_CONSOLE + N_UNLISTED             # 14


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        fails.append(name)


print("[1] 缓存不存在 -> 静态兜底")
p = TMP / "nope.json"
p.unlink(missing_ok=True)
models, source = us.resolve_models(p)
check("source == static", source == "static", source)
check(f"{N_ALL} 个静态模型（含漏网）", len(models) == N_ALL, str(len(models)))
check("每项有 vendor_label", all(m.get("vendor_label") for m in models))
check("is_cache_expired == True", us.is_cache_expired(p) is True)

print("\n[2] 老格式 list[str] 自动升级")
p2 = TMP / "old.json"
p2.write_text(json.dumps({"models": ["glm-5.2", "kimi-k2.6"], "timestamp": time.time()}))
c = us.load_models_cache(p2)
check("升级为 dict", isinstance(c["models"][0], dict), str(c["models"][0])[:60])
check("id 保留", [m["id"] for m in c["models"]] == ["glm-5.2", "kimi-k2.6"])

print("\n[3] 上游失败 -> 负缓存，旧数据不丢")
p3 = TMP / "fail.json"
good = {"models": [{"id": "glm-5.2", "name": "GLM", "ctx": 1000, "max_output_tokens": 10}],
        "timestamp": time.time(), "available_count": 1, "source": "console_api"}
p3.write_text(json.dumps(good))
orig = us.upstream.fetch_official_models
us.upstream.fetch_official_models = lambda *a, **k: {"error": "503 Service Unavailable", "models": []}
try:
    r = us.sync_models_from_upstream("faketoken", cache_path=p3)
finally:
    us.upstream.fetch_official_models = orig
check("ok == False", r.get("ok") is False)
check("error 透传", "503" in str(r.get("error")), str(r.get("error")))
check("旧 models 保留", [m["id"] for m in r["models"]] == ["glm-5.2"])
after = us.load_models_cache(p3)
check("in_fail_cooldown == True", us.in_fail_cooldown(after) is True)
check("落盘后仍能解析出模型", us.resolve_models(p3)[0][0]["id"] == "glm-5.2")

print("\n[4] TTL 判定")
p4 = TMP / "ttl.json"
p4.write_text(json.dumps({"models": [{"id": "x"}], "timestamp": time.time() - 3700}))
check("1h+ 判过期", us.is_cache_expired(p4) is True)
p4.write_text(json.dumps({"models": [{"id": "x"}], "timestamp": time.time() - 600}))
check("10min 未过期", us.is_cache_expired(p4) is False)

print("\n[5] to_openai_data 字段")
data = us.to_openai_data(us.STATIC_MODELS)
check("数量一致", len(data) == N_CONSOLE, str(len(data)))
need = {"id", "object", "created", "owned_by", "context_length", "max_output_tokens", "display_name"}
check("必需字段齐全", all(need <= set(d) for d in data))
check("owned_by 已人类化", {d["owned_by"] for d in data} >= {"Zhipu", "Moonshot", "DeepSeek", "MiniMax", "Tencent"},
      str(sorted({d["owned_by"] for d in data})))
check("坏数据不炸", us.to_openai_data([{"name": "无 id"}, {"id": "ok"}]) and
      len(us.to_openai_data([{"name": "无 id"}, {"id": "ok"}])) == 1)
unl = us.to_openai_data(us.UNLISTED_MODELS)
check("unlisted 标记透传", all(d.get("unlisted") is True for d in unl))

print("\n[6] 损坏缓存不炸")
p6 = TMP / "broken.json"
p6.write_text("{ 这不是 json")
check("坏 json -> 空结构", us.load_models_cache(p6)["models"] == [])
check("坏 json -> 静态兜底", us.resolve_models(p6)[1] == "static")

print("\n[7] 漏网模型合并（console 未列但实测可调）")
unlisted_ids = {m["id"] for m in us.UNLISTED_MODELS}
check("含 glm-5.3 / kimi-k3 / kimi-k3-2",
      unlisted_ids == {"glm-5.3", "kimi-k3", "kimi-k3-2"}, str(sorted(unlisted_ids)))
merged = us.merge_unlisted([{"id": "glm-5.2", "name": "GLM-5.2"}])
check("并入后 1+3 项", len(merged) == 1 + N_UNLISTED, str(len(merged)))
check("漏网项带 vendor_label",
      all(m.get("vendor_label") for m in merged if m["id"] in unlisted_ids))
check("Zhipu/Moonshot 归类正确",
      us.vendor_of("glm-5.3") == "Zhipu" and us.vendor_of("kimi-k3") == "Moonshot")
# 幂等：并两次不重复
check("幂等（并两次不重复）",
      len(us.merge_unlisted(merged)) == len(merged), str(len(us.merge_unlisted(merged))))
# 不覆盖：上游若开始正式返回 glm-5.3，用上游那份
up = us.merge_unlisted([{"id": "glm-5.3", "name": "官方版", "ctx": 123}])
g53 = [m for m in up if m["id"] == "glm-5.3"]
check("同 id 不覆盖上游", len(g53) == 1 and g53[0]["name"] == "官方版", str(g53[:1]))
# 缓存命中路径也要补漏网
p7 = TMP / "hit.json"
p7.write_text(json.dumps({"models": [{"id": "glm-5.2", "name": "GLM-5.2", "ctx": 1000,
                                      "max_output_tokens": 10}],
                          "timestamp": time.time(), "source": "console_api"}))
got, src = us.resolve_models(p7)
check("缓存命中也带漏网模型", {m["id"] for m in got} >= unlisted_ids, str(sorted(m["id"] for m in got)))
check("source 仍是 console_api", src == "console_api", src)
check("static_models() == 14", len(us.static_models()) == N_ALL, str(len(us.static_models())))

print("\n" + ("全部通过" if not fails else f"{len(fails)} 项失败: {fails}"))
sys.exit(1 if fails else 0)
