"""辞書の試し読み。dev/dict_ui/design.md §7・§11.6。

保存の前に、手元の PDF（done\\ と input\\）を「今の辞書」と「変更後の辞書」の両方で読み、
結果が変わる名刺を数える。PDF は読むだけで、移動も変更もしない。

- 画面に返すのは件数と、比較表を置いたフォルダの名前だけ。名刺から読み取った文字は返さない
- どの名刺のどの項目がどう変わるかは、比較表（xlsx）に書く。比較表は一覧表と同じ扱いの
  個人情報ファイル（output\\ に置く。clear.bat で消える）
- ログには件数だけを書く（I9）
"""
from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app import extract, fileops, rows, writers
from app.models import COLUMNS, FileStatus

if TYPE_CHECKING:
    from app.models import CardRecord, Dictionaries, PdfResult, Settings

MAX_PDFS = 30                              # 試し読みに使う PDF の上限（新しい順）
MAX_PDF_BYTES = 200 * 1024 * 1024          # これより大きい PDF は、試し読みに使わない
_FOLDER_RE = re.compile(r"辞書の試し読み_[0-9]{8}_[0-9]{6}(?:_[0-9]{1,4}){0,2}")
FOLDER_PREFIX = "辞書の試し読み_"
COMPARE_NAME = "比較.xlsx"
SHEET_TITLE = "比較"
HEADER = ("PDF", "ページ", "ラベル", "項目", "変更前", "変更後")
SPLIT_ITEM = "名刺の分け方"                # 分け方が変わったページの行の「項目」
# 一覧表の列のうち、比べないもの（名刺の対応を取るのに使う列と、枠の座標）
_SKIP_COLUMNS = frozenset({"ファイル名", "ページ", "ラベル", "領域座標"})
_COMPARED = tuple((i, name) for i, name in enumerate(COLUMNS) if name not in _SKIP_COLUMNS)


@dataclass
class TrialResult:
    pdfs: int = 0                 # 読んだ PDF の数
    cards: int = 0                # 比べた名刺の数（今の辞書で読んだ枚数）
    unreadable: int = 0           # 読めなかった PDF の数（飛ばした）
    changed_cards: int = 0        # どれかの項目が変わる名刺の数
    changed_pages: int = 0        # 名刺の分け方が変わるページの数
    fields: dict[str, int] = field(default_factory=dict)   # 項目 → その項目が変わる名刺の数
    folder: str = ""              # 比較表を置いたフォルダの名前（output\\ の中。変化が無ければ ""）
    cancelled: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "pdfs": self.pdfs, "cards": self.cards, "unreadable": self.unreadable,
            "changed_cards": self.changed_cards, "changed_pages": self.changed_pages,
            "fields": [{"name": name, "cards": n} for name, n in self.fields.items()],
            "folder": self.folder, "file": COMPARE_NAME if self.folder else "", "cancelled": self.cancelled,
        }


def select_pdfs(settings: "Settings") -> list[Path]:
    """試し読みに使う PDF。done\\ と input\\ の直下から、更新の新しい順に MAX_PDFS 本まで。

    中身が同じ PDF（同じものを何度も読み込んだ時の写し）は 1 本にする。読めないファイルは飛ばす。
    """
    found: list[tuple[int, str, Path]] = []
    for folder in (settings.paths.done, settings.paths.input):
        pdfs, _ignored = fileops.list_input_pdfs(folder)
        for path in pdfs:
            try:
                found.append((path.stat().st_mtime_ns, path.name.casefold(), path))
            except OSError:
                continue
    found.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[Path] = []
    seen: set[str] = set()
    for _mtime, _name, path in found:
        if len(chosen) >= MAX_PDFS:
            break
        try:
            if path.stat().st_size > MAX_PDF_BYTES:
                continue
            with path.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()     # 少しずつ読む（丸ごと抱えない）
        except OSError:
            continue
        if digest in seen:
            continue
        seen.add(digest)
        chosen.append(path)
    return chosen


