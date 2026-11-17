"""run の全体制御。

DESIGN.md §4.1（処理フロー）・§14.1（PDF ごとの出力フォルダ）・§19.2（関数シグネチャ）
・§19.3（並行作業の進め方）を実装する。結合点は 2 つだけ（§19.3-3）:
`app.extract.extract_pdf`（既定の extractor）と `app.rows.to_row`。どちらも
抽出担当のモジュールで、import に失敗した場合は「呼ばれたら分かるエラー」を
出すフォールバックに差し替える（テストでは偽の extractor・偽の to_row を渡せる）。
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app import fileops, runlog, writers
from app.split_pdf import render_split_pdf
from app.summary import BatchSummary, FileOutcome

if TYPE_CHECKING:
    from app.models import CardRecord, Dictionaries, OutputCfg, PdfResult, Settings

# ---- 結合点 1: extract_pdf（§19.3-3） ----
try:
    from app.extract import extract_pdf
except ImportError:  # pragma: no cover - app.extract が未実装の間のフォールバック

    def extract_pdf(path: Path, s: "Settings", d: "Dictionaries") -> "PdfResult":  # type: ignore[misc]
        raise RuntimeError(
            "app.extract.extract_pdf が未実装です。"
            "run_batch(..., extractor=<偽の抽出関数>) のように明示的に渡してください。"
        )


# ---- 結合点 2: rows.to_row（§19.3-3） ----
try:
    from app.rows import to_row
except ImportError:  # pragma: no cover - app.rows が未実装の間のフォールバック

    def to_row(card: "CardRecord", cfg: "OutputCfg") -> list[str]:  # type: ignore[misc]
        raise RuntimeError("app.rows.to_row が未実装です。")


# COLUMNS は DESIGN.md §5 の確定値（変更時は DESIGN.md を先に直す）。
# app.models が無い間のフォールバックで、実装後は同じ値になる。
try:
    from app.models import COLUMNS
except ImportError:  # pragma: no cover - app.models が未実装の間のフォールバック
    COLUMNS: tuple[str, ...] = (
        "ファイル名", "ページ", "ラベル", "会社名", "氏名", "役職", "部署",
        "郵便番号1", "住所1", "郵便番号2", "住所2", "郵便番号3", "住所3",
        "電話1", "電話2", "電話3",
        "FAX1", "FAX2", "メール1", "メール2", "URL1", "URL2",
        "未分類", "要確認", "要確認理由", "領域座標",
    )

Extractor = Callable[[Path, "Settings", "Dictionaries"], "PdfResult"]

_MOVE_DIR_BY_STATUS = {"ok": "done", "partial": "done", "no_text": "error", "error": "error"}


def _output_stem(input_name: str) -> str:
    """出力フォルダとその中のファイルに使う名前（入力の stem。80 文字で切る。R11）。"""
    stem = Path(input_name).stem
    return stem[:80] if len(stem) > 80 else stem


def _process_one(
    pdf: Path, s: "Settings", d: "Dictionaries", extractor: Extractor, logger: logging.Logger,
) -> FileOutcome:
    """1 本の PDF を抽出し、output\\<stem>\\ に一覧表と分割確認 PDF を書いてから移動する。"""
    result = extractor(pdf, s, d)
    outcome = FileOutcome(result=result)

    rows_data = [to_row(card, s.output) for card in result.cards]
    outcome.rows = len(rows_data)
    outcome.review_rows = sum(1 for card in result.cards if card.fields.reasons)

    want_split = s.output.split_pdf and result.status in ("ok", "partial")
    if rows_data or want_split:
        stem = _output_stem(pdf.name)
        out_dir = fileops.unique_dir(s.paths.output, stem)
        try:
            out_dir.mkdir(parents=True, exist_ok=False)
            if rows_data:
                all_rows = [list(COLUMNS)] + rows_data
                if "csv" in s.output.formats:
                    outcome.csv_path = writers.write_csv(out_dir / f"{stem}.csv", all_rows)
                if "xlsx" in s.output.formats:
                    outcome.xlsx_path = writers.write_xlsx(out_dir / f"{stem}.xlsx", all_rows)
            outcome.out_dir = out_dir
        except OSError:
            # I7: 一覧表を書けなければ入力を動かさない。書きかけのフォルダ（.tmp を含む）は
            # 今作ったものなので消す（名刺の中身を残さない）。
            outcome.csv_path = None
            outcome.xlsx_path = None
            outcome.output_failed = True
            shutil.rmtree(out_dir, ignore_errors=True)
            logger.info(f"OUTPUT_WRITE_FAILED name={pdf.name} dir={out_dir}")
            return outcome

        if want_split:
            try:
                outcome.split_pdf = render_split_pdf(pdf, out_dir / f"{stem}_split.pdf", result)
            except Exception:  # noqa: BLE001 - 1 ファイルの分割確認 PDF 失敗は継続する
                outcome.split_failed = True
        logger.info(f"OUTPUT name={pdf.name} dir={out_dir} rows={len(rows_data)}")

    move_dir = s.paths.done if _MOVE_DIR_BY_STATUS[result.status] == "done" else s.paths.error
    try:
        outcome.moved_to = fileops.move_no_overwrite(pdf, move_dir)
    except OSError:
        outcome.move_failed = True
    return outcome


def run_batch(
    s: "Settings",
    d: "Dictionaries",
    extractor: Extractor = extract_pdf,
    now: Callable[[], datetime] = datetime.now,
) -> BatchSummary:
    """input\\ の PDF を 1 本ずつ処理する。

    PDF ごとに output\\<PDF 名>\\ を作り、一覧表（CSV・xlsx）と分割確認 PDF を
    書いてから、入力を done\\ / error\\ へ移動する。1 ファイルの失敗は他の
    ファイルの処理を止めない。一覧表を書けなかった PDF は移動しない（I7）。
    """
    try:
        fileops.ensure_dirs(s.paths)
    except OSError:
        return BatchSummary(
            run_id="", outcomes=[], ignored_files=[], rows=0, review_rows=0, fatal=True,
        )

    run_id = fileops.make_run_id(now(), s.paths.output, s.paths.logs)
    log_path = s.paths.logs / f"meishi_{run_id}.log"
    logger = runlog.setup_run_logger(log_path)

    pdfs, ignored = fileops.list_input_pdfs(s.paths.input)
    logger.info(f"START run_id={run_id} files={len(pdfs)} ignored={len(ignored)}")

    outcomes: list[FileOutcome] = [_process_one(pdf, s, d, extractor, logger) for pdf in pdfs]

    for outcome in outcomes:
        runlog.log_file_outcome(logger, outcome)

    summary = BatchSummary(
        run_id=run_id, outcomes=outcomes, ignored_files=ignored,
        rows=sum(o.rows for o in outcomes), review_rows=sum(o.review_rows for o in outcomes),
        log_path=log_path,
    )
    logger.info(f"END exit_code={summary.exit_code()}")
    return summary
