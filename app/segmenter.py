"""1 ページを名刺 1 枚ずつの領域に分割する。DESIGN.md §7・§19.1。

import 許可: models, geometry, cardscore（layout は cardscore 経由でのみ使う）。
判定・分割の計算はすべて「文字」（非空白グリフ。§7.1）だけで行い、空白グリフは
最終的な Region.glyphs を組み立てる段でだけ、矩形への中心点包含で割り当てる
（※設計の穴 E-8。詳細は報告参照）。
"""
from __future__ import annotations

import bisect
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app import cardscore, geometry
from app.models import (
    CardSignals,
    Dictionaries,
    EdgeMap,
    Glyph,
    ManualSplit,
    PageData,
    Reason,
    Rect4,
    Region,
    Settings,
    SplitCfg,
    SplitMethod,
)

# ---------------------------------------------------------------------------
# §7.2: 画像候補のフィルタ
# ---------------------------------------------------------------------------

_ImageReason = tuple[Rect4, str]


def explain_images(page: PageData, cfg: SplitCfg) -> list[tuple[Rect4, str]]:
    """diagnose 用。各画像 bbox と判定理由（"ok"/"small"/"page_cover"/"nested"）。"""
    boxes = list(page.stats.image_boxes)
    page_area = page.stats.width * page.stats.height

    stage12: list[tuple[Rect4, str]] = []  # 段 1・2 判定後。"ok" は仮（段 3 で nested に変わりうる）
    survivors: list[Rect4] = []
    for b in boxes:
        area = geometry.area(b)
        short_side = min(geometry.width(b), geometry.height(b))
        if area < page_area * cfg.img_min_area_ratio or short_side < cfg.img_min_side_pt:
            stage12.append((b, "small"))
            continue
        if page_area and area >= page_area * cfg.img_page_cover_ratio:
            stage12.append((b, "page_cover"))
            continue
        stage12.append((b, "ok"))
        survivors.append(b)

    # 段 3: 他の survivor に面積の img_nested_ratio 以上を含まれている survivor は nested。
    # 互いに含み合う組（同じ bbox で xref 違い等）は、面積の大きい方（同じなら先の方）を残す。
    nested: set[int] = set()
    for i, a_rect in enumerate(survivors):
        a_area = geometry.area(a_rect)
        if a_area <= 0:
            continue
        for j, b_rect in enumerate(survivors):
            if i == j:
                continue
            inter = geometry.intersection(a_rect, b_rect)
            if inter is None:
                continue
            inter_area = geometry.area(inter)
            if inter_area < a_area * cfg.img_nested_ratio:
                continue
            b_area = geometry.area(b_rect)
            mutual = b_area > 0 and inter_area >= b_area * cfg.img_nested_ratio
            if mutual and not (b_area > a_area or (b_area == a_area and j < i)):
                continue
            nested.add(i)
            break

    result: list[tuple[Rect4, str]] = []
    survivor_idx = 0
    for b, reason in stage12:
        if reason != "ok":
            result.append((b, reason))
            continue
        result.append((b, "nested" if survivor_idx in nested else "ok"))
        survivor_idx += 1
    return result


def candidate_images(page: PageData, cfg: SplitCfg) -> list[Rect4]:
    return [b for b, reason in explain_images(page, cfg) if reason == "ok"]


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def _is_space(g: Glyph) -> bool:
    return g.ch.isspace()


def _members_in_rect(glyphs: Sequence[Glyph], rect: Rect4) -> list[Glyph]:
    """中心点が rect に入る glyphs（※ E-8: 空白グリフの領域割り当てに使う基準）。"""
    out: list[Glyph] = []
    for g in glyphs:
        cx, cy = geometry.center(g.bbox)
        if geometry.contains_point(rect, cx, cy):
            out.append(g)
    return out


def _median_height(glyphs: Sequence[Glyph]) -> float:
    heights = [g.bbox[3] - g.bbox[1] for g in glyphs]
    return statistics.median(heights) if heights else 1.0


# ---------------------------------------------------------------------------
# §7.4: XY-cut
# ---------------------------------------------------------------------------


def _significant_gaps(
    glyphs: Sequence[Glyph], axis: str, threshold: float,
) -> list[tuple[float, float]]:
    idx = 1 if axis == "y" else 0
    intervals = sorted((g.bbox[idx], g.bbox[idx + 2]) for g in glyphs)
    merged: list[list[float]] = []
    for a0, a1 in intervals:
        if merged and a0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], a1)
        else:
            merged.append([a0, a1])
    gaps: list[tuple[float, float]] = []
    for (_, prev_end), (cur_start, _) in zip(merged, merged[1:]):
        if cur_start - prev_end >= threshold:
            gaps.append((prev_end, cur_start))
    return gaps


def _rect_for_part(bounds: Rect4, axis: str, lo: float, hi: float) -> Rect4:
    if axis == "y":
        return (bounds[0], lo, bounds[2], hi)
    return (lo, bounds[1], hi, bounds[3])


def _split_by_gaps(
    glyphs: Sequence[Glyph], bounds: Rect4, gaps: Sequence[tuple[float, float]], axis: str,
) -> tuple[list[tuple[Rect4, list[Glyph]]], list[float]]:
    """bounds をギャップの中点で切り、各セルへ glyphs を振り分ける。"""
    idx = 1 if axis == "y" else 0
    lo, hi = bounds[idx], bounds[idx + 2]
    sorted_gaps = sorted(gaps)
    cuts = [(g0 + g1) / 2.0 for g0, g1 in sorted_gaps]
    boundaries = [lo, *cuts, hi]

    buckets: list[list[Glyph]] = [[] for _ in range(len(boundaries) - 1)]
    for g in glyphs:
        center = (g.bbox[idx] + g.bbox[idx + 2]) / 2.0
        pos = bisect.bisect_right(cuts, center)
        buckets[pos].append(g)

    parts = [
        (_rect_for_part(bounds, axis, boundaries[i], boundaries[i + 1]), buckets[i])
        for i in range(len(boundaries) - 1)
    ]
    gap_widths = [g1 - g0 for g0, g1 in sorted_gaps]
    return parts, gap_widths


