"""1 行の正規化文字列から連絡先・会社名・役職・氏名らしさ・住所開始を検出する。
DESIGN.md §10。すべて TextLine 1 行の NormText（または norm 文字列）に対して動き、
norm 上の位置を返す。英字の照合は大文字小文字を区別しない。
"""
from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from app.models import (
    BRANCH_LABELS,
    FAX_PREFIXES,
    ORG_SUFFIXES,
    TEL_PREFIXES,
    ClassifyCfg,
    Dictionaries,
    Hit,
    HitKind,
    NormText,
)
from app.textnorm import compact_view, fix_ocr_digits

# ---- §10.8: 都道府県・拠点ラベル ----

PREFECTURES: tuple[str, ...] = (
    "北海道",
    "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)

# 拠点の語（BRANCH_LABELS）・組織名の語尾（ORG_SUFFIXES）・電話と FAX の見出し（TEL_PREFIXES・FAX_PREFIXES）は
# 辞書ファイルから読む（§18.2）。既定の語は models にあり、ここでは同じ名前で参照する。
# 語の一覧を受け取る関数は `d: Dictionaries | None` を取る。None は既定の語。

# ---- §10.4: URL の TLD ----

URL_TLDS: tuple[str, ...] = (
    "jp", "com", "net", "org", "info", "biz", "io",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "lg.jp", "ed.jp", "gr.jp",
)

_MOBILE_WORDS = frozenset({"携帯", "mobile"})
_COMPOUND_TLDS = frozenset(t for t in URL_TLDS if "." in t)
_SIMPLE_TLDS = frozenset(t for t in URL_TLDS if "." not in t)

_PREF_ALT = "|".join(re.escape(p) for p in PREFECTURES)
_POSTAL_OPT = r"(?:〒?\s*[0-9]{3}\s*-?\s*[0-9]{4}\s*)?"


# ---- 辞書の語から作る正規表現（§18.2・I16） ----
#
# 語の組（tuple）を鍵にして作り置く。辞書は実行ごとに読み直すが、中身が同じなら作り直さない
# （中身で引くので、辞書を差し替えれば必ず効く）。


def _words(words: tuple[str, ...], default: tuple[str, ...]) -> tuple[str, ...]:
    """空の語（空白だけの語を含む）を除く。1 語も残らなければ既定の語。"""
    kept = tuple(w for w in words if w.strip())
    return kept or default


def _alternation(
    words: tuple[str, ...], default: tuple[str, ...], extra: tuple[str, ...] = (),
) -> str:
    """辞書の語から正規表現の選択肢を作る、ただ 1 つの入口。

    空の語を捨て、語は必ず re.escape を通す（空の選択肢が 1 つ入ると、どんな文字列にも一致する。
    `TEL(` のような語をそのままつなぐと、正規表現を組み立てられない）。extra は固定の正規表現の断片。
    """
    return "|".join([re.escape(w) for w in _words(words, default)] + list(extra))


@dataclass(frozen=True)
class _BranchRes:
    label: re.Pattern[str]              # 片の全体が拠点ラベル
    address_start: re.Pattern[str]      # 住所の開始（§10.8）
    mid_address_start: re.Pattern[str]  # 行の途中から始まる 2 件目の住所


@functools.lru_cache(maxsize=16)
def _branch_res(labels: tuple[str, ...]) -> _BranchRes:
    core = (
        r"[【\[(<「]?\S{0,8}?(?:" + _alternation(labels, BRANCH_LABELS) + r")[】\])>」]?[:：]?"
    )
    return _BranchRes(
        label=re.compile(r"^(?:" + core + r")$"),
        # 住所開始（§10.8）。拠点の語に無い任意のラベル（`サテライト★ミホン 東京都…`）も、
        # 空白で区切られていれば 12 文字まで許す。法人格を含む行は手順 2 で先に会社名になる。
        address_start=re.compile(
            r"^(?:(?:" + core + r")\s*|\S{1,12}\s+)?" + _POSTAL_OPT + r"(?:" + _PREF_ALT + r")"
        ),
        # 行の途中から始まる 2 件目の住所（拠点ラベル＋都道府県）。2 段組みが 1 行に連結された時用
        mid_address_start=re.compile(
            r"\s+((?:" + core + r")\s*" + _POSTAL_OPT + r"(?:" + _PREF_ALT + r"))"
        ),
    )


@dataclass(frozen=True)
class _PhoneRes:
    combo: re.Pattern[str]          # `TEL/FAX` のように、電話と FAX の見出しが並んだ形
    single: re.Pattern[str]         # 電話か FAX の見出し 1 つ
    fax_words: frozenset[str]       # FAX の見出し（casefold 済み）
    label_only: re.Pattern[str]     # 見出しだけの片（§11.1-2）
    label_before: re.Pattern[str]   # 記号の無い `NNN-NNNN` の直前の見出し（電話番号とみなす）


@functools.lru_cache(maxsize=16)
def _phone_res(tel: tuple[str, ...], fax: tuple[str, ...]) -> _PhoneRes:
    tel_words = _words(tel, TEL_PREFIXES)
    fax_words = _words(fax, FAX_PREFIXES)
    both = tel_words + fax_words
    tel_alt = _alternation(tel_words, TEL_PREFIXES)
    fax_alt = _alternation(fax_words, FAX_PREFIXES)
    # 1 文字の見出し（T・F）は、ここでは見出しにしない（`T 100-0001` の T は 〒 の読み違い）
    long_words = tuple(w for w in both if len(w) >= 2)
    return _PhoneRes(
        combo=re.compile(r"(" + tel_alt + r")\s*[/・&､、,]\s*(" + fax_alt + r")$", re.IGNORECASE),
        single=re.compile(r"(" + tel_alt + r"|" + fax_alt + r")$", re.IGNORECASE),
        fax_words=frozenset(w.casefold() for w in fax_words),
        label_only=re.compile(
            r"^(?:" + _alternation(both, both, ("E-?mail", "Mail", "URL", "HP", "Web", "〒", "メール"))
            + r")?[\s:：/・|.,、。\-]*$",
            re.IGNORECASE,
        ),
        label_before=re.compile(
            r"(?:" + _alternation(long_words, (), ("PHONE", "ＦＡＸ")) + r")[\s:：.]*$", re.IGNORECASE,
        ),
    )


@functools.lru_cache(maxsize=16)
def _org_suffix_words(words: tuple[str, ...]) -> tuple[str, ...]:
    return _words(words, ORG_SUFFIXES)


def _branch(d: Dictionaries | None) -> _BranchRes:
    # tuple(...) は、語の一覧を list で持つ辞書（テストで手で組んだもの）でも cache の鍵にできるように
    return _branch_res(BRANCH_LABELS if d is None else tuple(d.branch_labels))


def _phone(d: Dictionaries | None) -> _PhoneRes:
    if d is None:
        return _phone_res(TEL_PREFIXES, FAX_PREFIXES)
    return _phone_res(tuple(d.tel_prefixes), tuple(d.fax_prefixes))


def warm(d: Dictionaries) -> None:
    """辞書の語から作る正規表現を、ここで組み立てる（保存の前に、組み立てられることを確かめるのに使う）。"""
    _branch(d)
    _phone(d)
    _org_suffix_words(tuple(d.org_suffixes))

# ---- 氏名に使える文字（§10.7） ----

_NAME_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
)
_NAME_EXTRA_CHARS = frozenset("々〆ヵヶー・ ")
_HIRAGANA_RANGE = (0x3041, 0x3096)
_KATAKANA_RANGE = (0x30A1, 0x30FA)


