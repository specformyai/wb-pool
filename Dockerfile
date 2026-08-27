# wb-pool —— WorkBuddy / CodeBuddy 账号池反向代理
#
#   docker build -t wb-pool .
#   docker run -d --name wb-pool -p 127.0.0.1:9188:9188 \
#     --env-file .env -v "$PWD/data:/app/data" wb-pool
#
# 或者直接 docker compose up -d（见 docker-compose.yml）。

FROM python:3.12-slim

# tzdata：签到判重和「今日到账」按本地日算，容器默认 UTC 会让日期整体错一天。
#         装了它 WB_TZ 才有意义（见 .env.example）。
# curl：  HEALTHCHECK 要用。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：改代码不用重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

# 数据目录。**必须挂卷出来** —— 账号 token、API Key、运行时配置、账本全在这，
# 容器重建就没了。
ENV WB_DATA_DIR=/app/data
ENV WB_TZ=Asia/Shanghai

# 不用 root 跑。data 目录要归它，否则挂卷进来写不进去。
RUN useradd -r -u 10001 -d /app wbpool \
 && mkdir -p /app/data \
 && chown -R wbpool:wbpool /app
USER wbpool

VOLUME ["/app/data"]
EXPOSE 9188

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:9188/api/health || exit 1

# 容器里必须绑 0.0.0.0，否则宿主的 -p 转不进来。
# ⚠️ 这等于把服务开到容器网络上：发布端口时请只绑 127.0.0.1，前面放
#    nginx/caddy 做 TLS。面板首次是 admin/admin，改密前服务端会锁住除
#    登录/改密以外的所有接口，但别指望这一道就够 —— WB_API_KEY 也要设。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9188"]