@dataclass
class _Part:
    rect: Rect4
    glyphs: list[Glyph]
    sig: CardSignals


def _score_parts(
    parts: list[tuple[Rect4, list[Glyph]]], s: Settings, d: Dictionaries,
) -> list[_Part]:
    return [_Part(rect, glyphs, cardscore.score(glyphs, s, d)) for rect, glyphs in parts]


def _gap_between(gap_widths: list[float], i: int) -> float | None:
    if 0 <= i < len(gap_widths):
        return gap_widths[i]
    return None


def _merge_weak_parts(
    parts: list[_Part], gap_widths: list[float], axis: str, s: Settings, d: Dictionaries,
) -> list[_Part]:
    """§7.4 merge_weak_parts: 切りすぎの補正。

    名刺らしくない part を隣と結合する。ただし自分にも隣にも目立つ氏名（name_texts）が
    あり、その氏名が異なる時は別の人の名刺とみなして結合せず残す（§7.5-4 の全体パスで、
    別の帯にある相手＝縦型名刺の下半分＝と組めるようにする。2026-10-06 実データ: 行を
    またぐ縦型名刺の上半分（氏名だけ）が隣の横型名刺に吸収された）。小さい文字の氏名
    らしい注記は目立つ氏名に数えないので、普通の名刺の氏名ブロックは従来どおり吸収される。
    """
    parts = list(parts)
    gap_widths = list(gap_widths)
    deferred: set[int] = set()
    while len(parts) >= 2:
        weak = [
            i for i, p in enumerate(parts)
            if not cardscore.is_card_like(p.sig, s.split) and id(p) not in deferred
        ]
        if not weak:
            break
        i = weak[0]
        w = parts[i]
        has_left = i - 1 >= 0
        has_right = i + 1 < len(parts)

        if has_left and not has_right:
            j = i - 1
        elif has_right and not has_left:
            j = i + 1
        elif axis == "y" and w.sig.identity and not w.sig.contact:
            j = i + 1
        elif axis == "y" and w.sig.contact and not w.sig.identity:
            j = i - 1
        else:
            left_weak = not cardscore.is_card_like(parts[i - 1].sig, s.split)
            right_weak = not cardscore.is_card_like(parts[i + 1].sig, s.split)
            if left_weak != right_weak:
                j = i - 1 if left_weak else i + 1
            else:
                gl = _gap_between(gap_widths, i - 1)
                gr = _gap_between(gap_widths, i)
                if gr is None or (gl is not None and gl <= gr):
                    j = i - 1
                else:
                    j = i + 1

        lo, hi = (i, j) if i < j else (j, i)
        ref = _pair_ref_size(w.sig, parts[j].sig)
        w_names = w.sig.prominent_names(ref, s.classify.name_size_ratio)
        j_names = parts[j].sig.prominent_names(ref, s.classify.name_size_ratio)
        if w_names and j_names and not (w_names & j_names):
            deferred.add(id(w))
            continue
        merged_rect = geometry.union([parts[lo].rect, parts[hi].rect])
        merged_glyphs = parts[lo].glyphs + parts[hi].glyphs
        merged_sig = cardscore.score(merged_glyphs, s, d)
        parts = parts[:lo] + [_Part(merged_rect, merged_glyphs, merged_sig)] + parts[hi + 1:]
        gap_widths = gap_widths[:lo] + gap_widths[lo + 1:]
    return parts


def _xy_cut(
    glyphs: list[Glyph], bounds: Rect4, depth: int, relax: float, s: Settings, d: Dictionaries,
) -> list[tuple[Rect4, list[Glyph]]]:
    cfg = s.split
    if depth >= cfg.xy_max_depth or len(glyphs) < 2 * cfg.card_min_chars:
        return [(bounds, glyphs)]

    h = _median_height(glyphs)
    ty = max(cfg.xy_gap_min_pt, cfg.xy_gap_y_ratio * h) * relax
    tx = max(cfg.xy_gap_min_pt, cfg.xy_gap_x_ratio * h) * relax
    gy = _significant_gaps(glyphs, "y", ty)
    gx = _significant_gaps(glyphs, "x", tx)

    axes: list[tuple[str, list[tuple[float, float]], float]] = []
    if gy:
        axes.append(("y", gy, ty))
    if gx:
        axes.append(("x", gx, tx))

    def _axis_key(c: tuple[str, list[tuple[float, float]], float]) -> tuple[float, int]:
        axis, gaps, t = c
        widest = max(e - b for b, e in gaps)
        return (-(widest / t), 0 if axis == "y" else 1)

    axes.sort(key=_axis_key)

    for axis, gaps, _t in axes:
        raw_parts, gap_widths = _split_by_gaps(glyphs, bounds, gaps, axis)
        scored = _score_parts(raw_parts, s, d)
        merged = _merge_weak_parts(scored, gap_widths, axis, s, d)
        if len(merged) >= 2:
            out: list[tuple[Rect4, list[Glyph]]] = []
            for p in merged:
                out.extend(_xy_cut(p.glyphs, p.rect, depth + 1, relax, s, d))
            return out

    return [(bounds, glyphs)]


