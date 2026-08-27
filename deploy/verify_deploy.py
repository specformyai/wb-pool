#!/usr/bin/env python3
"""部署后端到端验证 wb-pool（会打真实上游，消耗少量积分）。"""
import json, pathlib, urllib.request, time

# 用法：
#   python3 deploy/verify_deploy.py                    # 默认读 ./.env
#   WB_BASE=http://127.0.0.1:9188 WB_KEY=xxx python3 deploy/verify_deploy.py
#
# ⚠️ 这个脚本会打**真实上游**（探活出口、真发一次 chat），会消耗少量积分。
#    它是部署后的人工验收工具，不进 CI —— CI 全程假上游。
import os

BASE = os.environ.get("WB_BASE", "http://127.0.0.1:9188").rstrip("/")


def _read_key() -> str:
    """优先环境变量，其次项目根的 .env。不写死任何机器路径。"""
    if os.environ.get("WB_KEY"):
        return os.environ["WB_KEY"].strip()
    root = pathlib.Path(__file__).resolve().parent.parent
    envf = pathlib.Path(os.environ.get("WB_ENV_FILE") or (root / ".env"))
    if envf.is_file():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if line.startswith("WB_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


KEY = _read_key()
if not KEY:
    print("拿不到 WB_API_KEY：设 WB_KEY 环境变量，或在项目根放 .env")
    raise SystemExit(2)

def call(path, body=None, timeout=300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()[:400]
    except Exception as e:
        return 0, {}, str(e)[:300]

print(f"API KEY = {KEY}\n")

print("[1] 健康检查")
st, _, d = call("/api/health")
print("   ", st, json.dumps(d, ensure_ascii=False)[:200])

print("\n[2] resin 出口全量探活（打真实业务端点）")
st, _, d = call("/api/proxy/probe", {})
if isinstance(d, dict):
    print(f"    可达 {len(d.get('usable', []))}/{len(d.get('results', []))} 个出口")
    for r in d.get("results", []):
        print(f"      {'OK' if r['ok'] else '--'} :{r['port']} {r['cc']:6s} {r.get('ip','')}")
else:
    print("   ", st, d)

print("\n[3] 鉴权校验（不带 key 应当 401）")
try:
    urllib.request.urlopen(urllib.request.Request(BASE + "/v1/models"), timeout=30)
    print("    异常：无 key 也能访问")
except urllib.error.HTTPError as e:
    print(f"    HTTP {e.code}（预期 401）")

print("\n[4] 模型探测（经 resin 出口）")
st, _, d = call("/api/models?force=true", timeout=400)
models = d.get("models", []) if isinstance(d, dict) else []
print(f"    {len(models)} 个可用: {models}")

print("\n[5] 余额")
st, _, d = call("/api/pool/refresh_balance", {}, timeout=200)
if isinstance(d, dict):
    for r in d.get("results", []):
        print(f"    {r['masked']} 余额={r['total']}")
        for p in r.get("packages", []):
            print(f"       {p['name']} {p['remain']} (至 {p['cycle_end']})")

print("\n[6] 非流式对话（反代聚合上游流）")
st, h, d = call("/v1/chat/completions", {
    "model": "deepseek-v3",
    "messages": [{"role": "user", "content": "用一句中文说明你是谁"}]}, timeout=200)
if isinstance(d, dict) and "choices" in d:
    u = d.get("usage", {})
    print(f"    HTTP {st} 账号={h.get('X-WB-Account')} credit={u.get('credit')} "
          f"tokens={u.get('total_tokens')}")
    print(f"    内容: {d['choices'][0]['message']['content'][:70]}")
else:
    print("   ", st, str(d)[:300])

print("\n[7] 流式对话")
req = urllib.request.Request(BASE + "/v1/chat/completions",
    data=json.dumps({"model": "glm-5.2", "stream": True,
                     "messages": [{"role": "user", "content": "从1数到5"}]}).encode(),
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=200) as r:
        acct = r.headers.get("X-WB-Account")
        n, out = 0, ""
        for line in r:
            s = line.decode().strip()
            if not s.startswith("data:"):
                continue
            p = s[5:].strip()
            if p == "[DONE]":
                break
            j = json.loads(p)
            n += 1
            for c in j.get("choices", []):
                out += (c.get("delta") or {}).get("content") or ""
        print(f"    {n} 个 SSE chunk，账号={acct}，内容={out[:60]!r}")
except Exception as e:
    print("    失败:", str(e)[:200])

print("\n[8] Anthropic 兼容")
st, _, d = call("/v1/messages", {
    "model": "kimi-k2.6", "max_tokens": 100,
    "messages": [{"role": "user", "content": "用中文回一句问候"}]}, timeout=200)
if isinstance(d, dict) and d.get("content"):
    print(f"    HTTP {st} {d['content'][0]['text'][:60]}  usage={d.get('usage')}")
else:
    print("   ", st, str(d)[:250])

print("\n[9] 倍率表（按 usage.credit 累计）")
st, _, d = call("/api/rates")
if isinstance(d, dict):
    print(f"    基准模型: {d.get('base_model')}")
    for r in d.get("rows", []):
        print(f"      {r['model']:22s} credit={r['credits']:<8} tok={r['total_tokens']:<7} "
              f"/1k={r['credits_per_1k']} 倍率={r['multiplier']}")

print("\n[10] 池状态")
st, _, d = call("/api/pool")
if isinstance(d, dict):
    print("    stats:", json.dumps(d["stats"], ensure_ascii=False))
    for a in d["accounts"]:
        print(f"      {a['masked']} {a['status']} 余额={a['credits_total']} "
              f"已耗={a['credits_spent']} 请求={a['request_count']} token有效={a['expires_in_h']}h")

print("\n[11] WebUI")
for p in ("/", "/static/app.css", "/static/app.js"):
    try:
        with urllib.request.urlopen(BASE + p, timeout=30) as r:
            print(f"    {p} → HTTP {r.status} {len(r.read())} bytes")
    except Exception as e:
        print(f"    {p} → {str(e)[:80]}")
