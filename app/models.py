"""共有データ構造。DESIGN.md §5 の転記。変更は DESIGN.md を先に直す。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

Rect4 = tuple[float, float, float, float]                  # (x0, y0, x1, y1)。単位 pt
Matrix6 = tuple[float, float, float, float, float, float]  # fitz.Matrix の (a, b, c, d, e, f)

COLUMNS: tuple[str, ...] = (
    "ファイル名", "ページ", "ラベル", "会社名", "氏名", "役職", "部署",
    "郵便番号1", "住所1", "郵便番号2", "住所2", "郵便番号3", "住所3",
    "電話1", "電話2", "電話3",
    "FAX1", "FAX2", "メール1", "メール2", "URL1", "URL2",
    "未分類", "要確認", "要確認理由", "領域座標",
)

LIMITS: dict[str, int] = {"address": 3, "phone": 3, "fax": 2, "email": 2, "url": 2}


class Reason(StrEnum):
    """要確認理由。定義順が出力順。"""
    SPLIT_FALLBACK = "SPLIT_FALLBACK"
    SPLIT_WEAK = "SPLIT_WEAK"
    SPLIT_SUSPECT_MERGED = "SPLIT_SUSPECT_MERGED"
    SPLIT_ORPHAN_TEXT = "SPLIT_ORPHAN_TEXT"
    REGION_NO_TEXT = "REGION_NO_TEXT"
    VERTICAL_TEXT = "VERTICAL_TEXT"
    UNREADABLE_CHARS = "UNREADABLE_CHARS"
    COMPANY_MULTI = "COMPANY_MULTI"
    NAME_NOT_FOUND = "NAME_NOT_FOUND"
    NAME_MULTI_CANDIDATES = "NAME_MULTI_CANDIDATES"
    PHONE_FAX_GUESSED = "PHONE_FAX_GUESSED"
    OCR_DIGIT_FIX = "OCR_DIGIT_FIX"
    AREA_CODE_UNKNOWN = "AREA_CODE_UNKNOWN"
    PHONE_DIGITS_INVALID = "PHONE_DIGITS_INVALID"
    OVERFLOW_ADDRESS = "OVERFLOW_ADDRESS"
    OVERFLOW_PHONE = "OVERFLOW_PHONE"
    OVERFLOW_FAX = "OVERFLOW_FAX"
    OVERFLOW_EMAIL = "OVERFLOW_EMAIL"
    OVERFLOW_URL = "OVERFLOW_URL"


REASON_ORDER: tuple[Reason, ...] = tuple(Reason)


class SplitMethod(StrEnum):
    IMAGE = "image"        # 画像 bbox を領域にした
    DETECTED = "detected"  # ページ画像から見つけた名刺の枠を領域にした（§7.8）
    FITTED = "fitted"      # 縁が取れず、名刺の型を線の切れ端に当てはめた枠（§7.9）
    XYCUT = "xycut"        # 余白で切った
    SINGLE = "single"      # 切れなかったが 1 枚の名刺として妥当
    FALLBACK = "fallback"  # 切れず、名刺としても妥当でない（SPLIT_FALLBACK を付ける）


class PageKind(StrEnum):
    TEXT = "text"          # 非空白文字数 >= min_chars
    NO_TEXT = "no_text"    # 非空白文字数 < min_chars かつ 画像 >= 1（除外。要約に p 番号）
    BLANK = "blank"        # 非空白文字数 < min_chars かつ 画像 0（黙って飛ばす）


class FileStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"    # NO_TEXT ページあり、処理可能ページ >= 1
    NO_TEXT = "no_text"    # 処理可能ページ 0
    ERROR = "error"


class ErrorKind(StrEnum):
    OPEN_FAILED = "OPEN_FAILED"
    ENCRYPTED = "ENCRYPTED"
    NOT_PDF = "NOT_PDF"
    EXTRACT_FAILED = "EXTRACT_FAILED"


class HitKind(StrEnum):
    POSTAL = "postal"
    PHONE = "phone"
    FAX = "fax"
    PHONE_FAX = "phone_fax"   # 「TEL/FAX」の 1 番号 → 電話と FAX の両方に入れる
    EMAIL = "email"
    URL = "url"


# ---- PDF 読み取り結果（座標はすべて表示空間。§7.1） ----

@dataclass(frozen=True, slots=True)
class Glyph:
    ch: str                      # 原文 1 文字（制御文字は除去済み。空白文字も含む）
    bbox: Rect4
    size: float                  # 所属 span のフォントサイズ pt
    vertical: bool               # 所属 line の向きが縦
    line_key: tuple[int, int]    # (block_no, line_no)。縦書き行の再構成にだけ使う
    seq: int                     # ページ内の出現順。並べ替えの同点決着に使う


@dataclass(frozen=True, slots=True)
class ImageBox:
    bbox: Rect4
    xref: int


@dataclass(frozen=True, slots=True)
class PageStats:
    index: int                   # 0 始まり
    width: float                 # 表示空間 pt
    height: float
    rotation: int                # 0 / 90 / 180 / 270
    chars: int                   # 非空白文字数
    invisible_chars: int         # alpha == 0 の span に属する非空白文字数
    replacement_chars: int       # U+FFFD の数
    image_boxes: tuple[Rect4, ...]   # 全画像配置（フィルタ前・重複除去後）
    kind: PageKind


@dataclass(frozen=True, slots=True)
class EdgeMap:
    """ページ画像の短い直線の切れ端（§7.9）。座標は描画 px（dpi で pt に直す）。"""
    dpi: float
    horiz: tuple[tuple[int, int, int], ...]   # (行 y, x0, x1)
    vert: tuple[tuple[int, int, int], ...]    # (列 x, y0, y1)


@dataclass(frozen=True, slots=True)
class PageData:
    stats: PageStats
    glyphs: tuple[Glyph, ...]
    images: tuple[ImageBox, ...]
    to_pdf: Matrix6              # 表示空間 → PDF 空間（page.derotation_matrix）
    card_boxes: tuple[Rect4, ...] = ()  # 画像から見つけた名刺の枠（表示空間。§7.8）
    card_edges: EdgeMap | None = None   # 枠の当てはめに使う線の切れ端（§7.9）


# ---- 分割結果 ----

@dataclass(slots=True)
class Region:
    page_index: int
    rect: Rect4                  # 表示空間。領域座標列と読み順に使う
    glyphs: list[Glyph]
    method: SplitMethod
    flags: list[Reason] = field(default_factory=list)


# ---- テキスト ----

@dataclass(frozen=True, slots=True)
class NormText:
    raw: str                                 # 原文（見たまま）
    norm: str                                # 判定用
    src: tuple[tuple[int, int], ...]         # len(src) == len(norm)。norm[k] は raw[src[k][0]:src[k][1]] 由来。非減少

    def raw_span(self, start: int, end: int) -> tuple[int, int]:
        """norm の [start, end) に対応する raw の範囲。0 <= start < end <= len(norm)。"""
        return (self.src[start][0], self.src[end - 1][1])

    def raw_slice(self, start: int, end: int) -> str:
        if start >= end:
            return ""
        a, b = self.raw_span(start, end)
        return self.raw[a:b]


@dataclass(frozen=True, slots=True)
class TextLine:
    text: NormText
    bbox: Rect4
    size: float                  # 行内グリフのフォントサイズ中央値
    vertical: bool
    gaps: tuple[float, ...]      # len == len(text.raw)。空白位置に直前・直後グリフ間の距離 pt、他は 0.0
    row: int                     # 領域内の行番号（y 帯）。0 始まり
    col: int                     # 同じ row 内の左からの順番


@dataclass(frozen=True, slots=True)
class Hit:
    kind: HitKind
    start: int                   # TextLine.text.norm 上の消費範囲 [start, end)。接頭辞・ラベルを含む
    end: int
    value_start: int             # 値そのものの範囲（接頭辞を含まない）
    value_end: int
    digits: str                  # 電話・FAX・郵便番号: 数字列（+81 処理後）。それ以外は ""
    value: str                   # メール・URL: 整形前の値（空白除去済み）。それ以外は ""
    prefixed: bool               # 接頭辞あり（電話・FAX）
    mobile_prefix: bool          # 接頭辞が「携帯」「Mobile」
    ocr_fixed: bool               # O→0 / l→1 等の補正を行った


@dataclass(frozen=True, slots=True)
class CardSignals:
    chars: int
    has_phone: bool
    has_fax: bool
    has_email: bool
    has_url: bool
    has_company: bool
    has_name_like: bool
    cjk_companies: int
    emails: int
    postals: int
    name_pieces: tuple[tuple[str, float], ...] = ()  # 氏名らしい語（norm・空白除去）と行の size。§7.6
    median_size: float = 0.0                         # 横書き行の size の中央値（本文の文字の大きさ）

    @property
    def signals(self) -> int:
        return sum((self.has_phone, self.has_fax, self.has_email, self.has_url, self.has_company))

    def prominent_names(self, ref_size: float, ratio: float) -> frozenset[str]:
        """本文の文字の大きさ ref_size に対して ratio 倍以上の氏名らしい語（§7.6・2026-10-06）。

        ref_size は比べる相手（隣の葉）も含めた本文の大きさを渡す。小さい文字の氏名らしい
        注記（`再生紙使用`）を氏名と見なさないため。
        """
        return frozenset(t for t, size in self.name_pieces if size >= ratio * ref_size)

    @property
    def identity(self) -> bool:
        return self.has_company or self.has_name_like

    @property
    def contact(self) -> bool:
        return self.has_phone or self.has_fax or self.has_email or self.has_url


# ---- 項目 ----

@dataclass(slots=True)
class CardFields:
    company: str = ""
    name: str = ""
    title: str = ""
    department: str = ""          # 部署（§11.5b。複数行は U+0020 で連結）
    addresses: list[str] = field(default_factory=list)
    postals: list[str] = field(default_factory=list)    # addresses と同じ長さ。整形済み郵便番号、無ければ ""
    phones: list[str] = field(default_factory=list)
    faxes: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    reasons: list[Reason] = field(default_factory=list)   # 重複なし。順序は rows.to_row で REASON_ORDER に揃える


@dataclass(slots=True)
class CardRecord:
    file_name: str               # 入力時のファイル名（done\ での連番後の名前ではない）
    page_no: int                 # 1 始まり
    label: str
    fields: CardFields
    rect: Rect4                  # 表示空間（領域座標列）
    draw_rect: Rect4              # PDF 空間（領域の矩形）
    method: SplitMethod
    text_rect: Rect4 | None = None       # 表示空間。領域内の非空白文字を囲む矩形（無ければ None）
    draw_text_rect: Rect4 | None = None  # PDF 空間。分割確認 PDF の枠はこちらを使う（§14.4）


@dataclass(slots=True)
class PdfResult:
    file_name: str
    path: Path
    status: FileStatus
    page_count: int = 0
    cards: list[CardRecord] = field(default_factory=list)
    skipped_pages: list[int] = field(default_factory=list)   # NO_TEXT。1 始まり
    blank_pages: list[int] = field(default_factory=list)     # BLANK。1 始まり
    error_kind: ErrorKind | None = None
    error_type: str | None = None    # 例外クラス名のみ。メッセージは保持しない


# ---- 設定（config.py が組み立てる。値の既定は §18.1） ----

@dataclass(frozen=True, slots=True)
class PathsCfg:
    base: Path
    input: Path
    output: Path
    done: Path
    error: Path
    logs: Path
    dict_dir: Path


@dataclass(frozen=True, slots=True)
class OutputCfg:
    formats: tuple[str, ...] = ("csv", "xlsx")
    split_pdf: bool = True
    unclassified_sep: str = " | "
    review_mark: str = "1"


@dataclass(frozen=True, slots=True)
class TextLayerCfg:
    min_chars: int = 10


@dataclass(frozen=True, slots=True)
class SplitCfg:
    img_min_area_ratio: float = 0.01
    img_min_side_pt: float = 56.7
    img_page_cover_ratio: float = 0.90
    img_nested_ratio: float = 0.90
    xy_gap_min_pt: float = 6.0
    xy_gap_y_ratio: float = 1.2
    xy_gap_x_ratio: float = 3.0
    xy_relax_factor: float = 0.5
    xy_max_depth: int = 8
    card_min_chars: int = 15
    card_min_signals: int = 2
    multi_cjk_company_min: int = 2
    multi_email_min: int = 3
    multi_postal_min: int = 3
    row_overlap_min: float = 0.5
    detect_cards: bool = True          # §7.8: ページ画像から名刺の枠を探す
    detect_dpi: int = 150
    detect_threshold: int = 250        # これより暗い画素を線の候補にする（255 = 白）
    detect_line_mm: float = 20.0       # この長さ以上の直線だけを縁とみなす
    detect_gap_mm: float = 8.0         # 縁の線の途切れをこの長さまでつなぐ
    card_short_mm_min: float = 40.0    # 名刺の大きさ（短辺・長辺）
    card_short_mm_max: float = 70.0
    card_long_mm_min: float = 75.0
    card_long_mm_max: float = 105.0
    detect_margin_mm: float = 3.0      # 枠のすぐ外の文字もその名刺に含める幅
    fit_cards: bool = True             # §7.9: 縁が取れなかった名刺に名刺の型を当てはめる
    fit_edge_min_mm: float = 4.0       # 当てはめに使う線の切れ端の最短の長さ
    fit_min_score: float = 0.35        # 4 辺が線の切れ端と重なる割合の平均がこれ以上なら採用
    card_std_long_mm: float = 91.0     # 型が決められない時（枠が 1 つも取れない）の名刺の大きさ
    card_std_short_mm: float = 55.0
    frag_size_ratio: float = 0.75     # §7.5-4: 典型的な葉のこの倍率未満なら「断片」
    frag_merged_ratio: float = 1.3    # §7.5-4: 断片 2 つを合わせた文字の範囲がこの倍率以内なら 1 枚に戻す


@dataclass(frozen=True, slots=True)
class LayoutCfg:
    line_overlap_min: float = 0.5
    space_gap_ratio: float = 0.25
    segment_gap_ratio: float = 2.5


@dataclass(frozen=True, slots=True)
class ClassifyCfg:
    name_min_chars: int = 2
    name_max_chars: int = 6
    name_part_max_chars: int = 5
    name_size_ratio: float = 1.25
    name_space_dominance: float = 1.3
    name_merge_gap_ratio: float = 4.0
    address_max_lines: int = 3
    address_line_gap_ratio: float = 1.0
    address_size_tolerance: float = 0.3


@dataclass(frozen=True, slots=True)
class Settings:
    paths: PathsCfg
    output: OutputCfg
    textlayer: TextLayerCfg
    split: SplitCfg
    layout: LayoutCfg
    classify: ClassifyCfg


@dataclass(frozen=True, slots=True)
class Dictionaries:
    titles: tuple[str, ...]          # 判定用正規化済み。長い順
    corp_suffixes: tuple[str, ...]   # 判定用正規化済み。長い順
    dept_suffixes: tuple[str, ...]   # 判定用正規化済み。長い順
    area_codes: frozenset[str]       # 先頭 0 を含む 2〜5 桁の数字列
    old_kanji: dict[str, str]        # 1 文字 → 1 文字