# ---------------------------------------------------------------------------
# §7.5: XY-cut モードの後処理
# ---------------------------------------------------------------------------


# (セルの矩形, 非空白文字, 事前フラグ, 領域に入れる全グリフ)。結合した葉の文字は元の葉の和にする
# （結合後の矩形で取り直すと、矩形が隣のセルに重なった時に隣の名刺の文字まで取り込む。2026-10-06 実データ）。
_Leaf = tuple[Rect4, list[Glyph], list[Reason], list[Glyph]]


def _extent(rect: Rect4, axis: str) -> float:
    return rect[3] - rect[1] if axis == "y" else rect[2] - rect[0]


def _touching(a: Rect4, b: Rect4, axis: str, overlap_min: float) -> bool:
    """b が a の直下（axis="y"）または直右（axis="x"）に接し、もう一方の軸で半分以上重なる。"""
    if axis == "y":
        touch = abs(b[1] - a[3]) <= 1.0
        overlap = min(a[2], b[2]) - max(a[0], b[0])
        narrow = min(a[2] - a[0], b[2] - b[0])
    else:
        touch = abs(b[0] - a[2]) <= 1.0
        overlap = min(a[3], b[3]) - max(a[1], b[1])
        narrow = min(a[3] - a[1], b[3] - b[1])
    return touch and narrow > 0 and overlap / narrow >= overlap_min


def _text_extent(chars: Sequence[Glyph], axis: str) -> float:
    """非空白グリフの bbox の和の axis 方向の長さ（セルの余白を含めない大きさ）。"""
    xs = [g for g in chars if not _is_space(g)]
    if not xs:
        return 0.0
    if axis == "y":
        return max(g.bbox[3] for g in xs) - min(g.bbox[1] for g in xs)
    return max(g.bbox[2] for g in xs) - min(g.bbox[0] for g in xs)


def _pair_ref_size(a, b) -> float:
    """2 つの葉を比べる時の本文の文字の大きさ（小さい方の中央値。0 は無視）。"""
    sizes = [x.median_size for x in (a, b) if x.median_size > 0.0]
    return min(sizes) if sizes else 0.0


def _complementary(a, b, ratio: float) -> bool:
    """2 つの CardSignals が「氏名側」と「連絡先側」に分かれているか（§7.5-4）。

    片方だけに目立つ氏名（name_texts。§7.6）があり、氏名の無い側に何かの情報（電話・FAX・
    メール・URL・会社名・郵便番号）があり、氏名のある側が完全な名刺ではない（会社名と
    電話・FAX の両方は持たない）。1 枚の名刺が内部の余白で切れた形（氏名ブロック｜会社・
    住所ブロック、縦型名刺の上半分｜下半分）。氏名・会社名・電話がそろった葉は完全な
    1 枚なので相手にしない。
    """
    ref = _pair_ref_size(a, b)
    a_named = bool(a.prominent_names(ref, ratio))
    b_named = bool(b.prominent_names(ref, ratio))
    if a_named == b_named:
        return False
    n, c = (a, b) if a_named else (b, a)
    c_has_info = (
        c.has_phone or c.has_fax or c.has_email or c.has_url or c.has_company or c.postals > 0
    )
    n_incomplete = not (n.has_company and (n.has_phone or n.has_fax))
    return c_has_info and n_incomplete


