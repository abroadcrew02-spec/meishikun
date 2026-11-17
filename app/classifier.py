"""項目の振り分け。DESIGN.md §11・§19.1。

classify(region, s, d) -> CardFields が唯一の公開関数。内部で使う「片
（Piece）」は classifier の私的な型で、他のモジュールには出さない。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from app import formatter, geometry, layout, patterns
from app.models import (
    CardFields,
    ClassifyCfg,
    Dictionaries,
    Hit,
    HitKind,
    LIMITS,
    REASON_ORDER,
    Reason,
    Region,
    Settings,
    TextLine,
)
from app.textnorm import fix_ocr_digits

# §11.1-2: ラベルだけ・記号だけの片を捨てる時に両端から落とす文字。
_TRIM_CHARS = " \t/・|,、:："

# §11.2: 070/080/090 で始まる番号は接頭辞に関わらず必ず電話。
_MOBILE_DIGIT_PREFIXES = ("070", "080", "090")

# 氏名に使う CJK 統合漢字の範囲（§10.7 と同じ範囲。patterns の private 関数は
# 公開されていないため classifier 側で独立して持つ）。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
)
_HIRAGANA_RANGE = (0x3041, 0x3096)
_KATAKANA_RANGE = (0x30A1, 0x30FA)

_CONTACT_KINDS = (
    HitKind.PHONE, HitKind.FAX, HitKind.PHONE_FAX, HitKind.EMAIL, HitKind.URL,
)


def _has_cjk(s: str) -> bool:
    """文字列に CJK 統合漢字が含まれるか（§23 Q9: 英文社名・英字役職を和文の
    有無で未分類に回す判定を、この 1 関数に閉じ込める）。"""
    return any(
        any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in s
    )


def _is_cjk_or_kana(ch: str) -> bool:
    """§11.6-1「どちらも CJK・かなだけ」の判定（々〆ヵヶー・空白等は含めない）。"""
    cp = ord(ch)
    if any(lo <= cp <= hi for lo, hi in _CJK_RANGES):
        return True
    if _HIRAGANA_RANGE[0] <= cp <= _HIRAGANA_RANGE[1]:
        return True
    if _KATAKANA_RANGE[0] <= cp <= _KATAKANA_RANGE[1]:
        return True
    return False


# ---------------------------------------------------------------------------
# 片（Piece）
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Piece:
    """classifier の私的な型（DESIGN.md §11.0）。

    parts: (行, norm 上の開始, norm 上の終了) のリスト。通常は 1 要素、
    氏名の 2 片連結（§11.6-1）でだけ 2 要素になる。eq=False で identity
    比較にし、list.remove 等が内容一致ではなく同一インスタンスで動くように
    する（classifier 内部でだけ使うので外に出さない）。
    """

    parts: list[tuple[TextLine, int, int]]


def _piece_norm(p: _Piece) -> str:
    return " ".join(ln.text.norm[s:e] for ln, s, e in p.parts)


def _piece_raw(p: _Piece) -> str:
    return " ".join(ln.text.raw_slice(s, e) for ln, s, e in p.parts)


def _piece_gaps(p: _Piece) -> list[float]:
    """compress_name_spaces に渡す gaps（raw と同じ長さ）。

    2 要素連結時は、連結点（U+0020）の gap を 2 片の x 方向の隙間にする
    （brief_common の既知の穴・§11.6-1 の仮定どおり）。
    """
    if len(p.parts) == 1:
        ln, s, e = p.parts[0]
        ra, rb = ln.text.raw_span(s, e)
        return list(ln.gaps[ra:rb])
    result: list[float] = []
    prev_ln: TextLine | None = None
    for i, (ln, s, e) in enumerate(p.parts):
        if i > 0 and prev_ln is not None:
            result.append(ln.bbox[0] - prev_ln.bbox[2])
        ra, rb = ln.text.raw_span(s, e)
        result.extend(ln.gaps[ra:rb])
        prev_ln = ln
    return result


def _piece_row_col(p: _Piece) -> tuple[int, int]:
    ln = p.parts[0][0]
    return (ln.row, ln.col)


def _piece_size(p: _Piece) -> float:
    return max(ln.size for ln, _, _ in p.parts)


def _piece_lines(p: _Piece) -> list[TextLine]:
    seen: list[TextLine] = []
    for ln, _, _ in p.parts:
        if ln not in seen:
            seen.append(ln)
    return seen


def _x_overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2]


def _trim_span(norm: str, s: int, e: int) -> tuple[int, int]:
    while s < e and norm[s] in _TRIM_CHARS:
        s += 1
    while e > s and norm[e - 1] in _TRIM_CHARS:
        e -= 1
    return s, e


def _remaining_spans(length: int, consumed: list[tuple[int, int]]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for s, e in consumed:
        if s > pos:
            spans.append((pos, s))
        pos = max(pos, e)
    if pos < length:
        spans.append((pos, length))
    return spans


def _make_piece(ln: TextLine, s: int, e: int) -> _Piece | None:
    """両端の空白・区切りを落とし、ラベルだけ・記号だけ・1 文字の片は捨てる
    （§11.1-2）。patterns.is_label_only が同じ正規表現を公開している。"""
    ts, te = _trim_span(ln.text.norm, s, e)
    if ts >= te:
        return None
    if patterns.is_label_only(ln.text.norm[ts:te]):
        return None
    return _Piece([(ln, ts, te)])


# ---------------------------------------------------------------------------
# §11.1: 連絡先の切り出し
# ---------------------------------------------------------------------------


def _collect_pieces_and_hits(
    lines: list[TextLine],
) -> tuple[list[_Piece], dict[int, list[Hit]]]:
    pieces: list[_Piece] = []
    line_hits: dict[int, list[Hit]] = {}
    for ln in lines:
        hits = patterns.find_contacts(ln.text)
        line_hits[id(ln)] = hits
        consumed = sorted((h.start, h.end) for h in hits if h.kind != HitKind.POSTAL)
        for s, e in _remaining_spans(len(ln.text.norm), consumed):
            piece = _make_piece(ln, s, e)
            if piece is not None:
                pieces.append(piece)
    return pieces, line_hits


def _collect_kind_hits(
    lines: list[TextLine], line_hits: dict[int, list[Hit]], kind: HitKind,
) -> list[tuple[TextLine, Hit]]:
    items = [
        (ln, h) for ln in lines for h in line_hits.get(id(ln), []) if h.kind == kind
    ]
    items.sort(key=lambda pair: (pair[0].row, pair[0].col, pair[1].start))
    return items


def _line_starts_with_contact(hits: list[Hit]) -> bool:
    return any(h.kind in _CONTACT_KINDS and h.start == 0 for h in hits)


def _postal_hit_in(p: _Piece, line_hits: dict[int, list[Hit]]) -> Hit | None:
    ln, s, e = p.parts[0]
    for h in line_hits.get(id(ln), []):
        if h.kind == HitKind.POSTAL and s <= h.start and h.end <= e:
            return h
    return None


def _piece_has_postal(p: _Piece, line_hits: dict[int, list[Hit]]) -> bool:
    return _postal_hit_in(p, line_hits) is not None


def _is_postal_only(p: _Piece, line_hits: dict[int, list[Hit]]) -> bool:
    """郵便番号だけの片（`〒100-0001`）。住所の直上・直下の行に置かれた形（§11.5-6）。"""
    if len(p.parts) != 1:
        return False
    hit = _postal_hit_in(p, line_hits)
    if hit is None:
        return False
    ln, s, e = p.parts[0]
    rest = ln.text.norm[s:hit.start] + ln.text.norm[hit.end:e]
    return not rest.strip(_TRIM_CHARS)


# ---------------------------------------------------------------------------
# §11.2: 電話と FAX の確定
# ---------------------------------------------------------------------------


def _collect_phone_fax_hits(
    lines: list[TextLine], line_hits: dict[int, list[Hit]],
) -> list[tuple[TextLine, Hit]]:
    items = [
        (ln, h)
        for ln in lines
        for h in line_hits.get(id(ln), [])
        if h.kind in (HitKind.PHONE, HitKind.FAX, HitKind.PHONE_FAX)
    ]
    items.sort(key=lambda pair: (pair[0].row, pair[0].col, pair[1].start))
    return items


def _is_mobile_number(hit: Hit) -> bool:
    return hit.digits[:3] in _MOBILE_DIGIT_PREFIXES


def _resolve_phone_fax_kinds(
    items: list[tuple[TextLine, Hit]],
) -> tuple[dict[int, set[HitKind]], bool]:
    """各 hit（id(hit) で識別）の最終的な種別を決める（§11.2 の表＋連続規則）。

    戻り値の bool は PHONE_FAX_GUESSED が必要かどうか。
    """
    kinds: dict[int, set[HitKind]] = {}
    guessed = False
    run: list[Hit] = []

    def flush_run() -> None:
        nonlocal guessed
        if not run:
            return
        if len(run) == 1:
            kinds[id(run[0])] = {HitKind.PHONE}
        elif len(run) == 2:
            kinds[id(run[0])] = {HitKind.PHONE}
            kinds[id(run[1])] = {HitKind.FAX}
            guessed = True
        else:
            for h in run:
                kinds[id(h)] = {HitKind.PHONE}
            guessed = True
        run.clear()

    for _, hit in items:
        if _is_mobile_number(hit):
            flush_run()
            kinds[id(hit)] = {HitKind.PHONE}
        elif hit.prefixed:
            flush_run()
            if hit.kind == HitKind.PHONE_FAX:
                kinds[id(hit)] = {HitKind.PHONE, HitKind.FAX}
            elif hit.kind == HitKind.FAX:
                kinds[id(hit)] = {HitKind.FAX}
            else:
                kinds[id(hit)] = {HitKind.PHONE}
        else:
            run.append(hit)
    flush_run()
    return kinds, guessed


def _apply_phone_fax(
    items: list[tuple[TextLine, Hit]],
    kinds: dict[int, set[HitKind]],
    d: Dictionaries,
    fields: CardFields,
    reasons: set[Reason],
) -> None:
    phone_seen: list[str] = []
    fax_seen: list[str] = []
    work_cache: dict[int, str] = {}
    for ln, hit in items:
        if id(ln) not in work_cache:
            work, _changed = fix_ocr_digits(ln.text.norm)
            work_cache[id(ln)] = work
        printed = work_cache[id(ln)][hit.value_start:hit.value_end]
        value, phone_reasons = formatter.format_phone(hit.digits, printed, d)
        reasons.update(phone_reasons)
        if hit.ocr_fixed:
            reasons.add(Reason.OCR_DIGIT_FIX)
        for kind in kinds.get(id(hit), set()):
            if kind == HitKind.PHONE:
                if value not in phone_seen:
                    phone_seen.append(value)
                    fields.phones.append(value)
            elif kind == HitKind.FAX:
                if value not in fax_seen:
                    fax_seen.append(value)
                    fields.faxes.append(value)


# ---------------------------------------------------------------------------
# §11.3: 会社名
# ---------------------------------------------------------------------------


def _extract_company(
    pieces: list[_Piece], d: Dictionaries,
) -> tuple[list[_Piece], str, bool]:
    remaining: list[_Piece] = []
    candidates: list[tuple[TextLine, int, int, bool]] = []  # ln, gs, ge, has_cjk

    for p in pieces:
        if len(p.parts) != 1:
            remaining.append(p)
            continue
        ln, s, e = p.parts[0]
        norm = ln.text.norm[s:e]
        found = patterns.find_company(norm, d)
        if found is None:
            remaining.append(p)
            continue
        cs, ce = found
        gs, ge = s + cs, s + ce
        candidates.append((ln, gs, ge, _has_cjk(ln.text.norm[gs:ge])))
        for ns, ne in ((s, gs), (ge, e)):
            if ns >= ne:
                continue
            new_piece = _make_piece(ln, ns, ne)
            if new_piece is not None:
                remaining.append(new_piece)

    if not candidates:
        return remaining, "", False

    candidates.sort(key=lambda c: (not c[3], -c[0].size, c[0].row, c[0].col))
    best_ln, best_gs, best_ge, _ = candidates[0]
    company = best_ln.text.raw_slice(best_gs, best_ge)
    cjk_count = sum(1 for c in candidates if c[3])
    multi = cjk_count >= 2

    for ln, gs, ge, _ in candidates[1:]:
        remaining.append(_Piece([(ln, gs, ge)]))

    return remaining, company, multi


# ---------------------------------------------------------------------------
# §11.4: 役職
# ---------------------------------------------------------------------------


def _extract_title(
    pieces: list[_Piece], cfg: ClassifyCfg, d: Dictionaries,
) -> tuple[list[_Piece], str, list[TextLine], list[tuple[TextLine, int, int]]]:
    remaining: list[_Piece] = []
    candidates: list[tuple[TextLine, int, int, bool]] = []  # ln, gs, ge, has_cjk
    dept_parts: list[tuple[TextLine, int, int]] = []        # 役職の前に付いていた部署名

    for p in pieces:
        if len(p.parts) != 1:
            remaining.append(p)
            continue
        ln, s, e = p.parts[0]
        norm = ln.text.norm[s:e]
        found = patterns.find_title(norm, d)
        if found is None:
            remaining.append(p)
            continue
        ts, te = found

        # 役職の後ろに氏名が続く形。空白区切り（`部長 見本 太郎`）のほか、役職語が
        # 行頭にあって氏名が密着した形（`部長見本太郎`。実データ 2026-10-06）も、
        # 残りが 3 文字以上で氏名らしければ分ける（`部長代理` `部長補佐` は残りが
        # 2 文字なので役職のまま）。
        rest_from = -1
        after = norm[te:]
        if after.startswith(" "):
            rest_from = te + 1
        elif ts == 0 and len(after.replace(" ", "")) >= 3:
            rest_from = te
        split_back = (
            rest_from >= 0 and patterns.is_name_like(norm[rest_from:].strip(), cfg, d)
        )
        split_front = (
            not split_back and ts > 0 and norm[ts - 1] == " "
            and patterns.is_name_like(norm[:ts].strip(), cfg, d)
        )

        if split_back:
            gs, ge = s, s + te
            nts, nte = _trim_span(norm, rest_from, len(norm))
            if nts < nte:
                new_piece = _make_piece(ln, s + nts, s + nte)
                if new_piece is not None:
                    remaining.append(new_piece)
        elif split_front:
            gs, ge = s + ts, e
            nts, nte = _trim_span(norm, 0, ts)
            if nts < nte:
                new_piece = _make_piece(ln, s + nts, s + nte)
                if new_piece is not None:
                    remaining.append(new_piece)
        else:
            gs, ge = s, e

        # §11.4-5: 役職語の前に部署名が付いている形（`営業部 部長` `品質保証部主任`
        # `… 第一技術部 設計管理室長`）は、部署を切り離して役職は役職語から始める。
        if not split_front and ts > 0:
            k = _dept_prefix_end(ln.text.norm, s, s + ts, d)
            if k is not None:
                dept_parts.append((ln, s, k))
                gs = _trim_span(ln.text.norm, k, ge)[0]

        candidates.append((ln, gs, ge, _has_cjk(ln.text.norm[gs:ge])))

    if not candidates:
        return remaining, "", [], dept_parts

    has_cjk_any = any(c[3] for c in candidates)
    kept: list[tuple[TextLine, int, int, bool]] = []
    for c in candidates:
        if has_cjk_any and not c[3]:
            # §23 Q9: 和文の役職があれば英字だけの役職片は未分類へ。
            remaining.append(_Piece([(c[0], c[1], c[2])]))
        else:
            kept.append(c)

    if not kept:
        return remaining, "", [], dept_parts

    kept.sort(key=lambda c: (c[0].row, c[0].col))
    title = " ".join(ln.text.raw_slice(gs, ge) for ln, gs, ge, _ in kept)
    title_lines = [ln for ln, _, _, _ in kept]
    return remaining, title, title_lines, dept_parts


def _dept_prefix_end(norm: str, s: int, title_start: int, d: Dictionaries) -> int | None:
    """norm[s:title_start]（役職語の前）が部署名なら、その終端（norm 上の絶対位置）を返す。

    前全体が部署名でなければ、最後の空白までを試す（`第一技術部 設計管理室長` の
    `第一技術部`）。どちらでもなければ None。
    """
    ts, te = _trim_span(norm, s, title_start)
    if te > ts and patterns.is_department(norm[ts:te], d):
        return te
    cut = norm.rfind(" ", s, title_start)
    if cut > s:
        ts2, te2 = _trim_span(norm, s, cut)
        if te2 > ts2 and patterns.is_department(norm[ts2:te2], d):
            return te2
    return None


# ---------------------------------------------------------------------------
# §11.5b: 部署（2026-10-06 追加・Q46）
# ---------------------------------------------------------------------------


def _extract_departments(
    pieces: list[_Piece], d: Dictionaries, dept_parts: list[tuple[TextLine, int, int]],
) -> tuple[list[_Piece], str]:
    """部署名らしい片を集め、読み順に U+0020 で連結した部署を返す。"""
    remaining: list[_Piece] = []
    parts = list(dept_parts)
    for p in pieces:
        if len(p.parts) == 1:
            ln, s, e = p.parts[0]
            if patterns.is_department(ln.text.norm[s:e], d):
                parts.append((ln, s, e))
                continue
        remaining.append(p)
    parts.sort(key=lambda t: (t[0].row, t[0].col, t[1]))
    department = formatter.clean_spaces(" ".join(ln.text.raw_slice(s, e) for ln, s, e in parts))
    return remaining, department


# ---------------------------------------------------------------------------
# §11.5: 拠点ラベル＋住所
# ---------------------------------------------------------------------------


def _find_branch_label(start: _Piece, remaining: list[_Piece]) -> _Piece | None:
    ln0 = start.parts[0][0]
    h = geometry.height(ln0.bbox)
    for p in remaining:
        if len(p.parts) != 1:
            continue
        ln, s, e = p.parts[0]
        if ln is ln0:
            continue
        norm = ln.text.norm[s:e]
        if not patterns.is_branch_label(norm):
            continue
        if ln.row == ln0.row and ln.col == ln0.col - 1:
            return p
        if _x_overlaps(ln.bbox, ln0.bbox):
            gap = ln0.bbox[1] - ln.bbox[3]
            if 0 <= gap <= 1.5 * h:
                return p
    return None


def _find_continuation(
    p: _Piece,
    remaining: list[_Piece],
    cfg: ClassifyCfg,
    line_hits: dict[int, list[Hit]],
) -> _Piece | None:
    ln0 = p.parts[0][0]
    h = geometry.height(ln0.bbox)
    best: _Piece | None = None
    best_gap: float | None = None
    for c in remaining:
        if len(c.parts) != 1:
            continue
        lnc, sc, ec = c.parts[0]
        if lnc is ln0:
            continue
        gap = lnc.bbox[1] - ln0.bbox[3]
        if not (0 <= gap <= cfg.address_line_gap_ratio * h):
            continue
        if not _x_overlaps(lnc.bbox, ln0.bbox):
            continue
        if abs(lnc.size - ln0.size) > cfg.address_size_tolerance * ln0.size:
            continue
        norm_c = lnc.text.norm[sc:ec]
        if patterns.is_address_start(norm_c, _piece_has_postal(c, line_hits)):
            continue
        if patterns.is_branch_label(norm_c):
            continue
        if _line_starts_with_contact(line_hits.get(id(lnc), [])):
            continue
        if best is None or best_gap is None or gap < best_gap:
            best = c
            best_gap = gap
    return best


def _address_text_and_postal(
    chain: list[_Piece], line_hits: dict[int, list[Hit]],
) -> tuple[str, str]:
    """住所の片を連結し、最初の郵便番号 hit（〒 を含む消費範囲）を取り除く（§11.5-4）。

    返り値は (住所, 整形済み郵便番号)。郵便番号が無ければ郵便番号は ""。
    """
    parts: list[str] = []
    postal = ""
    for p in chain:
        hit: Hit | None = None
        if not postal:
            ln, s, e = p.parts[0]
            hit = next(
                (h for h in line_hits.get(id(ln), [])
                 if h.kind == HitKind.POSTAL and s <= h.start and h.end <= e),
                None,
            )
        if hit is None:
            parts.append(_piece_raw(p))
            continue
        postal = formatter.format_postal(hit.digits)
        parts.append(ln.text.raw_slice(s, hit.start))
        parts.append(ln.text.raw_slice(hit.end, e))
    return formatter.clean_spaces(" ".join(parts)), postal


# §11.5-6: 郵便番号だけの片を住所に付ける時の上下の隙間（行高に対する倍率。§11.5-2 と同じ）
_POSTAL_ATTACH_GAP_RATIO = 1.5


def _split_mid_address(p: _Piece) -> list[_Piece]:
    """行の途中に 2 件目の住所（拠点ラベル＋都道府県）があれば片を分ける（§11.5-1）。"""
    ln, s, e = p.parts[0]
    out: list[_Piece] = []
    cur = s
    while True:
        k = patterns.find_mid_address_start(ln.text.norm, cur, e)
        if k is None:
            break
        out.append(_Piece([(ln, cur, k)]))
        cur = k
    out.append(_Piece([(ln, cur, e)]))
    return out


def _find_adjacent_postal(
    chain: list[_Piece], remaining: list[_Piece], line_hits: dict[int, list[Hit]],
) -> _Piece | None:
    """住所の直下（優先）か直上にある郵便番号だけの片（§11.5-6）。"""
    first = chain[0].parts[0][0]
    last = chain[-1].parts[0][0]
    candidates = sorted(
        (p for p in remaining if _is_postal_only(p, line_hits)), key=_piece_row_col,
    )
    for ref, below in ((last, True), (first, False)):
        h = geometry.height(ref.bbox)
        for c in candidates:
            lnc = c.parts[0][0]
            if lnc is ref or not _x_overlaps(lnc.bbox, ref.bbox):
                continue
            gap = lnc.bbox[1] - ref.bbox[3] if below else ref.bbox[1] - lnc.bbox[3]
            if 0 <= gap <= _POSTAL_ATTACH_GAP_RATIO * h:
                return c
    return None


def _extract_addresses(
    pieces: list[_Piece], cfg: ClassifyCfg, line_hits: dict[int, list[Hit]],
) -> tuple[list[_Piece], list[str], list[str]]:
    remaining = list(pieces)
    chains: list[list[_Piece]] = []

    while True:
        start: _Piece | None = None
        for p in sorted(
            (p for p in remaining if len(p.parts) == 1), key=_piece_row_col,
        ):
            if _is_postal_only(p, line_hits):
                continue
            ln, s, e = p.parts[0]
            norm = ln.text.norm[s:e]
            if patterns.is_address_start(norm, _piece_has_postal(p, line_hits)):
                start = p
                break
        if start is None:
            break
        remaining.remove(start)

        for part in _split_mid_address(start):
            chain_lines = [part]
            while len(chain_lines) < cfg.address_max_lines:
                nxt = _find_continuation(chain_lines[-1], remaining, cfg, line_hits)
                if nxt is None:
                    break
                remaining.remove(nxt)
                chain_lines.append(nxt)

            label_piece = _find_branch_label(part, remaining)
            if label_piece is not None:
                remaining.remove(label_piece)
                chains.append([label_piece, *chain_lines])
            else:
                chains.append(chain_lines)

    addresses: list[str] = []
    postals: list[str] = []
    for chain in chains:
        address, postal = _address_text_and_postal(chain, line_hits)
        if not postal:
            po = _find_adjacent_postal(chain, remaining, line_hits)
            if po is not None:
                remaining.remove(po)
                hit = _postal_hit_in(po, line_hits)
                postal = formatter.format_postal(hit.digits) if hit else ""
        addresses.append(address)
        postals.append(postal)

    # どの住所にも付かなかった郵便番号だけの片は、住所 "" の項目にする（郵便番号列に出す）
    orphans = sorted(
        (p for p in remaining if _is_postal_only(p, line_hits)), key=_piece_row_col,
    )
    for p in orphans:
        remaining.remove(p)
        hit = _postal_hit_in(p, line_hits)
        addresses.append("")
        postals.append(formatter.format_postal(hit.digits) if hit else "")

    return remaining, addresses, postals


# ---------------------------------------------------------------------------
# §11.6: 氏名（消去法）
# ---------------------------------------------------------------------------

# §11.6-1: 2 片連結の各片の文字数上限（空白を除く）。name_part_max_chars とは別の規則。
NAME_MERGE_PIECE_MAX_CHARS = 4


def _can_merge_name_pieces(p1: _Piece, p2: _Piece, cfg: ClassifyCfg) -> bool:
    ln1, s1, e1 = p1.parts[0]
    ln2, s2, e2 = p2.parts[0]
    if ln1.row != ln2.row or abs(ln1.col - ln2.col) != 1:
        return False
    size1, size2 = ln1.size, ln2.size
    if size1 <= 0 or size2 <= 0:
        return False
    if abs(size1 - size2) > 0.1 * max(size1, size2):
        return False
    for ln, s, e in (p1.parts[0], p2.parts[0]):
        stripped = ln.text.norm[s:e].replace(" ", "")
        if not stripped or len(stripped) > NAME_MERGE_PIECE_MAX_CHARS:
            return False
        if not all(_is_cjk_or_kana(c) for c in stripped):
            return False
    left, right = (p1, p2) if ln1.col < ln2.col else (p2, p1)
    ln_l = left.parts[0][0]
    ln_r = right.parts[0][0]
    x_gap = ln_r.bbox[0] - ln_l.bbox[2]
    h = max(geometry.height(ln1.bbox), geometry.height(ln2.bbox))
    return 0 <= x_gap <= cfg.name_merge_gap_ratio * h


def _merge_short_name_pieces(pieces: list[_Piece], cfg: ClassifyCfg) -> list[_Piece]:
    singles = [p for p in pieces if len(p.parts) == 1]
    others = [p for p in pieces if len(p.parts) != 1]
    order = sorted(range(len(singles)), key=lambda i: _piece_row_col(singles[i]))
    used = [False] * len(singles)
    result: list[_Piece] = []

    i = 0
    while i < len(order):
        idx_a = order[i]
        if used[idx_a]:
            i += 1
            continue
        merged = False
        if i + 1 < len(order):
            idx_b = order[i + 1]
            if not used[idx_b] and _can_merge_name_pieces(singles[idx_a], singles[idx_b], cfg):
                result.append(_Piece([singles[idx_a].parts[0], singles[idx_b].parts[0]]))
                used[idx_a] = used[idx_b] = True
                merged = True
                i += 2
        if not merged:
            result.append(singles[idx_a])
            used[idx_a] = True
            i += 1

    result.extend(others)
    return result


def _adjacent_to_title(p: _Piece, title_lines: list[TextLine]) -> bool:
    ln0 = p.parts[0][0]
    # 役職と同じ TextLine から切り離された片（§11.4-2）は隣接とみなす。
    if any(tln is ln for ln, _, _ in p.parts for tln in title_lines):
        return True
    h = geometry.height(ln0.bbox)
    for tln in title_lines:
        if tln.row == ln0.row:
            x_gap = max(ln0.bbox[0] - tln.bbox[2], tln.bbox[0] - ln0.bbox[2])
            if x_gap <= 3.0 * h:
                return True
        if _x_overlaps(tln.bbox, ln0.bbox):
            y_gap = max(ln0.bbox[1] - tln.bbox[3], tln.bbox[1] - ln0.bbox[3])
            if y_gap <= 1.5 * h:
                return True
    return False


def _score_name_candidate(
    p: _Piece, median_size: float, max_size: float, title_lines: list[TextLine], cfg: ClassifyCfg,
) -> int:
    size = _piece_size(p)
    score = 0
    if median_size > 0 and size >= cfg.name_size_ratio * median_size:
        score += 2
    if abs(size - max_size) <= 0.1:
        score += 1
    if _adjacent_to_title(p, title_lines):
        score += 1
    return score


# 英数字と記号だけの語（ロゴの文字 `ISP` `DX` など）
_ASCII_TOKEN_RE = re.compile(r"^[A-Za-z0-9&.,'()\-/+:]+$")


def _split_ascii_edges_for_names(
    pieces: list[_Piece], cfg: ClassifyCfg, d: Dictionaries,
) -> list[_Piece]:
    """`ABC 見本 太郎` のように、英数字だけの語（ロゴ等）が氏名の行の端に乗った片を、
    英数字の語と残りに分け、残りが氏名らしければ候補にする（§11.6-2・2026-10-06・Q52）。
    英数字の語は未分類に残す。
    """
    out: list[_Piece] = []
    for p in pieces:
        if len(p.parts) != 1 or patterns.is_name_like(_piece_norm(p), cfg, d):
            out.append(p)
            continue
        ln, s, e = p.parts[0]
        norm = ln.text.norm
        tokens = [(m.start() + s, m.end() + s) for m in re.finditer(r"\S+", norm[s:e])]
        if len(tokens) < 2:
            out.append(p)
            continue
        lo, hi = 0, len(tokens)
        while lo < hi and _ASCII_TOKEN_RE.match(norm[tokens[lo][0]:tokens[lo][1]]):
            lo += 1
        while hi > lo and _ASCII_TOKEN_RE.match(norm[tokens[hi - 1][0]:tokens[hi - 1][1]]):
            hi -= 1
        if lo >= hi or (lo == 0 and hi == len(tokens)):
            out.append(p)
            continue
        core_s, core_e = tokens[lo][0], tokens[hi - 1][1]
        if not patterns.is_name_like(norm[core_s:core_e], cfg, d):
            out.append(p)
            continue
        spans = [(core_s, core_e)]
        if lo > 0:
            spans.insert(0, (tokens[0][0], tokens[lo - 1][1]))
        if hi < len(tokens):
            spans.append((tokens[hi][0], tokens[-1][1]))
        for a, b in spans:
            piece = _make_piece(ln, a, b)
            if piece is not None:
                out.append(piece)
    return out


def _extract_name(
    pieces: list[_Piece],
    lines: list[TextLine],
    cfg: ClassifyCfg,
    d: Dictionaries,
    title_lines: list[TextLine],
) -> tuple[list[_Piece], str, Reason | None]:
    merged = _merge_short_name_pieces(pieces, cfg)
    merged = _split_ascii_edges_for_names(merged, cfg, d)

    name_candidates = [p for p in merged if patterns.is_name_like(_piece_norm(p), cfg, d)]

    if not name_candidates:
        return merged, "", Reason.NAME_NOT_FOUND

    horizontal_sizes = [ln.size for ln in lines if not ln.vertical]
    median_size = statistics.median(horizontal_sizes) if horizontal_sizes else 0.0
    max_size = max(horizontal_sizes) if horizontal_sizes else 0.0

    scored = [
        (_score_name_candidate(p, median_size, max_size, title_lines, cfg), p)
        for p in name_candidates
    ]
    if any(score >= 1 for score, _ in scored):
        scored = [(score, p) for score, p in scored if score >= 1]

    scored.sort(key=lambda sp: (-sp[0], -_piece_size(sp[1]), *_piece_row_col(sp[1])))

    filtered = [p for _, p in scored]
    best = filtered[0]
    name_reason = Reason.NAME_MULTI_CANDIDATES if len(filtered) >= 2 else None

    name_raw = formatter.compress_name_spaces(
        _piece_raw(best), _piece_gaps(best), cfg.name_space_dominance,
    )

    leftover = [p for p in merged if p is not best]
    return leftover, name_raw, name_reason


# ---------------------------------------------------------------------------
# §11.7: 未分類と上限
# ---------------------------------------------------------------------------


_OVERFLOW_SPECS: tuple[tuple[str, str, Reason], ...] = (
    ("addresses", "住所:", Reason.OVERFLOW_ADDRESS),
    ("phones", "電話:", Reason.OVERFLOW_PHONE),
    ("faxes", "FAX:", Reason.OVERFLOW_FAX),
    ("emails", "メール:", Reason.OVERFLOW_EMAIL),
    ("urls", "URL:", Reason.OVERFLOW_URL),
)
_LIMIT_KEYS = {"addresses": "address", "phones": "phone", "faxes": "fax", "emails": "email", "urls": "url"}


def _apply_overflow(
    fields: CardFields, unclassified: list[str], reasons: set[Reason],
) -> None:
    for attr, label, reason in _OVERFLOW_SPECS:
        values: list[str] = getattr(fields, attr)
        limit = LIMITS[_LIMIT_KEYS[attr]]
        if len(values) > limit:
            overflow = values[limit:]
            del values[limit:]
            if attr == "addresses":
                postals = fields.postals[limit:]
                del fields.postals[limit:]
                overflow = [f"{pc} {v}" if pc else v for pc, v in zip(postals, overflow)]
            for v in overflow:
                unclassified.append(f"{label}{v}")
            reasons.add(reason)


# ---------------------------------------------------------------------------
# 公開関数
# ---------------------------------------------------------------------------


def classify(region: Region, s: Settings, d: Dictionaries) -> CardFields:
    """項目の振り分け（DESIGN.md §11）。"""
    fields = CardFields()
    reasons: set[Reason] = set(region.flags)

    lines = layout.build_lines(region.glyphs, s.layout, d.old_kanji)
    if not lines:
        fields.reasons = [Reason.REGION_NO_TEXT]
        return fields

    if any(ln.vertical for ln in lines):
        reasons.add(Reason.VERTICAL_TEXT)
    if any("�" in ln.text.raw for ln in lines):
        reasons.add(Reason.UNREADABLE_CHARS)

    pieces, line_hits = _collect_pieces_and_hits(lines)

    phone_fax_items = _collect_phone_fax_hits(lines, line_hits)
    kinds, guessed = _resolve_phone_fax_kinds(phone_fax_items)
    if guessed:
        reasons.add(Reason.PHONE_FAX_GUESSED)
    _apply_phone_fax(phone_fax_items, kinds, d, fields, reasons)

    for ln, h in _collect_kind_hits(lines, line_hits, HitKind.EMAIL):
        fields.emails.append(formatter.format_email(h.value))
    for ln, h in _collect_kind_hits(lines, line_hits, HitKind.URL):
        fields.urls.append(formatter.format_url(h.value))

    pieces, company, company_multi = _extract_company(pieces, d)
    fields.company = company
    if company_multi:
        reasons.add(Reason.COMPANY_MULTI)

    pieces, title, title_lines, dept_parts = _extract_title(pieces, s.classify, d)
    fields.title = title

    pieces, addresses, postals = _extract_addresses(pieces, s.classify, line_hits)
    fields.addresses = addresses
    fields.postals = postals

    pieces, department = _extract_departments(pieces, d, dept_parts)
    fields.department = department

    pieces, name, name_reason = _extract_name(pieces, lines, s.classify, d, title_lines)
    fields.name = name
    if name_reason is not None:
        reasons.add(name_reason)

    unclassified = [formatter.clean_spaces(_piece_raw(p)) for p in sorted(pieces, key=_piece_row_col)]
    _apply_overflow(fields, unclassified, reasons)
    fields.unclassified = unclassified
    fields.reasons = sorted(reasons, key=REASON_ORDER.index)
    return fields
