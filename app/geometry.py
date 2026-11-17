"""矩形演算・行グループ化。DESIGN.md §19.1。

すべて表示空間の Rect4 = (x0, y0, x1, y1) を前提にする（I2）。
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from app.models import Rect4


def width(r: Rect4) -> float:
    return r[2] - r[0]


def height(r: Rect4) -> float:
    return r[3] - r[1]


def area(r: Rect4) -> float:
    return width(r) * height(r)


def center(r: Rect4) -> tuple[float, float]:
    return ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0)


def contains_point(r: Rect4, x: float, y: float) -> bool:
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def intersection(a: Rect4, b: Rect4) -> Rect4 | None:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0, y0, x1, y1)


def union(rects: Sequence[Rect4]) -> Rect4:
    if not rects:
        raise ValueError("union には 1 件以上の矩形が必要")
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)
    return (x0, y0, x1, y1)


def overlap_ratio_1d(a0: float, a1: float, b0: float, b1: float) -> float:
    """重なり / 短い方の長さ。重なりなし・長さ 0 は 0.0。"""
    overlap = min(a1, b1) - max(a0, b0)
    if overlap <= 0:
        return 0.0
    shorter = min(a1 - a0, b1 - b0)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def distance_point_rect(x: float, y: float, r: Rect4) -> float:
    dx = max(r[0] - x, 0.0, x - r[2])
    dy = max(r[1] - y, 0.0, y - r[3])
    return math.hypot(dx, dy)


def group_rows(rects: Sequence[Rect4], overlap_min: float) -> list[list[int]]:
    """行は上から、行内は x0 順の添字で返す。

    (y0, x0) で並べて順に見て、既存の行のうち y 方向の重なり ÷ 低い方の高さ
    が最大かつ overlap_min 以上の行に入れる。無ければ新しい行を作る。
    行の帯はその行の最初の要素の y 範囲で固定する（§7.7）。
    """
    order = sorted(range(len(rects)), key=lambda i: (rects[i][1], rects[i][0]))
    rows: list[list[int]] = []
    bands: list[tuple[float, float]] = []
    for i in order:
        r = rects[i]
        best_row = -1
        best_ratio = 0.0
        for row_idx, (b0, b1) in enumerate(bands):
            ratio = overlap_ratio_1d(r[1], r[3], b0, b1)
            if ratio > best_ratio:
                best_ratio = ratio
                best_row = row_idx
        if best_row >= 0 and best_ratio >= overlap_min:
            rows[best_row].append(i)
        else:
            rows.append([i])
            bands.append((r[1], r[3]))
    for row in rows:
        row.sort(key=lambda i: rects[i][0])
    return rows
