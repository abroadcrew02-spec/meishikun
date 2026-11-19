"""枠の確認・修正（review）。

確認作業の状態（ReviewSession）と全体の流れ（run_review）を持つ。fitz もネットワーク系の
モジュールも import しない（ページ画像の描画は pdf_reader、待ち受けは review_server）。

不変条件:
- R1: 利用者が触っていないページは、自動で決めた領域をそのまま使う。1 ページも触っていない
  ファイルは、下読みの時に作った結果をそのまま出力する（全自動の run と同じ結果になる）。
- R2: 1 か所でも触ったページは、そのページの全部の枠を segmenter.split_by_rects で切り直す。
- R3: 送られた枠が自動の枠と id・座標とも一致したら、触っていない扱い（R1）に戻す。
- R4: ラベルは PDF 内の通し番号で、ページ順 → 読み順（I12）。
- 画面へ返す値とログに、名刺から抽出した文字を含めない（I9）。
"""
from __future__ import annotations

import math
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app import batch, extract, geometry, labels, pdf_reader, segmenter
from app.apperror import AppError
from app.models import FileStatus, PageKind, Reason
from app.summary import BatchSummary, FileOutcome, format_summary

if TYPE_CHECKING:
    from app.models import (
        Dictionaries,
        Glyph,
        ManualSplit,
        PdfResult,
        PreparedPage,
        PreparedPdf,
        Rect4,
        Region,
        Settings,
    )

IMAGE_DPI = 150            # 確認画面に出すページ画像の解像度
FRAME_MARGIN = 8.0         # 文字の範囲から枠までの余白 pt（split_pdf と同じ。§14.4）
MAX_FRAMES = 200           # 1 ページの枠の上限
MIN_SIDE = 10.0            # 枠の一辺の下限 pt
MAX_OUTSIDE_MARKS = 500    # 枠の外の文字の位置として返す矩形の上限
_IMAGE_CACHE = 3           # 描画済みのページ画像を覚えておく数（メモリ上だけ）
_ID_RE = re.compile(r"[a-z0-9-]{1,24}")
_SAME = 0.011              # 小数 2 桁に丸めた座標が同じとみなす差
_MAX_COORD = 1e6           # 座標として受け付ける大きさの上限 pt
_MAX_REV = 2 ** 53         # rev の上限（画面の JavaScript が正確に扱える整数の範囲）

# 区切りに関する旗だけを画面に返す（振り分けの旗は一覧表で見る）
_SPLIT_FLAGS = frozenset({
    Reason.SPLIT_FALLBACK, Reason.SPLIT_WEAK, Reason.SPLIT_SUSPECT_MERGED,
    Reason.SPLIT_ORPHAN_TEXT, Reason.SPLIT_FRAME_OFF, Reason.REGION_NO_TEXT,
})

REVIEWING = "reviewing"
FINISHING = "finishing"
FINISHED = "finished"
CANCELLED = "cancelled"


class ReviewError(AppError):
    """確認画面からの要求を受け付けられない。code は固定の短い語で、抽出値や例外の内容を含めない。"""


def _round_rect(rect: "Rect4") -> "Rect4":
    return (round(rect[0], 2), round(rect[1], 2), round(rect[2], 2), round(rect[3], 2))


def _same_rect(a: "Rect4", b: "Rect4") -> bool:
    return all(abs(x - y) < _SAME for x, y in zip(a, b))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and abs(value) <= _MAX_COORD
    except OverflowError:   # float にできないほど大きい整数
        return False


def _marks(glyphs: "list[Glyph]") -> "list[Rect4]":
    """枠の外の文字を、行のまとまりごとの矩形にする（位置だけ。文字は返さない）。"""
    out: list[Rect4] = []
    run: list[Glyph] = []

    def flush() -> None:
        if run:
            out.append(_round_rect(geometry.union([g.bbox for g in run])))
            run.clear()

    for g in sorted(glyphs, key=lambda item: item.seq):
        if run and (g.line_key != run[-1].line_key or g.seq - run[-1].seq > 2):
            flush()
        run.append(g)
    flush()
    return out


