"""文字 (Glyph) -> 行 (TextLine) の再構成。DESIGN.md §8・§19.1。

コアの座標はすべて表示空間（I2）。ここでは PDF を扱わないので fitz は import しない（I1）。
"""
from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import replace

from app import geometry
from app.models import Glyph, LayoutCfg, Rect4, TextLine
from app.textnorm import normalize


def _is_space_glyph(g: Glyph) -> bool:
    return g.ch.isspace()


def _median(values: Sequence[float], default: float = 0.0) -> float:
    if not values:
        return default
    return statistics.median(values)


def _bbox_union(glyphs: Sequence[Glyph]) -> Rect4:
    return geometry.union([g.bbox for g in glyphs])


def _group_vertical_lines(glyphs: Sequence[Glyph]) -> list[list[Glyph]]:
    """line_key ごとにまとめる（§8 手順 1）。グループ内は呼び出し側で seq 順に並べる。"""
    groups: dict[tuple[int, int], list[Glyph]] = {}
    order: list[tuple[int, int]] = []
    for g in glyphs:
        if g.line_key not in groups:
            groups[g.line_key] = []
            order.append(g.line_key)
        groups[g.line_key].append(g)
    return [groups[key] for key in order]


def _build_vertical_line(members: Sequence[Glyph], old_kanji: Mapping[str, str]) -> TextLine:
    ordered = sorted(members, key=lambda g: g.seq)
    raw = "".join(g.ch for g in ordered)
    text = normalize(raw, old_kanji)
    bbox = _bbox_union(ordered)
    size = _median([g.size for g in ordered])
    gaps = tuple(0.0 for _ in raw)  # 空白補完なし（§8 手順 1）
    return TextLine(text=text, bbox=bbox, size=size, vertical=True, gaps=gaps, row=0, col=0)


def _group_horizontal_lines(glyphs: Sequence[Glyph], overlap_min: float) -> list[list[Glyph]]:
    """(中心y, x0, seq) で並べ、y 帯の重なりで行へ振り分ける（§8 手順 2）。

    行の帯は追加のたびに (y0 の中央値, y1 の中央値) で更新する。
    """
    ordered = sorted(glyphs, key=lambda g: (geometry.center(g.bbox)[1], g.bbox[0], g.seq))
    lines: list[list[Glyph]] = []
    bands: list[tuple[float, float]] = []
    for g in ordered:
        y0, y1 = g.bbox[1], g.bbox[3]
        best_idx = -1
        best_ratio = 0.0
        for idx, (b0, b1) in enumerate(bands):
            ratio = geometry.overlap_ratio_1d(y0, y1, b0, b1)
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = idx
        if best_idx >= 0 and best_ratio >= overlap_min:
            lines[best_idx].append(g)
            members = lines[best_idx]
            bands[best_idx] = (
                statistics.median(m.bbox[1] for m in members),
                statistics.median(m.bbox[3] for m in members),
            )
        else:
            lines.append([g])
            bands.append((y0, y1))
    return lines


def _has_space_between(
    space_glyphs: Sequence[Glyph], a: Glyph, b: Glyph, overlap_min: float,
) -> bool:
    """a と b の間（x 方向）に中心が入る空白グリフがあるか（※設計の穴。下記報告参照）。

    DESIGN.md §8 手順 3 は「a と b の間に空白グリフの中心がある」とだけ定義し、
    どの空白グリフ集合・どの y 判定を使うかは明示していない。ここでは
    (1) ページ内の空白文字グリフ全体を候補にし、(2) 中心 x が a.x1 と b.x0 の
    間にあり、(3) a・b の y 範囲の和と overlap_min 以上重なるものを採用する。
    """
    x_lo = a.bbox[2]
    x_hi = b.bbox[0]
    if x_hi <= x_lo:
        return False
    y_lo = min(a.bbox[1], b.bbox[1])
    y_hi = max(a.bbox[3], b.bbox[3])
    for sg in space_glyphs:
        cx, cy = geometry.center(sg.bbox)
        if not (x_lo < cx < x_hi):
            continue
        if geometry.overlap_ratio_1d(sg.bbox[1], sg.bbox[3], y_lo, y_hi) >= overlap_min:
            return True
    return False


