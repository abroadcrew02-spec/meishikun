"""出力行。DESIGN.md §13・§19.1。

`to_row` は app.batch が呼ぶ結合点（§19.3-3）。シグネチャ・引数名・列の並びは
DESIGN.md §13 のとおりに固定する。
"""
from __future__ import annotations

from app.models import CardRecord, LIMITS, OutputCfg, Reason, REASON_ORDER


def _slots(values: list[str], n: int) -> list[str]:
    """先頭から n 個を詰める。無い所は ""。n を超える分は切り捨てる（防御的）。"""
    out = list(values[:n])
    out += [""] * (n - len(out))
    return out


def _reason_str(reasons: list[Reason]) -> str:
    """REASON_ORDER の順・重複なし・`;` 区切り（I13）。"""
    ordered = sorted(set(reasons), key=REASON_ORDER.index)
    return ";".join(str(r) for r in ordered)


def _rect_str(rect: tuple[float, float, float, float]) -> str:
    x0, y0, x1, y1 = rect
    return f"{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}"


def to_row(card: CardRecord, cfg: OutputCfg) -> list[str]:
    """長さは len(COLUMNS) == 26。"""
    fields = card.fields
    row: list[str] = [
        card.file_name,
        str(card.page_no),
        card.label,
        fields.company,
        fields.name,
        fields.title,
        fields.department,
    ]
    # 郵便番号N・住所N を対で並べる（DESIGN.md §13）
    addresses = _slots(fields.addresses, LIMITS["address"])
    postals = _slots(fields.postals, LIMITS["address"])
    for postal, address in zip(postals, addresses):
        row += [postal, address]
    row += _slots(fields.phones, LIMITS["phone"])
    row += _slots(fields.faxes, LIMITS["fax"])
    row += _slots(fields.emails, LIMITS["email"])
    row += _slots(fields.urls, LIMITS["url"])
    row.append(cfg.unclassified_sep.join(fields.unclassified))
    row.append(cfg.review_mark if fields.reasons else "")
    row.append(_reason_str(fields.reasons))
    row.append(_rect_str(card.rect))
    return row


def needs_review(card: CardRecord) -> bool:
    """要確認理由が 1 つ以上あるか。"""
    return bool(card.fields.reasons)
