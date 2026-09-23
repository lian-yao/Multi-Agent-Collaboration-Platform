#!/usr/bin/env python
"""把前端样式表里的颜色字面量令牌化，并为暗色主题生成取值。

为什么要有这个脚本（背景见 `doc/decisions/040-dark-mode-token-layer.md`）：

- 四个样式表里散着 700 多个颜色字面量、430 多种颜色。暗色主题不是"再加一份样式"，
  而是把这些字面量收进一层令牌，然后按主题给令牌取不同的值。
- **浅色取值按构造等于原字面量**（令牌名里就带着原色值，`preview`/`verify` 都会核对），
  所以浅色主题一个像素都不该变；暗色取值是"同一色相上重新取明度"的结果。
- 角色的区分是必须的：同一个 `#fff` 当背景（卡片底）和当文字（强调按钮上的字）
  在暗色里该去两个方向。因此令牌键是 **(角色, 色值)**，角色从声明属性推出来。

用法（在仓库根目录执行）：

```bash
python frontend/rendercheck/theme-tokens.py preview     # 打印映射表 + 对比度审计
python frontend/rendercheck/theme-tokens.py apply       # 首次：写 theme.css 并就地替换颜色字面量
python frontend/rendercheck/theme-tokens.py regenerate  # 之后：按新规则重算 theme.css 的暗色取值
python frontend/rendercheck/theme-tokens.py verify      # 令牌闭合 + 浅色恒等 + 对比度门禁
```

`apply` 只跑一次：跑过之后样式表里就没有颜色字面量了，再跑一次会被拒绝（否则会把
theme.css 冲成空的）。调色板要改就改上面的映射函数，再跑 `regenerate`。
"""

from __future__ import annotations

import colorsys
import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
SRC = os.path.normpath(SRC)

STYLESHEETS = [
    "styles.css",
    "workspace/workspace.css",
    "config/config.css",
    "records/records.css",
    "components/inline-confirm.css",
    "components/page-tabs.css",
]

THEME_FILE = os.path.join(SRC, "theme.css")

INK_PROPS = {
    "color", "fill", "stroke", "-webkit-text-fill-color", "caret-color", "accent-color",
}
BG_PROPS = {"background", "background-color", "background-image"}
BD_PROPS = {
    "border", "border-color", "border-top", "border-right", "border-bottom", "border-left",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "outline", "outline-color", "border-block", "border-inline", "column-rule",
    "text-decoration-color",
}
SHADOW_PROPS = {"box-shadow", "text-shadow"}

ROLES = ("bg", "ink", "bd", "shadow")

HEX = re.compile(r"#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})")
FUNC = re.compile(r"\b(rgba?)\(([^)]*)\)")

HOUSE_HUE = 202.0
"""中性色的底色相。浅色主题的中性灰本来就带一点青蓝（`#f8fafb` 是 hsl(200,27%,98%)），
暗色沿用同一色相，免得"冷色 UI 里嵌进一块纯灰"。纯 `#fff`/`#000` 没有色相可继承，
一律注入这个值。"""


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def hex_to_rgb(literal: str) -> tuple[float, float, float, float]:
    text = literal.lstrip("#")
    if len(text) in (3, 4):
        text = "".join(ch * 2 for ch in text)
    alpha = 1.0
    if len(text) == 8:
        alpha = int(text[6:8], 16) / 255
    return (
        float(int(text[0:2], 16)),
        float(int(text[2:4], 16)),
        float(int(text[4:6], 16)),
        alpha,
    )


def norm_hex(rgb: tuple[float, float, float, float]) -> str:
    r, g, b, alpha = rgb
    if alpha < 0.999:
        return "#%02x%02x%02x%02x" % (round(r), round(g), round(b), round(alpha * 255))
    return "#%02x%02x%02x" % (round(r), round(g), round(b))


def to_hsl(rgb: tuple[float, float, float, float]) -> tuple[float, float, float]:
    r, g, b = (channel / 255 for channel in rgb[:3])
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    return hue * 360, sat, light


def from_hsl(hue: float, sat: float, light: float, alpha: float = 1.0) -> tuple:
    r, g, b = colorsys.hls_to_rgb(
        (hue % 360) / 360, clamp(light, 0, 1), clamp(sat, 0, 1)
    )
    return r * 255, g * 255, b * 255, alpha