def _is_name_char(ch: str) -> bool:
    if ch in _NAME_EXTRA_CHARS:
        return True
    cp = ord(ch)
    if any(lo <= cp <= hi for lo, hi in _NAME_CJK_RANGES):
        return True
    if _HIRAGANA_RANGE[0] <= cp <= _HIRAGANA_RANGE[1]:
        return True
    if _KATAKANA_RANGE[0] <= cp <= _KATAKANA_RANGE[1]:
        return True
    return False


def _is_ascii_word_char(c: str) -> bool:
    return c.isascii() and c.isalnum()


def _is_company_ascii_char(c: str) -> bool:
    return (c.isascii() and c.isalnum()) or c in "&.,'-"


def _is_prefix_boundary_char(c: str) -> bool:
    """単独の T/F を接頭辞とみなしてよい直前の文字か（行頭・空白・記号のときだけ真）。"""
    if c.isspace():
        return True
    if c.isalnum():
        return False
    if _is_name_char(c):
        return False
    return True


# ---- 語の走査 ----


def _word_spans(norm: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"\S+", norm)]


def _find_all_titles(norm: str, word: str) -> list[tuple[int, int]]:
    """英字の語は前後が英字でないことを条件にする（§10.6）。"""
    is_ascii = word.isascii()
    hay = norm.casefold() if is_ascii else norm
    needle = word.casefold() if is_ascii else word
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = hay.find(needle, start)
        if idx == -1:
            break
        end = idx + len(needle)
        if is_ascii:
            before_ok = idx == 0 or not _is_ascii_word_char(norm[idx - 1])
            after_ok = end == len(norm) or not _is_ascii_word_char(norm[end])
            if before_ok and after_ok:
                spans.append((idx, end))
        else:
            spans.append((idx, end))
        start = idx + 1
    return spans


def _find_all_suffixes(norm: str, suffix: str) -> list[tuple[int, int]]:
    """法人格の語境界チェックなしの単純検索。英字の法人格が語に直接続く場合に備える（§10.5）。"""
    is_ascii = suffix.isascii()
    hay = norm.casefold() if is_ascii else norm
    needle = suffix.casefold() if is_ascii else suffix
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = hay.find(needle, start)
        if idx == -1:
            break
        spans.append((idx, idx + len(needle)))
        start = idx + 1
    return spans


def _word_at(spans: list[tuple[int, int]], ms: int, me: int) -> tuple[int, tuple[int, int]] | None:
    for i, (ws, we) in enumerate(spans):
        if ws <= ms and me <= we:
            return i, (ws, we)
    return None


_COMPANY_NAME_MAX_WORDS = 6     # 法人格の隣の語に加えて、英字の社名の語として足す語数の上限
# 英語の部署・拠点の語。会社名の後ろへ伸ばす時だけ、ここで止める（前の語も、部署名・拠点名の一部とみなす）。
# 社名の末尾にもなる語（Group・Center・Lab など）は入れない
_ENGLISH_UNIT_WORDS = frozenset({
    "dept", "department", "departments", "div", "division", "section", "branch", "office", "unit",
    "headquarters", "hq", "team",
})


def _is_english_unit_word(word: str) -> bool:
    """英語の部署・拠点の語か。後ろの句読点（`Dept.,`）と、囲む括弧（`(Dept.)`）は外して比べる。

    括弧つきの語は、英字の社名の語にならない（_is_english_text が通さない）。括弧を外すのは、次の語を先読みして
    「部署名の前の語」を見分ける時のため。
    """
    core = word.casefold().strip("()[]{}<>").rstrip(",;:").rstrip(".")
    return core in _ENGLISH_UNIT_WORDS


def _is_ascii_text(text: str) -> bool:
    return bool(text) and all(_is_company_ascii_char(c) for c in text)


def _is_english_text(text: str) -> bool:
    """英字の社名に使う文字だけでできているか（英数字・`& . , ' - +`・アクセントつきのラテン文字）。"""
    return bool(text) and all(_is_company_ascii_char(c) or c == "+" or "À" <= c <= "ɏ" for c in text)


def _is_type_only_katakana_word(word: str, d: Dictionaries) -> bool:
    """会社の種類を表すだけのカタカナの語（`ジャパン`・`ホールディングス`）か。

    この語の前は、英字のブランド名が続くことが多い（`Dummy ジャパン 株式会社`）。カタカナの社名
    （`ダミー 株式会社`）の前の英字は、英語の表記や部署名なので、さかのぼらない。
    """
    if not all("ァ" <= c <= "ヶ" or c in "ー・" for c in word):
        return False     # `商事`・`研究所` のような漢字の語尾の前の英字は、英語の表記や拠点名なので含めない
    return word == "ジャパン" or word in _org_suffix_words(tuple(d.org_suffixes))


def _has_company_type(word: str, d: Dictionaries) -> bool:
    folded = word.casefold()
    return any((s.casefold() in folded) if s.isascii() else (s in word) for s in d.corp_suffixes)


