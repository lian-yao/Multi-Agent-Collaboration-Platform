"""视觉能力评测集（成员 D）。

## 为什么需要它

多模态附件打通之后，我们证明的只是**通路通**（截图能让模型说出界面上的文字），
不是**能力可用**。两者是不同的事：模型可能确实收到了图，却把 5 个圆数成 4 个、
把红色的方块说成蓝色。通路问题改代码就能修，能力问题只能靠度量发现。

这个脚本把「能力可用」变成可回归的数字：一组**答案唯一、机器可判**的图片，
让真实模型逐张回答并打分。

## 判据为什么可信

- 图片由 Pillow 按**固定坐标**画出来，没有随机数、没有字体依赖（数字用七段数码管
  自绘）——同一份代码在任何机器上画出同一张图；
- 期望答案写在用例里，且只看**归一化后必须命中**的片段，不做语义评判；
- 走的是**真实链路**：`prepare_upload` 解析 → `AttachmentPayload` → `build_human_content`
  拼出 image_url 内容块 → 真实模型。不绕过附件层直接喂图片，否则测的是模型而不是我们的链路。

## 这不能替代什么

它不评「回答质量」（那需要人工或模型评审），只评「看不看得对」。数字类用例的期望串
理论上可能被无关内容命中（例如回答里凑巧出现同一个数字），所以它适合当**趋势指标**
与回归闸门，不适合当作精确指标。

## 环境前提：网关要求调用之间有间隔

本机网关（one-api 系）在**紧接着**上一次请求结束就发下一次时，必然在 ~1.4s 内返回
500 `do_request_failed`；隔 6 秒再发就成功。实测 A（成功）→B（立刻，失败）→C（隔 6s，
成功），**新建客户端同样失败**，所以不是连接复用而是网关侧节流。脚本用 `--gap` 隔开
用例并在失败后按倍数退避重试；报告里记的耗时**含这段等待**，不是模型响应时间。

用法：

    MACP_VISION_EVAL=1 uv run --with pillow python scripts/vision_eval.py --out doc/evals/vision.md
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WHITE = (255, 255, 255)
RED = (220, 60, 60)
BLUE = (60, 90, 220)
GREEN = (60, 170, 90)
DARK = (30, 30, 30)
CANVAS = (480, 320)

_SEGMENTS: dict[str, tuple[str, ...]] = {
    "0": ("a", "b", "c", "d", "e", "f"),
    "1": ("b", "c"),
    "2": ("a", "b", "g", "e", "d"),
    "3": ("a", "b", "c", "d", "g"),
    "4": ("b", "c", "f", "g"),
    "5": ("a", "c", "d", "f", "g"),
    "6": ("a", "c", "d", "e", "f", "g"),
    "7": ("a", "b", "c"),
    "8": ("a", "b", "c", "d", "e", "f", "g"),
    "9": ("a", "b", "c", "d", "f", "g"),
}


@dataclass(frozen=True)
class VisionCase:
    """一个评测用例：怎么画、问什么、期望命中哪些片段。"""

    name: str
    question: str
    expect: tuple[str, ...]
    draw: Callable[[Any, Any], None]
    note: str = ""


def _canvas():
    from PIL import Image

    return Image.new("RGB", CANVAS, WHITE)


def _circle(draw, center: tuple[int, int], radius: int, color: tuple[int, int, int]) -> None:
    draw.ellipse(
        [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
        fill=color,
    )


def _square(draw, top_left: tuple[int, int], size: int, color: tuple[int, int, int]) -> None:
    draw.rectangle(
        [top_left[0], top_left[1], top_left[0] + size, top_left[1] + size], fill=color
    )


def _triangle(draw, center: tuple[int, int], size: int, color: tuple[int, int, int]) -> None:
    half = size // 2
    draw.polygon(
        [
            (center[0], center[1] - half),
            (center[0] - half, center[1] + half),
            (center[0] + half, center[1] + half),
        ],
        fill=color,
    )


def _digit(draw, origin: tuple[int, int], char: str, *, height: int = 120, width: int = 70) -> None:
    """七段数码管画一个数字。

    自己画而不是用 `ImageFont.load_default()`：默认字体是位置无关的小点阵，
    放大后糊成一团，评测会变成"考字体渲染"而不是"考视觉"；而挂一个 TTF 依赖又让
    脚本换台机器就跑不起来。
    """

    x, y = origin
    thickness = max(6, height // 12)
    top, mid, bottom = y, y + height // 2, y + height
    left, right = x, x + width
    segments = {
        "a": [(left, top), (right, top)],
        "b": [(right, top), (right, mid)],
        "c": [(right, mid), (right, bottom)],
        "d": [(left, bottom), (right, bottom)],
        "e": [(left, mid), (left, bottom)],
        "f": [(left, top), (left, mid)],
        "g": [(left, mid), (right, mid)],
    }
    for name in _SEGMENTS.get(char, ()):
        (x1, y1), (x2, y2) = segments[name]
        draw.line([(x1, y1 - thickness // 2), (x2, y2 - thickness // 2)], fill=DARK, width=thickness)
        draw.line([(x1, y1 + thickness // 2), (x2, y2 + thickness // 2)], fill=DARK, width=thickness)


def _draw_digits(draw, text: str) -> None:
    spacing = 90
    start = (CANVAS[0] - spacing * len(text)) // 2 + 10
    for index, char in enumerate(text):
        _digit(draw, (start + index * spacing, 100), char)


# --------------------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------------------


def _draw_mixed_shapes(draw, red_circles: int, blue_squares: int) -> None:
    for index in range(red_circles):
        _circle(draw, (70 + index * 80, 90), 30, RED)
    for index in range(blue_squares):
        _square(draw, (50 + index * 70, 190), 56, BLUE)


def _draw_sizes(draw, _unused=None) -> None:
    _square(draw, (40, 130), 60, BLUE)
    _square(draw, (170, 100), 90, GREEN)
    _square(draw, (320, 60), 130, RED)


def _draw_three_shapes(draw, _unused=None) -> None:
    _circle(draw, (110, 160), 60, RED)
    _triangle(draw, (250, 160), 110, GREEN)
    _square(draw, (340, 105), 110, BLUE)


def _draw_bars(draw, _unused=None) -> None:
    draw.rectangle([60, 180, 160, 280], fill=BLUE)   # 左柱，矮
    draw.rectangle([280, 70, 380, 280], fill=RED)    # 右柱，高


def _draw_only_circles(draw, _unused=None) -> None:
    for index in range(4):
        _circle(draw, (80 + index * 100, 160), 45, BLUE)


def _draw_grid(draw, _unused=None) -> None:
    size = 80
    origin = (120, 40)
    for row in range(3):
        for column in range(3):
            x = origin[0] + column * size
            y = origin[1] + row * size
            draw.rectangle([x, y, x + size, y + size], outline=DARK, width=3)
    # 第 2 行第 3 列（从 1 数起）
    dot_x = origin[0] + 2 * size + size // 2
    dot_y = origin[1] + 1 * size + size // 2
    _circle(draw, (dot_x, dot_y), 22, RED)


CASES: tuple[VisionCase, ...] = (
    VisionCase(
        name="count_red_circles",
        question="图中红色圆形有几个？只回答数字。",
        expect=("5", "五"),
        draw=lambda draw, _: _draw_mixed_shapes(draw, red_circles=5, blue_squares=3),
        note="干扰项：同图有 3 个蓝色方块",
    ),
    VisionCase(
        name="count_blue_squares",
        question="图中蓝色方块有几个？只回答数字。",
        expect=("6", "六"),
        draw=lambda draw, _: _draw_mixed_shapes(draw, red_circles=2, blue_squares=6),
        note="计数对象与颜色/形状双重筛选",
    ),
    VisionCase(
        name="largest_square_color",
        question="图中最大的正方形是什么颜色？",
        expect=("红", "red"),
        draw=_draw_sizes,
        note="三档大小，最大的在最右",
    ),
    VisionCase(
        name="leftmost_shape",
        question="图中最左边的是什么形状？",
        expect=("圆", "circle"),
        draw=_draw_three_shapes,
        note="圆形/三角形/正方形，考位置与形状",
    ),
    VisionCase(
        name="digits_4729",
        question="图中显示的是哪几位数字？只回答数字。",
        expect=("4729",),
        draw=lambda draw, _: _draw_digits(draw, "4729"),
        note="七段数码管，考 OCR 能力",
    ),
    VisionCase(
        name="digits_8035",
        question="图中显示的是哪几位数字？只回答数字。",
        expect=("8035",),
        draw=lambda draw, _: _draw_digits(draw, "8035"),
        note="含 0 与 8，段数差异最大",
    ),
    VisionCase(
        name="taller_bar_side",
        question="图中哪一根柱子更高，左边还是右边？",
        expect=("右", "right"),
        draw=_draw_bars,
        note="左右高度差明显",
    ),
    VisionCase(
        name="no_triangle_present",
        question="图中有三角形吗？",
        expect=("没有", "无", "否", "no"),
        draw=_draw_only_circles,
        note="全部是圆形，考「不存在」的正确回答",
    ),
    VisionCase(
        name="count_red_circles_two",
        question="图中红色圆形有几个？只回答数字。",
        expect=("2", "二", "两"),
        draw=lambda draw, _: _draw_mixed_shapes(draw, red_circles=2, blue_squares=2),
        note="与第一例同题不同数，防止模型靠猜同一答案",
    ),
    VisionCase(
        name="grid_dot_row_column",
        question="图中红色圆点在第几行第几列？请按「第X行第Y列」回答。",
        expect=("2", "二"),
        draw=_draw_grid,
        note="3x3 网格，点在第二行第三列",
    ),
)

_NORMALIZE = re.compile(r"[\s，。、；：！？,.;:!?（）()\[\]「」『』\"'`]+")


def normalize(text: str) -> str:
    """归一化：去掉空白与中英文标点，英文转小写，便于做片段命中。"""

    return _NORMALIZE.sub("", (text or "").strip().lower())


def grade(case: VisionCase, answer: str) -> bool:
    normalized = normalize(answer)
    return any(normalize(item) in normalized for item in case.expect)


# --------------------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------------------


@dataclass
class CaseResult:
    case: VisionCase
    answer: str
    passed: bool
    error: str | None = None
    seconds: float = 0.0
    image_bytes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def build_image(case: VisionCase) -> bytes:
    from PIL import ImageDraw

    image = _canvas()
    case.draw(ImageDraw.Draw(image), None)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def run_case(
    case: VisionCase,
    llm: Any,
    *,
    keep_dir: Path | None = None,
    retries: int = 2,
    delay: float = 3.0,
) -> CaseResult:
    """跑一个用例；失败按 `retries` 重试。

    为什么要重试：这些请求是**连着的**，而网关在连续请求下会偶发 500（实测第一例
    25s 成功、紧接着的第二例 1.4s 就 500；隔开几秒单跑同一例又是成功的）。
    评测脚本把这种噪声记成"模型看不懂图"会得出错误结论，所以重试是必要的，
    但**重试次数有限**——一直重试会把真正的故障藏起来。
    """

    from app.attachments import build_human_content, prepare_upload, to_payload
    from langchain_core.messages import HumanMessage

    data = build_image(case)
    if keep_dir is not None:
        keep_dir.mkdir(parents=True, exist_ok=True)
        (keep_dir / f"{case.name}.png").write_bytes(data)

    # 走真实上传解析：分类、限额、mime 都在这里发生，不是把字节直接塞给模型。
    prepared = prepare_upload(f"{case.name}.png", data, "image/png")
    payload = to_payload(prepared)
    # 有图片时 `build_human_content` 返回 content block **列表**，它是
    # `HumanMessage(content=...)` 的入参而不是消息列表——直接把它交给 `invoke`
    # 会被当成「一串消息」解析，报的是 "Message dict must contain 'role'"。
    #
    # 刻意**不加**角色 system prompt：这里测的是「看不看得对」。套上 collector 的
    # 角色指令后，"只回答数字" 这种要求会与角色人设互相打架，答错时无法区分是
    # 看不清还是没听话。角色级的指令遵循由冒烟与真机回归覆盖，不混进这份指标。
    messages = [HumanMessage(content=build_human_content(case.question, [payload]))]

    started = time.perf_counter()
    last_error = ""
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(delay * attempt)
        try:
            response = llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - 评测脚本要把失败如实记进报告
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        answer = _answer_text(response)
        return CaseResult(
            case=case,
            answer=answer,
            passed=grade(case, answer),
            seconds=round(time.perf_counter() - started, 2),
            image_bytes=len(data),
            extra={"attempts": attempt + 1},
        )

    return CaseResult(
        case=case,
        answer="",
        passed=False,
        error=f"（重试 {retries} 次后仍失败）{last_error}",
        seconds=round(time.perf_counter() - started, 2),
        image_bytes=len(data),
        extra={"attempts": retries + 1},
    )


def _answer_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


def report(results: list[CaseResult], *, model: str, base_url: str, gap: float) -> str:
    passed = sum(1 for item in results if item.passed)
    total = len(results)
    lines = [
        "# 视觉能力评测",
        "",
        f"- 运行时间：{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        f"- 模型：`{model}`（base_url=`{base_url}`）",
        f"- 结果：**{passed}/{total}** 通过",
        "",
        "| 用例 | 问题 | 期望命中 | 模型回答 | 判定 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        answer = item.answer.replace("\n", " ").strip()
        if item.error:
            answer = f"（失败）{item.error}"
        if len(answer) > 60:
            answer = answer[:60] + "…"
        verdict = "通过" if item.passed else "未通过"
        lines.append(
            f"| `{item.case.name}` | {item.case.question} | "
            f"{' / '.join(item.case.expect)} | {answer} | {verdict} | {item.seconds}s |"
        )
    notes = [item for item in results if item.case.note]
    if notes:
        lines += ["", "## 用例说明", ""]
        lines += [f"- `{item.case.name}`：{item.case.note}" for item in notes]
    lines += [
        "",
        "> 判据是「归一化后命中期望片段」，它评的是**看不看得对**，不评回答质量。",
        "> 数字类用例的期望串理论上可能被无关内容命中，因此这份结果适合当趋势指标与",
        "> 回归闸门，不适合当成精确准确率。",
        "",
        "> **关于重试**：本机网关在**紧接着**上一次请求结束就发下一次时，必然在 ~1.4s 内",
        "> 返回 500 `do_request_failed`；隔 6 秒再发就成功（实测 A→B 立刻→C 隔 6s 为",
        "> 「成功 / 失败 / 成功」，新建客户端同样失败，因此不是连接复用）。所以脚本用",
        f"> `--gap`（默认 5s，本次 {gap:.1f}s）隔开用例、失败后按倍数退避重试。",
        "> **上面的耗时里含这次等待**，它不是模型的响应时间。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="视觉能力评测（真实模型）")
    parser.add_argument("--out", type=Path, default=None, help="把 Markdown 报告写到该路径")
    parser.add_argument("--keep-images", type=Path, default=None, help="把评测图片留到该目录")
    parser.add_argument("--only", default="", help="只跑名字含该子串的用例")
    parser.add_argument("--min-accuracy", type=float, default=0.7, help="低于该通过率时退出码非 0")
    parser.add_argument("--retry", dest="retries", type=int, default=2, help="单个用例的失败重试次数")
    parser.add_argument("--gap", type=float, default=5.0, help="用例之间的间隔秒数（网关连续请求会偶发 500）")
    parser.add_argument(
        "--base-url",
        default="",
        help=(
            "覆盖模型 base_url。默认把 host.docker.internal 换成 localhost——"
            "脚本跑在宿主机上，容器内才认得 host.docker.internal。"
        ),
    )
    args = parser.parse_args(argv)

    if os.environ.get("MACP_VISION_EVAL") != "1":
        print(
            "未设置 MACP_VISION_EVAL=1：这个脚本会调用真实模型并产生费用，"
            "因此默认不跑。设置后重试。",
            file=sys.stderr,
        )
        return 2

    from app.config import get_settings
    from app.core.provider_config import resolve_provider_settings
    from app.orchestration.llm import build_chat_model

    settings = resolve_provider_settings(get_settings())
    base_url = args.base_url or settings.openai_base_url
    if base_url and "host.docker.internal" in base_url:
        base_url = base_url.replace("host.docker.internal", "localhost")
    if base_url != settings.openai_base_url:
        settings = settings.model_copy(update={"openai_base_url": base_url})

    llm = build_chat_model(settings)
    model_name = settings.openai_model or settings.ollama_model

    cases = [case for case in CASES if args.only in case.name] if args.only else list(CASES)
    if not cases:
        print(f"没有匹配 --only={args.only} 的用例", file=sys.stderr)
        return 2

    results: list[CaseResult] = []
    for index, case in enumerate(cases):
        if index:
            time.sleep(args.gap)
        result = run_case(
            case, llm, keep_dir=args.keep_images, retries=args.retries, delay=args.gap
        )
        results.append(result)
        verdict = "PASS" if result.passed else "FAIL"
        print(f"[{verdict}] {case.name}: {result.answer[:70]!r}", flush=True)

    text = report(results, model=model_name, base_url=base_url or "", gap=args.gap)
    print()
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"报告已写入 {args.out}")

    passed = sum(1 for item in results if item.passed)
    return 0 if passed >= args.min_accuracy * len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