def luminance(rgb: tuple[float, float, float, float]) -> float:
    channels = []
    for value in rgb[:3]:
        c = value / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: tuple, b: tuple) -> float:
    la, lb = luminance(a), luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------------------------
# 暗色取值
# --------------------------------------------------------------------------------------


def dark_bg(rgb):
    """背景：**与浅色同向**——浅色里越白（越"升起"）在暗色里越亮。

    方向不能反：反了之后卡片会比页面底色更暗，卡片看着像挖出来的一个洞。
    浅色主题里本来就是深色的块（深色按钮、侧栏运行卡片）单独走一支，保持深底。
    """

    hue, sat, light = to_hsl(rgb)
    if sat < 0.05:
        hue = HOUSE_HUE
    if light < 0.62:
        return from_hsl(
            hue, clamp(sat, 0.10, 0.34), clamp(light + 0.04, 0.13, 0.30), rgb[3]
        )
    return from_hsl(
        hue,
        clamp(max(sat * 0.16, 0.08), 0.08, 0.28),
        clamp(0.11 + (light - 0.93) * 0.95, 0.105, 0.20),
        rgb[3],
    )


def dark_ink(rgb):
    """文字：**与浅色反向**——浅色里越深（越重要）在暗色里越亮。

    本来就是浅色的字（强调按钮上的白字、深色卡片上的浅字）原样保留，
    否则会把"深底白字"翻成"深底深字"。
    """

    hue, sat, light = to_hsl(rgb)
    if sat < 0.20:
        if light >= 0.78:
            return from_hsl(hue, clamp(sat * 0.8, 0.0, 0.12), light, rgb[3])
        return from_hsl(
            hue,
            clamp(sat * 0.85, 0.03, 0.13),
            clamp(1.02 - light * 0.62, 0.62, 0.96),
            rgb[3],
        )
    if light >= 0.80:
        return from_hsl(hue, clamp(sat * 0.9, 0.0, 0.60), light, rgb[3])
    return from_hsl(hue, clamp(sat, 0.0, 0.58), clamp(0.52 + light * 0.32, 0.56, 0.84), rgb[3])


def dark_bd(rgb):
    """描边：与浅色反向——浅色里越淡（越贴底色）在暗色里也越贴底色。"""

    hue, sat, light = to_hsl(rgb)
    if sat < 0.05:
        hue = HOUSE_HUE
    if light < 0.62:
        return from_hsl(
            hue, clamp(sat, 0.10, 0.34), clamp(light + 0.10, 0.20, 0.40), rgb[3]
        )
    return from_hsl(
        hue,
        clamp(sat * 0.5 + 0.08, 0.08, 0.30),
        clamp(0.19 + (1 - light) * 0.85, 0.16, 0.36),
        rgb[3],
    )


def dark_shadow(rgb):
    """投影：暗色底上原样的淡影几乎看不见，加深并提高不透明度。

    不透明的颜色出现在 `box-shadow` 里说明那是**色环**（focus ring / 强调描边），
    不是柔影——按影处理会把它的色相抹成黑色，那是丢信息。这类走文字那一支。
    """

    if rgb[3] >= 0.9:
        return dark_ink(rgb)
    return (10.0, 14.0, 18.0, clamp(rgb[3] * 2.4, 0.10, 0.72))


TRANSFORM = {"bg": dark_bg, "ink": dark_ink, "bd": dark_bd, "shadow": dark_shadow}


def custom_prop_role(name: str) -> str | None:
    """自定义属性按**名字**定角色：`--tint-bg` 是底、`--tint-fg` 是字。

    没有名字线索就返回 None，让调用方报出来人工定，而不是猜一个。
    """

    lowered = name.lower()
    if "shadow" in lowered:
        return "shadow"
    if any(word in lowered for word in ("bg", "surface", "fill", "backdrop")):
        return "bg"
    if any(word in lowered for word in ("line", "border", "ring", "rule", "divider")):
        return "bd"
    if any(word in lowered for word in ("ink", "fg", "text", "color", "accent", "tint")):
        return "ink"
    return None


def role_of(prop: str) -> str | None:
    if prop.startswith("--"):
        return custom_prop_role(prop)
    if prop in INK_PROPS:
        return "ink"
    if prop in BG_PROPS or prop.startswith("background"):
        return "bg"
    if prop in BD_PROPS or prop.startswith("border") or prop.startswith("outline"):
        return "bd"
    if prop in SHADOW_PROPS:
        return "shadow"
    return None