@dataclass(slots=True)
class _Page:
    prepared: "PreparedPage"
    auto: "list[tuple[str, Rect4]]"            # 自動の枠（id・画面に出す矩形）。読み順
    auto_outside: "list[Glyph]"                # 自動の領域のどれにも入っていない非空白の文字
    frames: "list[tuple[str, Rect4]] | None" = None   # 利用者が決めた枠（id 順）。None は R1
    split: "ManualSplit | None" = None
    rev: int = 0
    viewed: bool = False

    @property
    def kind(self) -> PageKind:
        return self.prepared.data.stats.kind

    def view(self) -> "list[tuple[str, Rect4, list[str]]]":
        """画面に出す枠（id・矩形・区切りの旗）。読み順。"""
        if self.frames is None or self.split is None:
            return [
                (fid, rect, [str(f) for f in region.flags if f in _SPLIT_FLAGS])
                for (fid, rect), region in zip(self.auto, self.prepared.regions)
            ]
        return [
            (self.frames[src][0], _round_rect(region.rect), [str(f) for f in region.flags])
            for region, src in zip(self.split.regions, self.split.source)
        ]

    def outside(self) -> "list[Glyph]":
        return self.auto_outside if self.split is None else self.split.outside


@dataclass(slots=True)
class _File:
    prepared: "PreparedPdf"
    auto_result: "PdfResult"                   # 1 ページも触らなかった時に出力する結果
    stamp: tuple[int, int] | None              # 下読みした時の (大きさ, 更新時刻)。出力の直前に照合する
    pages: list[_Page] = field(default_factory=list)

    @property
    def reviewable(self) -> bool:
        return self.auto_result.status in (FileStatus.OK, FileStatus.PARTIAL)