def _is_english_name_word(word: str, d: Dictionaries, extending_forward: bool = False) -> bool:
    """英字の社名の 1 語として、会社名に足してよい語か。

    英数字と `& . , ' - +`・アクセントつきのラテン文字だけでできていて、英字を 1 つ以上含む語
    （`&` だけの語は、語をつなぐ記号として通す）。
    数字だけの語・肩書き（`President`）・見出しだけの語（`TEL`・`URL`）・法人格を含む語（別の会社名）は、
    社名の語にしない。extending_forward（会社名の後ろへ伸ばす時）は、英語の部署・拠点の語（`Dept.`）も社名の語にしない。
    """
    if not _is_english_text(word) or _has_company_type(word, d):
        return False
    if sum(c.isdigit() for c in word) >= 6:
        return False     # 電話番号がくっついた語（`TEL03-0000-0001`）
    if extending_forward and _is_english_unit_word(word):
        return False
    if word != "&" and not any(c.isalpha() for c in word):
        return False
    return find_title(word, d) is None and not _phone(d).label_only.fullmatch(word)


def _extend_start_over_english_words(
    norm: str, spans: list[tuple[int, int]], first: int, start: int, d: Dictionaries,
) -> int:
    """spans[first] から前へ、英字の社名の語が続く間さかのぼった開始位置（1 語も無ければ start のまま）。

    `,` で終わる語は区切りなので、そこで止める。先頭に残った `&` だけの語は外す。
    """
    taken: list[int] = []
    j = first
    for _ in range(_COMPANY_NAME_MAX_WORDS):
        if j < 0:
            break
        word = norm[spans[j][0]:spans[j][1]]
        if word.endswith(",") or not _is_english_name_word(word, d):
            break
        taken.append(j)
        j -= 1
    while taken and norm[spans[taken[-1]][0]:spans[taken[-1]][1]] == "&":
        taken.pop()
    return spans[taken[-1]][0] if taken else start


def _extend_end_over_english_words(
    norm: str, spans: list[tuple[int, int]], first: int, end: int, d: Dictionaries,
) -> int:
    """spans[first] から後ろへ、英字の社名の語が続く間進んだ終了位置（1 語も無ければ end のまま）。

    次の語が役職の語か英語の部署・拠点の語なら、その語の手前で止める（`株式会社 Dummy Sales Manager` の `Sales`、
    `株式会社 Dummy Sales Dept.` の `Sales` は、役職・部署名の一部）。
    `,` で終わる語は区切りなので、そこで止める（`株式会社 Dummy Trading, Sales Manager`）。末尾に残った `&` だけの語は外す。
    """
    taken: list[int] = []
    k = first
    for _ in range(_COMPANY_NAME_MAX_WORDS):
        if k >= len(spans):
            break
        word = norm[spans[k][0]:spans[k][1]]
        if word.endswith(",") or not _is_english_name_word(word, d, extending_forward=True):
            break
        following = norm[spans[k + 1][0]:spans[k + 1][1]] if k + 1 < len(spans) else ""
        if following and (find_title(following, d) is not None or _is_english_unit_word(following)):
            break
        taken.append(k)
        k += 1
    while taken and norm[spans[taken[-1]][0]:spans[taken[-1]][1]] == "&":
        taken.pop()
    return spans[taken[-1]][1] if taken else end


def find_company(norm: str, d: Dictionaries) -> tuple[int, int] | None:
    """会社名の範囲。法人格（会社の種類の語）を含む語を軸に、名前の語を足す。

    - 法人格が単独の語: 前株（`株式会社 ダミー商事`）なら次の語、後株（`ダミー商事 株式会社`）なら前の語を足す。
      法人格の前にも後ろにも語がある時は、前株とみなす（今までどおり。`… 株式会社 東京支店` は、名前が前にあっても
      `株式会社 東京支店` になる。どちらの語が名前かを、文字の並びだけでは決められないため）
    - 法人格が語にくっついている（`ダミー商事株式会社`）: その語の全体
    - 英字の社名は、空白で区切った何語かでできている（`Dummy Trading Japan 株式会社`）。隣の 1 語だけを取ると
      `Japan 株式会社` で切れるので、英字の社名の語が続く間は、その語も足す（隣の語に加えて 6 語まで）
    - かな・漢字の語は、隣の 1 語より先へは足さない（部署名・拠点名・肩書きを巻き込まないため）。
      和文の社名の後ろの英字（`株式会社 ダミー商事 Tokyo Office`）も足さない。後株で隣が `ジャパン` のような
      会社の種類だけのカタカナの語の時だけ、その前の英字を足す（`Dummy ジャパン 株式会社`）
    - 法人格を含む語（別の会社名）を越えては足さない
    """
    spans = _word_spans(norm)

    def word(index: int) -> str:
        return norm[spans[index][0]:spans[index][1]]

    best: tuple[int, int] | None = None
    for suffix in d.corp_suffixes:
        is_ascii = suffix.isascii()
        for ms, me in _find_all_suffixes(norm, suffix):
            found = _word_at(spans, ms, me)
            if found is None:
                continue
            i, (ws, we) = found
            prev_i, next_i = i - 1, i + 1
            if is_ascii:
                # 英字の法人格: 直前に続く英字の語（§10.5）を語単位で遡って含める。
                start = ws
                j = prev_i
                while j >= 0 and _is_ascii_text(word(j)):
                    start = spans[j][0]
                    j -= 1
                candidate = (start, me)
            elif ms == ws and me == we:
                # 語が法人格だけ
                has_prev, has_next = prev_i >= 0, next_i < len(spans)
                if has_next:
                    # 前株: 次の語。その語が英字の社名の語なら、続く英字の語も（`,` で終わる語は区切りなので、その先は足さない）
                    end = spans[next_i][1]
                    neighbor = word(next_i)
                    if not neighbor.endswith(",") and _is_english_name_word(neighbor, d, extending_forward=True):
                        end = _extend_end_over_english_words(norm, spans, next_i + 1, end, d)
                    candidate = (ws, end)
                elif has_prev:
                    # 後株: 前の語。その語が英字の社名の語か、会社の種類だけのカタカナの語なら、その前に続く英字の語も
                    start = spans[prev_i][0]
                    if _is_english_name_word(word(prev_i), d) or _is_type_only_katakana_word(word(prev_i), d):
                        start = _extend_start_over_english_words(norm, spans, prev_i - 1, start, d)
                    candidate = (start, we)
                else:
                    candidate = (ws, we)
            else:
                # 法人格が名前の語にくっついている（`Japan株式会社`・`株式会社Dummy`）。その語の全体が会社名
                # （`Dummy Trading株式会社東京支店` のように後ろにくっついた拠点も、今までどおり含む）。
                # くっついた側が英字なら、その先に続く英字の社名の語も足す
                start, end = ws, we
                if _is_english_text(norm[ws:ms]):
                    start = _extend_start_over_english_words(norm, spans, prev_i, ws, d)
                if _is_english_text(norm[me:we]) and not norm[me:we].endswith(","):
                    end = _extend_end_over_english_words(norm, spans, next_i, we, d)
                candidate = (start, end)
            if best is None or (candidate[0], -(candidate[1] - candidate[0])) < (
                best[0], -(best[1] - best[0]),
            ):
                best = candidate
    return best