def _merge_split_fragments(leaves: list[_Leaf], s: Settings, d: Dictionaries) -> list[_Leaf]:
    """§7.5-4: 名刺の内部の余白で氏名側と連絡先側に切れた断片を 1 枚に戻す。

    上下（左右）に接する 2 つの葉が相補的（_complementary）なら結合する。葉が 3 つ
    以上あれば大きさも見る: 文字の範囲の長さの中央値を典型値とし、両方が典型値の
    frag_size_ratio 倍未満で、合わせた範囲が frag_merged_ratio 倍以内の時だけ。
    セルの矩形で測らないのは、ページ端の空白を含むセルが実際の名刺より大きく出て、
    細い 2 枚を誤って結合したため。
    """
    if len(leaves) < 2:
        return leaves
    cfg = s.split
    items = list(leaves)
    for axis in ("y", "x"):
        merged = True
        while merged and len(items) >= 2:
            merged = False
            sigs = [cardscore.score(c, s, d) for _, c, _, _ in items]
            typical: float | None = None
            typical_long = 0.0
            if len(items) >= 3:
                extents = sorted(_text_extent(c, axis) for _, c, _, _ in items)
                typical = extents[len(extents) // 2]
                # 縦型名刺は横型より縦に長い。名刺らしくない断片を含む組は長辺（幅と高さの
                # 中央値の大きい方）を基準にする（2026-10-06 実データ: 行をまたぐ縦型名刺）。
                widths = sorted(_text_extent(c, "x") for _, c, _, _ in items)
                heights = sorted(_text_extent(c, "y") for _, c, _, _ in items)
                typical_long = max(widths[len(widths) // 2], heights[len(heights) // 2])
            for i, (ra, ca, fa, ma) in enumerate(items):
                for j, (rb, cb, fb, mb) in enumerate(items):
                    if i == j or not _touching(ra, rb, axis, cfg.row_overlap_min):
                        continue
                    if not _complementary(sigs[i], sigs[j], s.classify.name_size_ratio):
                        continue
                    if typical is not None:
                        weak_pair = not (
                            cardscore.is_card_like(sigs[i], cfg) and cardscore.is_card_like(sigs[j], cfg)
                        )
                        bound = cfg.frag_merged_ratio * (typical_long if weak_pair else typical)
                        ea, eb = _text_extent(ca, axis), _text_extent(cb, axis)
                        if not (ea < cfg.frag_size_ratio * typical
                                and eb < cfg.frag_size_ratio * typical
                                and _text_extent(ca + cb, axis) <= bound):
                            continue
                    union = (min(ra[0], rb[0]), min(ra[1], rb[1]),
                             max(ra[2], rb[2]), max(ra[3], rb[3]))
                    items[i] = (union, ca + cb, list(dict.fromkeys([*fa, *fb])), ma + mb)
                    del items[j]
                    merged = True
                    break
                if merged:
                    break
    return items


def _xycut_mode(
    page_index: int, root: Rect4, root_chars: list[Glyph], all_glyphs: list[Glyph],
    page_rect: Rect4, s: Settings, d: Dictionaries,
) -> list[Region]:
    leaves = _xy_cut(root_chars, root, 0, 1.0, s, d)

    refined: list[_Leaf] = []
    for rect, chars in leaves:
        sig = cardscore.score(chars, s, d)
        if cardscore.looks_multi(sig, s.split):
            resplit = _xy_cut(chars, rect, 0, s.split.xy_relax_factor, s, d)
            if len(resplit) >= 2:
                refined.extend((r2, c2, [], _members_in_rect(all_glyphs, r2)) for r2, c2 in resplit)
                continue
            refined.append((rect, chars, [Reason.SPLIT_SUSPECT_MERGED], _members_in_rect(all_glyphs, rect)))
        else:
            refined.append((rect, chars, [], _members_in_rect(all_glyphs, rect)))

    refined = _merge_split_fragments(refined, s, d)

    regions: list[Region] = []
    if len(refined) == 1:
        rect, chars, pre_flags, members = refined[0]
        sig = cardscore.score(chars, s, d)
        if cardscore.is_card_like(sig, s.split) and not cardscore.looks_multi(sig, s.split):
            regions.append(Region(page_index, rect, members, SplitMethod.SINGLE, list(pre_flags)))
        else:
            flags = list(dict.fromkeys([Reason.SPLIT_FALLBACK, *pre_flags]))
            regions.append(
                Region(page_index, page_rect, list(all_glyphs), SplitMethod.FALLBACK, flags)
            )
    else:
        for rect, chars, pre_flags, members in refined:
            sig = cardscore.score(chars, s, d)
            flags = list(pre_flags)
            if not cardscore.is_card_like(sig, s.split) and Reason.SPLIT_WEAK not in flags:
                flags.append(Reason.SPLIT_WEAK)
            regions.append(Region(page_index, rect, members, SplitMethod.XYCUT, flags))
    return regions


# ---------------------------------------------------------------------------
# §7.3: 画像モード
# ---------------------------------------------------------------------------


def _nearest_image_index(cx: float, cy: float, image_rects: Sequence[Rect4]) -> int:
    return min(
        range(len(image_rects)),
        key=lambda i: geometry.distance_point_rect(cx, cy, image_rects[i]),
    )


def _image_index_for_point(cx: float, cy: float, image_rects: Sequence[Rect4]) -> int | None:
    hits = [i for i, r in enumerate(image_rects) if geometry.contains_point(r, cx, cy)]
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    return min(hits, key=lambda i: geometry.area(image_rects[i]))


def _image_mode(
    page_index: int, image_rects: list[Rect4], chars: list[Glyph], all_glyphs: list[Glyph],
    s: Settings, d: Dictionaries,
) -> list[Region]:
    cfg = s.split

    # §7.3-1: 文字・空白とも同じ基準（中心点包含・複数なら面積の小さい方）で 1 画像に振り分ける。
    # 画像が一部重なっても同じグリフが 2 領域に入らない。
    assigned_chars: list[list[Glyph]] = [[] for _ in image_rects]
    assigned_spaces: list[list[Glyph]] = [[] for _ in image_rects]
    orphan_chars: list[Glyph] = []
    # 画像の外側にある全グリフ（文字＋空白）。新規領域の組み立てに使うプール
    # （E-6: 既存の画像領域グリフと混ざらないようにする）。
    unassigned_all: list[Glyph] = []
    for g in all_glyphs:
        cx, cy = geometry.center(g.bbox)
        idx = _image_index_for_point(cx, cy, image_rects)
        if idx is None:
            unassigned_all.append(g)
            if not _is_space(g):
                orphan_chars.append(g)
        elif _is_space(g):
            assigned_spaces[idx].append(g)
        else:
            assigned_chars[idx].append(g)

    extra_regions: list[Region] = []
    reassigned: dict[int, list[Glyph]] = {}
    if orphan_chars:
        sig = cardscore.score(orphan_chars, s, d)
        if cardscore.is_card_like(sig, cfg):
            root = geometry.union([g.bbox for g in orphan_chars])
            for rect, _leaf_chars in _xy_cut(orphan_chars, root, 0, 1.0, s, d):
                members = _members_in_rect(unassigned_all, rect)
                extra_regions.append(Region(page_index, rect, members, SplitMethod.XYCUT, []))
        else:
            # ※設計の穴 E-5: 孤立文字 1 字ごとに最も近い画像領域へ振り分ける
            # （集合ごとまとめて 1 領域に送る案より頑健と判断）。
            for g in orphan_chars:
                cx, cy = geometry.center(g.bbox)
                nearest = _nearest_image_index(cx, cy, image_rects)
                assigned_chars[nearest].append(g)
                reassigned.setdefault(nearest, []).append(g)

    order = {id(g): k for k, g in enumerate(all_glyphs)}

    def _in_page_order(glyphs: list[Glyph]) -> list[Glyph]:
        return sorted(glyphs, key=lambda g: order[id(g)])

    regions: list[Region] = []
    for i, rect in enumerate(image_rects):
        region_chars = assigned_chars[i]
        orphans_here = reassigned.get(i, [])
        orphan_ids = {id(g) for g in orphans_here}
        in_image = [g for g in region_chars if id(g) not in orphan_ids] + assigned_spaces[i]
        members = _in_page_order(in_image) + orphans_here

        if not region_chars:
            regions.append(Region(page_index, rect, members, SplitMethod.IMAGE, [Reason.REGION_NO_TEXT]))
            continue

        sig = cardscore.score(region_chars, s, d)
        if cardscore.looks_multi(sig, cfg):
            resplit = _xy_cut(region_chars, rect, 0, 1.0, s, d)
            if len(resplit) >= 2:
                leaf_rects = [r for r, _ in resplit]
                # 画像内の文字は xy_cut の振り分けをそのまま使う（重複なし）。
                leaf_glyphs: list[list[Glyph]] = [
                    [g for g in lc if id(g) not in orphan_ids] for _, lc in resplit
                ]
                leaf_orphan = [False] * len(leaf_rects)
                # 孤立文字（画像外）は最寄りの葉へ入れ、その葉に SPLIT_ORPHAN_TEXT（§7.3-2）。
                for g in orphans_here:
                    k = _nearest_image_index(*geometry.center(g.bbox), leaf_rects)
                    leaf_glyphs[k].append(g)
                    leaf_orphan[k] = True
                # 空白は中心点が入る最初の葉、どれにも入らなければ最寄りの葉。
                for g in assigned_spaces[i]:
                    cx, cy = geometry.center(g.bbox)
                    k = next(
                        (n for n, r in enumerate(leaf_rects) if geometry.contains_point(r, cx, cy)),
                        None,
                    )
                    if k is None:
                        k = _nearest_image_index(cx, cy, leaf_rects)
                    leaf_glyphs[k].append(g)
                for k, leaf_rect in enumerate(leaf_rects):
                    leaf_flags = [Reason.SPLIT_ORPHAN_TEXT] if leaf_orphan[k] else []
                    # 葉の外側の辺は画像の縁なので、縁をまたぐ文字は葉の枠からもはみ出す（下の IMAGE の枠と同じ）
                    leaf_in_image = [g for g in resplit[k][1] if id(g) not in orphan_ids]
                    if _frame_is_off(leaf_rect, leaf_in_image, cfg.frame_out_ratio):
                        leaf_flags.append(Reason.SPLIT_FRAME_OFF)
                    regions.append(
                        Region(
                            page_index, leaf_rect, _in_page_order(leaf_glyphs[k]),
                            SplitMethod.XYCUT, leaf_flags,
                        )
                    )
                continue
            flags = [Reason.SPLIT_SUSPECT_MERGED]
        elif not cardscore.is_card_like(sig, cfg):
            flags = [Reason.SPLIT_WEAK]
        else:
            flags = []

        if i in reassigned:
            flags = [*flags, Reason.SPLIT_ORPHAN_TEXT]
        # 画像の縁をまたぐ文字が多い時は、画像と文字の位置が合っていない。後から足したはぐれた文字は
        # 数えない（そちらは SPLIT_ORPHAN_TEXT で知らせている）。利用者がこのページの枠を 1 つでも
        # 動かすと全部の枠を split_by_rects で切り直すので、動かす前と後で「確認してほしい枠かどうか」が
        # 変わらないようにする。はぐれた文字のある枠は、動かした後は理由の語が変わることがある
        # （SPLIT_ORPHAN_TEXT → SPLIT_FRAME_OFF。どちらも確認してほしい枠）
        in_image_chars = [g for g in region_chars if id(g) not in orphan_ids]
        if _frame_is_off(rect, in_image_chars, cfg.frame_out_ratio):
            flags = [*flags, Reason.SPLIT_FRAME_OFF]
        regions.append(Region(page_index, rect, members, SplitMethod.IMAGE, flags))

    regions.extend(extra_regions)
    return regions


# ---------------------------------------------------------------------------
# §7.7: 読み順
# ---------------------------------------------------------------------------


def _order_regions(regions: list[Region], overlap_min: float) -> list[Region]:
    if not regions:
        return []
    rows = geometry.group_rows([r.rect for r in regions], overlap_min)
    return [regions[i] for row in rows for i in row]


# ---------------------------------------------------------------------------
# §7.1: 全体
# ---------------------------------------------------------------------------


def _center(g: Glyph) -> tuple[float, float]:
    return ((g.bbox[0] + g.bbox[2]) / 2.0, (g.bbox[1] + g.bbox[3]) / 2.0)


class _EdgeIndex:
    """EdgeMap を pt の座標で引けるようにした索引（§7.9）。"""

    def __init__(self, edges: EdgeMap) -> None:
        self.k = edges.dpi / 72.0
        self.tol = max(1, int(round(1.0 * edges.dpi / 25.4)))   # 辺から ±1mm を同じ線とみなす
        self.rows: dict[int, list[tuple[int, int]]] = {}
        self.cols: dict[int, list[tuple[int, int]]] = {}
        for y, x0, x1 in edges.horiz:
            self.rows.setdefault(y, []).append((x0, x1))
        for x, y0, y1 in edges.vert:
            self.cols.setdefault(x, []).append((y0, y1))

    def _coverage(self, index: dict[int, list[tuple[int, int]]], line: float, a: float, b: float) -> float:
        lp, ap, bp = int(round(line * self.k)), a * self.k, b * self.k
        if bp <= ap:
            return 0.0
        spans = []
        for li in range(lp - self.tol, lp + self.tol + 1):
            for s0, s1 in index.get(li, ()):
                lo, hi = max(s0, ap), min(s1, bp)
                if hi > lo:
                    spans.append((lo, hi))
        if not spans:
            return 0.0
        spans.sort()
        covered, cur_lo, cur_hi = 0.0, spans[0][0], spans[0][1]
        for lo, hi in spans[1:]:
            if lo > cur_hi:
                covered += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
            else:
                cur_hi = max(cur_hi, hi)
        covered += cur_hi - cur_lo
        return covered / (bp - ap)

    def score(self, r: Rect4) -> float:
        """4 辺が線の切れ端と重なる割合の平均。"""
        x0, y0, x1, y1 = r
        return (
            self._coverage(self.rows, y0, x0, x1) + self._coverage(self.rows, y1, x0, x1)
            + self._coverage(self.cols, x0, y0, y1) + self._coverage(self.cols, x1, y0, y1)
        ) / 4.0


def _card_template(boxes: Sequence[Rect4], cfg: SplitCfg) -> tuple[float, float]:
    """名刺の型（長辺, 短辺 pt）。取れた枠の中央値。枠が無ければ標準の大きさ。"""
    if not boxes:
        return cfg.card_std_long_mm * 72.0 / 25.4, cfg.card_std_short_mm * 72.0 / 25.4
    longs = sorted(max(b[2] - b[0], b[3] - b[1]) for b in boxes)
    shorts = sorted(min(b[2] - b[0], b[3] - b[1]) for b in boxes)
    return longs[len(longs) // 2], shorts[len(shorts) // 2]


def _overlap_ratio(a: Rect4, b: Rect4) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return (w * h) / ((a[2] - a[0]) * (a[3] - a[1]))


def _fit_card(
    chars: Sequence[Glyph], template: tuple[float, float], index: _EdgeIndex,
    taken: Sequence[Rect4], page_rect: Rect4, cfg: SplitCfg,
) -> Rect4 | None:
    """文字のかたまりを含む名刺の型の位置を、線の切れ端に最もよく合うように決める（§7.9）。

    横向き・縦向きの両方を試す（縦型名刺の縁が取れなかった時も当てはめられる）。
    1mm 刻みで、文字をすべて含む範囲をずらし、4 辺が線と重なる割合の平均が最も高い位置を
    採る。fit_min_score 未満、または取れた枠と 10% 以上重なる位置は採らない。
    """
    tx0 = min(g.bbox[0] for g in chars)
    ty0 = min(g.bbox[1] for g in chars)
    tx1 = max(g.bbox[2] for g in chars)
    ty1 = max(g.bbox[3] for g in chars)
    step = 72.0 / 25.4
    long_, short = template
    best: tuple[float, Rect4] | None = None
    for w, h in ((long_, short), (short, long_)):
        if tx1 - tx0 > w + step or ty1 - ty0 > h + step:
            continue
        xs = max(page_rect[0], tx1 - w)
        xe = min(tx0, page_rect[2] - w)
        ys = max(page_rect[1], ty1 - h)
        ye = min(ty0, page_rect[3] - h)
        x = xs
        while x <= xe + 1e-6:
            y = ys
            while y <= ye + 1e-6:
                r = (x, y, x + w, y + h)
                if not any(_overlap_ratio(r, t) > 0.10 for t in taken):
                    sc = index.score(r)
                    if best is None or sc > best[0]:
                        best = (sc, r)
                y += step
            x += step
    if best is None or best[0] < cfg.fit_min_score:
        return None
    return best[1]


def _fit_missing_cards(
    boxes: list[Rect4], rest_chars: list[Glyph], edges: EdgeMap, page_rect: Rect4,
    s: Settings, d: Dictionaries,
) -> list[Rect4]:
    """枠の外に残った文字のかたまりごとに名刺の型を当てはめる（§7.9）。

    当てはめた枠に入った文字を除いて、残りをまとめ直して繰り返す（最大 3 回）。隣の名刺の
    文字が混ざって名刺より大きくなったかたまりも、隣に枠が付いた後なら当てはめられる。
    """
    index = _EdgeIndex(edges)
    template = _card_template(boxes, s.split)
    m = s.split.detect_margin_mm * 72.0 / 25.4
    fitted: list[Rect4] = []
    remaining = list(rest_chars)
    for _round in range(3):
        if len(remaining) < s.split.card_min_chars:
            break
        root = geometry.union([g.bbox for g in remaining])
        added = 0
        for _rect, chars in _xy_cut(remaining, root, 0, 1.0, s, d):
            chars = [g for g in chars if not _is_space(g)]
            if len(chars) < s.split.card_min_chars:
                continue
            r = _fit_card(chars, template, index, boxes + fitted, page_rect, s.split)
            if r is not None:
                fitted.append(r)
                added += 1
        if not added:
            break
        grown = [(f[0] - m, f[1] - m, f[2] + m, f[3] + m) for f in fitted]
        remaining = [g for g in remaining if _image_index_for_point(*_center(g), grown) is None]
    return fitted


def _assign_to_boxes(
    glyphs: Sequence[Glyph], boxes: Sequence[Rect4], margin: float,
) -> tuple[list[list[Glyph]], list[Glyph]]:
    """グリフを枠に振り分ける。戻り値は (枠ごとのグリフ, どの枠にも入らなかったグリフ)。

    中心点が入る枠（複数なら面積の小さい方）に入れる。どの枠にも入らないグリフは、枠を
    margin だけ広げた範囲で同じ判定をする。縁ぎりぎりの飾り文字（`50th` の `h`）が
    はみ出して、隣の枠の無い名刺に混ざるのを防ぐ（2026-10-06 実データ）。
    """
    grown = [(b[0] - margin, b[1] - margin, b[2] + margin, b[3] + margin) for b in boxes]
    inside: list[list[Glyph]] = [[] for _ in boxes]
    rest: list[Glyph] = []
    for g in glyphs:
        k = _image_index_for_point(*_center(g), boxes)
        if k is None:
            k = _image_index_for_point(*_center(g), grown)
        if k is None:
            rest.append(g)
        else:
            inside[k].append(g)
    return inside, rest


_FRAME_OUT_EPS = 0.01     # 枠からのはみ出しとみなす最小の長さ pt（座標の丸めの誤差を数えない）


def _frame_is_off(box: Rect4, chars: Sequence[Glyph], ratio: float, left_out: int = 0) -> bool:
    """枠が名刺からずれている疑いがあるか: 枠の文字のうち、枠からはみ出した文字の割合が ratio 以上。

    画像から見つけた枠・型を当てはめた枠・利用者が決めた枠は、枠のすぐ外の文字もその名刺として読む
    （`_assign_to_boxes`）。読み取りは欠けないが、枠が名刺からずれていても分からない。名刺の文字は
    ふつう名刺の内側にあるので、枠の辺にかかる文字・枠の外にある文字が多い時は、枠のほうがずれている。
    名刺の画像を枠にする時（`_image_mode`）も、画像の縁をまたぐ文字で同じように見る。

    割合で見るので、文字の多い名刺では、縁ぎりぎりの飾り文字の数文字では付かない（既定の 5% なら、
    100 文字の名刺で 4 文字まで）。文字の少ない枠（20 文字以下）では 1 文字で付く。
    はみ出した文字が 1 つも無い枠には、ratio が 0 以下でも付けない。chars は空白以外の文字。

    left_out は、同じ名刺の文字なのに、どの枠にも入らなかった文字の数（利用者が決めた枠の時だけ。
    `_left_out_counts`）。はみ出した文字と同じに数える。利用者が枠を大きくずらすと、文字は枠から
    3 mm（`detect_margin_mm`）より外へ出て、どの枠の文字でもなくなる。これを数えないと、ずらすほど
    「問題なし」に戻ってしまう。文字が 1 つも無い枠には付けない（`REGION_NO_TEXT` で知らせる）。
    """
    if not chars:
        return False
    x0, y0, x1, y1 = box
    out = sum(
        1 for g in chars
        if max(x0 - g.bbox[0], y0 - g.bbox[1], g.bbox[2] - x1, g.bbox[3] - y1) > _FRAME_OUT_EPS
    )
    out += left_out
    return out > 0 and out / (len(chars) + left_out) >= ratio


def _left_out_counts(
    inside: Sequence[Sequence[Glyph]], rest: Sequence[Glyph], auto: Sequence[Region], margin: float,
) -> list[int]:
    """利用者が決めた枠ごとに、「同じ名刺の文字なのに、どの枠にも入らなかった文字」の数を返す。

    同じ名刺かどうかは、自動で決めた領域（auto）で決める。枠の文字がいちばん多く属する自動の領域を、
    その枠の名刺とみなし、その領域の文字のうち rest（どの枠にも入らなかった文字）に残ったものを数える。
    - 隣の名刺の枠を利用者が消した時、隣の名刺の文字は別の領域なので数えない
    - 自動の時に後から足したはぐれた文字（領域の矩形から margin より外の文字）は、名刺の文字に数えない
      （利用者が触る前と後で、結果が変わらないようにする）
    - 2 つの枠が同じ領域に属する時（1 枚を 2 つに分けた時）は、残った文字を両方の枠に数える
    """
    counts = [0] * len(inside)
    if not auto or not rest:
        return counts
    home: dict[int, int] = {}                  # 文字（seq）→ 自動の領域の番号
    for index, region in enumerate(auto):
        x0, y0, x1, y1 = region.rect
        for g in region.glyphs:
            if _is_space(g):
                continue
            cx, cy = _center(g)
            if x0 - margin <= cx <= x1 + margin and y0 - margin <= cy <= y1 + margin:
                home[g.seq] = index
    left = Counter(home[g.seq] for g in rest if not _is_space(g) and g.seq in home)
    if not left:
        return counts
    for k, members in enumerate(inside):
        votes = Counter(home[g.seq] for g in members if not _is_space(g) and g.seq in home)
        if votes:
            card = min(votes, key=lambda index: (-votes[index], index))
            counts[k] = left.get(card, 0)
    return counts


def split_by_rects(
    page: PageData, rects: Sequence[Rect4], s: Settings, d: Dictionaries,
    auto: Sequence[Region] = (),
) -> ManualSplit:
    """利用者が確認画面で決めた枠で切る（method=MANUAL）。

    文字の振り分けは画像から見つけた枠（§7.8）と同じ規則。枠の外に残った文字はどの名刺にも
    入れず、outside で返す（画面が位置を示す。自動の時のように XY-cut や最寄りの枠へは回さない）。

    auto は、このページを自動で区切った時の領域。枠がずれている疑い（SPLIT_FRAME_OFF）を決める時に、
    「同じ名刺の文字なのに、どの枠にも入らなかった文字」を数えるのに使う。振り分けには使わない。
    """
    boxes = list(rects)
    margin = s.split.detect_margin_mm * 72.0 / 25.4
    inside, rest = _assign_to_boxes(page.glyphs, boxes, margin)
    left_out = _left_out_counts(inside, rest, auto, margin)
    regions: list[Region] = []
    for k, (box, members) in enumerate(zip(boxes, inside)):
        chars = [g for g in members if not _is_space(g)]
        flags: list[Reason] = []
        if not chars:
            flags.append(Reason.REGION_NO_TEXT)
        else:
            sig = cardscore.score(chars, s, d)
            if cardscore.looks_multi(sig, s.split):
                flags.append(Reason.SPLIT_SUSPECT_MERGED)
            if not cardscore.is_card_like(sig, s.split):
                flags.append(Reason.SPLIT_WEAK)
            if _frame_is_off(box, chars, s.split.frame_out_ratio, left_out[k]):
                flags.append(Reason.SPLIT_FRAME_OFF)
        regions.append(Region(page.stats.index, box, members, SplitMethod.MANUAL, flags))
    rows = geometry.group_rows([r.rect for r in regions], s.split.row_overlap_min) if regions else []
    order = [i for row in rows for i in row]
    return ManualSplit(
        regions=[regions[i] for i in order],
        source=order,
        outside=[g for g in rest if not _is_space(g)],
    )


def _detected_mode(
    page_index: int, boxes: list[Rect4], all_glyphs: list[Glyph], page_rect: Rect4,
    s: Settings, d: Dictionaries, edges: EdgeMap | None = None,
) -> list[Region]:
    """§7.8: 画像から見つけた名刺の枠で切る。

    枠の中のグリフ（中心点。複数の枠に入るなら面積の小さい方）はその名刺にする。
    枠の外に残った文字（枠が見つからなかった名刺）には、名刺の型を当てはめる（§7.9）。
    それでも残った文字は、残りだけで従来の XY-cut を行う。残りが名刺 1 枚分に
    満たない時は、最寄りの枠に足して SPLIT_ORPHAN_TEXT。
    """
    m = s.split.detect_margin_mm * 72.0 / 25.4
    grown_boxes = [(b[0] - m, b[1] - m, b[2] + m, b[3] + m) for b in boxes]
    fitted: list[Rect4] = []
    if edges is not None and s.split.fit_cards:
        pre_rest = [
            g for g in all_glyphs
            if not _is_space(g) and _image_index_for_point(*_center(g), grown_boxes) is None
        ]
        if len(pre_rest) >= s.split.card_min_chars:
            fitted = _fit_missing_cards(boxes, pre_rest, edges, page_rect, s, d)
    n_detected = len(boxes)
    boxes = boxes + fitted

    inside, rest = _assign_to_boxes(all_glyphs, boxes, m)

    regions: list[Region] = []
    box_index: list[int] = []
    for k, (box, members) in enumerate(zip(boxes, inside)):
        chars = [g for g in members if not _is_space(g)]
        if not chars:
            continue
        sig = cardscore.score(chars, s, d)
        flags: list[Reason] = []
        if cardscore.looks_multi(sig, s.split):
            flags.append(Reason.SPLIT_SUSPECT_MERGED)
        if not cardscore.is_card_like(sig, s.split):
            flags.append(Reason.SPLIT_WEAK)
        if _frame_is_off(box, chars, s.split.frame_out_ratio):
            flags.append(Reason.SPLIT_FRAME_OFF)
        method = SplitMethod.DETECTED if k < n_detected else SplitMethod.FITTED
        regions.append(Region(page_index, box, members, method, flags))
        box_index.append(k)

    rest_chars = [g for g in rest if not _is_space(g)]
    if len(rest_chars) >= s.split.card_min_chars:
        root = geometry.union([g.bbox for g in rest_chars])
        regions += _xycut_mode(page_index, root, rest_chars, rest, page_rect, s, d)
    elif rest_chars and regions:
        rects = [r.rect for r in regions]
        for g in rest:
            j = _nearest_image_index(*_center(g), rects)
            regions[j].glyphs.append(g)
            if not _is_space(g) and Reason.SPLIT_ORPHAN_TEXT not in regions[j].flags:
                regions[j].flags.append(Reason.SPLIT_ORPHAN_TEXT)
    return regions


def segment_page(page: PageData, s: Settings, d: Dictionaries) -> list[Region]:
    all_glyphs = list(page.glyphs)
    chars = [g for g in all_glyphs if not _is_space(g)]
    # PageStats に x0/y0 が無いため、ページ矩形は (0, 0, width, height) とする
    # （※設計の穴 E-4: §6.1 により page.rect は回転後の寸法を返す前提）。
    page_rect: Rect4 = (0.0, 0.0, page.stats.width, page.stats.height)

    cands = candidate_images(page, s.split)

    if len(cands) >= 2:
        regions = _image_mode(page.stats.index, cands, chars, all_glyphs, s, d)
    elif page.card_boxes and any(
        _image_index_for_point(*_center(g), page.card_boxes) is not None for g in chars
    ):
        regions = _detected_mode(
            page.stats.index, list(page.card_boxes), all_glyphs, page_rect, s, d, page.card_edges,
        )
    else:
        if cands:
            char_union = geometry.union([g.bbox for g in chars]) if chars else cands[0]
            root = geometry.union([cands[0], char_union])
        else:
            root = page_rect
        regions = _xycut_mode(page.stats.index, root, chars, all_glyphs, page_rect, s, d)

    return _order_regions(regions, s.split.row_overlap_min)
