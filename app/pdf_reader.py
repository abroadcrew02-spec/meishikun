"""PDF 読み取り（PyMuPDF）。DESIGN.md §6・§19.1 を実装する。

I1（不変条件）: fitz を import するのはこのモジュールと split_pdf.py・
tests/cardgen.py だけ。読み取った座標はすべて表示空間に変換してから渡す
（I2）。PDF 空間へ戻すのは to_pdf_rect のみで、描画（分割確認 PDF）の時に
extract 側が使う。
"""
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import fitz

from app.models import (
    ErrorKind,
    Glyph,
    ImageBox,
    PageData,
    PageKind,
    PageStats,
)

if TYPE_CHECKING:
    from app.models import Matrix6, Rect4, SplitCfg, TextLayerCfg

# DESIGN.md §6.2: 既定の TEXTFLAGS_RAWDICT (199) から TEXT_CID_FOR_UNKNOWN_UNICODE と
# TEXT_PRESERVE_IMAGES を外したもの。ToUnicode の無いフォントの文字は U+FFFD のまま
# 受け取り、画像は get_image_info で別途取る。値は 75（固定値テストあり）。
TEXT_FLAGS: int = (
    fitz.TEXT_PRESERVE_WHITESPACE
    | fitz.TEXT_PRESERVE_LIGATURES
    | fitz.TEXT_MEDIABOX_CLIP
    | fitz.TEXT_INHIBIT_SPACES
)

_BBOX_ROUND_NDIGITS = 1


