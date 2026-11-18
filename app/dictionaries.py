"""辞書ファイル（dict\\）の読み込みと検証。DESIGN.md §18.2・§19.1。

すべて UTF-8（BOM 可）。`#` で始まる行と空行は無視する。
語の辞書は old_kanji で先に読んだ対応表を使って `textnorm.normalize(...).norm` で正規化し、
長い順に並べて保持する（辞書は実行ごとに読み直す。I16）。

辞書の種類（ファイル名・形式・必須か・既定の語）は KINDS の 1 か所だけに書く。
辞書を読む入口は load_dictionaries の 1 つだけにする（全自動・確認画面・辞書の画面の保存前の検査・
試し読みが、同じ読み方を通る）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.models import BRANCH_LABELS, FAX_PREFIXES, ORG_SUFFIXES, TEL_PREFIXES, Dictionaries
from app.textnorm import normalize

FORM_WORDS = "words"            # 1 行 1 語
FORM_OLD_KANJI = "old_kanji"    # old,new の CSV
FORM_AREA_CODES = "area_codes"  # area_code,note の CSV


@dataclass(frozen=True)
class DictKind:
    """辞書の種類 1 つ。"""
    key: str                                # 識別子（Dictionaries の項目名と同じ）
    filename: str
    form: str
    required: bool = False                  # 語が 1 つも無いとエラーにする
    default: tuple[str, ...] | None = None  # ファイルが無い・語が無い時に使う既定の語（無ければ None）


KINDS: tuple[DictKind, ...] = (
    DictKind("old_kanji", "old_kanji.csv", FORM_OLD_KANJI),
    DictKind("titles", "titles.txt", FORM_WORDS, required=True),
    DictKind("corp_suffixes", "corp_suffixes.txt", FORM_WORDS, required=True),
    DictKind("dept_suffixes", "dept_suffixes.txt", FORM_WORDS),
    DictKind("branch_labels", "branch_labels.txt", FORM_WORDS, default=BRANCH_LABELS),
    DictKind("org_suffixes", "org_suffixes.txt", FORM_WORDS, default=ORG_SUFFIXES),
    DictKind("tel_prefixes", "tel_prefixes.txt", FORM_WORDS, default=TEL_PREFIXES),
    DictKind("fax_prefixes", "fax_prefixes.txt", FORM_WORDS, default=FAX_PREFIXES),
    DictKind("area_codes", "area_codes.csv", FORM_AREA_CODES),
)


def kind_of(key: str) -> DictKind:
    for kind in KINDS:
        if kind.key == key:
            return kind
    raise KeyError(key)


class DictionaryError(Exception):
    """辞書ファイルの読み込み・検証エラー。ファイル名と行番号をメッセージに入れる。"""


def _error(path: Path, line_no: int | None, message: str) -> DictionaryError:
    where = f"{path.name}:{line_no}" if line_no is not None else path.name
    return DictionaryError(f"{where}: {message}")


def _read_text(path: Path, *, optional: bool) -> str | None:
    """辞書ファイルの中身。optional が真で、ファイルが無い時は None。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        if optional:
            return None
        raise _error(path, None, "ファイルが見つかりません") from exc
    except OSError as exc:
        raise _error(path, None, f"読み込みに失敗しました: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise _error(path, None, f"UTF-8 として読めません: {exc}") from exc


def _data_lines(text: str | None) -> list[tuple[int, str]]:
    """`#` 始まりの行・空行を除いた (行番号, 内容) のリスト。行番号は 1 始まり。"""
    out: list[tuple[int, str]] = []
    for i, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        out.append((i, line))
    return out


def _sorted_words(words: list[str]) -> tuple[str, ...]:
    words.sort(key=len, reverse=True)
    return tuple(words)


def _parse_words(
    path: Path, text: str | None, old_kanji: dict[str, str], kind: DictKind,
) -> tuple[str, ...]:
    # 正規化すると空になる語は入れない（空の語は、どんな文字列にも一致してしまう）
    words = [normalize(content, old_kanji).norm for _, content in _data_lines(text)]
    words = [w for w in words if w.strip()]
    if not words:
        if kind.required:
            raise _error(path, None, "1 行以上の語が必要です")
        if kind.default is not None:
            # 既定の語にも、ファイルの語と同じ正規化と並べ替えを当てる
            words = [normalize(w, old_kanji).norm for w in kind.default]
    return _sorted_words(words)


def _parse_old_kanji(path: Path, text: str | None) -> dict[str, str]:
    lines = _data_lines(text)
    if not lines:
        return {}
    header_no, header = lines[0]
    header_cols = [c.strip().lower() for c in header.split(",")]
    if header_cols[:2] != ["old", "new"]:
        raise _error(path, header_no, "ヘッダは old,new である必要があります")

    mapping: dict[str, str] = {}
    for line_no, content in lines[1:]:
        parts = content.split(",", 1)
        if len(parts) != 2:
            raise _error(path, line_no, "old,new の 2 列が必要です")
        old, new = parts[0].strip(), parts[1].strip()
        if len(old) != 1 or len(new) != 1:
            raise _error(path, line_no, "old・new は各 1 文字である必要があります")
        mapping[old] = new
    return mapping


def _parse_area_codes(path: Path, text: str | None) -> frozenset[str]:
    lines = _data_lines(text)
    if not lines:
        return frozenset()
    header_no, header = lines[0]
    header_cols = [c.strip().lower() for c in header.split(",")]
    if header_cols[0] != "area_code":
        raise _error(path, header_no, "ヘッダは area_code,note である必要があります")

    codes: set[str] = set()
    for line_no, content in lines[1:]:
        parts = content.split(",", 1)
        code = parts[0].strip()
        if not code.isdigit() or not code.startswith("0") or not (2 <= len(code) <= 5):
            raise _error(path, line_no, "area_code は 0 始まり 2〜5 桁の数字である必要があります")
        codes.add(code)
    return frozenset(codes)


def load_dictionaries(dict_dir: Path, replace: Mapping[str, str] | None = None) -> Dictionaries:
    """dict_dir の辞書を読む。

    replace は「ファイル名 → 中身」。渡したファイルだけ、ディスクの代わりにその中身で読む
    （保存の前に、書き換えた後の辞書の一式を検査する・試し読みで変更後の辞書を作る、のに使う）。
    既定の語を持つ種類（KINDS の default）は、ファイルが無い・語が無い時に既定の語で動く。
    """
    def text_of(kind: DictKind) -> tuple[Path, str | None]:
        path = dict_dir / kind.filename
        if replace is not None and kind.filename in replace:
            return path, replace[kind.filename]
        return path, _read_text(path, optional=kind.default is not None)

    values: dict[str, object] = {}
    old_kanji = _parse_old_kanji(*text_of(kind_of("old_kanji")))
    values["old_kanji"] = old_kanji
    for kind in KINDS:
        if kind.form == FORM_WORDS:
            path, text = text_of(kind)
            values[kind.key] = _parse_words(path, text, old_kanji, kind)
        elif kind.form == FORM_AREA_CODES:
            values[kind.key] = _parse_area_codes(*text_of(kind))
    return Dictionaries(**values)