# 和文の文の語尾。日本語の文は空白で区切られないので、法人格を含む注記の文
# （`ダミーシステムは株式会社見本の登録商標です。`）が 1 語になり、会社名の候補になってしまう。
_SENTENCE_ENDINGS = ("です", "ます", "ません", "ました", "ください", "います", "あります")


# ひらがな（小書きのかな・`ゔ`・`ゕ`・`ゖ`・踊り字の `ゝ`・`ゞ` を含む）。
_HIRAGANA = "ぁ-ゖゝゞ"

# 文の中には、ひらがなの直後に漢字・カタカナ・英数字が続く所がある（`は株`・`の登`・`お問`）。
# `株式会社まるます`・`株式会社魚ます`・`株式会社ダミーです` のような社名には無い。`ー` と空白は数えない
_HIRAGANA_THEN_OTHER_RE = re.compile(rf"[{_HIRAGANA}][^{_HIRAGANA}ー\s]")

# 文の判定で両端から外す字。classifier が片の両端から落とす字（classifier._TRIM_CHARS）と同じにする。
# 行の全体から会社名の範囲を取る cardscore と、片から取る classifier とで、両端の `、`・`・` の有無が
# 違っても、同じ判定になるようにする（`株式会社見本あります、`）
SENTENCE_EDGE_CHARS = " \t/・|,、:："
_SENTENCE_EDGE_RE = re.compile(rf"^[{re.escape(SENTENCE_EDGE_CHARS)}\s]+|[{re.escape(SENTENCE_EDGE_CHARS)}\s]+$")


class SentenceKind(Enum):
    """会社名の範囲が文である時の、文とみなした根拠。"""

    PUNCTUATION = "punctuation"   # 句点（`。`）か読点（`、`）を含む
    ENDING = "ending"             # 句読点は無く、文の語尾とひらがなの並びだけを根拠にした


def sentence_kind(text: str) -> SentenceKind | None:
    """text（正規化済み。find_company が返した範囲）が文か、文ならその根拠。文でなければ None。

    両端の区切り（SENTENCE_EDGE_CHARS と空白）を外してから判定する。
    - PUNCTUATION: 句点（`。`）か読点（`、`）を含む
    - ENDING: 文の語尾（`です`・`ます` など）で終わり、かつ、ひらがなの直後にひらがな以外の字が続く所がある
      （`…は株式会社見本の登録商標です`）。語尾だけでは決めない（`ます`・`です` で終わる社名がある）
    """
    core = _SENTENCE_EDGE_RE.sub("", text)
    if "。" in core or "、" in core:
        return SentenceKind.PUNCTUATION
    if core.endswith(_SENTENCE_ENDINGS) and _HIRAGANA_THEN_OTHER_RE.search(core) is not None:
        return SentenceKind.ENDING
    return None


def is_sentence(text: str) -> bool:
    """text（正規化済み）が会社名ではなく文か（sentence_kind が None でない）。"""
    return sentence_kind(text) is not None


def company_candidate_flags(kinds: Sequence[SentenceKind | None]) -> list[bool]:
    """1 つの名刺（領域）の会社名の範囲ごとの文の判定から、どれを会社名の候補にするか。

    - 文でない範囲は、候補にする
    - 句点・読点を含む文は、いつでも候補にしない
    - 語尾だけを根拠にした文は、文でない範囲が 1 つ以上ある時だけ候補にしない。無い時は、今までどおり候補にする
      （`株式会社みらいの森です` のような、語尾とひらがなの並びが文に似た社名を空にしないため）

    classifier（項目の振り分け）と cardscore（領域の合図）の両方が使う。
    """
    has_plain = any(k is None for k in kinds)
    return [k is None or (k is SentenceKind.ENDING and not has_plain) for k in kinds]


# 辞書に無い「〜長」の語（班長・グループ長・ライン長 など）を役職とみなす（§10.6）。
# 空白で区切られた 2〜8 文字の CJK・かなの語で、末尾が「長」。役職でない「〜長」は除外する。
_SUFFIX_TITLE_RE = re.compile(r"(?<!\S)([一-龥々〆ヵヶぁ-んァ-ヶー]{1,7}長)(?!\S)")
_SUFFIX_TITLE_STOP = frozenset({
    "延長", "成長", "最長", "全長", "身長", "年長", "伸長", "増長", "波長", "特長", "冗長", "助長", "長長",
})


def find_title(norm: str, d: Dictionaries) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    for title in d.titles:
        for s, e in _find_all_titles(norm, title):
            if best is None or (s, -(e - s)) < (best[0], -(best[1] - best[0])):
                best = (s, e)
    if best is not None:
        return best
    for m in _SUFFIX_TITLE_RE.finditer(norm):
        if m.group(1) not in _SUFFIX_TITLE_STOP:
            return m.span(1)
    return None


def _starts_with_prefecture(norm: str) -> bool:
    return any(norm.startswith(p) for p in PREFECTURES)


