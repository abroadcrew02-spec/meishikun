"""CSV・xlsx の書き出し。

DESIGN.md §14.2 (CSV)・§14.3 (xlsx)・§19.2 (関数シグネチャ) を実装する。
app.models への実行時依存は持たない（受け取った rows をそのまま書くだけ）。
"""
from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

# 先頭がこれらの文字なら CSV インジェクション対策で ' を付ける。
_GUARD_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


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
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        # rows が空だと writer.writerow が一度も呼ばれず、utf-8-sig の BOM も
        # 書き出されないまま 0 バイトになる。空書き込みで BOM だけを先に出しておく。
        f.write("")
        writer = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        for row in rows:
            writer.writerow([guard_cell(str(cell)) for cell in row])
    tmp.replace(path)
    return path


def write_xlsx(path: Path, rows: Sequence[Sequence[str]]) -> Path:
    """全セル文字列型・書式 @ で xlsx を書く。

    openpyxl は "=" で始まる文字列を数式と推定するため、値を入れた後に
    data_type="s" で明示的に上書きする（DESIGN.md §24 M18 実測）。
    """
    tmp = path.with_name(path.name + ".tmp")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "名刺"
    for row in rows:
        values = [ILLEGAL_CHARACTERS_RE.sub("", str(cell)) for cell in row]
        ws.append(values)
    for row_cells in ws.iter_rows():
        for cell in row_cells:
            cell.data_type = "s"
            cell.number_format = "@"
    ws.freeze_panes = "A2"
    wb.save(tmp)
    tmp.replace(path)
    return path
