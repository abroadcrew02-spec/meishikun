"""diagnose の全体制御。

DESIGN.md §4.3・§16・§19.2 を実装する。pdf_reader・segmenter・labels は
抽出担当のモジュールで、実際に PDF を読む時にだけ import する（input\\ が
0 件であればこれらが無くても run_diagnose は動く）。
出してよいのはページ寸法・回転・文字数（不可視・U+FFFD の内数）・画像数・
画像 bbox と候補判定の理由・ページ種別・分割方式・領域数・領域 bbox・
分割系の要確認コードだけ。テキストの中身・項目の値は一切出さない（§16）。
ファイルの移動・output\\ への書き込みはしない。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app import fileops

if TYPE_CHECKING:
    from app.models import Dictionaries, Settings

_PT_PER_MM = 2.835


def _setup_diagnose_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"meishi.diagnose.{log_path.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def _emit(logger: logging.Logger, line: str) -> None:
    print(line)
    logger.info(line)


def _image_candidate_reason(bbox, reasons_by_bbox) -> str:
    for rect, reason in reasons_by_bbox:
        if rect == bbox:
            return reason
    return "ok"


def _dominant_method(regions) -> str:
    if not regions:
        return "none"
    methods = {str(r.method) for r in regions}
    return next(iter(methods)) if len(methods) == 1 else "mixed"


def _report_images(page_data, s: "Settings", logger: logging.Logger) -> None:
    stats = page_data.stats
    from app import segmenter  # 抽出担当。画像候補の説明にだけ使う

    reasons_by_bbox = segmenter.explain_images(page_data, s.split)
    page_area = stats.width * stats.height
    for i, bbox in enumerate(stats.image_boxes, start=1):
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        ratio = (bw * bh) / page_area if page_area else 0.0
        reason = _image_candidate_reason(bbox, reasons_by_bbox)
        candidate = "yes" if reason == "ok" else f"no({reason})"
        _emit(
            logger,
            f"      img{i}  bbox=({bbox[0]:.1f},{bbox[1]:.1f},{bbox[2]:.1f},{bbox[3]:.1f})  "
            f"page_ratio={ratio:.2f}  candidate={candidate}",
        )


def _report_split(page_data, s: "Settings", d: "Dictionaries", logger: logging.Logger) -> None:
    from app import labels, segmenter  # 抽出担当

    regions = segmenter.segment_page(page_data, s, d)
    _emit(logger, f"      split method={_dominant_method(regions)} regions={len(regions)}")
    flag_entries = []
    for n, region in enumerate(regions, start=1):
        label = labels.label(n)
        r = region.rect
        _emit(logger, f"        {label}  ({r[0]:.1f},{r[1]:.1f},{r[2]:.1f},{r[3]:.1f})")
        if region.flags:
            flag_entries.append(f"{label}=" + ",".join(str(f) for f in region.flags))
    if flag_entries:
        _emit(logger, "        flags: " + "; ".join(flag_entries))


def _diagnose_one(
    pdf: Path, s: "Settings", d: "Dictionaries", with_split: bool,
    logger: logging.Logger, pdf_reader,
) -> None:
    doc = pdf_reader.open_pdf(pdf)
    try:
        page_count = doc.page_count
        _emit(logger, f"[file] {pdf.name}  pages={page_count}")
        for page_index in range(page_count):
            # 本番（extract）と同じ順番で読む（枠を探してから画像の配置を読む）
            if with_split:
                page_data = pdf_reader.read_page_with_cards(doc[page_index], s.textlayer, s.split)
            else:
                page_data = pdf_reader.read_page(doc[page_index], s.textlayer)
            stats = page_data.stats
            w_mm = stats.width / _PT_PER_MM
            h_mm = stats.height / _PT_PER_MM
            _emit(
                logger,
                f"  p{page_index + 1}  {stats.width:.1f}x{stats.height:.1f}pt "
                f"({w_mm:.1f}x{h_mm:.1f}mm)  rot={stats.rotation}  "
                f"chars={stats.chars} (invisible={stats.invisible_chars}, "
                f"unreadable={stats.replacement_chars})  images={len(stats.image_boxes)}  "
                f"kind={stats.kind}",
            )
            if not stats.image_boxes:
                continue
            _report_images(page_data, s, logger)
            if with_split and stats.kind == "text":
                _report_split(page_data, s, d, logger)
    finally:
        doc.close()


def run_diagnose(s: "Settings", d: "Dictionaries", with_split: bool = True) -> int:
    """input\\ の PDF を診断する。ファイルは動かさず、output\\ にも書かない。"""
    fileops.ensure_dirs(s.paths)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = _setup_diagnose_logger(s.paths.logs / f"diagnose_{run_id}.log")

    pdfs, _ignored = fileops.list_input_pdfs(s.paths.input)

    had_error = False
    if pdfs:
        from app import pdf_reader  # 抽出担当。PDF が 1 件以上ある時だけ要る

        for pdf in pdfs:
            try:
                _diagnose_one(pdf, s, d, with_split, logger, pdf_reader)
            except Exception as exc:  # noqa: BLE001 - 1 ファイルの失敗は継続する
                had_error = True
                _emit(logger, f"[file] {pdf.name}  ERROR type={type(exc).__name__}")

    return 1 if had_error else 0
