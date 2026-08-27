#!/usr/bin/env bash
# 把 wb-pool 装成 systemd 服务。任何 Linux 发行版通用，不写死路径。
#
#   sudo deploy/install-systemd.sh                    # 用默认值
#   sudo WB_PORT=9200 WB_USER=wbpool deploy/install-systemd.sh
#
# 做了什么：建 venv → 装依赖 → 生成 .env（如果还没有）→ 渲染 systemd 单元 → 起服务。
# 幂等：重复跑不会覆盖已有的 .env，也不会重置数据目录。
set -euo pipefail

# 仓库根 = 这个脚本的上一级，不假设你从哪个目录调用它
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/wb-pool.service"
UNIT_DST="/etc/systemd/system/wb-pool.service"

WB_PORT="${WB_PORT:-9188}"
WB_USER="${WB_USER:-wbpool}"
PYTHON="${PYTHON:-python3}"

if [[ $EUID -ne 0 ]]; then
  echo "需要 root（要写 /etc/systemd/system 并建用户）。用 sudo 再跑一次。" >&2
  exit 1
fi
[[ -f "$UNIT_SRC" ]] || { echo "找不到 $UNIT_SRC" >&2; exit 1; }

echo "── 仓库根: $ROOT"
echo "── 端口:   $WB_PORT"
echo "── 用户:   $WB_USER"

# 1) 服务账号。不用 root 跑一个对外的 HTTP 服务。
if ! id -u "$WB_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$WB_USER"
  echo "   建了系统用户 $WB_USER"
fi

# 2) venv + 依赖
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$ROOT/.venv"
  echo "   建了 venv"
fi
"$ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"
echo "   依赖装好了"

# 3) .env —— 已存在就绝不覆盖（里面是真凭据）
if [[ ! -f "$ROOT/.env" ]]; then
  gen_key() { head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 40; }
  {
    echo "# 由 deploy/install-systemd.sh 生成于 $(date -Is)"
    echo "WB_API_KEY=wb-$(gen_key)"
    echo "WB_ADMIN_KEY=wb-$(gen_key)"
    echo "WB_DATA_DIR=$ROOT/data"
    echo "WB_PROXY_MODE=off"
    echo "WB_TZ=${WB_TZ:-Asia/Shanghai}"
  } > "$ROOT/.env"
  chmod 600 "$ROOT/.env"
  echo "   生成了 .env（随机密钥）"
else
  echo "   .env 已存在，跳过（不覆盖你的凭据）"
fi
chown "$WB_USER:$WB_USER" "$ROOT/.env"

# 4) 数据目录归服务账号
mkdir -p "$ROOT/data"
chown -R "$WB_USER:$WB_USER" "$ROOT/data"

# 5) 渲染并安装 systemd 单元
sed -e "s|__WB_ROOT__|$ROOT|g" \
    -e "s|__WB_USER__|$WB_USER|g" \
    -e "s|__WB_PORT__|$WB_PORT|g" \
    "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"
systemctl daemon-reload
systemctl enable wb-pool >/dev/null 2>&1 || true
systemctl restart wb-pool
echo "   systemd 单元已装并启动"

# 6) 等健康检查真的通，而不是 sleep 完就宣布成功
for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$WB_PORT/api/health" >/dev/null 2>&1; then
    echo
    echo "启动成功: http://127.0.0.1:$WB_PORT"
    echo "面板首次登录 admin/admin —— 改密之前服务端会锁住其余所有接口。"
    echo "日志: journalctl -u wb-pool -f"
    exit 0
  fi
  sleep 0.5
done

echo
echo "服务起来了但健康检查没通，看日志：" >&2
systemctl --no-pager --lines=30 status wb-pool >&2 || true
exit 1