def is_name_like(norm: str, cfg: ClassifyCfg, d: Dictionaries) -> bool:
    if not norm:
        return False
    if not all(_is_name_char(c) for c in norm):
        return False
    stripped = norm.replace(" ", "")
    if not stripped:
        return False

    n = len(stripped)
    simple_ok = cfg.name_min_chars <= n <= cfg.name_max_chars
    two_part_ok = False
    parts = [p for p in norm.split(" ") if p]
    if len(parts) == 2:
        if all(1 <= len(p) <= cfg.name_part_max_chars for p in parts) and (
            sum(len(p) for p in parts) <= 8
        ):
            two_part_ok = True
    if not (simple_ok or two_part_ok):
        return False

    if any(stripped.endswith(suf) for suf in d.dept_suffixes):
        return False
    if stripped.endswith(_org_suffix_words(tuple(d.org_suffixes))):
        return False
    if find_title(norm, d) is not None:
        return False
    if find_company(norm, d) is not None:
        return False
    if is_branch_label(norm, d):
        return False
    if _starts_with_prefecture(norm):
        return False
    return True


# ---- 氏名の候補が無い時の代わりの候補: ローマ字・カタカナの氏名の形（2026-11-20） ----

# 大文字 1 字＋小文字（`Taro`・`O'Neil`・`Ma'ia`）
_LATIN_CAPITALIZED_RE = re.compile(r"[A-Z](?:[a-z]+(?:'[a-z]+)?|'[A-Z][a-z]+)")
# 全部大文字で 2 字以上（`MIHON`・`O'NEIL`）
_LATIN_UPPER_RE = re.compile(r"[A-Z](?:'?[A-Z])+")
# 頭文字（`J.`）
_LATIN_INITIAL_RE = re.compile(r"[A-Z]\.")
# 地名・建物の語（氏名の語にしない）
_LATIN_PLACE_WORDS = frozenset({"tokyo", "osaka", "japan", "bldg", "building", "floor", "tower"})
_LATIN_NAME_MIN_WORDS = 2
_LATIN_NAME_MAX_WORDS = 3
# メールのドメイン・URL のホストと比べる語の最短の字数
_HOST_WORD_MIN_CHARS = 4

# カタカナの氏名の 1 語（ァ〜ヺ と ー）
_KATAKANA_NAME_WORD_RE = re.compile("[ァ-ヺー]{2,8}")
_KATAKANA_NAME_SEP_RE = re.compile(r"\s*・\s*|\s+")

# 役割の宛先（@ の前がこれらの語だけなら、氏名の根拠にしない）
_ROLE_MAILBOXES = frozenset({
    "info", "sales", "support", "contact", "office", "admin", "mail", "webmaster", "inquiry", "hello",
    "recruit", "pr", "hr", "service", "shop", "order", "customer", "press", "jobs", "career", "marketing",
    "general", "soumu", "eigyo", "help", "team", "staff", "contactus", "noreply", "inquiries", "recruiting",
})
# @ の前を語に分ける区切り（`.` `_` `-` `+` と数字）
_EMAIL_LOCAL_SPLIT_RE = re.compile(r"[._\-+0-9]+")
_EMAIL_PARTIAL_MIN_CHARS = 4    # これ以上の語は部分一致で比べる
_EMAIL_EXACT_CHARS = 3          # この字数の語は、区切った語と完全に一致した時だけ


def _alnum_fold(text: str) -> str:
    """英数字だけにして、大文字と小文字を区別しない形にする。"""
    return "".join(c for c in text.casefold() if c.isascii() and c.isalnum())


def _latin_word_kind(word: str) -> str | None:
    """ローマ字の氏名の 1 語の種類（"initial"・"capitalized"・"upper"）。氏名の語の形でなければ None。

    ハイフンでつないだ語（`Jean-Luc`）は、どの部分も大文字で始まる時だけ通す（`Minato-ku` は通さない）。
    """
    if _LATIN_INITIAL_RE.fullmatch(word):
        return "initial"
    kinds: list[str] = []
    for seg in word.split("-"):
        if _LATIN_CAPITALIZED_RE.fullmatch(seg):
            kinds.append("capitalized")
        elif _LATIN_UPPER_RE.fullmatch(seg):
            kinds.append("upper")
        else:
            return None
    return "capitalized" if "capitalized" in kinds else "upper"


def _split_latin_name_tokens(norm: str) -> list[str] | None:
    """空白で 2〜3 語に分け、最初の語の後ろのカンマ（`Mihon, Taro`）を外す。ほかの位置のカンマは None。"""
    tokens = norm.split()
    if not _LATIN_NAME_MIN_WORDS <= len(tokens) <= _LATIN_NAME_MAX_WORDS:
        return None
    words: list[str] = []
    for i, token in enumerate(tokens):
        if i == 0 and token.endswith(","):
            token = token[:-1]
        if "," in token:
            return None
        words.append(token)
    return words


def _is_excluded_latin_word(word: str, d: Dictionaries, host_keys: list[str]) -> bool:
    """法人格・見出し・英語の部署や拠点の語・地名や建物の語・メールのドメインや URL のホストの語か。"""
    if _has_company_type(word, d) or _phone(d).label_only.fullmatch(word) or _is_english_unit_word(word):
        return True
    key = _alnum_fold(word)
    if key in _LATIN_PLACE_WORDS:
        return True
    return len(key) >= _HOST_WORD_MIN_CHARS and any(key in host for host in host_keys)


def latin_name_words(
    norm: str, d: Dictionaries, hosts: tuple[str, ...] | list[str] = (),
) -> tuple[str, ...] | None:
    """ローマ字の氏名の形の片なら、その語（最初の語の後ろのカンマは外す）。形が合わなければ None。

    - 2〜3 語。各語は大文字 1 字＋小文字（`Taro`）か、全部大文字で 2 字以上（`MIHON`）か、頭文字（`J.`。1 つまで）。
      全部大文字の語と頭文字だけの片（`ABC DEF`）は合わない。アポストロフィ（`O'Neil`）は可
    - 役職の語・法人格・見出しの語（TEL・URL など）・英語の部署や拠点の語・地名や建物の語を含まない
    - hosts（その名刺のメールのドメインと URL のホスト）に入っている 4 字以上の語を含まない（社名・ブランド名）
    """
    words = _split_latin_name_tokens(norm)
    if words is None:
        return None
    kinds = [_latin_word_kind(w) for w in words]
    if None in kinds or kinds.count("initial") > 1 or "capitalized" not in kinds:
        return None
    if find_title(norm, d) is not None or find_company(norm, d) is not None:
        return None
    host_keys = [_alnum_fold(h) for h in hosts]
    for word, kind in zip(words, kinds):
        if kind != "initial" and _is_excluded_latin_word(word, d, host_keys):
            return None
    return tuple(words)


