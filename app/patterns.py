"""1 行の正規化文字列から連絡先・会社名・役職・氏名らしさ・住所開始を検出する。
DESIGN.md §10。すべて TextLine 1 行の NormText（または norm 文字列）に対して動き、
norm 上の位置を返す。英字の照合は大文字小文字を区別しない。
"""
from __future__ import annotations

import re

from app.models import ClassifyCfg, Dictionaries, Hit, HitKind, NormText
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

# 法人格の無い組織名（`見本銀行` `見本工業` `見本不動産協会`）を氏名らしい語にしないための語尾
# （§10.7・2026-10-06・Q50）。人名がこれらで終わることはまず無い。
ORG_SUFFIXES: tuple[str, ...] = (
    "銀行", "信用金庫", "信用組合", "証券", "保険", "大学", "高校", "学校", "学園", "病院", "医院",
    "協会", "組合", "連合会", "財団", "法人", "事務所", "商店", "商会", "商事", "物産", "工業", "産業",
    "興業", "鉱業", "製作所", "研究所", "電機", "電器", "電気", "不動産", "建設", "運輸", "通信",
    "出版", "印刷", "書店", "薬局", "ホールディングス", "グループ", "センター", "サービス",
    "システム", "システムズ", "エンジニアリング", "コーポレーション", "カンパニー", "ネットワーク",
    "テクノロジー", "テクノロジーズ", "ソリューション", "ソリューションズ", "クリニック",
)

BRANCH_LABELS: tuple[str, ...] = (
    "本社", "本店", "支社", "支店", "営業所", "事業所", "工場", "研究所", "センター", "オフィス",
)

# ---- §10.2: 電話・FAX の接頭辞 ----

TEL_PREFIXES: tuple[str, ...] = ("TEL", "T", "電話", "代表", "直通", "携帯", "MOBILE")
FAX_PREFIXES: tuple[str, ...] = ("FAX", "F")

# ---- §10.4: URL の TLD ----

URL_TLDS: tuple[str, ...] = (
    "jp", "com", "net", "org", "info", "biz", "io",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "lg.jp", "ed.jp", "gr.jp",
)

_MOBILE_WORDS = frozenset({"携帯", "mobile"})
_COMPOUND_TLDS = frozenset(t for t in URL_TLDS if "." in t)
_SIMPLE_TLDS = frozenset(t for t in URL_TLDS if "." not in t)

_PREF_ALT = "|".join(re.escape(p) for p in PREFECTURES)
_BRANCH_LABEL_WORDS_ALT = "|".join(re.escape(b) for b in BRANCH_LABELS)
_BRANCH_LABEL_CORE = (
    r"[【\[(<「]?\S{0,8}?(?:" + _BRANCH_LABEL_WORDS_ALT + r")[】\])>」]?[:：]?"
)

_BRANCH_LABEL_RE = re.compile(r"^(?:" + _BRANCH_LABEL_CORE + r")$")
_POSTAL_OPT = r"(?:〒?\s*[0-9]{3}\s*-?\s*[0-9]{4}\s*)?"
# 住所開始（§10.8）。拠点ラベル語に無い任意のラベル（`サテライト★ミホン 東京都…`）も、
# 空白で区切られていれば 12 文字まで許す。法人格を含む行は手順 2 で先に会社名になる。
_ADDRESS_START_RE = re.compile(
    r"^(?:(?:" + _BRANCH_LABEL_CORE + r")\s*|\S{1,12}\s+)?"
    + _POSTAL_OPT
    + r"(?:" + _PREF_ALT + r")"
)
# 行の途中から始まる 2 件目の住所（拠点ラベル＋都道府県）。2 段組みが 1 行に連結された時用
_MID_ADDRESS_START_RE = re.compile(
    r"\s+((?:" + _BRANCH_LABEL_CORE + r")\s*" + _POSTAL_OPT + r"(?:" + _PREF_ALT + r"))"
)

_LABEL_ONLY_RE = re.compile(
    r"^(?:TEL|FAX|T|F|E-?mail|Mail|URL|HP|Web|〒|電話|代表|直通|携帯|Mobile|メール)?"
    r"[\s:：/・|.,、。\-]*$",
    re.IGNORECASE,
)

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


def find_company(norm: str, d: Dictionaries) -> tuple[int, int] | None:
    spans = _word_spans(norm)
    best: tuple[int, int] | None = None
    for suffix in d.corp_suffixes:
        is_ascii = suffix.isascii()
        for ms, me in _find_all_suffixes(norm, suffix):
            found = _word_at(spans, ms, me)
            if found is None:
                continue
            i, (ws, we) = found
            if is_ascii:
                # 英字の法人格: 直前に続く英字の語（§10.5）を語単位で遡って含める。
                start = ws
                j = i - 1
                while j >= 0 and all(
                    _is_company_ascii_char(c) for c in norm[spans[j][0]:spans[j][1]]
                ):
                    start = spans[j][0]
                    j -= 1
                candidate = (start, me)
            elif ms == ws and me == we:
                # 語が法人格だけ: 前株なら次の語、無ければ後株として前の語を含める。
                if i + 1 < len(spans):
                    candidate = (ws, spans[i + 1][1])
                elif i - 1 >= 0:
                    candidate = (spans[i - 1][0], we)
                else:
                    candidate = (ws, we)
            else:
                candidate = (ws, we)
            if best is None or (candidate[0], -(candidate[1] - candidate[0])) < (
                best[0], -(best[1] - best[0]),
            ):
                best = candidate
    return best


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
    if stripped.endswith(ORG_SUFFIXES):
        return False
    if find_title(norm, d) is not None:
        return False
    if find_company(norm, d) is not None:
        return False
    if is_branch_label(norm):
        return False
    if _starts_with_prefecture(norm):
        return False
    return True


