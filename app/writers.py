"""CSV・xlsx の書き出し。

DESIGN.md §14.2 (CSV)・§14.3 (xlsx)・§19.2 (関数シグネチャ) を実装する。
app.models への実行時依存は持たない（受け取った rows をそのまま書くだけ）。
"""
from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.writer.excel import ExcelWriter

# 先頭がこれらの文字なら CSV インジェクション対策で ' を付ける。
_GUARD_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _is_unwritable(ch: str) -> bool:
    """一覧表に書かない文字か（孤立サロゲートと、Unicode の非文字）。

    PDF の文字の対応表が壊れていると、読み取りがこうした符号位置を返すことがある。孤立サロゲートは
    UTF-8 にできず（CSV）、孤立サロゲートと U+FFFE・U+FFFF は XML に入れられず（xlsx）、書き出しが
    途中で止まる。ほかの非文字は書けるが、文字としての意味が無いので、CSV と xlsx でそろえて取り除く。
    """
    cp = ord(ch)
    return 0xD800 <= cp <= 0xDFFF or 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def clean_cell(value: object) -> str:
    """セルの値を文字列にして、書けない文字を取り除く。"""
    text = str(value)
    if any(_is_unwritable(ch) for ch in text):
        text = "".join(ch for ch in text if not _is_unwritable(ch))
    return text


def _discard(tmp: Path) -> None:
    """書きかけ（名刺の内容が入りうる）を消す。消せなくても、元の失敗を隠さない。"""
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def guard_cell(value: str) -> str:
    """セルの先頭が = + - @ TAB CR のいずれかなら先頭に ' を付ける。"""
    if value.startswith(_GUARD_PREFIXES):
        return "'" + value
    return value


def write_csv(path: Path, rows: Sequence[Sequence[str]]) -> Path:
    """UTF-8 BOM・CRLF・全項目引用符で CSV を書く。

    <path>.tmp に書いてから rename するので、書き込み途中の失敗で path が
    中途半端な内容になることはない。rows の先頭行をヘッダにするかどうかは
    呼び出し側の責務（このモジュールはヘッダを特別扱いしない）。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            # rows が空だと writer.writerow が一度も呼ばれず、utf-8-sig の BOM も
            # 書き出されないまま 0 バイトになる。空書き込みで BOM だけを先に出しておく。
            f.write("")
            writer = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
            for row in rows:
                writer.writerow([guard_cell(clean_cell(cell)) for cell in row])
        tmp.replace(path)
    except BaseException:
        _discard(tmp)
        raise
    return path


def write_xlsx(path: Path, rows: Sequence[Sequence[str]], title: str = "名刺") -> Path:
    """全セル文字列型・書式 @ で xlsx を書く。title はシートの名前。

    openpyxl は "=" で始まる文字列を数式と推定するため、値を入れた後に
    data_type="s" で明示的に上書きする（DESIGN.md §24 M18 実測）。
    """
    tmp = path.with_name(path.name + ".tmp")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for row in rows:
        # 書けない文字を先に取り除く。openpyxl は、シートの中身を Windows の一時フォルダに書いてから
        # まとめる。書く途中で止まると、その一時ファイル（名刺の内容が入る）が残ることがある
        values = [ILLEGAL_CHARACTERS_RE.sub("", clean_cell(cell)) for cell in row]
        ws.append(values)
    for row_cells in ws.iter_rows():
        for cell in row_cells:
            cell.data_type = "s"
            cell.number_format = "@"
    ws.freeze_panes = "A2"
    try:
        # いったんメモリに組み立てる。wb.save() は、保存の途中で失敗しても書き出し先を閉じない。
        # ファイルへ直に書かせると、開いたままの書きかけが残り、消すこともできない。
        # 書き出し先（zip）は with で必ず閉じる
        buf = io.BytesIO()
        with ZipFile(buf, "w", ZIP_DEFLATED, allowZip64=True) as archive:
            ExcelWriter(wb, archive).save()
        tmp.write_bytes(buf.getvalue())
        tmp.replace(path)
    except BaseException:
        _discard(tmp)
        raise
    finally:
        wb.close()
    return path