def is_katakana_name(norm: str, d: Dictionaries) -> bool:
    """カタカナの氏名の形の片か（空白か `・` で区切った 2 語。各語は 2〜8 字のカタカナ。`ー` を含む）。

    組織名の語尾・部署の語尾・拠点の語で終わる語と、役職の語を含む片は合わない
    （is_name_like と同じ語の一覧で除く）。
    """
    words = [w for w in _KATAKANA_NAME_SEP_RE.split(norm.strip()) if w]
    if len(words) != 2 or not all(_KATAKANA_NAME_WORD_RE.fullmatch(w) for w in words):
        return False
    org_suffixes = _org_suffix_words(tuple(d.org_suffixes))
    branch_labels = _words(tuple(d.branch_labels), BRANCH_LABELS)
    for w in words:
        if any(w.endswith(suf) for suf in d.dept_suffixes):
            return False
        if w.endswith(org_suffixes) or w.endswith(branch_labels):
            return False
    return find_title(norm, d) is None


def _is_role_mailbox(local: str) -> bool:
    parts = [p.casefold() for p in _EMAIL_LOCAL_SPLIT_RE.split(local) if p]
    return not parts or all(p in _ROLE_MAILBOXES for p in parts)


def email_matches_name(words: tuple[str, ...] | list[str], emails: list[str]) -> bool:
    """氏名の語のどれかが、メールアドレスの @ の前に入っているか。

    4 字以上の語は部分一致。3 字の語は、@ の前を `.` `_` `-` `+` と数字で区切った語と完全に一致した時だけ。
    どちらも英数字だけにして、大文字と小文字を区別せずに比べる。@ の前が役割の宛先（info・sales など）の
    メールは根拠にしない。
    """
    keys = [_alnum_fold(w) for w in words]
    for email in emails:
        local, sep, _domain = email.partition("@")
        if not sep or _is_role_mailbox(local):
            continue
        local_key = _alnum_fold(local)
        tokens = {_alnum_fold(t) for t in _EMAIL_LOCAL_SPLIT_RE.split(local)}
        for key in keys:
            if len(key) >= _EMAIL_PARTIAL_MIN_CHARS and key in local_key:
                return True
            if len(key) == _EMAIL_EXACT_CHARS and key in tokens:
                return True
    return False


def is_address_start(norm: str, has_postal: bool, d: Dictionaries | None = None) -> bool:
    if has_postal:
        return True
    return bool(_branch(d).address_start.match(norm))


def find_mid_address_start(
    norm: str, start: int, end: int, d: Dictionaries | None = None,
) -> int | None:
    """norm[start:end] の途中から始まる 2 件目の住所の開始位置（norm 上の絶対位置）。

    拠点ラベル＋（郵便番号）＋都道府県の並びを探す。無ければ None（§10.8）。
    """
    m = _branch(d).mid_address_start.search(norm, start, end)
    return m.start(1) if m else None


def is_branch_label(norm: str, d: Dictionaries | None = None) -> bool:
    return bool(_branch(d).label.match(norm))


def is_department(norm: str, d: Dictionaries) -> bool:
    """部署名らしい片か（§11.5b・2026-10-06）。

    空白を除いた文字列が `dept_suffixes`（部・課・室・係・本部・グループ…）で終わる
    （末尾の `付` は除いて見る: `事業部付`）。CJK・かなを含み 30 文字以内。拠点ラベル
    （`東京本社`）・都道府県で始まるもの・法人格を含むものは除く。
    """
    s = norm.replace(" ", "")
    if not s or len(s) > 30 or not any(_is_name_char(c) for c in s):
        return False
    if s.endswith("付") and len(s) > 1:
        s = s[:-1]
    if not any(s.endswith(suf) for suf in d.dept_suffixes):
        return False
    if is_branch_label(norm, d) or _starts_with_prefecture(norm):
        return False
    return find_company(norm, d) is None


def is_label_only(norm: str, d: Dictionaries | None = None) -> bool:
    if _phone(d).label_only.fullmatch(norm):
        return True
    return len(norm.strip()) <= 1


# ---- §10.1〜10.4: find_contacts ----


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    s, e = span
    return any(s < te and ts < e for ts, te in taken)


# 〒 付き。OCR が 〒 を `T` と読む形（`T100-0001`）も受ける。直前が英数字（`PT100…`）なら不可。
_POSTAL_MARK_RE = re.compile(r"(?<![A-Za-z0-9])(?:〒|T)\s*([0-9]{3})\s*-?\s*([0-9]{4})(?![0-9])")
# OCR が 〒 を `+` と読む形（`+100-0001`）。国際電話の表記（`+81-3-0000-0000` `+813-0000-0000`）と
# 区別するため、ハイフン入りの 3 桁-4 桁で、後ろに数字・ハイフンが続かないものだけを受ける。
_POSTAL_PLUS_RE = re.compile(r"(?<![A-Za-z0-9+])\+\s*([0-9]{3})-([0-9]{4})(?![0-9\-])")
# 記号無しの `NNN-NNNN`（2026-10-06・Q47）。前後が数字・ハイフンなら電話番号の一部なので不可
# （`090-1234-5678` の `090-1234` は後ろのハイフン、`1234-5678` は前のハイフンで外れる）。
# 空白を挟んだ文脈は _bare_postal_context_ok で見る（`(012) 345-6789` `TEL 345-6789` は電話）。
_POSTAL_BARE_RE = re.compile(r"(?<![0-9\-])([0-9]{3})-([0-9]{4})(?![0-9\-])")


def _bare_postal_context_ok(work: str, start: int, end: int, res: _PhoneRes) -> bool:
    """記号無しの `NNN-NNNN` が電話番号の一部・市外局番なしの電話でないか（空白を挟んで見る）。

    直前の見出しは、電話・FAX の見出しのうち 2 文字以上の語と、PHONE・ＦＡＸ（res.label_before）。
    """
    before = work[:start].rstrip()
    if before and (before[-1] in ")-" or before[-1].isdigit()):
        return False
    if res.label_before.search(before):
        return False
    after = work[end:].lstrip()
    if after and (after[0] in "-)" or after[0].isdigit()):
        return False
    return True


