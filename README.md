# wb-pool

WorkBuddy / CodeBuddy 账号池反向代理。把账号的 token 池化，统一暴露成 OpenAI 与 Anthropic 兼容端点，
带 LRU 轮询、token 自动续期、余额与倍率看板、每日签到，以及一个自带的 WebUI。

## 能力

- **OpenAI 兼容**：`GET /v1/models`、`POST /v1/chat/completions`（流式与非流式都支持）
- **Anthropic 兼容**：`POST /v1/messages`（流式与非流式）
- **账号池**：LRU 轮询、失败自动切换、配额耗尽 12h 冷却、token 到期前 1h 自动刷新
- **余额**：按套餐拆分展示，定时刷新
- **倍率**：上游没有倍率接口，本代理从每次请求的 `usage.credit` 累计算出真实倍率
- **每日签到**：APScheduler 定时 +100 积分
- **出口代理**：可接 resin/gost 多国出口，直连 / 固定 / 轮换三种模式
- **WebUI**：池子、余额、模型、倍率、出口、对话调试、添加账号

## 上游实测要点（2026-08-13）

| 项 | 结论 |
|---|---|
| chat 端点 | `POST copilot.tencent.com/v2/chat/completions`，`azp=console` 的 token 可用 |
| 流式 | **只支持 `stream=true`**，`stream=false` 返回 `400 code:11101` |
| 模型列表 | 没有列表接口（21 条 models 路径全 404），只能候选名单并发探测 |
| 倍率 | 没有倍率接口，但 `usage.credit` 就是本次真实扣的积分 |
| 余额 | `POST /v2/billing/meter/get-user-resource`，注册后需等几秒异步发放 |
| 签到 | `POST /v2/billing/meter/daily-checkin`，每天 +100 |
| 手机号 | 仅中国大陆 +86，其他国家号码一律 400 |

实测可用模型 13 个（+ `default`/`auto` 两个别名）：
`kimi-k3` `kimi-k2.6` `kimi-k2.5` `deepseek-v4-pro` `deepseek-v4-flash` `deepseek-v3`
`deepseek-v3-2-volc` `deepseek-r1` `hunyuan-2.0-instruct` `glm-5.2` `glm-5.1` `glm-5.0` `minimax-m2.7`

## 添加账号

WebUI「添加账号」页填**你自己的手机号** → 服务端协议级请求短信 → 你把收到的验证码填进第二步 →
自动换 token、查余额、入池。也支持直接粘贴已有的 `access_token` 手动导入。

## 部署

```bash
cp .env.example .env      # 填 WB_API_KEY
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9188
```

systemd 单元见 `deploy/wb-pool.service`，一键安装见 `deploy/setup_165.sh`，
端到端验证脚本见 `deploy/verify_165.py`。

## 接入

```bash
curl $BASE/v1/chat/completions \
  -H "Authorization: Bearer $WB_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v3","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 边界

本项目**不含**接码平台批量注册，也**不含**池内账号互相邀请刷奖励。邀请页只做只读查询。
号源是使用者本人合法持有的账号。
