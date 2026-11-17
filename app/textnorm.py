"""判定用正規化と文字オフセット対応表。DESIGN.md §9。

normalize() は「展開」（1 単位 → n 文字）と「1:1 置換」だけを行うので、
`NormText.src` は常に非減少になる（I3）。
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from app.models import NormText

# DESIGN.md §9.2-4 の列。NFKC 後に残る/残り得るダッシュ類。
# 一部（NON-BREAKING HYPHEN・SMALL HYPHEN-MINUS・半角ー）は実際には NFKC で
# 他の文字（U+2010 / '-' / U+30FC）へ変わるため正規化後には現れないが、
# DESIGN.md の列をそのまま転記する（現れなくても害はない）。
DASH_CHARS: frozenset[str] = frozenset(
    "‐‑‒–—―−ー─━﹣ｰ"
)

_HALFWIDTH_VOICE_MARKS = ("ﾞ", "ﾟ")

_OCR_DIGIT_RE = re.compile(r"(?<![A-Za-z])[0-9OoIl|][0-9OoIl|\-() ]{5,}(?<! )(?![A-Za-z])")

_OCR_O = ("O", "o")
_OCR_I = ("I", "l", "|")

_COMPACT_SYMS = ("@", ".", ":", "/")


def _split_units(raw: str) -> list[tuple[int, int]]:
    """raw を「基底文字＋後続の結合文字・半角濁点/半濁点」の単位に分ける。"""
    units: list[tuple[int, int]] = []
    n = len(raw)
    i = 0
    while i < n:
        start = i
        i += 1
        while i < n and (unicodedata.combining(raw[i]) != 0 or raw[i] in _HALFWIDTH_VOICE_MARKS):
            i += 1
        units.append((start, i))
    return units


def normalize(raw: str, old_kanji: Mapping[str, str]) -> NormText:
    units = _split_units(raw)

    norm_chars: list[str] = []
    src: list[tuple[int, int]] = []
    for start, end in units:
        unit_norm = unicodedata.normalize("NFKC", raw[start:end])
        for ch in unit_norm:
            norm_chars.append(ch)
            src.append((start, end))

    # 旧字体置換（1 文字 → 1 文字。位置は変わらない）
    for i, ch in enumerate(norm_chars):
        replacement = old_kanji.get(ch)
        if replacement is not None:
            norm_chars[i] = replacement

    # ハイフン統一（数字文脈のみ。1 文字 → 1 文字）
    n = len(norm_chars)
    for i, ch in enumerate(norm_chars):
        if ch not in DASH_CHARS:
            continue
        prev_ok = False
        j = i - 1
        while j >= 0:
            if norm_chars[j].isspace():
                j -= 1
                continue
            prev_ok = norm_chars[j].isdigit() or norm_chars[j] == ")"
            break
        if not prev_ok:
            continue
        next_ok = False
        k = i + 1
        while k < n:
            if norm_chars[k].isspace():
                k += 1
                continue
            next_ok = norm_chars[k].isdigit() or norm_chars[k] == "("
            break
        if next_ok:
            norm_chars[i] = "-"

    return NormText(raw=raw, norm="".join(norm_chars), src=tuple(src))


def fix_ocr_digits(norm: str) -> tuple[str, list[bool]]:
    """OCR で誤認されやすい数字（O/o→0, I/l/|→1）を補正する（作業用。位置は変わらない）。"""
    chars = list(norm)
    changed = [False] * len(norm)
    for m in _OCR_DIGIT_RE.finditer(norm):
        s, e = m.span()
        segment = norm[s:e]
        digit_count = sum(1 for c in segment if "0" <= c <= "9")
        if digit_count < 4:
            continue
        for i in range(s, e):
            c = chars[i]
            if c in _OCR_O:
                chars[i] = "0"
                changed[i] = True
            elif c in _OCR_I:
                chars[i] = "1"
                changed[i] = True
    return "".join(chars), changed


def compact_view(norm: str) -> tuple[str, list[int]]:
    """`@` `.` `:` `/` に隣接する空白だけを除いた文字列と、位置の対応表を返す。"""
    n = len(norm)
    out_chars: list[str] = []
    out_idx: list[int] = []
    for i, c in enumerate(norm):
        if c == " ":
            prev_sym = i > 0 and norm[i - 1] in _COMPACT_SYMS
            next_sym = i + 1 < n and norm[i + 1] in _COMPACT_SYMS
            if prev_sym or next_sym:
                continue
        out_chars.append(c)
        out_idx.append(i)
    return "".join(out_chars), out_idx