def _by_page(result: "PdfResult") -> dict[int, list["CardRecord"]]:
    pages: dict[int, list["CardRecord"]] = {}
    for card in result.cards:
        pages.setdefault(card.page_no, []).append(card)
    return pages


def _compare_pdf(
    name: str, before: "PdfResult", after: "PdfResult", settings: "Settings",
    result: TrialResult, out_rows: list[list[str]],
) -> None:
    """1 本の PDF の結果を比べ、件数を result に足し、違いを out_rows に足す。"""
    pages_before, pages_after = _by_page(before), _by_page(after)
    result.cards += len(before.cards)
    for page_no in sorted(set(pages_before) | set(pages_after)):
        cards_b, cards_a = pages_before.get(page_no, []), pages_after.get(page_no, [])
        # 名刺の対応は、ラベルではなく (枠, 方法) の並びで取る（前のページで分け方が変わると、ラベルがずれる）
        if [(c.rect, c.method) for c in cards_b] != [(c.rect, c.method) for c in cards_a]:
            result.changed_pages += 1
            out_rows.append([name, str(page_no), "", SPLIT_ITEM, f"{len(cards_b)} 枚", f"{len(cards_a)} 枚"])
            continue
        for card_b, card_a in zip(cards_b, cards_a):
            row_b, row_a = rows.to_row(card_b, settings.output), rows.to_row(card_a, settings.output)
            changed = False
            for index, column in _COMPARED:
                if row_b[index] != row_a[index]:
                    changed = True
                    result.fields[column] = result.fields.get(column, 0) + 1
                    out_rows.append([name, str(page_no), card_b.label, column, row_b[index], row_a[index]])
            result.changed_cards += changed


def run_trial(
    settings: "Settings", before: "Dictionaries", after: "Dictionaries", pdfs: list[Path], run_id: str,
    *, stop: Callable[[], bool] = lambda: False, progress: Callable[[int, int], None] = lambda done, total: None,
) -> TrialResult:
    """pdfs を 2 つの辞書で読み比べる。stop() が真になったら、読んでいる PDF の後で止める。

    比較表は、違いが 1 つでもある時だけ書く。止められた時は書かない。
    """
    result = TrialResult()
    out_rows: list[list[str]] = []
    total = len(pdfs)
    for n, pdf in enumerate(pdfs):
        if stop():
            result.cancelled = True
            return result
        progress(n, total)
        # PDF は 1 回だけ読む（文字の読み取りと枠の検出は、辞書に依らない）。読み終えたら結果だけを残す
        res_before, res_after = extract.compare_pdf(pdf, settings, before, after)
        if res_before.status == FileStatus.ERROR or res_after.status == FileStatus.ERROR:
            result.unreadable += 1
            continue
        result.pdfs += 1
        _compare_pdf(pdf.name, res_before, res_after, settings, result, out_rows)
    progress(total, total)
    if stop():
        result.cancelled = True
        return result
    # 項目は、一覧表の列の順に並べる
    result.fields = {name: result.fields[name] for _i, name in _COMPARED if name in result.fields}
    if out_rows:
        folder = fileops.unique_dir(settings.paths.output, f"{FOLDER_PREFIX}{run_id}")
        folder.mkdir(parents=True)
        try:
            writers.write_xlsx(folder / COMPARE_NAME, [list(HEADER), *out_rows], title=SHEET_TITLE)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)    # 書きかけの比較表（名刺の内容が入る）を残さない
            raise
        result.folder = folder.name
    return result


def remove_folder(settings: "Settings", name: str) -> bool:
    """前の試し読みの比較表のフォルダを消す（名刺の内容の写しを増やさない）。消せたら True。

    name は、この実行の run_trial が返したフォルダの名前だけを渡す。
    """
    if not _FOLDER_RE.fullmatch(name):      # 名前の形を全体で確かめる（末尾の「.」などを通さない）
        return False
    try:
        shutil.rmtree(settings.paths.output / name)
    except FileNotFoundError:
        return True         # もう無い（clear.bat で消した後など）
    except OSError:
        return False        # Excel で開いている など。残して、利用者に知らせる
    return True
