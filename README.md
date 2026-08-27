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

## 快速开始

三条命令，不需要代理池、不需要接码平台，起来就能用：

```bash
cp .env.example .env                 # 至少改 WB_API_KEY / WB_ADMIN_KEY
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9188
```

打开 http://127.0.0.1:9188 —— 会跳到 `/login`，默认账号 **admin / admin**。

**首次登录必须改密码**：没改之前服务端会锁住除登录/改密以外的所有接口
（403 + `need_password_change`）。这是防止有人把带默认密码的面板直接挂到公网。
忘记密码就删掉 `data/webauth.json` 重启，会重新按 `WB_ADMIN_USER` / `WB_ADMIN_PASS` 建账号。

改完密码之后，剩下的配置都在 WebUI「设置 → 运行时配置」里填，改完立即生效不用重启：

| 想做什么 | 去哪 |
|---|---|
| 加账号 | 「注册中心」填自己的手机号，或直接粘贴已有 `access_token` |
| 生成反代 key | 「API Key」页，可多把、可单独启停 |
| 填接码平台 token | 「设置 → 接码平台」（也可以用 `WB_UOOMSG_TOKEN`） |
| 配出口代理 | 「代理池」页手动加端口，或点「自动探测」扫一段 |
| 改签到时间 / 余额刷新间隔 | 「设置 → 运行时配置」 |

配置优先级是 **面板设置 > 环境变量 > 代码默认值**，面板改的值存在 `data/settings.json`。
所以 `.env` 只需要填最少的两把密钥，别的都能事后在界面上调。

## 部署

### Docker

```bash
cp .env.example .env
docker compose up -d
```

数据挂在 `./data`，容器重建不丢。镜像里不含任何真实数据（`.dockerignore` 挡掉了 `data/` 和 `.env`）。

### systemd

```bash
sudo ./deploy/install-systemd.sh                       # 默认装到当前目录、当前用户、9188
sudo WB_ROOT=/opt/wb-pool WB_USER=wbpool WB_PORT=9188 \
     ./deploy/install-systemd.sh                       # 或者显式指定
```

脚本会建 venv、装依赖、生成 `.env`（密钥随机）、替换 `deploy/wb-pool.service`
里的占位符再装进 systemd。**那个 `.service` 是模板，别直接 cp** —— 里面的
`__WB_ROOT__` / `__WB_USER__` / `__WB_PORT__` 需要替换。

部署完跑一遍端到端验证（会打真实上游，需要池里有号）：

```bash
.venv/bin/python deploy/verify_deploy.py
```

### 对外暴露

服务默认只绑 `127.0.0.1`。要对外提供服务就在前面放 nginx / caddy 终结 TLS，
**反代必须关掉响应缓冲**，否则流式输出会被攒成一坨最后一起吐出来：

```nginx
location / {
    proxy_pass http://127.0.0.1:9188;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;   # 让 Cookie 的 Secure 判断生效
    proxy_buffering off;                          # SSE 必须
    proxy_read_timeout 1800s;                     # 思考模型单次可能跑十几分钟
}
```

不要直接把 9188 开到公网 —— 管理面板和账号 token 都在这个端口上。

## 开发

```bash
# 全部离线测试（假上游，不打网络、不烧接码余额）
for f in tests/test_*.py; do PYTHONPATH=. .venv/bin/python "$f"; done
node tests/test_chat_utils.js

# 冷启动冒烟：全新实例什么都没配也能起来
PYTHONPATH=. .venv/bin/python tests/smoke_cold_start.py
```

浏览器端的渲染验证（需要一个 headless Chrome 开着 CDP 端口）：

```bash
WB_VERIFY_PORT=8931 .venv/bin/python tests/serve_verify.py &     # 隔离实例
WB_VERIFY_BASE=http://127.0.0.1:8931 .venv/bin/python tests/verify_login_ui.py
WB_VERIFY_BASE=http://127.0.0.1:8931 .venv/bin/python tests/verify_pages_ui.py
```

⚠️ **改了 `web/*.js` 或 `web/*.css` 必须重算资源哈希**：

```bash
python scripts/bump_static_version.py
```

ES module 有独立于 HTTP 缓存的模块图缓存，`?v=<sha1>` 不变浏览器就一直用旧文件
—— 你本地测得通，用户刷新一百次还是旧的。CI 会把「哈希过期」判成硬失败。

## 接入

```bash
curl $BASE/v1/chat/completions \
  -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"model":"hy3","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 边界

本项目**不含**接码平台批量注册，也**不含**池内账号互相邀请刷奖励。邀请页只做只读查询。
号源是使用者本人合法持有的账号。
