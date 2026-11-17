"""分割確認 PDF の描画。

DESIGN.md §14.4・§19.2 を実装する。fitz を import する（I1 不変条件で許可
された 3 ファイルの 1 つ）。CardRecord・PdfResult は型ヒントとしてのみ使い、
実行時は属性アクセスだけで動く。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import fitz

if TYPE_CHECKING:
    from app.models import PdfResult

_RED = (1, 0, 0)
_WHITE = (1, 1, 1)
_LABEL_FONTSIZE = 14.0
_MARK_FONTSIZE = 14.0
_FRAME_MARGIN = 8.0   # 文字の範囲から枠までの余白 pt（§14.4）


def _draw_label(page: fitz.Page, rect: fitz.Rect, label: str) -> None:
    box_w = 9.0 + 6.0 * len(label)
    box_h = _LABEL_FONTSIZE + 2.0
    box = fitz.Rect(rect.x0, rect.y0, min(rect.x0 + box_w, rect.x1), min(rect.y0 + box_h, rect.y1))
    page.draw_rect(box, color=None, fill=_WHITE)
    page.insert_text(
        (rect.x0 + 2.0, rect.y0 + _LABEL_FONTSIZE),
        label,
        fontsize=_LABEL_FONTSIZE,
        fontname="helv",
        color=_RED,
        rotate=page.rotation,
    )


def _draw_review_mark(page: fitz.Page, rect: fitz.Rect) -> None:
    page.insert_text(
        (max(rect.x1 - _MARK_FONTSIZE, rect.x0), rect.y0 + _MARK_FONTSIZE),
        "!",
        fontsize=_MARK_FONTSIZE,
        fontname="helv",
        color=_RED,
        rotate=page.rotation,
    )


def _frame_rect(card) -> fitz.Rect:
    """枠は「1 枚として読んだ文字の範囲」に余白を足したもの（§14.4。2026-10-06 変更）。

    領域のセル（draw_rect）はページ端の空白や隣の名刺の余白まで含むので、枠にすると
    名刺を横切る線や空白だけの大きな枠になって読みにくい。文字の範囲が無い時だけ
    セルを使う。
    """
    # 画像の縁から取った枠・型を当てはめた枠（§7.8・7.9）は名刺の外形そのものなので、そのまま描く
    if str(getattr(card, "method", "")) in ("detected", "fitted"):
        return fitz.Rect(card.draw_rect)
    if getattr(card, "draw_text_rect", None) is None:
        return fitz.Rect(card.draw_rect)
    cell = fitz.Rect(card.draw_rect)
    text = fitz.Rect(card.draw_text_rect)
    framed = fitz.Rect(
        text.x0 - _FRAME_MARGIN, text.y0 - _FRAME_MARGIN,
        text.x1 + _FRAME_MARGIN, text.y1 + _FRAME_MARGIN,
    )
    return framed & cell if (framed & cell).is_valid and not (framed & cell).is_empty else framed


def render_split_pdf(src: Path, dest: Path, result: "PdfResult") -> Path:
    """src を開き、各名刺領域に枠とラベル・要確認マークを描いて dest に保存する。

    テキストの中身は描かない。NO_TEXT ページの上端には案内文字だけ書く。
    出力 PDF には元のテキスト層が残る（個人情報ファイルとして CSV と同じ扱い）。
    """
    doc = fitz.open(str(src))
    try:
        for page_no in result.skipped_pages:
            page = doc[page_no - 1]
            page.insert_text(
                (10.0, 20.0),
                "NO TEXT LAYER - skipped",
                fontsize=12.0,
                fontname="helv",
                color=_RED,
                rotate=page.rotation,
            )
        for card in result.cards:
            page = doc[card.page_no - 1]
            rect = _frame_rect(card)
            page.draw_rect(rect, color=_RED, width=1.5)
            _draw_label(page, rect, card.label)
            if card.fields.reasons:
                _draw_review_mark(page, rect)
        tmp = dest.with_name(dest.name + ".tmp")
        doc.save(str(tmp), garbage=1, deflate=True)
    finally:
        doc.close()
    tmp.replace(dest)
    return dest