def _plus_postal_context_ok(work: str, end: int) -> bool:
    """`+NNN-NNNN` の後ろに、空白を挟んで数字が続かないか（`+852-2345 6789` のような国際電話を除く）。

    前は正規表現が見る（英数字・`+` の直後でないこと）。`(本社) +100-0001` や、同じ行の電話番号の後ろの
    `+100-0001` も郵便番号として受けるので、記号無しの形（_bare_postal_context_ok）ほど前を絞らない。
    """
    after = work[end:].lstrip()
    return not (after and (after[0] in "-)" or after[0].isdigit()))


def _find_postal_hits(
    work: str, changed: list[bool], taken: list[tuple[int, int]], res: _PhoneRes,
) -> list[Hit]:
    hits: list[Hit] = []
    marked = list(_POSTAL_MARK_RE.finditer(work))
    marked += [m for m in _POSTAL_PLUS_RE.finditer(work) if _plus_postal_context_ok(work, m.end())]
    for m in sorted(marked, key=lambda found: found.start()):
        span = m.span()
        if _overlaps(span, taken):
            continue
        value_start = m.start(1)
        digits = m.group(1) + m.group(2)
        hits.append(Hit(
            kind=HitKind.POSTAL, start=span[0], end=span[1],
            value_start=value_start, value_end=span[1],
            digits=digits, value="", prefixed=False, mobile_prefix=False,
            ocr_fixed=any(changed[span[0]:span[1]]),
        ))
        taken.append(span)

    for m2 in _POSTAL_BARE_RE.finditer(work):
        span = m2.span()
        if _overlaps(span, taken) or not _bare_postal_context_ok(work, span[0], span[1], res):
            continue
        digits = m2.group(1) + m2.group(2)
        hits.append(Hit(
            kind=HitKind.POSTAL, start=span[0], end=span[1],
            value_start=span[0], value_end=span[1],
            digits=digits, value="", prefixed=False, mobile_prefix=False,
            ocr_fixed=any(changed[span[0]:span[1]]),
        ))
        taken.append(span)

    hits.sort(key=lambda h: h.start)
    return hits


#   先頭そのものが 0・(0・+81 であることをマッチ条件に含める（先に別の数字（郵便番号
#   など）が続いていて、それに連なってしまうのを防ぐ。§10.2・※仮定: この境界は find_contacts
#   内で postal を先に確定させる前提に依存しない、正規表現だけでの自己完結を優先した）。
_NUMBER_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+81|\(0|0)(?:[0-9()\-]| (?=[0-9()\-]))*"
)
_TOKEN_RE = re.compile(r"\S+")

_Chunk = tuple[int, int, str]


def _candidate_number_groups(work: str) -> list[list[_Chunk]]:
    groups: list[list[_Chunk]] = []
    for run_m in _NUMBER_RUN_RE.finditer(work):
        run_text = run_m.group()
        run_start = run_m.start()
        chunks: list[_Chunk] = [
            (run_start + tm.start(), run_start + tm.end(), tm.group())
            for tm in _TOKEN_RE.finditer(run_text)
        ]
        cur: list[_Chunk] = []
        cur_digits = 0
        for cs, ce, ctext in chunks:
            if cur and cur_digits >= 10 and ctext[0] in "0(+":
                groups.append(cur)
                cur, cur_digits = [], 0
            cur.append((cs, ce, ctext))
            cur_digits += sum(c.isdigit() for c in ctext)
            if cur_digits >= 11:
                groups.append(cur)
                cur, cur_digits = [], 0
        if cur:
            groups.append(cur)
    return groups


def _group_span_and_digits(group: list[_Chunk]) -> tuple[int, int, str]:
    gstart = group[0][0]
    gend = group[-1][1]
    text = "".join(ctext for _, _, ctext in group)
    digits = "".join(c for c in text if c.isdigit())
    if group[0][2].startswith("+81"):
        digits = digits[2:]
        if not digits.startswith("0"):
            digits = "0" + digits
    return gstart, gend, digits


_DELIM_ONLY_RE = re.compile(r"^[\s/,、・]*$")
_TRAIL_STRIP_CHARS = " \t\r\n\u3000.:：)]）"



def _match_combo_prefix(trimmed: str, res: _PhoneRes) -> tuple[int, bool] | None:
    m = res.combo.search(trimmed)
    if m is None:
        return None
    tel_word = m.group(1)
    if len(tel_word) == 1:
        pos = m.start(1)
        if pos > 0 and not _is_prefix_boundary_char(trimmed[pos - 1]):
            return None
    return m.start(), tel_word.casefold() in _MOBILE_WORDS


def _match_single_prefix(trimmed: str, res: _PhoneRes) -> tuple[int, HitKind, bool] | None:
    m = res.single.search(trimmed)
    if m is None:
        return None
    word = m.group(1)
    wf = word.casefold()
    if len(word) == 1:
        pos = m.start()
        if pos > 0 and not _is_prefix_boundary_char(trimmed[pos - 1]):
            return None
    kind = HitKind.FAX if wf in res.fax_words else HitKind.PHONE
    return m.start(), kind, wf in _MOBILE_WORDS


def _resolve_phone_prefix(
    gap: str, gap_start: int, last_hit: Hit | None, res: _PhoneRes,
) -> tuple[HitKind, bool, bool, int | None]:
    if _DELIM_ONLY_RE.fullmatch(gap) and last_hit is not None and last_hit.kind in (
        HitKind.PHONE, HitKind.FAX, HitKind.PHONE_FAX,
    ):
        return last_hit.kind, last_hit.prefixed, last_hit.mobile_prefix, None

    trimmed = gap.rstrip(_TRAIL_STRIP_CHARS)
    if not trimmed:
        return HitKind.PHONE, False, False, None

    combo = _match_combo_prefix(trimmed, res)
    if combo is not None:
        label_start_rel, mobile = combo
        return HitKind.PHONE_FAX, True, mobile, gap_start + label_start_rel

    single = _match_single_prefix(trimmed, res)
    if single is not None:
        label_start_rel, kind, mobile = single
        return kind, True, mobile, gap_start + label_start_rel

    return HitKind.PHONE, False, False, None


