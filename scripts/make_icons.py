#!/usr/bin/env python3
"""
从品牌原图生成全套站点图标。

用法:
    python3 scripts/make_icons.py <源图> [输出目录=web]

为什么需要这个脚本（别直接把原图丢进 web/）：

原图是**铺满画布的圆角方形**图标，四个圆角之外是不透明的近白色三角区
（实测：中间行彩色像素 0..W-1 全宽，而第 0 行只有 x=125..W-125 有色）。
直接当 favicon / 侧栏 logo 用，深色界面上会出现四个白色尖角。

⚠️ 不能用「从四角做连通域抠白底」那套通用做法：本图左上角有三道白色
弧线装饰，和角落白是**连通**的，连通域 BFS 会吃掉 29.4% 的像素、把图案
本身削掉。只能用几何圆角遮罩。

产物：
    favicon.ico            16/32/48/64 多尺寸，带 alpha
    icon-192.png           带 alpha（侧栏 brand-mark 和登录页 <img> 用这个）
    icon-512.png           带 alpha
    apple-touch-icon.png   180px **不透明**（iOS 对带 alpha 的图会合成黑底）

apple-touch 合成到白底而非粉底：原图角落本来就是白的，视觉与源一致；
且 iOS 自己的圆角遮罩（超椭圆，约 22%）比本图的 9.8% 更狠，白角根本露不出来。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

SS = 4  # 遮罩超采样倍数，用来拿到抗锯齿的圆角边缘


def measure_corner_radius(im: Image.Image, sat_thr: int = 25) -> int:
    """量出左上圆角的半径：第 0 行第一个「有彩度」像素的 x 就是 r。

    圆角矩形铺满画布时，y=0 那一行的填充从 x=r 开始到 W-r 结束。
    用彩度（max-min）而不是亮度判定，因为背景白和图案里的白都很亮，
    只有粉色底板有彩度。
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    def first_colored_in_row(y: int) -> int | None:
        for x in range(w):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) >= sat_thr:
                return x
        return None

    def first_colored_in_col(x: int) -> int | None:
        for y in range(h):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) >= sat_thr:
                return y
        return None

    rx = first_colored_in_row(0)
    ry = first_colored_in_col(0)
    cands = [v for v in (rx, ry) if v is not None]
    if not cands:
        # 图形没有圆角（或量不出来）——退化成不遮罩
        return 0
    return int(round(sum(cands) / len(cands)))


def rounded_mask(size: int, radius: int) -> Image.Image:
    """生成抗锯齿的圆角矩形 alpha 遮罩。"""
    big = size * SS
    m = Image.new("L", (big, big), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=max(0, radius * SS), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def build(src: Path, size: int, radius_ratio: float) -> Image.Image:
    """输出 size×size 的 RGBA，四角按 radius_ratio 透明。"""
    im = Image.open(src).convert("RGB")
    # 原图 1262×1280 不是正方（比例 0.986）。直接 resize 成正方比中心裁切好：
    # 裁切会削掉圆角顶端，而 1.4% 的形变肉眼不可见。
    sq = im.resize((size, size), Image.LANCZOS)
    out = sq.convert("RGBA")
    out.putalpha(rounded_mask(size, int(round(size * radius_ratio))))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    outdir = Path(sys.argv[2] if len(sys.argv) > 2 else "web")
    if not src.exists():
        print(f"源图不存在: {src}")
        return 1
    outdir.mkdir(parents=True, exist_ok=True)

    orig = Image.open(src)
    r = measure_corner_radius(orig)
    ratio = r / orig.size[0]
    print(f"源图 {orig.size[0]}×{orig.size[1]}  量得圆角 r={r}px  "
          f"= 边长的 {ratio * 100:.2f}%")

    stamp = time.strftime("%Y%m%dT%H%M%S")
    targets = {
        "icon-192.png": 192,
        "icon-512.png": 512,
        "apple-touch-icon.png": 180,
    }

    # 先备份旧图标，生产上没有 git 可回滚
    for name in list(targets) + ["favicon.ico"]:
        p = outdir / name
        if p.exists():
            bak = p.with_name(f"{p.name}.bak-brand-{stamp}")
            shutil.copy2(p, bak)
            print(f"  备份 {p.name} -> {bak.name}")

    for name, size in targets.items():
        img = build(src, size, ratio)
        if name == "apple-touch-icon.png":
            # iOS 不吃 alpha：合成到白底（原图角落本来就是白的）
            flat = Image.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, (0, 0), img)
            flat.save(outdir / name, "PNG", optimize=True)
        else:
            img.save(outdir / name, "PNG", optimize=True)
        p = outdir / name
        print(f"  写出 {name}  {size}×{size}  {p.stat().st_size} bytes")

    # favicon.ico：多尺寸 + alpha，标签栏在深浅底上都干净
    ico = build(src, 256, ratio)
    ico_path = outdir / "favicon.ico"
    ico.save(ico_path, "ICO",
             sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)])
    print(f"  写出 favicon.ico  多尺寸  {ico_path.stat().st_size} bytes")

    # 自证：角落必须透明，中心必须不透明
    chk = build(src, 192, ratio)
    a = chk.split()[-1]
    corner = a.getpixel((1, 1))
    center = a.getpixel((96, 96))
    edge_mid = a.getpixel((96, 1))
    print(f"\n自证 alpha: 角落={corner}(应为0)  中心={center}(应为255)  "
          f"上边中点={edge_mid}(应为255)")
    ok = corner == 0 and center == 255 and edge_mid == 255
    print("PASS" if ok else "FAIL —— 遮罩没按预期生效")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
