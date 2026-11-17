"""ページ画像から名刺の外枠（薄い縁の線）を見つける。DESIGN.md §7.8。

スキャン画像では名刺の地と背景がどちらも白で色の差が無いが、名刺の縁に
1 画素ほどの薄い線（影）が出る。その長い直線だけを拾って、つながった線の
外接矩形のうち名刺の大きさのものを名刺の枠とする。

外部ライブラリ（OpenCV・numpy）を使わず、グレースケール画素列に対する
bytes.translate と正規表現（どちらも C 実装）で処理する。fitz は import しない
（画素列は pdf_reader が渡す。I1）。
"""
from __future__ import annotations

import re

from app.models import EdgeMap, Rect4, SplitCfg

_CELL = 4        # 線分をまとめる格子の大きさ（px）。線の太さ・ずれ・小さな途切れを吸収する
_RUN_RE = re.compile(rb"\x01+")


def _table(threshold: int) -> bytes:
    return bytes(1 if v < threshold else 0 for v in range(256))


def _row_segments(
    samples: bytes, width: int, height: int, threshold: int, min_len: int, gap: int,
    run_min: int | None = None,
) -> list[tuple[int, int, int]]:
    """各行の (行, 開始 x, 終了 x)。暗い画素が min_len 以上続く区間（gap 未満の途切れはつなぐ）。

    つなぐ前の区間は run_min（既定 min_len の 1/3）以上のものだけを使う。文字の画は短いので
    ここで落ち、文字の行が 1 本の長い線に化けない。
    """
    mask = samples.translate(_table(threshold))
    run_re = re.compile(rb"\x01{%d,}" % max(1, run_min if run_min is not None else min_len // 3))
    out: list[tuple[int, int, int]] = []
    for y in range(height):
        row = mask[y * width:(y + 1) * width]
        cur: list[int] | None = None
        for m in run_re.finditer(row):
            a, b = m.span()
            if cur is not None and a - cur[1] < gap:
                cur[1] = b
                continue
            if cur is not None and cur[1] - cur[0] >= min_len:
                out.append((y, cur[0], cur[1]))
            cur = [a, b]
        if cur is not None and cur[1] - cur[0] >= min_len:
            out.append((y, cur[0], cur[1]))
    return out


def _components(
    horiz: list[tuple[int, int, int]], vert: list[tuple[int, int, int]], width: int, height: int,
) -> list[tuple[int, int, int, int]]:
    """線分を _CELL px の格子に描き、8 近傍でつながった格子のまとまりの外接矩形（px）を返す。

    horiz は (行, x0, x1)、vert は (列, y0, y1)。格子に描いてから行ごとの連続区間で
    つなぐので、塗りつぶし（線分が大量に出る）でも線分の数に比例した時間で済む。
    """
    gw, gh = width // _CELL + 1, height // _CELL + 1
    grid = bytearray(gw * gh)
    for y, x0, x1 in horiz:
        off = (y // _CELL) * gw
        a, b = x0 // _CELL, (x1 - 1) // _CELL + 1
        grid[off + a:off + b] = b"\x01" * (b - a)
    for x, y0, y1 in vert:
        c = x // _CELL
        for r in range(y0 // _CELL, (y1 - 1) // _CELL + 1):
            grid[r * gw + c] = 1

    parent: list[int] = []
    boxes: list[list[int]] = []   # 区間ごとの [x0, y0, x1, y1]（格子単位）

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    prev: list[tuple[int, int, int]] = []   # 直前の行の (a, b, 区間番号)
    for r in range(gh):
        row = bytes(grid[r * gw:(r + 1) * gw])
        cur: list[tuple[int, int, int]] = []
        for m in _RUN_RE.finditer(row):
            a, b = m.span()
            k = len(parent)
            parent.append(k)
            boxes.append([a, r, b, r + 1])
            for pa, pb, pk in prev:
                if pa <= b and a <= pb:   # 8 近傍（斜めの接触も含む）
                    ra, rb = find(k), find(pk)
                    if ra != rb:
                        parent[ra] = rb
            cur.append((a, b, k))
        prev = cur

    merged: dict[int, list[int]] = {}
    for i, bx in enumerate(boxes):
        c = merged.setdefault(find(i), list(bx))
        c[0] = min(c[0], bx[0])
        c[1] = min(c[1], bx[1])
        c[2] = max(c[2], bx[2])
        c[3] = max(c[3], bx[3])
    return [(c[0] * _CELL, c[1] * _CELL, c[2] * _CELL, c[3] * _CELL) for c in merged.values()]


def detect_edges(
    gray: bytes, width: int, height: int,
    gray_rot: bytes, rot_width: int, rot_height: int,
    dpi: float, cfg: SplitCfg,
) -> EdgeMap:
    """枠の当てはめ（§7.9）に使う、短めの直線の切れ端（fit_edge_min_mm 以上）。"""
    mm_px = dpi / 25.4
    min_len = int(cfg.fit_edge_min_mm * mm_px)
    gap = int(1.5 * mm_px)
    run_min = max(2, int(2.0 * mm_px))
    horiz = _row_segments(gray, width, height, cfg.detect_threshold, min_len, gap, run_min)
    vert = [
        (yr, rot_width - x1, rot_width - x0)
        for yr, x0, x1 in _row_segments(gray_rot, rot_width, rot_height, cfg.detect_threshold, min_len, gap, run_min)
    ]
    return EdgeMap(dpi=dpi, horiz=tuple(horiz), vert=tuple(vert))


def detect_card_boxes(
    gray: bytes, width: int, height: int,
    gray_rot: bytes, rot_width: int, rot_height: int,
    dpi: float, cfg: SplitCfg,
) -> tuple[Rect4, ...]:
    """名刺の枠（表示空間 pt）。gray は表示空間のグレースケール画素列、gray_rot は
    それを 90 度回した画素列（元の列が行になる。元 x = 回転後の行、元 y = 回転後の幅 - 1 - 列）。
    """
    mm_px = dpi / 25.4
    min_len = int(cfg.detect_line_mm * mm_px)
    gap = int(cfg.detect_gap_mm * mm_px)
    horiz = _row_segments(gray, width, height, cfg.detect_threshold, min_len, gap)
    # 回した画像の行 yr は元の列、列 x は元の行 (rot_width - 1 - x)
    vert = [
        (yr, rot_width - x1, rot_width - x0)
        for yr, x0, x1 in _row_segments(gray_rot, rot_width, rot_height, cfg.detect_threshold, min_len, gap)
    ]
    scale = 72.0 / dpi
    found: list[Rect4] = []
    for x0, y0, x1, y1 in _components(horiz, vert, width, height):
        short, long_ = sorted(((x1 - x0) / mm_px, (y1 - y0) / mm_px))
        if (cfg.card_short_mm_min <= short <= cfg.card_short_mm_max
                and cfg.card_long_mm_min <= long_ <= cfg.card_long_mm_max):
            found.append((x0 * scale, y0 * scale, x1 * scale, y1 * scale))
    # 入れ子は外側だけ残す（名刺の中の飾り枠を名刺と取り違えない）
    found.sort(key=lambda r: -(r[2] - r[0]) * (r[3] - r[1]))
    kept: list[Rect4] = []
    for r in found:
        if not any(r[0] >= k[0] - 3 and r[1] >= k[1] - 3 and r[2] <= k[2] + 3 and r[3] <= k[3] + 3
                   for k in kept):
            kept.append(r)
    kept.sort(key=lambda r: (r[1], r[0]))
    return tuple(kept)
