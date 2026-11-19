"""config.ini → Settings。

DESIGN.md §18.1・§19.2 を実装する。Settings 他の dataclass を実際に構築する
ため、app.models への実行時 import は（config.py に限り）避けられない。
app.models が存在しない間は import 自体が失敗するので、このモジュールを
使うテストは app.models の実装後に実行する。
"""
from __future__ import annotations

import configparser
from pathlib import Path

from app.models import (
    ClassifyCfg,
    LayoutCfg,
    OutputCfg,
    PathsCfg,
    Settings,
    SplitCfg,
    TextLayerCfg,
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(Exception):
    """config.ini の読み込み・値の検証に失敗した時に投げる（キー名を含む）。"""


def _get_str(cp: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    return cp.get(section, key) if cp.has_option(section, key) else default


def _get_float(cp: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    if not cp.has_option(section, key):
        return default
    raw = cp.get(section, key)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{section}.{key}") from exc


def _get_int(cp: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    if not cp.has_option(section, key):
        return default
    raw = cp.get(section, key)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{section}.{key}") from exc


def _get_bool(cp: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    if not cp.has_option(section, key):
        return default
    raw = cp.get(section, key).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{section}.{key}")


def _get_tuple(
    cp: configparser.ConfigParser, section: str, key: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    if not cp.has_option(section, key):
        return default
    raw = cp.get(section, key)
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    return items if items else default


def _get_int_tuple(
    cp: configparser.ConfigParser, section: str, key: str, default: tuple[int, ...]
) -> tuple[int, ...]:
    """カンマ区切りの整数の並び。キーが無ければ default、値が空なら空の並び。"""
    if not cp.has_option(section, key):
        return default
    raw = cp.get(section, key)
    try:
        return tuple(int(x) for x in raw.split(",") if x.strip())
    except ValueError as exc:
        raise ConfigError(f"{section}.{key}") from exc


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def load_settings(base: Path, ini_path: Path | None = None) -> Settings:
    """config.ini から Settings を組み立てる。無ければ既定値のみで組み立てる。"""
    path = ini_path if ini_path is not None else base / "config.ini"
    cp = configparser.ConfigParser()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig") as f:
                cp.read_file(f)
        except (OSError, configparser.Error) as exc:
            raise ConfigError(str(path)) from exc

    def rel(key: str, default: str) -> Path:
        value = _get_str(cp, "paths", key, default)
        p = Path(value)
        return p if p.is_absolute() else base / p

    paths = PathsCfg(
        base=base,
        input=rel("input", "input"),
        output=rel("output", "output"),
        done=rel("done", "done"),
        error=rel("error", "error"),
        logs=rel("logs", "logs"),
        dict_dir=rel("dict", "dict"),
    )

    output = OutputCfg(
        formats=_get_tuple(cp, "output", "formats", ("csv", "xlsx")),
        split_pdf=_get_bool(cp, "output", "split_pdf", True),
        unclassified_sep=_unquote(_get_str(cp, "output", "unclassified_sep", '" | "')),
        review_mark=_get_str(cp, "output", "review_mark", "1"),
    )

    textlayer = TextLayerCfg(min_chars=_get_int(cp, "textlayer", "min_chars", 10))

    split = SplitCfg(
        img_min_area_ratio=_get_float(cp, "split", "img_min_area_ratio", 0.01),
        img_min_side_pt=_get_float(cp, "split", "img_min_side_pt", 56.7),
        img_page_cover_ratio=_get_float(cp, "split", "img_page_cover_ratio", 0.90),
        img_nested_ratio=_get_float(cp, "split", "img_nested_ratio", 0.90),
        xy_gap_min_pt=_get_float(cp, "split", "xy_gap_min_pt", 6.0),
        xy_gap_y_ratio=_get_float(cp, "split", "xy_gap_y_ratio", 1.2),
        xy_gap_x_ratio=_get_float(cp, "split", "xy_gap_x_ratio", 3.0),
        xy_relax_factor=_get_float(cp, "split", "xy_relax_factor", 0.5),
        xy_max_depth=_get_int(cp, "split", "xy_max_depth", 8),
        card_min_chars=_get_int(cp, "split", "card_min_chars", 15),
        card_min_signals=_get_int(cp, "split", "card_min_signals", 2),
        multi_cjk_company_min=_get_int(cp, "split", "multi_cjk_company_min", 2),
        multi_email_min=_get_int(cp, "split", "multi_email_min", 3),
        multi_postal_min=_get_int(cp, "split", "multi_postal_min", 3),
        row_overlap_min=_get_float(cp, "split", "row_overlap_min", 0.5),
        detect_cards=_get_bool(cp, "split", "detect_cards", True),
        detect_dpi=_get_int(cp, "split", "detect_dpi", 150),
        detect_threshold=_get_int(cp, "split", "detect_threshold", 250),
        # 足し引きした結果が 1〜255 に入らない値は、探す時に飛ばす（detect_threshold を上げ下げしても起動できるように）
        detect_threshold_steps=_get_int_tuple(cp, "split", "detect_threshold_steps", (-5, -10, -20, 2)),
        detect_line_mm=_get_float(cp, "split", "detect_line_mm", 20.0),
        detect_gap_mm=_get_float(cp, "split", "detect_gap_mm", 4.0),
        card_short_mm_min=_get_float(cp, "split", "card_short_mm_min", 40.0),
        card_short_mm_max=_get_float(cp, "split", "card_short_mm_max", 70.0),
        card_long_mm_min=_get_float(cp, "split", "card_long_mm_min", 75.0),
        card_long_mm_max=_get_float(cp, "split", "card_long_mm_max", 105.0),
        detect_margin_mm=_get_float(cp, "split", "detect_margin_mm", 3.0),
        frame_out_ratio=_get_float(cp, "split", "frame_out_ratio", 0.05),
        fit_cards=_get_bool(cp, "split", "fit_cards", True),
        fit_edge_min_mm=_get_float(cp, "split", "fit_edge_min_mm", 4.0),
        fit_min_score=_get_float(cp, "split", "fit_min_score", 0.35),
        card_std_long_mm=_get_float(cp, "split", "card_std_long_mm", 91.0),
        card_std_short_mm=_get_float(cp, "split", "card_std_short_mm", 55.0),
        frag_size_ratio=_get_float(cp, "split", "frag_size_ratio", 0.75),
        frag_merged_ratio=_get_float(cp, "split", "frag_merged_ratio", 1.3),
    )

    layout = LayoutCfg(
        line_overlap_min=_get_float(cp, "layout", "line_overlap_min", 0.5),
        space_gap_ratio=_get_float(cp, "layout", "space_gap_ratio", 0.25),
        segment_gap_ratio=_get_float(cp, "layout", "segment_gap_ratio", 2.5),
    )

    classify = ClassifyCfg(
        name_min_chars=_get_int(cp, "classify", "name_min_chars", 2),
        name_max_chars=_get_int(cp, "classify", "name_max_chars", 6),
        name_part_max_chars=_get_int(cp, "classify", "name_part_max_chars", 5),
        name_size_ratio=_get_float(cp, "classify", "name_size_ratio", 1.25),
        name_space_dominance=_get_float(cp, "classify", "name_space_dominance", 1.3),
        name_merge_gap_ratio=_get_float(cp, "classify", "name_merge_gap_ratio", 4.0),
        address_max_lines=_get_int(cp, "classify", "address_max_lines", 3),
        address_line_gap_ratio=_get_float(cp, "classify", "address_line_gap_ratio", 1.0),
        address_size_tolerance=_get_float(cp, "classify", "address_size_tolerance", 0.3),
    )

    return Settings(
        paths=paths, output=output, textlayer=textlayer,
        split=split, layout=layout, classify=classify,
    )