class PdfOpenError(Exception):
    """open_pdf が投げる。extract.py が kind を見て PdfResult.error_kind に変換する。

    メッセージには kind の値だけを入れる（I9: 抽出値・例外メッセージをログに
    書かないため、元の例外はメッセージに含めず __cause__ にだけ保持する）。
    """

    def __init__(self, kind: ErrorKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


def open_pdf(path: Path) -> fitz.Document:
    """path を開く。開けない／暗号化／PDF でない場合は PdfOpenError を投げる。

    実測（2026-10-06・PyMuPDF 1.26.7・Python 3.13.7 win32）:
    - 存在しないファイルは `fitz.FileNotFoundError`（RuntimeError のサブクラス）。
    - PDF として認識できない内容（拡張子は無関係）は `fitz.FileDataError`。
      空ファイルは `fitz.EmptyFileError`（FileDataError のサブクラス）。
    - 拡張子が .pdf でも内容が別形式（PNG 等）のファイルは例外を投げずに開けて
      `doc.is_pdf == False` になる。
    - 暗号化 PDF は例外を投げずに開けて `doc.needs_pass == True` になり、
      認証前にページへアクセスすると ValueError になる。
    """
    try:
        doc = fitz.open(str(path))
    except fitz.FileDataError as exc:
        raise PdfOpenError(ErrorKind.NOT_PDF) from exc
    except Exception as exc:  # noqa: BLE001 - 分類できない「開けない」系はまとめる
        raise PdfOpenError(ErrorKind.OPEN_FAILED) from exc
    if not doc.is_pdf:
        doc.close()
        raise PdfOpenError(ErrorKind.NOT_PDF)
    if doc.needs_pass:
        doc.close()
        raise PdfOpenError(ErrorKind.ENCRYPTED)
    return doc


def _transform_rect(rect: "Rect4 | fitz.Rect", m: fitz.Matrix) -> fitz.Rect:
    return fitz.Rect(rect) * m


def _transform_vector(dx: float, dy: float, m: fitz.Matrix) -> tuple[float, float]:
    """m の線形部分（平行移動 e, f を除く a, b, c, d）だけをベクトルに適用する。"""
    return (dx * m.a + dy * m.c, dx * m.b + dy * m.d)


def _round_rect(rect: fitz.Rect) -> "Rect4":
    return (
        round(rect.x0, _BBOX_ROUND_NDIGITS),
        round(rect.y0, _BBOX_ROUND_NDIGITS),
        round(rect.x1, _BBOX_ROUND_NDIGITS),
        round(rect.y1, _BBOX_ROUND_NDIGITS),
    )


def _read_images(page: fitz.Page) -> tuple[ImageBox, ...]:
    """§6.3-5: 配置ごとに 1 件、表示空間へ変換してページ矩形でクリップする。

    小数 1 桁に丸めて (xref, bbox) が一致するものは 1 件にまとめる。
    ※設計の穴（§6.3-5）: 本書の文言は bbox の丸め一致だけを基準にしているが、
    xref が異なる画像がたまたま同じ位置に重なるケースまでまとめると情報を
    失うため、判定キーは (xref, 丸め後 bbox) のペアとした（安全側の解釈）。
    他の候補: bbox のみ（xref 不問）でまとめる。
    """
    m = page.rotation_matrix
    page_rect = page.rect
    seen: set[tuple[int, "Rect4"]] = set()
    boxes: list[ImageBox] = []
    for info in page.get_image_info(xrefs=True):
        xref = info.get("xref", 0)
        disp_rect = _transform_rect(info["bbox"], m)
        clipped = disp_rect & page_rect
        rounded = _round_rect(clipped)
        key = (xref, rounded)
        if key in seen:
            continue
        seen.add(key)
        boxes.append(ImageBox(bbox=rounded, xref=xref))
    return tuple(boxes)


def _iter_glyphs(page: fitz.Page, m: fitz.Matrix) -> tuple[tuple[Glyph, ...], int, int, int]:
    """rawdict を走査して Glyph 列と文字数の集計を作る（§6.3-1〜4）。

    Glyph.bbox は丸めない（90 度回転の往復変換で 0.1pt 以内に収める AC のため）。
    丸めは画像の bbox（§6.3-5）だけに適用する。
    """
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    glyphs: list[Glyph] = []
    seq = 0
    chars = 0
    invisible_chars = 0
    replacement_chars = 0
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        block_no = block.get("number", 0)
        for line_no, line in enumerate(block["lines"]):
            dx, dy = line["dir"]
            vdx, vdy = _transform_vector(dx, dy, m)
            vertical = abs(vdy) > abs(vdx)
            line_key = (block_no, line_no)
            for span in line["spans"]:
                size = span["size"]
                alpha = span.get("alpha", 255)
                for ch_info in span["chars"]:
                    ch = ch_info["c"]
                    category = unicodedata.category(ch)
                    if category in ("Cc", "Cf") and ch != "\t":
                        continue  # §6.3-2: 制御文字・書式文字は除去（\t だけ残す）
                    disp_rect = _transform_rect(ch_info["bbox"], m)
                    glyphs.append(
                        Glyph(
                            ch=ch,
                            bbox=(disp_rect.x0, disp_rect.y0, disp_rect.x1, disp_rect.y1),
                            size=size,
                            vertical=vertical,
                            line_key=line_key,
                            seq=seq,
                        )
                    )
                    seq += 1
                    if not ch.isspace():
                        chars += 1
                        if alpha == 0:
                            invisible_chars += 1
                        if ch == "�":
                            replacement_chars += 1
    return tuple(glyphs), chars, invisible_chars, replacement_chars


def _read(page: fitz.Page, cfg: "TextLayerCfg") -> PageData:
    m = page.rotation_matrix
    glyphs, chars, invisible_chars, replacement_chars = _iter_glyphs(page, m)
    images = _read_images(page)
    rect = page.rect

    if chars >= cfg.min_chars:
        kind = PageKind.TEXT
    elif images:
        kind = PageKind.NO_TEXT
    else:
        kind = PageKind.BLANK

    stats = PageStats(
        index=page.number,
        width=rect.width,
        height=rect.height,
        rotation=page.rotation,
        chars=chars,
        invisible_chars=invisible_chars,
        replacement_chars=replacement_chars,
        image_boxes=tuple(box.bbox for box in images),
        kind=kind,
    )
    to_pdf: "Matrix6" = tuple(page.derotation_matrix)  # type: ignore[assignment]
    return PageData(stats=stats, glyphs=glyphs, images=images, to_pdf=to_pdf)


def read_page_stats(page: fitz.Page, cfg: "TextLayerCfg") -> PageStats:
    return _read(page, cfg).stats


def read_page(page: fitz.Page, cfg: "TextLayerCfg") -> PageData:
    return _read(page, cfg)


def detect_cards(page: fitz.Page, cfg: "SplitCfg"):
    """ページを描画して、名刺の枠（§7.8）と当てはめ用の線の切れ端（§7.9）を返す。

    表示空間で描画するので回転ページもそのまま。detect_cards が false なら ((), None)。
    """
    from app import carddetect

    if not cfg.detect_cards:
        return (), None
    zoom = cfg.detect_dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    rot = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom).prerotate(90), colorspace=fitz.csGRAY, alpha=False)
    args = (pix.samples, pix.width, pix.height, rot.samples, rot.width, rot.height, cfg.detect_dpi, cfg)
    boxes = carddetect.detect_card_boxes(*args)
    edges = carddetect.detect_edges(*args) if cfg.fit_cards else None
    return boxes, edges


def detect_card_boxes(page: fitz.Page, cfg: "SplitCfg") -> tuple["Rect4", ...]:
    """名刺の枠だけ（§7.8）。"""
    return detect_cards(page, cfg)[0]


def to_pdf_rect(rect: "Rect4", m: "Matrix6") -> "Rect4":
    r = fitz.Rect(rect) * fitz.Matrix(*m)
    return (r.x0, r.y0, r.x1, r.y1)
