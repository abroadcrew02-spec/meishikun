"""出力値の整形。DESIGN.md §12・§19.1。

patterns.py は import しない（共通ブリーフの import 許可リスト）。電話番号の
数字列 (digits) は呼び出し側（classifier・patterns）が OCR 補正・+81 処理を
済ませた後の、純粋な数字文字列であることを前提にする。
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from app.models import Dictionaries, Reason

_MOBILE_PREFIXES = ("070", "080", "090", "050")
_FOUR_THREE_THREE_PREFIXES = ("0120", "0570")
# 0 で始まる 10 桁でも、市外局番ではない番号（伝言・テレドーム・情報料の代理徴収）。
# 市外局番の一覧に 017・018・099 があるので、区切らずに「要確認」にする
_NOT_AREA_CODE_PREFIXES = ("0170", "0180", "0990")
_AREA_CODE_LENGTHS = (5, 4, 3, 2)
_AREA_RULE_LENGTHS = (6, 5, 4, 3)

_GROUP_SPLIT_RE = re.compile(r"[-()\s]+")

_URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$")
_URL_HOST_END_RE = re.compile(r"[/?#]")

_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _find_area_code(digits: str, d: Dictionaries) -> str | None:
    """固定電話の番号（数字だけ）の市外局番。分からなければ None。

    先に区切りの例外の表（「この数字で始まる番号の市外局番は…」。長い方が優先）を見て、無ければ
    市外局番の一覧を長い順に当てはめる。一覧だけだと、短い局番と頭の数字が同じ番号を誤る
    （04-71xx-xxxx を 047-1xx-xxxx に、022-3xx-xxxx を 0223-xx-xxxx に区切ってしまう）。

    例外の表が指す局番が一覧に無い時（利用者が辞書の画面でその局番を消した時）は、分からないとする
    （消した局番が、表のせいで効き続けないようにする。一覧のほかの局番に当て直すと、誤った区切りになる）。
    """
    if digits.startswith(_NOT_AREA_CODE_PREFIXES):
        return None
    for length in _AREA_RULE_LENGTHS:
        area = d.area_rules.get(digits[:length])
        if area is not None:
            return area if area in d.area_codes else None
    for length in _AREA_CODE_LENGTHS:
        prefix = digits[:length]
        if prefix in d.area_codes:
            return prefix
    return None


def _printed_fixed_line(printed: str, digits: str) -> str | None:
    """固定電話の印字の区切り（§12.1）。

    `printed` が `-` `(` `)` 空白で 3 群に分かれ、桁数が (a, b, 4)・a+b=6・2≤a≤5 で、
    数字をつなぐと digits に一致するなら `a-b-c` の形で返す。名刺に印字された
    区切りは正しいとみなし、辞書より優先する（2026-10-06。暫定辞書の欠けで
    区切りを誤るのを防ぐ）。
    """
    groups = [g for g in _GROUP_SPLIT_RE.split(printed) if g]
    if len(groups) != 3 or not all(g.isdigit() for g in groups):
        return None
    a, b, c = (len(g) for g in groups)
    if c != 4 or a + b != 6 or not 2 <= a <= 5:
        return None
    if "".join(groups) != digits:
        return None
    return "-".join(groups)


def format_phone(digits: str, printed: str, d: Dictionaries) -> tuple[str, list[Reason]]:
    """DESIGN.md §12.1 の表どおりに整形する。"""
    n = len(digits)

    # 0800（フリーダイヤルの一種）は 080 携帯より先に見る（digits[:3] == "080" と
    # 重なるため、4 桁接頭辞の判定を 3 桁より先に確定させる）。
    if n == 11 and digits[:4] == "0800":
        return f"{digits[0:4]}-{digits[4:7]}-{digits[7:11]}", []

    if n == 11 and digits[:3] in _MOBILE_PREFIXES:
        return f"{digits[0:3]}-{digits[3:7]}-{digits[7:11]}", []

    if n == 10 and digits[:4] in _FOUR_THREE_THREE_PREFIXES:
        return f"{digits[0:4]}-{digits[4:7]}-{digits[7:10]}", []

    if n == 10 and digits[:1] == "0":
        as_printed = _printed_fixed_line(printed, digits)
        if as_printed is not None:
            return as_printed, []
        area = _find_area_code(digits, d)
        if area is not None:
            local = digits[len(area):6]
            subscriber = digits[6:10]
            return f"{area}-{local}-{subscriber}", []
        return digits, [Reason.AREA_CODE_UNKNOWN]

    return digits, [Reason.PHONE_DIGITS_INVALID]


def format_email(value: str) -> str:
    """ドメイン部だけ小文字にする（§12.2）。"""
    local, sep, domain = value.partition("@")
    if not sep:
        return value
    return f"{local}@{domain.lower()}"


def format_url(value: str) -> str:
    """スキームとホストだけ小文字にする。パスはそのまま。スキームが無ければ足さない（§12.3）。"""
    m = _URL_SCHEME_RE.match(value)
    if m:
        scheme, rest = m.groups()
        prefix = f"{scheme.lower()}://"
    else:
        rest = value
        prefix = ""
    end_match = _URL_HOST_END_RE.search(rest)
    split_at = end_match.start() if end_match else len(rest)
    host, path = rest[:split_at], rest[split_at:]
    return f"{prefix}{host.lower()}{path}"


def format_postal(digits: str) -> str:
    """郵便番号の数字列を NNN-NNNN にする。7 桁でなければ数字のまま（§12.4）。"""
    if len(digits) == 7 and digits.isdigit():
        return f"{digits[:3]}-{digits[3:]}"
    return digits


def clean_spaces(s: str) -> str:
    """空白の連続を U+0020 1 個にし、前後を除く（§12.5）。"""
    collapsed = _WHITESPACE_RUN_RE.sub(" ", s)
    return collapsed.strip(" ")


def compress_name_spaces(raw: str, gaps: Sequence[float], dominance: float) -> str:
    """氏名の字間圧縮（§11.6-7）。gaps は raw と同じ長さ、空白位置以外は 0.0。"""
    space_idx = [i for i, ch in enumerate(raw) if ch.isspace()]

    if not space_idx:
        return raw

    if len(space_idx) == 1:
        i = space_idx[0]
        return raw[:i] + " " + raw[i + 1:]

    space_gaps = [gaps[i] for i in space_idx]
    largest, second = sorted(space_gaps, reverse=True)[:2]

    keep_idx: int | None = None
    if largest >= dominance * second:
        for i in space_idx:
            if gaps[i] == largest:
                keep_idx = i
                break

    drop = set(space_idx)
    if keep_idx is not None:
        drop.discard(keep_idx)

    out_chars: list[str] = []
    for i, ch in enumerate(raw):
        if i in drop:
            continue
        out_chars.append(" " if i == keep_idx else ch)
    return "".join(out_chars)
