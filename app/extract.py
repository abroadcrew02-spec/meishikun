"""1 ファイルの抽出。DESIGN.md §4.2・§19.1 を実装する。

extract_pdf は KeyboardInterrupt 以外の例外を外に出さない。失敗は
status=ERROR の PdfResult で返す。

- I6: 1 ファイルの抽出は全か無か。ERROR の時 cards は空にする。
- I9: error_type には例外クラス名だけを入れる。例外メッセージや抽出値は
  保持しない。
- I12: ラベルは PDF 内の通し番号で、segment_page が返す順（読み順）に振る。

確認画面（app.review）のために、下読み（prepare_pdf: ページを読んで枠を自動で決める）
と振り分け（build_result: 枠ごとに項目を決める）を別々にも呼べる。extract_pdf は
ページごとに下読みと振り分けを続けて行う（全ページを抱えない）。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app import classifier, labels, pdf_reader, segmenter
from app.models import (
    CardRecord,
    ErrorKind,
    FileStatus,
    PageKind,
    PdfResult,
    PreparedPage,
    PreparedPdf,
)

if TYPE_CHECKING:
    from app.models import Dictionaries, Region, Settings


def _error(path: Path, kind: ErrorKind, exc: BaseException, page_count: int = 0) -> PdfResult:
    return PdfResult(
        file_name=path.name,
        path=path,
        status=FileStatus.ERROR,
        page_count=page_count,
        cards=[],
        skipped_pages=[],
        blank_pages=[],
        error_kind=kind,
        error_type=type(exc).__name__,
    )


def _status(page_count: int, skipped: list[int], blank: list[int]) -> FileStatus:
    """§4.2: 処理可能ページ 0 → NO_TEXT、NO_TEXT ページあり → PARTIAL、他は OK。"""
    processable = page_count - len(skipped) - len(blank)
    if processable <= 0:
        return FileStatus.NO_TEXT
    if skipped:
        return FileStatus.PARTIAL
    return FileStatus.OK


def _prepare_page(doc_page, page_index: int, s: "Settings", d: "Dictionaries") -> PreparedPage:
    """1 ページを読み、TEXT ページなら名刺の枠を探して領域に分ける。"""
    page = pdf_reader.read_page_with_cards(doc_page, s.textlayer, s.split)
    regions: list["Region"] = []
    if page.stats.kind == PageKind.TEXT:
        regions = segmenter.segment_page(page, s, d)
        # 線の切れ端は領域を決める時にしか使わない。確認画面は全ページを持ち続けるので捨てておく
        page = dataclasses.replace(page, card_edges=None)
    return PreparedPage(page_no=page_index + 1, data=page, regions=regions)


class _ResultBuilder:
    """ページを順に受け取り、項目を振り分けて PdfResult にまとめる（ラベルは通し番号。I12）。"""

    def __init__(self, path: Path, page_count: int, s: "Settings", d: "Dictionaries") -> None:
        self._path = path
        self._page_count = page_count
        self._s = s
        self._d = d
        self._cards: list[CardRecord] = []
        self._skipped: list[int] = []
        self._blank: list[int] = []
        self._label_no = 1

    def add_page(self, prepared: PreparedPage, regions: "list[Region] | None" = None) -> None:
        """regions を渡すと、自動の領域の代わりにそれを使う（確認画面で直したページ）。"""
        page = prepared.data
        if page.stats.kind == PageKind.NO_TEXT:
            self._skipped.append(prepared.page_no)
            return
        if page.stats.kind == PageKind.BLANK:
            self._blank.append(prepared.page_no)
            return
        for region in prepared.regions if regions is None else regions:
            fields = classifier.classify(region, self._s, self._d)
            text_rect = glyphs_text_rect(region.glyphs)
            self._cards.append(
                CardRecord(
                    file_name=self._path.name,
                    page_no=prepared.page_no,
                    label=labels.label(self._label_no),
                    fields=fields,
                    rect=region.rect,
                    draw_rect=pdf_reader.to_pdf_rect(region.rect, page.to_pdf),
                    method=region.method,
                    text_rect=text_rect,
                    draw_text_rect=(
                        pdf_reader.to_pdf_rect(text_rect, page.to_pdf) if text_rect else None
                    ),
                )
            )
            self._label_no += 1

    def result(self) -> PdfResult:
        return PdfResult(
            file_name=self._path.name,
            path=self._path,
            status=_status(self._page_count, self._skipped, self._blank),
            page_count=self._page_count,
            cards=self._cards,
            skipped_pages=self._skipped,
            blank_pages=self._blank,
        )


def _extract_open(path: Path, doc, s: "Settings", d: "Dictionaries") -> PdfResult:
    page_count = doc.page_count
    builder = _ResultBuilder(path, page_count, s, d)
    for page_index in range(page_count):
        builder.add_page(_prepare_page(doc[page_index], page_index, s, d))
    return builder.result()


def glyphs_text_rect(glyphs) -> tuple[float, float, float, float] | None:
    """領域内の非空白文字を囲む矩形（表示空間）。文字が無ければ None（§14.4 の枠用）。"""
    boxes = [g.bbox for g in glyphs if not g.ch.isspace()]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def _open(path: Path) -> "tuple[Any, PdfResult | None]":
    """(doc, None) か (None, 開けなかった時の PdfResult) を返す。"""
    try:
        return pdf_reader.open_pdf(path), None
    except KeyboardInterrupt:
        raise
    except pdf_reader.PdfOpenError as exc:
        # 分類は kind が持つ。error_type は原因側のクラス名（無ければ PdfOpenError）。
        return None, _error(path, exc.kind, exc.__cause__ or exc)
    except BaseException as exc:  # noqa: BLE001 - §19.1: KeyboardInterrupt 以外は外に出さない
        return None, _error(path, ErrorKind.OPEN_FAILED, exc)


def _close(doc) -> None:
    try:
        doc.close()
    except Exception:  # noqa: BLE001 - 閉じる失敗で結果を変えない
        pass


def extract_pdf(path: Path, s: "Settings", d: "Dictionaries") -> PdfResult:
    """path の PDF から名刺を抽出する。例外は KeyboardInterrupt だけを外に出す。"""
    doc, failed = _open(path)
    if failed is not None:
        return failed

    page_count = 0
    try:
        page_count = doc.page_count
        return _extract_open(path, doc, s, d)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - §4.2: 途中の例外はファイル全体を ERROR に
        return _error(path, ErrorKind.EXTRACT_FAILED, exc, page_count)
    finally:
        _close(doc)


def prepare_pdf(path: Path, s: "Settings", d: "Dictionaries") -> PreparedPdf:
    """全ページを読んで枠を自動で決めるところまで行う（項目の振り分けはしない）。

    extract_pdf と同じく、例外は KeyboardInterrupt だけを外に出す。失敗は error に入れる。
    """
    doc, failed = _open(path)
    if failed is not None:
        return PreparedPdf(path=path, file_name=path.name, error=failed)

    page_count = 0
    try:
        page_count = doc.page_count
        pages = [_prepare_page(doc[i], i, s, d) for i in range(page_count)]
        return PreparedPdf(path=path, file_name=path.name, page_count=page_count, pages=pages)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - §4.2: 途中の例外はファイル全体を ERROR に
        return PreparedPdf(
            path=path, file_name=path.name, page_count=page_count,
            error=_error(path, ErrorKind.EXTRACT_FAILED, exc, page_count),
        )
    finally:
        _close(doc)


def build_result(
    prepared: PreparedPdf, s: "Settings", d: "Dictionaries",
    overrides: "Mapping[int, list[Region]] | None" = None,
) -> PdfResult:
    """下読みの結果から PdfResult を作る。

    overrides は「ページ番号（0 始まり）→ そのページで使う領域」。無いページは自動の領域を
    使うので、overrides が空なら extract_pdf と同じ結果になる。失敗は全か無か（I6）。
    """
    if prepared.error is not None:
        return prepared.error
    try:
        builder = _ResultBuilder(prepared.path, prepared.page_count, s, d)
        for index, page in enumerate(prepared.pages):
            builder.add_page(page, overrides.get(index) if overrides else None)
        return builder.result()
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - §4.2: 途中の例外はファイル全体を ERROR に
        return _error(prepared.path, ErrorKind.EXTRACT_FAILED, exc, prepared.page_count)
