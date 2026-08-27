#!/usr/bin/env python3
"""app/settings.py 运行时配置层测试。

不打网络、不碰生产数据：全部在临时目录里跑。
直接执行即可（线上 venv 没装 pytest）：

    .venv/bin/python tests/test_settings.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.settings import SPEC, Settings, coerce  # noqa: E402

PASS = 0
FAIL = 0


def ck(cond: bool, name: str, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  [{extra}]")


def ck_raises(fn, name: str) -> None:
    try:
        fn()
    except ValueError:
        ck(True, name)
        return
    except Exception as exc:  # noqa: BLE001
        ck(False, name, f"抛的是 {type(exc).__name__} 不是 ValueError")
        return
    ck(False, name, "没抛异常")


def clear_env() -> None:
    for spec in SPEC.values():
        env = spec.get("env")
        if env:
            os.environ.pop(env, None)


def main() -> int:
    print("=== 1) 出口表解析：三种写法都要支持 ===")
    ck(coerce("proxy_exits", "61001:RO,61002:US") == {61001: "RO", 61002: "US"},
       "字符串 port:label 形式")
    ck(coerce("proxy_exits", "61001,61002") == {61001: "", 61002: ""},
       "字符串仅端口（标签留空）")
    ck(coerce("proxy_exits", {"61001": "RO", 61002: "US"}) == {61001: "RO", 61002: "US"},
       "dict 形式（键可以是 str 或 int）")
    ck(coerce("proxy_exits", [{"port": 61001, "label": "RO"}, 61002]) == {61001: "RO", 61002: ""},
       "list 形式（对象与裸端口混排）")
    ck(coerce("proxy_exits", "") == {}, "空字符串 → 空表")
    # 脏值必须被丢弃而不是让服务起不来
    ck(coerce("proxy_exits", "abc,99999,-1,61001:HK") == {61001: "HK"},
       "非法端口被丢弃，合法项保留")
    ck(coerce("proxy_exits", [{"port": 60001, "cc": "US-s1"}]) == {60001: "US-s1"},
       "list 里 cc 也认（兼容 /api/proxy 的字段名）")

    print("\n=== 2) 类型与范围校验 ===")
    ck(coerce("balance_interval_min", "10") == 10, "int 接受字符串数字")
    ck_raises(lambda: coerce("balance_interval_min", "abc"), "int 拒绝非数字")
    ck_raises(lambda: coerce("balance_interval_min", 0), "int 拒绝低于 min")
    ck_raises(lambda: coerce("balance_interval_min", 99999), "int 拒绝高于 max")
    ck(coerce("proxy_mode", "rotate") == "rotate", "枚举接受合法值")
    ck_raises(lambda: coerce("proxy_mode", "bogus"), "枚举拒绝非法值")
    ck_raises(lambda: coerce("nope", 1), "未知键报错")

    print("\n=== 3) 优先级：运行时 > env > 默认 ===")
    clear_env()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "settings.json"
        s = Settings(p)
        ck(s.get("proxy_mode") == "off", "默认值生效（proxy_mode=off）",
           s.get("proxy_mode"))
        ck(s.source_of("proxy_mode") == "default", "来源标记 default")
        ck(s.get("proxy_exits") == {}, "出口表默认为空（不内置任何私有拓扑）",
           s.get("proxy_exits"))

        os.environ["WB_PROXY_MODE"] = "rotate"
        ck(s.get("proxy_mode") == "rotate", "env 覆盖默认值")
        ck(s.source_of("proxy_mode") == "env", "来源标记 env")

        s.set_many({"proxy_mode": "fixed"})
        ck(s.get("proxy_mode") == "fixed", "运行时覆盖 env")
        ck(s.source_of("proxy_mode") == "runtime", "来源标记 runtime")

        # 关键语义：前端改过之后，改 env 不该再影响它
        os.environ["WB_PROXY_MODE"] = "off"
        ck(s.get("proxy_mode") == "fixed", "已被面板接管后 env 不再生效")

        s.reset(["proxy_mode"])
        ck(s.get("proxy_mode") == "off", "reset 后回落到 env")
        clear_env()
        ck(s.get("proxy_mode") == "off", "env 清掉后回落到默认")

    print("\n=== 4) 落盘与重载 ===")
    clear_env()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "settings.json"
        s = Settings(p)
        s.set_many({"proxy_exits": "61001:RO,60001:US-s1",
                    "uoomsg_token": "tok-abcdef123456",
                    "balance_interval_min": 25})
        ck(p.exists(), "配置文件已创建")
        mode = oct(p.stat().st_mode & 0o777)
        ck(mode == "0o600", "文件权限 0600（含 token）", mode)

        s2 = Settings(p)  # 全新实例，模拟重启
        ck(s2.get("proxy_exits") == {61001: "RO", 60001: "US-s1"},
           "重载后出口表还原（int 键没被 json 转成 str）", s2.get("proxy_exits"))
        ck(s2.get("balance_interval_min") == 25, "重载后 int 类型正确",
           type(s2.get("balance_interval_min")).__name__)
        ck(s2.get("uoomsg_token") == "tok-abcdef123456", "重载后 token 还原")

        # 未知键与脏值不能让加载崩
        p.write_text(json.dumps({"values": {"proxy_mode": "bogus",
                                            "ghost_key": 1,
                                            "balance_interval_min": 25}}),
                     encoding="utf-8")
        s3 = Settings(p)
        ck(s3.get("proxy_mode") == "off", "脏枚举值被丢弃，回落默认")
        ck(s3.get("balance_interval_min") == 25, "同文件里的合法值仍保留")

        p.write_text("{ this is not json", encoding="utf-8")
        s4 = Settings(p)
        ck(s4.get("proxy_mode") == "off", "损坏的 json 不抛异常")

    print("\n=== 5) 敏感值不出网 ===")
    clear_env()
    with tempfile.TemporaryDirectory() as td:
        s = Settings(Path(td) / "settings.json")
        s.set_many({"uoomsg_token": "tok-abcdef123456"})
        pub = s.public_view()
        blob = json.dumps(pub, ensure_ascii=False)
        ck("tok-abcdef123456" not in blob, "public_view 不含 token 明文", blob[:120])
        ck(pub["uoomsg_token"]["set"] is True, "public_view 标记已设置")
        ck(pub["uoomsg_token"]["hint"] == "…3456", "public_view 只给末 4 位",
           pub["uoomsg_token"]["hint"])
        ck(isinstance(pub["proxy_exits"], list) and pub["proxy_exits"] == [],
           "出口表以数组形式给前端")
        s.set_many({"proxy_exits": "61001:RO"})
        ck(s.public_view()["proxy_exits"] == [{"port": 61001, "label": "RO"}],
           "出口表数组元素形状 {port,label}")

    print("\n=== 6) 批量写入的原子性 ===")
    clear_env()
    with tempfile.TemporaryDirectory() as td:
        s = Settings(Path(td) / "settings.json")
        try:
            s.set_many({"balance_interval_min": 20, "proxy_mode": "bogus"})
        except ValueError:
            pass
        ck(s.source_of("balance_interval_min") == "default",
           "整批拒绝：合法项也不该被写入", s.source_of("balance_interval_min"))

    print("\n=== 7) 变更回调（热生效的基础）===")
    clear_env()
    with tempfile.TemporaryDirectory() as td:
        s = Settings(Path(td) / "settings.json")
        seen: list[dict] = []
        s.on_change(lambda ch: seen.append(ch))
        s.set_many({"proxy_mode": "rotate"})
        ck(len(seen) == 1 and seen[0].get("proxy_mode") == "rotate",
           "写入触发回调并带上变更内容", seen)

        def boom(_ch):
            raise RuntimeError("listener exploded")

        s.on_change(boom)
        try:
            s.set_many({"proxy_mode": "off"})
            ck(True, "监听器抛异常不影响写入")
        except Exception as exc:  # noqa: BLE001
            ck(False, "监听器抛异常不影响写入", exc)
        ck(s.get("proxy_mode") == "off", "写入结果仍然正确")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