# --------------------------------------------------------------------------------------
# 扫描与替换
# --------------------------------------------------------------------------------------


def scan(text: str) -> list[dict]:
    """逐字符遍历，跳过注释与字符串，给每个颜色字面量定位它所属的声明属性。"""

    hits: list[dict] = []
    index = 0
    decl_start = 0
    state = "code"
    quote = ""
    length = len(text)
    while index < length:
        char = text[index]
        if state == "code":
            if char == "/" and text.startswith("/*", index):
                state = "comment"
                index += 2
                continue
            if char in "\"'":
                state, quote = "string", char
                index += 1
                continue
            if char in "{};":
                decl_start = index + 1
                index += 1
                continue
            match = HEX.match(text, index)
            if match and (index == 0 or not _is_ident(text[index - 1])):
                hits.append(_hit(text, decl_start, index, match.group(0), match.end()))
                index = match.end()
                continue
            match = FUNC.match(text, index)
            if match and (index == 0 or not _is_ident(text[index - 1])):
                hits.append(_hit(text, decl_start, index, match.group(0), match.end()))
                index = match.end()
                continue
            index += 1
        elif state == "comment":
            if text.startswith("*/", index):
                state = "code"
                index += 2
                continue
            index += 1
        else:
            if char == quote:
                state = "code"
            index += 1
    return hits


def _is_ident(char: str) -> bool:
    return char.isalnum() or char in "_-&"


def _hit(text: str, decl_start: int, start: int, literal: str, end: int) -> dict:
    prefix = text[decl_start:start]
    colon = prefix.find(":")
    prop = prefix[:colon].strip().lower() if colon >= 0 else ""
    return {"start": start, "end": end, "literal": literal, "prop": prop}


def parse_color(literal: str) -> tuple:
    if literal.startswith("#"):
        return hex_to_rgb(literal)
    inner = literal[literal.find("(") + 1 : -1]
    parts = [part.strip() for part in inner.split(",")]
    values = [float(part) for part in parts[:3]]
    alpha = float(parts[3]) if len(parts) > 3 else 1.0
    return values[0], values[1], values[2], alpha


def token_name(role: str, key: str) -> str:
    return "--t-%s-%s" % (role, key.lstrip("#"))


def collect() -> tuple[dict, list[str]]:
    """扫描全部样式表，返回 {(角色, 色值): 次数} 与未识别属性的清单。"""

    tokens: dict[tuple[str, str], int] = {}
    unknown: list[str] = []
    for rel in STYLESHEETS:
        text = read(os.path.join(SRC, rel))
        for hit in scan(text):
            role = role_of(hit["prop"])
            if role is None:
                if hit["prop"] not in unknown:
                    unknown.append(hit["prop"])
                continue
            key = (role, norm_hex(parse_color(hit["literal"])).lstrip("#"))
            tokens[key] = tokens.get(key, 0) + 1
    return tokens, unknown


def read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def eol_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def tokens_from_theme() -> dict:
    """从 theme.css 的浅色块读回 (角色, 色值) 集合。

    令牌化是一次性动作，之后调色板只会反复重算——这时样式表里已经没有颜色字面量了，
    再扫一遍会得到空集合，把 theme.css 冲成空的。所以**重算口径以 theme.css 为准**。
    """

    if not os.path.exists(THEME_FILE):
        return {}
    theme = read(THEME_FILE)
    head = ':root[data-theme="dark"] {'
    light_block = theme.split(head, 1)[0] if head in theme else theme
    found: dict[tuple[str, str], int] = {}
    for match in re.finditer(r"(--t-([a-z]+)-([0-9a-f]{3,8})):(#[0-9a-f]{3,8});", light_block):
        found[(match.group(2), match.group(3))] = 1
    return found


# --------------------------------------------------------------------------------------
# theme.css
# --------------------------------------------------------------------------------------