def _finalize_segment(
    glyphs: Sequence[Glyph], raw_chars: Sequence[str], gap_values: Sequence[float],
    old_kanji: Mapping[str, str],
) -> TextLine:
    raw = "".join(raw_chars)
    text = normalize(raw, old_kanji)
    bbox = _bbox_union(glyphs)
    size = _median([g.size for g in glyphs])
    return TextLine(
        text=text, bbox=bbox, size=size, vertical=False,
        gaps=tuple(gap_values), row=0, col=0,
    )


def _build_horizontal_segments(
    members: Sequence[Glyph], space_glyphs: Sequence[Glyph], cfg: LayoutCfg,
    old_kanji: Mapping[str, str],
) -> list[TextLine]:
    """1 つの行グループを (x0, seq) で並べ、ギャップに応じて空白挿入／行分割する（§8 手順 3・4）。"""
    ordered = sorted(members, key=lambda g: (g.bbox[0], g.seq))
    h = _median([g.bbox[3] - g.bbox[1] for g in ordered], default=1.0)
    seg_gap_t = cfg.segment_gap_ratio * h
    space_gap_t = cfg.space_gap_ratio * h

    results: list[TextLine] = []
    seg_glyphs: list[Glyph] = [ordered[0]]
    raw_chars: list[str] = [ordered[0].ch]
    gap_values: list[float] = [0.0]

    for prev, cur in zip(ordered, ordered[1:]):
        gap = cur.bbox[0] - prev.bbox[2]
        if gap >= seg_gap_t:
            results.append(_finalize_segment(seg_glyphs, raw_chars, gap_values, old_kanji))
            seg_glyphs = [cur]
            raw_chars = [cur.ch]
            gap_values = [0.0]
            continue
        insert_space = gap >= space_gap_t or _has_space_between(
            space_glyphs, prev, cur, cfg.line_overlap_min
        )
        if insert_space:
            raw_chars.append(" ")
            gap_values.append(gap)
        raw_chars.append(cur.ch)
        gap_values.append(0.0)
        seg_glyphs.append(cur)

    results.append(_finalize_segment(seg_glyphs, raw_chars, gap_values, old_kanji))
    return results


def build_lines(
    glyphs: Sequence[Glyph], cfg: LayoutCfg, old_kanji: Mapping[str, str],
) -> list[TextLine]:
    """文字 -> 行 の再構成（DESIGN.md §8）。読み順（row, col 昇順）で返す。"""
    glyph_list = list(glyphs)
    vertical_glyphs = [g for g in glyph_list if g.vertical]
    horizontal_glyphs = [g for g in glyph_list if not g.vertical]
    space_glyphs = [g for g in horizontal_glyphs if _is_space_glyph(g)]
    content_glyphs = [g for g in horizontal_glyphs if not _is_space_glyph(g)]

    lines: list[TextLine] = []

    for members in _group_vertical_lines(vertical_glyphs):
        lines.append(_build_vertical_line(members, old_kanji))

    for members in _group_horizontal_lines(content_glyphs, cfg.line_overlap_min):
        lines.extend(_build_horizontal_segments(members, space_glyphs, cfg, old_kanji))

    if not lines:
        return []

    # §8 手順 5: geometry.group_rows で row を振り、row 内を x0 順にして col を振る。
    # group_rows の overlap_min は LayoutCfg に専用の項目が無いため line_overlap_min を
    # 再利用する（※設計の穴。下記報告参照）。
    rows = geometry.group_rows([ln.bbox for ln in lines], cfg.line_overlap_min)
    result: list[TextLine] = []
    for row_idx, indices in enumerate(rows):
        for col_idx, i in enumerate(indices):
            result.append(replace(lines[i], row=row_idx, col=col_idx))
    return result
