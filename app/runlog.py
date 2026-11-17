"""ログ設定と書式。

DESIGN.md §15.1・§19.2 を実装する。標準 logging のみを使う
（logging.handlers は socket を import するため使わない。I10 不変条件）。
抽出値・例外メッセージ・トレースバックは書かない（I9 不変条件）。
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.summary import FileOutcome


def setup_run_logger(log_path: Path) -> logging.Logger:
    """log_path に書く専用ロガーを作る。親ロガーへは伝播しない。"""
    logger = logging.getLogger(f"meishi.{log_path.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


def _reason_counts(cards) -> str:
    counter: Counter[str] = Counter()
    for card in cards:
        for reason in card.fields.reasons:
            counter[str(reason)] += 1
    if not counter:
        return ""
    return ",".join(f"{code}:{n}" for code, n in counter.items())


def log_file_outcome(logger: logging.Logger, o: "FileOutcome") -> None:
    """1 ファイルの処理結果を FILE 行としてログに書く。

    書いてよいのは件数・ファイル名・ステータス・エラー種別・ページ番号・
    要確認理由コードの件数・移動先パスだけ（抽出値そのものは書かない）。
    """
    r = o.result
    parts = [f"name={r.file_name}", f"status={r.status}", f"pages={r.page_count}"]

    if r.status == "error":
        parts.append(f"kind={r.error_kind}")
        if r.error_type:
            parts.append(f"type={r.error_type}")
    else:
        parts.append(f"cards={len(r.cards)}")
        review = sum(1 for c in r.cards if c.fields.reasons)
        if review:
            parts.append(f"review={review}")
        reasons = _reason_counts(r.cards)
        if reasons:
            parts.append(f"reasons={reasons}")
        if r.skipped_pages:
            parts.append("skipped_pages=" + ",".join(str(p) for p in r.skipped_pages))

    if o.moved_to is not None:
        parts.append(f"dest={o.moved_to}")
    elif o.move_failed:
        parts.append("dest=MOVE_FAILED")

    if o.split_pdf is not None:
        parts.append(f"split={o.split_pdf}")
    elif o.split_failed:
        parts.append("split=SPLIT_FAILED")

    logger.info("FILE " + " ".join(parts))
