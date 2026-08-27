# wb-pool

WorkBuddy / CodeBuddy 账号池反向代理。把账号的 token 池化，统一暴露成 OpenAI 与 Anthropic 兼容端点，
带轮询调度、token 自动续期、余额与倍率看板、每日签到、历史对话还原，以及一个自带的 WebUI。

## 能力

- **OpenAI 兼容**：`GET /v1/models`、`POST /v1/chat/completions`（流式与非流式都支持）
- **Anthropic 兼容**：`POST /v1/messages`（流式与非流式）
- **账号池**：三种调度策略、失败自动切换、配额耗尽 12h 冷却、token 到期前 1h 自动刷新
- **账号可用性**：连续 auth 失败计数出候选池；账号级故障不消耗正常重试预算
- **余额**：按套餐拆分展示，定时刷新；请求前对低额度账号做实时校验
- **包级到期感知**：逐包算到期状态，把「即将作废」和「已作废」的额度单独拆出来
- **倍率**：上游没有倍率接口，本代理从每次请求的 `usage.credit` 累计算出真实倍率
- **每日签到**：APScheduler 定时；奖励实际到账额从包体反推（上游把它发成赠送包，签到接口看不到）
- **历史对话**：从上游用量流水还原账号上的历史输入，按会话切分成聊天记录
- **出口代理**：可接 resin/gost 多国出口，直连 / 固定 / 轮换三种模式
- **WebUI**：概览、账号池、调用监控、历史对话、API Key、模型与倍率、注册中心、邀请返利、代理池、对话调试

## 调度策略

`GET/POST /api/pool/rotation`，三种（面板可切换）：

| key | 名称 | 行为 |
|---|---|---|
| `lru` | 轮询 | 取最久未使用的账号，请求摊到全池（默认） |
| `drain` | 耗尽优先 | 复用当前账号直到额度打光再换下一个 |
| `expiry` | 到期优先 | 先用最快到期的账号，避免签到额度作废 |

## 上游实测要点

| 项 | 结论 |
|---|---|
| chat 端点 | `POST copilot.tencent.com/v2/chat/completions` |
| 流式 | **只支持 `stream=true`**，`stream=false` 返回 `400 code:11101` |
| 模型列表 | `GET /console/enterprises/personal/models`，用 `agents` 里 `name=="cli"` 那项过滤全量表 |
| 倍率 | 模型清单里的 `credits` 字段（形如 `x0.79 credits`）；`hy3` 是 `x0.00` 免费 |
| 余额 | `POST /v2/billing/meter/get-user-resource`，返回 `Accounts[]` 资源包数组 |
| 用量流水 | `POST www.workbuddy.cn/billing/meter/get-user-request-usage`，逐笔请求，**只认自然月，跨月静默返 0** |
| 签到 | `POST /v2/billing/meter/daily-checkin`；奖励常在 00:03~00:21 先发成赠送包，签到接口只回「已签到」 |
| token 续期 | `POST tencent.sso.codebuddy.cn/v2/plugin/auth/token/refresh`，头 `X-Refresh-Token` + `X-Auth-Refresh-Source: plugin` |
| 手机号 | 仅中国大陆 +86，其他国家号码一律 400 |

模型清单是动态的，走上面那个 console 接口自动同步（正缓存 1h → 失败 5min 负缓存 → 静态兜底表），
`/v1/models` 永不返回空数组。上游偶尔开放了但 console 还没登记的 id，走 `upstream_sync.UNLISTED_MODELS` 并入。

### 资源包的时间字段

签到积分是**一个个独立的资源包**，各自到期。判到期时间要注意：

| 字段 | 类型 | 含义 |
|---|---|---|
| `ExpiredTime` | str | **只有终态包（`Status=3`）有值** = 实际消亡时刻，不是计划到期 |
| `DeductionEndTime` | int(ms) | 计划抵扣截止；体验版是远期占位（2034 年） |
| `CycleEndTime` | str | 周期结束 |

计划到期 = `min(DeductionEndTime, CycleEndTime)`。只看 `DeductionEndTime` 会把体验版算成 2034 年到期。
`remain` 求和前要 `max(0, ...)` 钳位，上游对超额消耗会回负数。

## 历史对话

上游没有会话/历史接口。唯一能拿到对话正文的是用量流水的 `input` 字段：

- 逐月分页抓取（接口不按传入范围过滤，只认当月），落盘缓存到 `data/history/<手机号>.json`
- 按时间间隔推断会话边界（上游没有 `conversationId`），间隔可调
- **只有用户侧输入，上游不返回助手回复**，所以是单侧气泡
- 被封号（聊天接口恒回 `11140`）同样能拉——那个 403 只挡聊天接口

## 添加账号

WebUI「注册中心」填**你自己的手机号** → 服务端协议级请求短信 → 你把收到的验证码填进第二步 →
自动换 token、查余额、入池。也支持直接粘贴已有的 `access_token` 手动导入。

## 部署

```bash
cp .env.example .env      # 填 WB_API_KEY
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9188
```

systemd 单元见 `deploy/wb-pool.service`，一键安装见 `deploy/setup_165.sh`，
端到端验证脚本见 `deploy/verify_165.py`。

WebUI 走账号密码 + `wb_session` cookie（默认 admin/admin，首次登录会强提示改密），
反代 key 在面板里生成、可多把并独立启停。

前端改动后必须重算 `web/index.html` 里 importmap 的 `?v=<sha1>` ——
ES module 有独立于 HTTP 缓存的模块图缓存，哈希不变浏览器就一直用旧文件。

## 接入

```bash
curl $BASE/v1/chat/completions \
  -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"model":"hy3","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 边界

本项目**不含**接码平台批量注册，也**不含**池内账号互相邀请刷奖励。邀请页只做只读查询。
号源是使用者本人合法持有的账号。