HEADER = """/* theme.css — 颜色令牌层（暗色模式，ADR-040）
 *
 * 由 `frontend/rendercheck/theme-tokens.py` 生成，不要手改颜色值：
 * 改令牌值请改脚本里的暗色映射规则再重跑，否则下一次重跑会把手改冲掉。
 *
 * 两句约定：
 * 1. `:root` 的浅色取值**逐字等于**令牌名里的色值（`--t-bg-f8fafb` 就是 `#f8fafb`），
 *    因此令牌化本身不改变浅色主题的任何一像素——这是可核对的构造性保证。
 * 2. `:root[data-theme="dark"]` 的取值由"同色相上重新取明度"得到：背景与浅色同向
 *    （越白越亮）、文字与描边反向（越深越亮）。角色必须区分，同一个 `#fff`
 *    当卡片底和当白字在暗色里去两个方向。
 *
 * 主题由 `src/theme/theme.ts` 写在 `<html data-theme>` 上，取值只有 `light`/`dark`：
 * `index.html` 的首屏脚本先按「偏好 → 系统」解析一次落成属性（避免闪白），
 * 之后由 React 侧的状态与 `matchMedia` 变化继续跟。「跟随系统」在 DOM 上没有独立取值，
 * 它是「localStorage 里没有键」这一状态。
 */
"""


def render_theme(tokens: dict) -> str:
    lines = [HEADER, ":root {", "  color-scheme:light;"]
    for role in ROLES:
        for (token_role, key), _count in sorted(tokens.items()):
            if token_role == role:
                lines.append("  %s:%s;" % (token_name(role, key), "#" + key))
    lines.append("}")
    lines.append("")
    lines.append(':root[data-theme="dark"] {')
    lines.append("  color-scheme:dark;")
    for role in ROLES:
        for (token_role, key), _count in sorted(tokens.items()):
            if token_role == role:
                dark = norm_hex(TRANSFORM[role](hex_to_rgb("#" + key)))
                lines.append("  %s:%s;" % (token_name(role, key), dark))
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def apply_tokens(tokens: dict) -> list[tuple[str, int, int]]:
    report = []
    for rel in STYLESHEETS:
        path = os.path.join(SRC, rel)
        text = read(path)
        eol = eol_of(text)
        hits = scan(text)
        pieces: list[str] = []
        cursor = 0
        replaced = 0
        for hit in hits:
            role = role_of(hit["prop"])
            if role is None:
                continue
            key = norm_hex(parse_color(hit["literal"])).lstrip("#")
            pieces.append(text[cursor : hit["start"]])
            pieces.append("var(%s)" % token_name(role, key))
            cursor = hit["end"]
            replaced += 1
        pieces.append(text[cursor:])
        new_text = "".join(pieces)
        if new_text != text:
            write(path, new_text)
        report.append((rel, replaced, len(text)))
    # 新文件按仓库惯例写 CRLF
    theme_text = render_theme(tokens).replace("\n", "\r\n")
    write(THEME_FILE, theme_text)
    return report


# --------------------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------------------


def cmd_preview() -> int:
    tokens, unknown = collect()
    total = sum(tokens.values())
    print("颜色字面量 %d 个，(角色, 色值) 令牌 %d 个" % (total, len(tokens)))
    if unknown:
        print("!! 未识别属性（不会被令牌化，需人工确认）: %s" % ", ".join(unknown))
    order = {role: index for index, role in enumerate(ROLES)}
    items = sorted(tokens.items(), key=lambda kv: (order[kv[0][0]], -kv[1]))
    # 正文对比度以「最底层的页面底色」为参照：卡片底色与它只差一点，
    # 用页面底色算是最保守的口径。
    page_dark = dark_bg(hex_to_rgb("#f7f9fa"))
    print("\n页面底色 #f7f9fa 的暗色取值：%s" % norm_hex(page_dark))
    print("%-7s %-10s %-5s %-10s %-26s %s"
          % ("角色", "浅色", "次数", "暗色", "浅色 HSL", "暗色下对页面的对比度"))
    for (role, key), count in items:
        light = hex_to_rgb("#" + key)
        dark = TRANSFORM[role](light)
        hue, sat, lgt = to_hsl(light)
        note = ""
        if role == "ink":
            ratio = contrast(dark, page_dark)
            note = "%.2f:1%s" % (ratio, "" if ratio >= 4.5 else "  ← 低于 4.5")
        elif role in ("bg", "bd"):
            note = "亮度差 %.3f" % (luminance(dark) - luminance(page_dark))
        print("%-7s #%-9s %-5d %-10s h=%6.1f s=%.2f l=%.2f  %s"
              % (role, key, count, norm_hex(dark), hue, sat, lgt, note))
    return 0


