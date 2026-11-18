"""実行結果の集計・要約文・終了コード。

DESIGN.md §15（15.2 要約・15.3 終了コード）・§19.2 を実装する。
出力は PDF ごとに 1 フォルダ（§14.1・Q42）なので、一覧表や分割確認 PDF の
パスは FileOutcome が持ち、BatchSummary は全体の件数とログの場所だけを持つ。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import PdfResult


@dataclass(slots=True)
class FileOutcome:
    result: "PdfResult"
    moved_to: Path | None = None
    move_failed: bool = False
    out_dir: Path | None = None        # output\<PDF 名>\（一覧表か分割確認 PDF を書いた時だけ）
    csv_path: Path | None = None
    xlsx_path: Path | None = None
    split_pdf: Path | None = None
    split_failed: bool = False
    output_failed: bool = False        # 一覧表を書けなかった（入力は input\ に残す。I7）
    input_changed: bool = False        # 確認中に入力 PDF が変わった・無くなった（出力も移動もしない）
    rows: int = 0
    review_rows: int = 0


@dataclass(slots=True)
class BatchSummary:
    run_id: str
    outcomes: list[FileOutcome]
    ignored_files: list[str]
    rows: int
    review_rows: int
    fatal: bool = False
    log_path: Path | None = None

    def exit_code(self) -> int:
        """全成功 0／エラー・除外・書き込み失敗あり 1／起動できない 2（DESIGN.md §15.3）。"""
        if self.fatal:
            return 2
        for o in self.outcomes:
            if o.move_failed or o.split_failed or o.output_failed or o.input_changed:
                return 1
            if o.result.status != "ok":
                return 1
        return 0


def _status_detail(o: FileOutcome) -> str:
    """`一部除外: b.pdf（テキスト層なし: p2）` のような 1 ファイルの状態（成功も含む）。"""
    r = o.result
    name = r.file_name
    if r.status == "partial":
        pages = ",".join(f"p{p}" for p in r.skipped_pages)
        detail = f"（テキスト層なし: {pages}）" if pages else "（テキスト層なし）"
        return f"一部除外: {name}{detail}"
    if r.status == "no_text":
        return f"処理不可: {name}（テキスト層なし）"
    if r.status == "error":
        return f"処理不可: {name}（抽出エラー）"
    return f"成功: {name}"


def _folder(path: Path) -> str:
    """移動先を `done フォルダ` のように表す（config.ini で名前を変えていてもその名前で出す）。"""
    return f"{path.parent.name} フォルダ" if path.parent.name else str(path.parent)


def format_summary(s: BatchSummary) -> str:
    """コンソール・ログ用の要約文を作る（DESIGN.md §15.2 の書式）。

    使う人が次に見るべき場所（結果のフォルダ・入力 PDF の行き先・ログ）が
    画面だけで分かるようにする。名刺の中身は書かない（I9）。
    """
    lines: list[str] = ["==== 名刺抽出 結果 ===="]

    total = len(s.outcomes)
    header = f"対象 PDF: {total} 件"
    if s.ignored_files:
        header += f"（対象外のファイル {len(s.ignored_files)} 件: {'、'.join(s.ignored_files)}）"
    lines.append(header)

    done = [o for o in s.outcomes if not o.input_changed]
    ok = sum(1 for o in done if o.result.status == "ok")
    partial = sum(1 for o in done if o.result.status == "partial")
    bad = sum(1 for o in done if o.result.status in ("no_text", "error")) + (len(s.outcomes) - len(done))
    lines.append(f"  成功: {ok} 件 / 一部除外: {partial} 件 / 処理不可: {bad} 件")

    if s.rows > 0:
        lines.append(f"名刺: {s.rows} 件（要確認 {s.review_rows} 件）")
    else:
        lines.append("名刺: 0 件（一覧表は作成していません）")

    written = [o for o in s.outcomes if o.out_dir is not None]
    if written:
        root = written[0].out_dir.parent
        lines.append("")
        lines.append(f"[結果の置き場所] {root} の中に、PDF ごとに同じ名前のフォルダを作りました")
        for o in written:
            assert o.out_dir is not None
            lines.append(f"  {o.result.file_name} → {o.out_dir.parent.name}\\{o.out_dir.name}\\")
            tables = [p.name for p in (o.xlsx_path, o.csv_path) if p is not None]
            if tables:
                lines.append(
                    f"      一覧表: {' / '.join(tables)}（名刺 {o.rows} 件、要確認 {o.review_rows} 件）"
                )
            if o.split_pdf is not None:
                lines.append(f"      分割確認: {o.split_pdf.name}")
            elif o.split_failed:
                lines.append("      分割確認: 作成に失敗しました")
        if any(o.split_pdf is not None for o in written):
            lines.append("  分割確認 PDF の赤い枠が「1 枚の名刺」として読んだ範囲です。枠がずれている名刺や、")
            lines.append("  一覧表の「要確認」列に 1 が付いた行は、元の名刺と見比べてください。")

    if s.outcomes:
        lines.append("")
        lines.append("[入力 PDF の行き先] 処理が終わった PDF は input フォルダから移動しました")
        for o in s.outcomes:
            if o.input_changed:
                lines.append(
                    f"  未出力: {o.result.file_name} → 確認中に PDF が変わったため出力していません"
                    "（input フォルダを確かめて、もう一度実行してください）"
                )
                continue
            line = f"  {_status_detail(o)}"
            if o.moved_to is not None:
                line += f" → {_folder(o.moved_to)}"
                if o.result.status in ("no_text", "error"):
                    line += "（一覧表は作っていません）"
            elif o.output_failed:
                line += " → 一覧表を書けなかったので input フォルダに残しました"
            elif o.move_failed:
                line += " → 移動失敗（input フォルダに残っています）"
            lines.append(line)

    if s.log_path is not None:
        lines.append("")
        lines.append(f"ログ: {s.log_path}（名刺の中身は書いていません）")

    return "\n".join(lines)
