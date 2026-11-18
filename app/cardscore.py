"""名刺らしさの判定。DESIGN.md §7.6・§19.1。

segmenter から「この文字の集まりは名刺 1 枚として妥当か」「複数枚が混ざっていそうか」
を判定するために呼ばれる。判定だけを行い、分割そのものは行わない。
"""
from __future__ import annotations

import statistics
from collections.abc import Sequence

from app import layout, patterns
from app.models import (
    CardSignals,
    Dictionaries,
    Glyph,
    Hit,
    HitKind,
    Settings,
    SplitCfg,
)

# §7.6 の「CJK 文字を含む会社名」は §11.3 の COMPANY_MULTI と同じ概念なので、
# classifier._has_cjk と同じ範囲（§10.7 の CJK 統合漢字。かなは含めない）にそろえる。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),   # CJK 統合漢字拡張 A
    (0x4E00, 0x9FFF),   # CJK 統合漢字
    (0xF900, 0xFAFF),   # CJK 互換漢字
)


def _contains_cjk(s: str) -> bool:
    return any(any(lo <= ord(c) <= hi for lo, hi in _CJK_RANGES) for c in s)


def _remaining_pieces(norm: str, hits: Sequence[Hit]) -> list[str]:
    """hit の消費範囲（start/end。ラベル込み）を取り除いた残りの区間（strip 済み）。

    ※設計の穴 E-1: §7.6 の「連絡先 hit を除いた残りの片」は value_start/value_end
    （値のみ）を除く案もあるが、ラベル文字（本社・〒 等）が住所・拠点ラベル判定の
    妨げにならないよう、消費範囲全体（start/end）を除く方を選んだ。
    """
    spans = sorted((h.start, h.end) for h in hits)
    pieces: list[str] = []
    pos = 0
    for s, e in spans:
        if s > pos:
            pieces.append(norm[pos:s])
        pos = max(pos, e)
    if pos < len(norm):
        pieces.append(norm[pos:])
    return [p.strip() for p in pieces if p.strip()]


def score(glyphs: Sequence[Glyph], s: Settings, d: Dictionaries) -> CardSignals:
    """glyphs（Region.glyphs 相当。空白グリフを含んでよい）から CardSignals を作る。

    chars は非空白文字数（※設計の穴 E-3: Region.glyphs は空白グリフを含む想定
    なので、ここで非空白だけを数える）。
    """
    lines = layout.build_lines(glyphs, s.layout, d.old_kanji)
    chars = sum(1 for g in glyphs if not g.ch.isspace())

    has_phone = False
    has_fax = False
    has_url = False
    has_company = False
    has_name_like = False
    cjk_companies = 0
    emails = 0
    postals = 0
    name_pieces: list[tuple[str, float]] = []   # (空白を除いた norm, 行の size)

    for line in lines:
        hits = patterns.find_contacts(line.text, d)
        norm = line.text.norm

        for h in hits:
            if h.kind in (HitKind.PHONE, HitKind.PHONE_FAX):
                has_phone = True
            if h.kind in (HitKind.FAX, HitKind.PHONE_FAX):
                has_fax = True
            if h.kind == HitKind.URL:
                has_url = True
            if h.kind == HitKind.EMAIL:
                emails += 1
            if h.kind == HitKind.POSTAL:
                postals += 1

        company_span = patterns.find_company(norm, d)
        if company_span is not None:
            has_company = True
            cs, ce = company_span
            if _contains_cjk(norm[cs:ce]):
                cjk_companies += 1

        for piece in _remaining_pieces(norm, hits):
            if patterns.is_name_like(piece, s.classify, d):
                has_name_like = True
                name_pieces.append((piece.replace(" ", ""), line.size))

    has_email = emails > 0

    # 氏名らしい語とその大きさ（§7.6・2026-10-06）。目立つかどうかは、比べる相手を含めた
    # 本文の文字の大きさに対して判定する（CardSignals.prominent_names）。
    sizes = [ln.size for ln in lines if not ln.vertical]
    median_size = statistics.median(sizes) if sizes else 0.0
    seen: dict[str, float] = {}
    for text, size in name_pieces:
        seen[text] = max(size, seen.get(text, 0.0))

    return CardSignals(
        chars=chars,
        has_phone=has_phone,
        has_fax=has_fax,
        has_email=has_email,
        has_url=has_url,
        has_company=has_company,
        has_name_like=has_name_like,
        cjk_companies=cjk_companies,
        emails=emails,
        postals=postals,
        name_pieces=tuple(seen.items()),
        median_size=median_size,
    )


def is_card_like(sig: CardSignals, cfg: SplitCfg) -> bool:
    """§7.6: 文字数・signals（電話/FAX/メール/URL/会社名のうち該当数）・identity の 3 条件。

    identity（会社名か氏名らしい片がある）条件は Q8 により本関数に閉じ込める
    （他の判定箇所で identity を重複してチェックしない）。
    """
    return (
        sig.chars >= cfg.card_min_chars
        and sig.signals >= cfg.card_min_signals
        and sig.identity
    )


def looks_multi(sig: CardSignals, cfg: SplitCfg) -> bool:
    """§7.6: CJK 会社名 2 件以上・メール 3 件以上・郵便番号 3 件以上のいずれか。"""
    return (
        sig.cjk_companies >= cfg.multi_cjk_company_min
        or sig.emails >= cfg.multi_email_min
        or sig.postals >= cfg.multi_postal_min
    )