def cmd_apply() -> int:
    tokens, unknown = collect()
    if unknown:
        print("!! 未识别属性，先处理再落盘: %s" % ", ".join(unknown))
        return 2
    if not tokens:
        print("!! 样式表里已经没有颜色字面量——已经令牌化过了。"
              "要重算暗色取值请用 `regenerate`。")
        return 2
    report = apply_tokens(tokens)
    for rel, replaced, size in report:
        print("%-30s 替换 %4d 处（原 %d 字符）" % (rel, replaced, size))
    print("theme.css 写出：%d 个令牌（浅 + 暗各一份）" % len(tokens))
    return 0


def cmd_regenerate() -> int:
    """按当前映射规则重算 theme.css 的暗色取值（令牌集合不变）。"""

    tokens = tokens_from_theme()
    if not tokens:
        print("!! theme.css 里读不到令牌，先跑一次 `apply`")
        return 2
    text = render_theme(tokens).replace("\n", "\r\n")
    write(THEME_FILE, text)
    print("theme.css 重算完成：%d 个令牌" % len(tokens))
    return 0


def cmd_verify() -> int:
    tokens = tokens_from_theme()
    problems: list[str] = []
    theme = read(THEME_FILE)
    dark_head = ':root[data-theme="dark"] {'
    if dark_head not in theme:
        print("!! theme.css 里找不到暗色块")
        return 1
    light_block, dark_block = theme.split(dark_head, 1)

    for rel in STYLESHEETS:
        text = read(os.path.join(SRC, rel))
        for name in sorted(set(re.findall(r"var\((--t-[a-z0-9-]+)\)", text))):
            if name not in light_block:
                problems.append("%s 引用了未在浅色块声明的 %s" % (rel, name))
            if name not in dark_block:
                problems.append("%s 引用了未在暗色块声明的 %s" % (rel, name))
    # 令牌名里的色值必须等于浅色取值（浅色恒等的构造性核对）
    for (role, key), _count in tokens.items():
        name = token_name(role, key)
        if ("%s:#%s;" % (name, key)) not in light_block:
            problems.append("浅色取值与令牌名不符: %s != #%s" % (name, key))

    worst: list[tuple[float, str]] = []
    for rel in STYLESHEETS:
        text = read(os.path.join(SRC, rel))
        for rule in re.findall(r"\{([^{}]*)\}", text):
            bg = re.search(r"(?<!-)background(?:-color)?:\s*var\((--t-bg-[a-z0-9]+)\)", rule)
            fg = re.search(r"(?<![\w-])color:\s*var\((--t-ink-[a-z0-9]+)\)", rule)
            if not (bg and fg):
                continue
            pairs = []
            for block in (light_block, dark_block):
                bg_value = _value_of(block, bg.group(1))
                fg_value = _value_of(block, fg.group(1))
                if not (bg_value and fg_value):
                    pairs = []
                    break
                pairs.append(contrast(hex_to_rgb(bg_value), hex_to_rgb(fg_value)))
            if len(pairs) != 2:
                continue
            light_ratio, dark_ratio = pairs
            # 只报「浅色本来就够、暗色掉下来」的组合：两边都低的说明这条规则的两条声明
            # 本来就属于不同变体（比如激活态与默认态写在一起），不是暗色引入的问题。
            if light_ratio >= 3.0 and dark_ratio < 3.0:
                worst.append(
                    (dark_ratio, "%s %s on %s：浅色 %.2f:1 → 暗色 %.2f:1"
                     % (rel, fg.group(1), bg.group(1), light_ratio, dark_ratio))
                )

    if problems:
        print("!! 令牌闭合检查失败 %d 条：" % len(problems))
        for item in problems[:25]:
            print("   -", item)
    else:
        print("令牌闭合：全部 var(--t-*) 在浅色/暗色两块都声明，且浅色取值与令牌名一致")
    worst.sort()
    if worst:
        print("暗色下掉出 3:1 的组合 %d 条（浅色合格、暗色不合格，需人工判断）：" % len(worst))
        for ratio, text in worst[:20]:
            print("   - %.2f:1  %s" % (ratio, text))
    else:
        print("暗色对比度：浅色合格的同规则前景/背景组合在暗色下仍 ≥ 3:1")
    return 1 if problems else 0


def _value_of(block: str, name: str) -> str | None:
    match = re.search(re.escape(name) + r":(#[0-9a-f]{3,8});", block)
    return match.group(1) if match else None


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "preview"
    if command == "preview":
        return cmd_preview()
    if command == "apply":
        return cmd_apply()
    if command == "regenerate":
        return cmd_regenerate()
    if command == "verify":
        return cmd_verify()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
