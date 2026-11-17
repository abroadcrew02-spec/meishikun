"""辞書ファイル（dict\\）の読み込みと検証。DESIGN.md §18.2・§19.1。

5 本すべて UTF-8（BOM 可）。`#` で始まる行と空行は無視する。
titles / corp_suffixes / dept_suffixes は old_kanji で先に読んだ対応表を使って
`textnorm.normalize(...).norm` で正規化し、長い順に並べて保持する（辞書は
実行ごとに読み直す。I16）。
"""
from __future__ import annotations

from pathlib import Path

from app.models import Dictionaries
from app.textnorm import normalize


class DictionaryError(Exception):
    """辞書ファイルの読み込み・検証エラー。ファイル名と行番号をメッセージに入れる。"""


def _error(path: Path, line_no: int | None, message: str) -> DictionaryError:
    where = f"{path.name}:{line_no}" if line_no is not None else path.name
    return DictionaryError(f"{where}: {message}")


def _read_data_lines(path: Path) -> list[tuple[int, str]]:
    """`#` 始まりの行・空行を除いた (行番号, 内容) のリスト。行番号は 1 始まり。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise _error(path, None, "ファイルが見つかりません") from exc
    except OSError as exc:
        raise _error(path, None, f"読み込みに失敗しました: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise _error(path, None, f"UTF-8 として読めません: {exc}") from exc

    out: list[tuple[int, str]] = []
    for i, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        out.append((i, line))
    return out


def _load_word_list(path: Path, old_kanji: dict[str, str], *, required: bool) -> tuple[str, ...]:
    lines = _read_data_lines(path)
    if required and not lines:
        raise _error(path, None, "1 行以上の語が必要です")
    words = [normalize(content, old_kanji).norm for _, content in lines]
    words.sort(key=len, reverse=True)
    return tuple(words)


def _load_old_kanji(path: Path) -> dict[str, str]:
    lines = _read_data_lines(path)
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


def _load_area_codes(path: Path) -> frozenset[str]:
    lines = _read_data_lines(path)
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


def load_dictionaries(dict_dir: Path) -> Dictionaries:
    old_kanji = _load_old_kanji(dict_dir / "old_kanji.csv")
    titles = _load_word_list(dict_dir / "titles.txt", old_kanji, required=True)
    corp_suffixes = _load_word_list(dict_dir / "corp_suffixes.txt", old_kanji, required=True)
    dept_suffixes = _load_word_list(dict_dir / "dept_suffixes.txt", old_kanji, required=False)
    area_codes = _load_area_codes(dict_dir / "area_codes.csv")
    return Dictionaries(
        titles=titles,
        corp_suffixes=corp_suffixes,
        dept_suffixes=dept_suffixes,
        area_codes=area_codes,
        old_kanji=old_kanji,
    )