def _mask_ranges(s: str, ranges: list[tuple[int, int]]) -> str:
    """ranges を空白で潰した同じ長さの文字列を返す（既に確定した hit の上で電話候補を探さない）。"""
    if not ranges:
        return s
    chars = list(s)
    for a, b in ranges:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def _last_hit_before(hits: list[Hit], pos: int) -> Hit | None:
    """pos より前で終わる hit のうち、最も後ろで終わるもの（同じ行の「前の hit」）。"""
    best: Hit | None = None
    for h in hits:
        if h.end <= pos and (best is None or h.end > best.end):
            best = h
    return best


def _find_phone_fax_hits(
    work: str, norm: str, changed: list[bool], taken: list[tuple[int, int]],
    postal_hits: list[Hit], res: _PhoneRes,
) -> list[Hit]:
    # 郵便番号など、既に確定した範囲の数字に連なって候補が膨らまないよう空白で潰す。
    masked_work = _mask_ranges(work, list(taken))
    groups = sorted(_candidate_number_groups(masked_work), key=lambda g: g[0][0])
    hits: list[Hit] = []
    timeline: list[Hit] = list(postal_hits)
    for group in groups:
        gstart, gend, digits = _group_span_and_digits(group)
        if _overlaps((gstart, gend), taken):
            continue
        prev = _last_hit_before(timeline, gstart)
        gap_start = prev.end if prev is not None else 0
        gap = norm[gap_start:gstart]
        prev_phone = prev if (
            prev is not None and prev.kind in (HitKind.PHONE, HitKind.FAX, HitKind.PHONE_FAX)
        ) else None
        kind, prefixed, mobile_prefix, label_start = _resolve_phone_prefix(
            gap, gap_start, prev_phone, res,
        )
        min_d, max_d = (8, 13) if prefixed else (10, 11)
        if not (min_d <= len(digits) <= max_d):
            continue
        hit_start = label_start if label_start is not None else gstart
        hit = Hit(
            kind=kind, start=hit_start, end=gend,
            value_start=gstart, value_end=gend,
            digits=digits, value="", prefixed=prefixed, mobile_prefix=mobile_prefix,
            ocr_fixed=any(changed[gstart:gend]),
        )
        hits.append(hit)
        taken.append((hit_start, gend))
        timeline.append(hit)
    return hits


# 否定後読みで、英数字の連なりの途中から照合を始めない（長い行で時間が 2 乗以上に伸びるのを防ぐ）
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"
)
_EMAIL_LABEL_RE = re.compile(r"(?:E-?mail|Mail|メール)[\s:：]*$", re.IGNORECASE)


def _find_email_hits(
    norm: str, compact: str, idx: list[int], taken: list[tuple[int, int]],
) -> list[Hit]:
    hits: list[Hit] = []
    for m in _EMAIL_RE.finditer(compact):
        cs, ce = m.span()
        last_label = m.group().rsplit(".", 1)[-1]
        if not (len(last_label) >= 2 and last_label.isalpha() and last_label.isascii()):
            continue
        norm_start, norm_end = idx[cs], idx[ce - 1] + 1
        if _overlaps((norm_start, norm_end), taken):
            continue
        label_m = _EMAIL_LABEL_RE.search(norm[:norm_start])
        hit_start = label_m.start() if label_m else norm_start
        hits.append(Hit(
            kind=HitKind.EMAIL, start=hit_start, end=norm_end,
            value_start=norm_start, value_end=norm_end,
            digits="", value=m.group(), prefixed=False, mobile_prefix=False, ocr_fixed=False,
        ))
        taken.append((hit_start, norm_end))
    hits.sort(key=lambda h: h.start)
    return hits


_URL_RE = re.compile(
    r"(?<![A-Za-z0-9\-.])"
    r"(?P<scheme>https?://)?(?P<host>(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,})"
    r"(?P<path>/[A-Za-z0-9\-._~%/?#=&+@!$'*,;:]*)?",
    re.IGNORECASE,
)
_URL_LABEL_RE = re.compile(r"(?:URL|HP|Web|ホームページ)[\s:：]*$", re.IGNORECASE)
_URL_TRAIL_RE = re.compile(r"[.,、。)]+$")


def _host_tld_ok(host: str) -> bool:
    labels = host.lower().split(".")
    if len(labels) >= 2 and ".".join(labels[-2:]) in _COMPOUND_TLDS:
        return True
    return labels[-1] in _SIMPLE_TLDS


def _find_url_hits(
    norm: str, compact: str, idx: list[int], taken: list[tuple[int, int]],
) -> list[Hit]:
    hits: list[Hit] = []
    for m in _URL_RE.finditer(compact):
        scheme = m.group("scheme")
        host = m.group("host")
        if not scheme and not host.lower().startswith("www.") and not _host_tld_ok(host):
            continue
        cs, ce = m.start(), m.end()
        text = compact[cs:ce]
        trail = _URL_TRAIL_RE.search(text)
        if trail:
            ce -= len(trail.group())
            text = text[: len(text) - len(trail.group())]
        if ce <= cs:
            continue
        norm_start, norm_end = idx[cs], idx[ce - 1] + 1
        if _overlaps((norm_start, norm_end), taken):
            continue
        label_m = _URL_LABEL_RE.search(norm[:norm_start])
        hit_start = label_m.start() if label_m else norm_start
        hits.append(Hit(
            kind=HitKind.URL, start=hit_start, end=norm_end,
            value_start=norm_start, value_end=norm_end,
            digits="", value=text, prefixed=False, mobile_prefix=False, ocr_fixed=False,
        ))
        taken.append((hit_start, norm_end))
    hits.sort(key=lambda h: h.start)
    return hits


def find_contacts(t: NormText, d: Dictionaries | None = None) -> list[Hit]:
    norm = t.norm
    work, changed = fix_ocr_digits(norm)
    compact, idx = compact_view(norm)
    res = _phone(d)

    taken: list[tuple[int, int]] = []
    hits: list[Hit] = []
    postal_hits = _find_postal_hits(work, changed, taken, res)
    hits.extend(postal_hits)
    hits.extend(_find_phone_fax_hits(work, norm, changed, taken, postal_hits, res))
    hits.extend(_find_email_hits(norm, compact, idx, taken))
    hits.extend(_find_url_hits(norm, compact, idx, taken))

    hits.sort(key=lambda h: h.start)
    return hits
