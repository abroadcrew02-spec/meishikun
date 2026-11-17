"""通し番号 → ラベル文字列。DESIGN.md §7.7・§19.1。

Excel の列名と同じ全単射 26 進（bijective base-26）。import してよい
app モジュールは無い（共通ブリーフの import 許可リスト）。
"""
from __future__ import annotations

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def label(n: int) -> str:
    """n (>= 1) をラベル文字列にする。label(1) -> "A", label(26) -> "Z", label(27) -> "AA"。"""
    if n < 1:
        raise ValueError(f"n は 1 以上である必要があります: {n}")

    chars: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))
