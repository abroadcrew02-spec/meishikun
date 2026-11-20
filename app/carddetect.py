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

from collections.abc import Sequence

from app import geometry
from app.models import EdgeMap, Rect4, SplitCfg

_CELL = 4        # 線分をまとめる格子の大きさ（px）。線の太さ・ずれ・小さな途切れを吸収する
_SAME_CARD_OVERLAP = 0.5    # 1 つのしきい値の中で、これ以上重なる枠は同じ名刺（大きい方を残す）
# 後のしきい値の枠が、既にある枠とこれ以上重なるなら足さない。同じ名刺が、しきい値が違うと縁の位置が数 mm
# ずれて見つかる（影を含むかどうか）ので、1 つのしきい値の中より緩く「同じ名刺」とみなす
_KNOWN_CARD_OVERLAP = 0.3
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
    dpi: float, cfg: SplitCfg, threshold: int | None = None,
) -> EdgeMap:
    """枠の当てはめ（§7.9）に使う、短めの直線の切れ端（fit_edge_min_mm 以上）。

    threshold を省くと detect_threshold で拾う。
    """
    if threshold is None:
        threshold = cfg.detect_threshold
    mm_px = dpi / 25.4
    min_len = int(cfg.fit_edge_min_mm * mm_px)
    gap = int(1.5 * mm_px)
    run_min = max(2, int(2.0 * mm_px))
    horiz = _row_segments(gray, width, height, threshold, min_len, gap, run_min)
    vert = [
        (yr, rot_width - x1, rot_width - x0)
        for yr, x0, x1 in _row_segments(gray_rot, rot_width, rot_height, threshold, min_len, gap, run_min)
    ]
    return EdgeMap(dpi=dpi, horiz=tuple(horiz), vert=tuple(vert))


