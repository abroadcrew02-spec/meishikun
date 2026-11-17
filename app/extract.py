"""1 ファイルの抽出。DESIGN.md §4.2・§19.1 を実装する。

extract_pdf は KeyboardInterrupt 以外の例外を外に出さない。失敗は
status=ERROR の PdfResult で返す。

- I6: 1 ファイルの抽出は全か無か。ERROR の時 cards は空にする。
- I9: error_type には例外クラス名だけを入れる。例外メッセージや抽出値は
  保持しない。
- I12: ラベルは PDF 内の通し番号で、segment_page が返す順（読み順）に振る。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from app import classifier, labels, pdf_reader, segmenter
from app.models import CardRecord, ErrorKind, FileStatus, PageKind, PdfResult

if TYPE_CHECKING:
    from app.models import Dictionaries, Settings


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


def _extract_open(path: Path, doc, s: "Settings", d: "Dictionaries") -> PdfResult:
    page_count = doc.page_count
    cards: list[CardRecord] = []
    skipped: list[int] = []
    blank: list[int] = []
    label_no = 1
    for page_index in range(page_count):
        page = pdf_reader.read_page(doc[page_index], s.textlayer)
        if page.stats.kind == PageKind.TEXT:
            boxes, edges = pdf_reader.detect_cards(doc[page_index], s.split)
            page = dataclasses.replace(page, card_boxes=boxes, card_edges=edges)
        page_no = page_index + 1
        if page.stats.kind == PageKind.NO_TEXT:
            skipped.append(page_no)
            continue
        if page.stats.kind == PageKind.BLANK:
            blank.append(page_no)
            continue
        for region in segmenter.segment_page(page, s, d):
            fields = classifier.classify(region, s, d)
            text_rect = _text_rect(region.glyphs)
            cards.append(
                CardRecord(
                    file_name=path.name,
                    page_no=page_no,
                    label=labels.label(label_no),
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
            label_no += 1
    return PdfResult(
        file_name=path.name,
        path=path,
        status=_status(page_count, skipped, blank),
        page_count=page_count,
        cards=cards,
        skipped_pages=skipped,
        blank_pages=blank,
    )


def _text_rect(glyphs) -> tuple[float, float, float, float] | None:
    """領域内の非空白文字を囲む矩形（表示空間）。文字が無ければ None（§14.4 の枠用）。"""
    boxes = [g.bbox for g in glyphs if not g.ch.isspace()]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def extract_pdf(path: Path, s: "Settings", d: "Dictionaries") -> PdfResult:
    """path の PDF から名刺を抽出する。例外は KeyboardInterrupt だけを外に出す。"""
    try:
        doc = pdf_reader.open_pdf(path)
    except KeyboardInterrupt:
        raise
    except pdf_reader.PdfOpenError as exc:
        # 分類は kind が持つ。error_type は原因側のクラス名（無ければ PdfOpenError）。
        return _error(path, exc.kind, exc.__cause__ or exc)
    except BaseException as exc:  # noqa: BLE001 - §19.1: KeyboardInterrupt 以外は外に出さない
        return _error(path, ErrorKind.OPEN_FAILED, exc)

    page_count = 0
    try:
        page_count = doc.page_count
        return _extract_open(path, doc, s, d)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - §4.2: 途中の例外はファイル全体を ERROR に
        return _error(path, ErrorKind.EXTRACT_FAILED, exc, page_count)
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001 - 閉じる失敗で結果を変えない
            pass