def file_stamp(path: Path) -> tuple[int, int] | None:
    """入力 PDF の (大きさ, 更新時刻)。確認中に変わっていないかの照合に使う。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _make_page(prepared: "PreparedPage") -> _Page:
    auto: list[tuple[str, Rect4]] = []
    taken: set[int] = set()
    for n, region in enumerate(prepared.regions, 1):
        rect = geometry.frame_rect(
            str(region.method), region.rect, extract.glyphs_text_rect(region.glyphs), FRAME_MARGIN,
        )
        auto.append((f"a{n}", _round_rect(rect)))
        taken.update(g.seq for g in region.glyphs)
    outside = [g for g in prepared.data.glyphs if not g.ch.isspace() and g.seq not in taken]
    return _Page(prepared=prepared, auto=auto, auto_outside=outside)


class ReviewSession:
    """確認作業の状態。メソッドは lock を取った上で呼ぶ（review_server が 1 件ずつ呼ぶ）。"""

    def __init__(
        self, s: "Settings", d: "Dictionaries", ctx: batch.RunContext,
        prepared: "list[PreparedPdf]",
        renderer: Callable[[Path, int, int], bytes] = pdf_reader.render_page_image,
        stamps: "list[tuple[int, int] | None] | None" = None,
    ) -> None:
        """stamps は、下読みの前に控えた入力の (大きさ, 更新時刻)。prepared と同じ並び。

        省略すると今の値を控える（下読みの間に差し替わったファイルは見分けられない）。
        """
        self._s = s
        self._d = d
        self._ctx = ctx
        self._renderer = renderer
        self._images: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self.lock = threading.RLock()
        self.state = REVIEWING
        self.summary: BatchSummary | None = None
        self._files: list[_File] = []
        for index, prep in enumerate(prepared):
            # 下読みの時点で自動の結果まで作る。振り分けで落ちるファイルは、ここで「読めない」と分かる
            auto_result = extract.build_result(prep, s, d)
            pages = [_make_page(p) for p in prep.pages] if auto_result.status != FileStatus.ERROR else []
            stamp = file_stamp(prep.path) if stamps is None else stamps[index]
            self._files.append(_File(prepared=prep, auto_result=auto_result, stamp=stamp, pages=pages))

    # ------------------------------------------------------------ 状態を返す

    def _labels(self, file: _File) -> dict[int, dict[str, str]]:
        """ページ番号 → {枠の id: ラベル}。build_result と同じ順に数える（I12）。"""
        out: dict[int, dict[str, str]] = {}
        n = 1
        for index, page in enumerate(file.pages):
            if page.kind != PageKind.TEXT:
                continue
            out[index] = {}
            for fid, _rect, _flags in page.view():
                out[index][fid] = labels.label(n)
                n += 1
        return out

    def _page_json(self, index: int, page: _Page, page_labels: dict[str, str]) -> dict[str, Any]:
        stats = page.prepared.data.stats
        outside = page.outside() if page.kind == PageKind.TEXT else []
        marks = _marks(outside)
        return {
            "index": index,
            "no": page.prepared.page_no,
            "kind": str(stats.kind),
            "width": round(stats.width, 2),
            "height": round(stats.height, 2),
            "edited": page.frames is not None,
            "viewed": page.viewed,
            "rev": page.rev,
            "frames": [
                {"id": fid, "label": page_labels.get(fid, ""), "rect": list(rect), "flags": flags}
                for fid, rect, flags in (page.view() if page.kind == PageKind.TEXT else [])
            ],
            "outside": [list(r) for r in marks[:MAX_OUTSIDE_MARKS]],
            "outside_count": len(outside),
        }

    def state_json(self) -> dict[str, Any]:
        files = []
        for fi, file in enumerate(self._files):
            file_labels = self._labels(file)
            files.append({
                "id": fi,
                "name": file.prepared.file_name,
                "status": str(file.auto_result.status),
                "pages": [
                    self._page_json(pi, page, file_labels.get(pi, {}))
                    for pi, page in enumerate(file.pages)
                ] if file.reviewable else [],
            })
        return {"state": self.state, "files": files, "summary": self._summary_json()}

    def _update_json(self, fi: int, pi: int) -> dict[str, Any]:
        """枠を変えた後の応答。同じファイルの他のページのラベルも変わるので、あわせて返す。"""
        file = self._files[fi]
        file_labels = self._labels(file)
        return {
            "page": self._page_json(pi, file.pages[pi], file_labels.get(pi, {})),
            "labels": {str(index): mapping for index, mapping in file_labels.items()},
        }

    # ------------------------------------------------------------ 画面からの操作

    def _page(self, fi: int, pi: int) -> _Page:
        if not (0 <= fi < len(self._files)):
            raise ReviewError(404, "not_found")
        file = self._files[fi]
        if not file.reviewable or not (0 <= pi < len(file.pages)):
            raise ReviewError(404, "not_found")
        page = file.pages[pi]
        if page.kind != PageKind.TEXT:
            raise ReviewError(409, "not_editable")
        return page

    def _require_reviewing(self) -> None:
        if self.state != REVIEWING:
            raise ReviewError(409, "closed")

    def _next_rev(self, page: _Page, payload: Any) -> int:
        if not isinstance(payload, dict) or not _is_int(payload.get("rev")):
            raise ReviewError(400, "bad_request")
        rev = payload["rev"]
        if not (0 < rev < _MAX_REV):
            raise ReviewError(400, "bad_request")
        if rev <= page.rev:
            raise ReviewError(409, "stale")   # 順番が前後した古い要求は捨てる
        return rev

    def _parse_frames(self, page: _Page, payload: dict[str, Any]) -> "list[tuple[str, Rect4]]":
        raw = payload.get("frames")
        if not isinstance(raw, list) or len(raw) > MAX_FRAMES:
            raise ReviewError(400, "bad_request")
        stats = page.prepared.data.stats
        auto = dict(page.auto)
        frames: dict[str, Rect4] = {}
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"id", "rect"}:
                raise ReviewError(400, "bad_request")   # ラベルや旗は画面から受け取らない
            fid, rect = item["id"], item["rect"]
            if not isinstance(fid, str) or not _ID_RE.fullmatch(fid) or fid in frames:
                raise ReviewError(400, "bad_request")
            if not isinstance(rect, list) or len(rect) != 4 or not all(_is_number(v) for v in rect):
                raise ReviewError(400, "bad_request")
            same = tuple(float(v) for v in rect)
            if fid in auto and _same_rect(same, auto[fid]):
                # 自動の枠をそのまま送り返してきたもの。小さい・はみ出している枠でも直させない
                # （触っていない枠が原因で、そのページの保存が丸ごと失敗しないようにする）
                frames[fid] = auto[fid]
                continue
            x0 = min(max(float(rect[0]), 0.0), stats.width)
            y0 = min(max(float(rect[1]), 0.0), stats.height)
            x1 = min(max(float(rect[2]), 0.0), stats.width)
            y1 = min(max(float(rect[3]), 0.0), stats.height)
            if x1 - x0 < MIN_SIDE or y1 - y0 < MIN_SIDE:
                raise ReviewError(400, "bad_rect")
            frames[fid] = _round_rect((x0, y0, x1, y1))
        # 同じ面積の枠が重なった時の勝ち負けを、送られてきた順に依存させない
        return sorted(frames.items())

    def set_frames(self, fi: int, pi: int, payload: Any) -> dict[str, Any]:
        """そのページの枠を置き換える。"""
        self._require_reviewing()
        page = self._page(fi, pi)
        rev = self._next_rev(page, payload)
        frames = self._parse_frames(page, payload)
        auto = dict(page.auto)
        if len(frames) == len(auto) and all(
            fid in auto and _same_rect(rect, auto[fid]) for fid, rect in frames
        ):
            page.frames, page.split = None, None        # R3: 自動の枠と同じなら触っていない扱い
        else:
            page.split = segmenter.split_by_rects(
                page.prepared.data, [rect for _fid, rect in frames], self._s, self._d,
            )
            page.frames = frames
        page.rev = rev
        return self._update_json(fi, pi)

    def reset_page(self, fi: int, pi: int, payload: Any) -> dict[str, Any]:
        """自動の枠に戻す（R1）。"""
        self._require_reviewing()
        page = self._page(fi, pi)
        page.rev = self._next_rev(page, payload)
        page.frames, page.split = None, None
        return self._update_json(fi, pi)

    def mark_viewed(self, fi: int, pi: int) -> dict[str, Any]:
        self._require_reviewing()
        self._page(fi, pi).viewed = True
        return {}

    def page_image(self, fi: int, pi: int) -> bytes:
        """ページ画像（JPEG）。メモリ上だけで扱い、ファイルには書かない。"""
        self._require_reviewing()
        self._page(fi, pi)
        key = (fi, pi)
        if key in self._images:
            self._images.move_to_end(key)
            return self._images[key]
        try:
            data = self._renderer(self._files[fi].prepared.path, pi, IMAGE_DPI)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - 入力が消えた・壊れた時も画面には固定の語だけ返す
            raise ReviewError(500, "image_failed") from exc
        self._images[key] = data
        while len(self._images) > _IMAGE_CACHE:
            self._images.popitem(last=False)
        return data

    # ------------------------------------------------------------ 出力・中止

    def _check_revs(self, payload: Any) -> None:
        """画面が持つ rev とサーバーの rev が全ページで一致すること（保存が終わっていること）。"""
        revs = payload.get("revs") if isinstance(payload, dict) else None
        if not isinstance(revs, dict):
            raise ReviewError(400, "bad_request")
        for fi, file in enumerate(self._files):
            for pi, page in enumerate(file.pages):
                got = revs.get(f"{fi}/{pi}", 0)
                if not _is_int(got) or got != page.rev:
                    raise ReviewError(409, "unsaved")

    def _result_for(self, file: _File) -> "PdfResult":
        overrides: dict[int, list[Region]] = {
            index: page.split.regions
            for index, page in enumerate(file.pages)
            if page.frames is not None and page.split is not None
        }
        if not overrides:
            return file.auto_result
        return extract.build_result(file.prepared, self._s, self._d, overrides)

    def edited_pages(self) -> int:
        return sum(1 for file in self._files for page in file.pages if page.frames is not None)

    def finish(self, payload: Any) -> dict[str, Any]:
        """修正後の枠で出力する。2 回目以降は保存済みの要約を返し、出力し直さない。"""
        if self.state == FINISHED:
            return self._summary_json()
        self._require_reviewing()
        self._check_revs(payload)
        self.state = FINISHING
        self._images.clear()
        outcomes: list[FileOutcome] = []
        try:
            for file in self._files:
                outcomes.append(self._output_one(file))
        finally:
            # 途中で止まっても必ず終了状態にする（待ち受けを残さない・2 回目の出力をさせない）。
            # 要約は先に組み立てる（この後のログの書き込みで失敗しても、出力した事実を失わない）
            self.summary = BatchSummary(
                run_id=self._ctx.run_id, outcomes=outcomes, ignored_files=self._ctx.ignored,
                rows=sum(o.rows for o in outcomes), review_rows=sum(o.review_rows for o in outcomes),
                log_path=self._ctx.log_path,
            )
            self.state = FINISHED
            self._ctx.logger.info(f"REVIEW_FINISH edited_pages={self.edited_pages()}")
            self.summary = batch.end_run(self._ctx, outcomes)
        return self._summary_json()

    def _output_one(self, file: _File) -> FileOutcome:
        path = file.prepared.path
        if file.stamp is None or file_stamp(path) != file.stamp:
            # 確認中に入力が変わった・無くなった。別の PDF に枠を描かないよう、出力も移動もしない
            return FileOutcome(result=file.auto_result, input_changed=True)
        result = self._result_for(file)
        try:
            return batch.write_and_move(path, result, self._s, self._ctx.logger)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - 1 本の失敗で残りの PDF の出力を止めない
            self.log_error(type(exc).__name__)
            return FileOutcome(result=result, output_failed=True)

    def log_error(self, type_name: str) -> None:
        """想定外の例外を、型の名前だけログに残す（中身は書かない。I9）。"""
        self._ctx.logger.info(f"REVIEW_ERROR type={type_name}")

    def cancel(self) -> dict[str, Any]:
        """何も出力せずに終える。"""
        if self.state == CANCELLED:
            return self._summary_json()
        self._require_reviewing()
        self.state = CANCELLED
        self._images.clear()
        self._ctx.logger.info("REVIEW_CANCEL")
        self._ctx.logger.info("END exit_code=0")
        return self._summary_json()

    def _summary_json(self) -> dict[str, Any] | None:
        if self.state == CANCELLED:
            return {"cancelled": True, "rows": 0, "review_rows": 0, "files": []}
        if self.summary is None:
            return None
        files = []
        for o in self.summary.outcomes:
            if o.input_changed:
                result = "input_changed"
            elif o.output_failed:
                result = "output_failed"
            elif o.move_failed:
                result = "move_failed"
            else:
                result = str(o.result.status)
            wrote = o.out_dir is not None and (o.csv_path is not None or o.xlsx_path is not None)
            files.append({
                "name": o.result.file_name,
                "result": result,
                "rows": o.rows,
                "review_rows": o.review_rows,
                "folder": f"{o.out_dir.parent.name}\\{o.out_dir.name}" if wrote and o.out_dir else "",
                "moved": o.moved_to.parent.name if o.moved_to is not None else "",
            })
        return {
            "cancelled": False,
            "rows": self.summary.rows,
            "review_rows": self.summary.review_rows,
            "files": files,
        }


def _echo(text: str) -> None:
    # 出力先がファイルやパイプの時も、アドレスの行がすぐ出るようにする
    print(text, flush=True)


def run_review(
    s: "Settings", d: "Dictionaries", *,
    open_browser: bool = True,
    now: Callable[[], datetime] = datetime.now,
    echo: Callable[[str], None] = _echo,
) -> int:
    """input\\ の PDF を下読みし、確認画面で枠を直してから出力する。戻り値は終了コード。"""
    ctx = batch.begin_run(s, now)
    if ctx is None:
        echo(format_summary(batch.fatal_summary()))
        return 2
    if not ctx.pdfs:
        summary = batch.end_run(ctx, [])
        echo(format_summary(summary))
        return summary.exit_code()

    ctx.logger.info(f"REVIEW_START files={len(ctx.pdfs)}")
    prepared = []
    stamps = []
    for n, pdf in enumerate(ctx.pdfs, 1):
        echo(f"読み込んでいます（{n}/{len(ctx.pdfs)}）: {pdf.name}")
        stamps.append(file_stamp(pdf))   # 読む前に控える。読んでいる間の差し替えも出力の時に見分ける
        prepared.append(extract.prepare_pdf(pdf, s, d))
    session = ReviewSession(s, d, ctx, prepared, stamps=stamps)

    from app import review_server   # 待ち受けを使う時だけ読み込む

    server = review_server.ReviewServer(session)
    server.start()
    echo("")
    echo("ブラウザで確認画面を開きます。枠を確かめて、画面の「出力する」か「中止する」を押してください。")
    echo("ブラウザを閉じただけでは終わりません。画面が開かない時、閉じてしまった時は、")
    echo("次のアドレスをブラウザで開いてください:")
    echo(f"  {server.url}")
    echo("（この黒い画面は、出力が終わるまで閉じないでください。閉じると中止になります）")
    echo("")
    if open_browser and not review_server.open_browser(server.url):
        echo("ブラウザを自動で開けませんでした。上のアドレスを手で開いてください。")
    try:
        server.wait()
    except KeyboardInterrupt:
        with session.lock:   # 出力の途中なら、終わるまで待ってから抜ける
            if session.state == REVIEWING:
                session.cancel()
    finally:
        server.stop()

    if session.summary is not None:
        echo(format_summary(session.summary))
        return session.summary.exit_code()
    echo("中止しました。何も出力していません。PDF は input フォルダに残っています。")
    return 0