def _overlap_of_smaller(a: Rect4, b: Rect4) -> float:
    """2 つの枠の重なりの面積が、小さい方の面積に占める割合（0〜1）。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return w * h / smaller if smaller > 0 else 0.0


def _keep_larger_of_overlapping(found: Sequence[Rect4]) -> list[Rect4]:
    """大きく重なる枠は、大きい方だけ残す。

    入れ子（名刺の中の飾り枠）のほか、名刺の中の線を拾って名刺から少しはみ出した枠も、名刺と
    取り違えない（2026-11-19 実データ: 1 枚の名刺が 2 行に割れた）。
    """
    kept: list[Rect4] = []
    for r in sorted(found, key=lambda r: -(r[2] - r[0]) * (r[3] - r[1])):
        if not any(_overlap_of_smaller(r, k) >= _SAME_CARD_OVERLAP for k in kept):
            kept.append(r)
    return kept


def _candidates_at(
    gray: bytes, width: int, height: int,
    gray_rot: bytes, rot_width: int, rot_height: int,
    dpi: float, cfg: SplitCfg, threshold: int,
) -> tuple[list[Rect4], int]:
    """1 つのしきい値で見つかる、名刺の大きさの枠（表示空間 pt）と、長い線分の数。"""
    mm_px = dpi / 25.4
    min_len = int(cfg.detect_line_mm * mm_px)
    gap = int(cfg.detect_gap_mm * mm_px)
    horiz = _row_segments(gray, width, height, threshold, min_len, gap)
    # 回した画像の行 yr は元の列、列 x は元の行 (rot_width - 1 - x)
    vert = [
        (yr, rot_width - x1, rot_width - x0)
        for yr, x0, x1 in _row_segments(gray_rot, rot_width, rot_height, threshold, min_len, gap)
    ]
    scale = 72.0 / dpi
    found: list[Rect4] = []
    for x0, y0, x1, y1 in _components(horiz, vert, width, height):
        short, long_ = sorted(((x1 - x0) / mm_px, (y1 - y0) / mm_px))
        if (cfg.card_short_mm_min <= short <= cfg.card_short_mm_max
                and cfg.card_long_mm_min <= long_ <= cfg.card_long_mm_max):
            found.append((x0 * scale, y0 * scale, x1 * scale, y1 * scale))
    return found, len(horiz) + len(vert)


def _takes_a_known_character(
    c: Rect4, kept: Sequence[Rect4], covered: Sequence[tuple[float, float]], margin: float,
) -> bool:
    """候補 c を足すと、既にある枠（余白を含む）の文字が c に移るか。

    文字の振り分け（segmenter._assign_to_boxes）は、まず枠そのもので、どれにも入らない文字だけを余白
    だけ広げた範囲で判定する。そのため
    - 既にある枠の内側の文字は、c の枠そのものに入る時だけ移りうる（c の余白に入るだけなら、元の枠に残る）
    - 既にある枠の余白だけにある文字は、c を余白だけ広げた範囲に入れば移りうる
    """
    if any(geometry.contains_point(c, *point) for point in covered):
        return True
    grown = (c[0] - margin, c[1] - margin, c[2] + margin, c[3] + margin)
    return any(
        geometry.contains_point(grown, *point)
        for point in covered
        if not any(geometry.contains_point(k, *point) for k in kept)
    )


def _detect(
    gray: bytes, width: int, height: int,
    gray_rot: bytes, rot_width: int, rot_height: int,
    dpi: float, cfg: SplitCfg, points: Sequence[tuple[float, float]] | None,
) -> tuple[list[Rect4], int]:
    """(名刺の枠, 枠が見つかったしきい値のうち一番低い値)。枠が無ければ (空, detect_threshold)。"""
    args = (gray, width, height, gray_rot, rot_width, rot_height, dpi, cfg)
    first, lines_at_first = _candidates_at(*args, cfg.detect_threshold)
    kept = _keep_larger_of_overlapping(first)
    found_at = [cfg.detect_threshold] if kept else []
    margin = cfg.detect_margin_mm * 72.0 / 25.4
    tried = {cfg.detect_threshold}
    for step in cfg.detect_threshold_steps:
        threshold = cfg.detect_threshold + step
        if threshold in tried or not 1 <= threshold <= 255:
            continue
        tried.add(threshold)
        if threshold < cfg.detect_threshold and not lines_at_first:
            continue   # しきい値を下げると暗い画素は減る一方なので、線が 1 本も無いページでは、下げても出ない
        covered: list[tuple[float, float]] = []
        uncovered: list[tuple[float, float]] = []
        if points is not None:
            grown = [(k[0] - margin, k[1] - margin, k[2] + margin, k[3] + margin) for k in kept]
            for point in points:
                (covered if any(geometry.contains_point(g, *point) for g in grown) else uncovered).append(point)
            if len(uncovered) < cfg.card_min_chars:
                break   # 枠の外に、名刺 1 枚ぶんの文字が残っていない
        candidates, _lines = _candidates_at(*args, threshold)
        # ふるいの順: 既にある枠と重なるものを外す → 文字の条件 → 残った候補どうしで大きい方を残す
        candidates = [
            c for c in candidates
            if not any(_overlap_of_smaller(c, k) >= _KNOWN_CARD_OVERLAP for k in kept)
        ]
        if points is not None:
            candidates = [
                c for c in candidates
                if sum(1 for point in uncovered if geometry.contains_point(c, *point)) >= cfg.card_min_chars
                and not _takes_a_known_character(c, kept, covered, margin)
            ]
        added = _keep_larger_of_overlapping(candidates)
        if added:
            kept += added
            found_at.append(threshold)
    kept.sort(key=lambda r: (r[1], r[0]))
    return kept, min(found_at) if found_at else cfg.detect_threshold


def detect_card_boxes(
    gray: bytes, width: int, height: int,
    gray_rot: bytes, rot_width: int, rot_height: int,
    dpi: float, cfg: SplitCfg,
    points: Sequence[tuple[float, float]] | None = None,
) -> tuple[Rect4, ...]:
    """名刺の枠（表示空間 pt）。gray は表示空間のグレースケール画素列、gray_rot は
    それを 90 度回した画素列（元の列が行になる。元 x = 回転後の行、元 y = 回転後の幅 - 1 - 列）。

    まず detect_threshold で探し、続けて detect_threshold_steps だけ足し引きしたしきい値でも探して、
    見つけた枠を足す。背景や名刺の紙が真っ白でないページでは、1 つのしきい値だと広い面が「暗い」
    ことになり、名刺より大きい塊になって枠が取れない（2026-11-19 実データ）。先に見つけた枠が優先で、
    後のしきい値は、既にある枠と大きく重ならない枠を足すだけ（先に見つけた枠の座標は変わらない）。

    points（空白でない文字の中心。表示空間 pt）を渡した時（通常の読み取り）:
    - 枠（detect_margin_mm だけ広げる）の外の文字が card_min_chars 未満になった所でやめる
    - 後のしきい値の枠は、まだ枠に入っていない文字を card_min_chars 以上含むものだけを足す
      （文字の無い場所の、名刺の大きさの四角を拾わない）
    - 既にある枠（広げた範囲）の文字を取ってしまう枠は足さない（先の枠の文字を、後の枠が取らない）。
      既にある枠の内側の文字は、後の枠そのものに入る時、既にある枠の余白だけにある文字は、後の枠を余白だけ
      広げた範囲に入る時に、取ってしまうとみなす（文字の振り分けと同じ順。_takes_a_known_character）
    points を渡さない時（画素だけのテスト）は、文字の条件なしで、すべてのしきい値を試す。
    """
    return tuple(_detect(gray, width, height, gray_rot, rot_width, rot_height, dpi, cfg, points)[0])


def detect(
    gray: bytes, width: int, height: int,
    gray_rot: bytes, rot_width: int, rot_height: int,
    dpi: float, cfg: SplitCfg,
    points: Sequence[tuple[float, float]] | None = None,
) -> tuple[tuple[Rect4, ...], EdgeMap | None]:
    """名刺の枠（§7.8）と、当てはめ用の線の切れ端（§7.9。fit_cards が false なら None）。

    線の切れ端は、枠が見つかったしきい値のうち一番低い値で拾う。背景が detect_threshold より暗い
    ページでは、detect_threshold で拾うと全部の行と列が線になり、型をどこに置いても満点になって、
    当てはめの位置が決まらない。detect_threshold だけで枠が見つかったページでは、今までと同じ。
    """
    boxes, lowest = _detect(gray, width, height, gray_rot, rot_width, rot_height, dpi, cfg, points)
    edges = (
        detect_edges(gray, width, height, gray_rot, rot_width, rot_height, dpi, cfg, lowest)
        if cfg.fit_cards else None
    )
    return tuple(boxes), edges