def is_address_start(norm: str, has_postal: bool) -> bool:
    if has_postal:
        return True
    return bool(_ADDRESS_START_RE.match(norm))


def find_mid_address_start(norm: str, start: int, end: int) -> int | None:
    """norm[start:end] の途中から始まる 2 件目の住所の開始位置（norm 上の絶対位置）。

    拠点ラベル＋（郵便番号）＋都道府県の並びを探す。無ければ None（§10.8）。
    """
    m = _MID_ADDRESS_START_RE.search(norm, start, end)
    return m.start(1) if m else None


def is_branch_label(norm: str) -> bool:
    return bool(_BRANCH_LABEL_RE.match(norm))


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
    if is_branch_label(norm) or _starts_with_prefecture(norm):
        return False
    return find_company(norm, d) is None


def is_label_only(norm: str) -> bool:
    if _LABEL_ONLY_RE.fullmatch(norm):
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
_PHONE_LABEL_BEFORE_RE = re.compile(
    r"(?:TEL|FAX|PHONE|MOBILE|電話|代表|直通|携帯|ＦＡＸ)[\s:：.]*$", re.IGNORECASE,
)


def _bare_postal_context_ok(work: str, start: int, end: int) -> bool:
    """記号無しの `NNN-NNNN` が電話番号の一部・市外局番なしの電話でないか（空白を挟んで見る）。"""
    before = work[:start].rstrip()
    if before and (before[-1] in ")-" or before[-1].isdigit()):
        return False
    if _PHONE_LABEL_BEFORE_RE.search(before):
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
    work: str, changed: list[bool], taken: list[tuple[int, int]],
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
        if _overlaps(span, taken) or not _bare_postal_context_ok(work, span[0], span[1]):
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

_TEL_ALT = "|".join(re.escape(w) for w in TEL_PREFIXES)
_FAX_ALT = "|".join(re.escape(w) for w in FAX_PREFIXES)
_COMBO_RE = re.compile(r"(" + _TEL_ALT + r")\s*[/・&､、,]\s*(" + _FAX_ALT + r")$", re.IGNORECASE)
_SINGLE_RE = re.compile(r"(" + _TEL_ALT + r"|" + _FAX_ALT + r")$", re.IGNORECASE)
_FAX_SET = frozenset(w.casefold() for w in FAX_PREFIXES)


def _match_combo_prefix(trimmed: str) -> tuple[int, bool] | None:
    m = _COMBO_RE.search(trimmed)
    if m is None:
        return None
    tel_word = m.group(1)
    if len(tel_word) == 1:
        pos = m.start(1)
        if pos > 0 and not _is_prefix_boundary_char(trimmed[pos - 1]):
            return None
    return m.start(), tel_word.casefold() in _MOBILE_WORDS


def _match_single_prefix(trimmed: str) -> tuple[int, HitKind, bool] | None:
    m = _SINGLE_RE.search(trimmed)
    if m is None:
        return None
    word = m.group(1)
    wf = word.casefold()
    if len(word) == 1:
        pos = m.start()
        if pos > 0 and not _is_prefix_boundary_char(trimmed[pos - 1]):
            return None
    kind = HitKind.FAX if wf in _FAX_SET else HitKind.PHONE
    return m.start(), kind, wf in _MOBILE_WORDS


def _resolve_phone_prefix(
    gap: str, gap_start: int, last_hit: Hit | None,
) -> tuple[HitKind, bool, bool, int | None]:
    if _DELIM_ONLY_RE.fullmatch(gap) and last_hit is not None and last_hit.kind in (
        HitKind.PHONE, HitKind.FAX, HitKind.PHONE_FAX,
    ):
        return last_hit.kind, last_hit.prefixed, last_hit.mobile_prefix, None

    trimmed = gap.rstrip(_TRAIL_STRIP_CHARS)
    if not trimmed:
        return HitKind.PHONE, False, False, None

    combo = _match_combo_prefix(trimmed)
    if combo is not None:
        label_start_rel, mobile = combo
        return HitKind.PHONE_FAX, True, mobile, gap_start + label_start_rel

    single = _match_single_prefix(trimmed)
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
    postal_hits: list[Hit],
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
            gap, gap_start, prev_phone,
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


def find_contacts(t: NormText) -> list[Hit]:
    norm = t.norm
    work, changed = fix_ocr_digits(norm)
    compact, idx = compact_view(norm)

    taken: list[tuple[int, int]] = []
    hits: list[Hit] = []
    postal_hits = _find_postal_hits(work, changed, taken)
    hits.extend(postal_hits)
    hits.extend(_find_phone_fax_hits(work, norm, changed, taken, postal_hits))
    hits.extend(_find_email_hits(norm, compact, idx, taken))
    hits.extend(_find_url_hits(norm, compact, idx, taken))

    hits.sort(key=lambda h: h.start)
    return hits
