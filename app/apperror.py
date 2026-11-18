"""画面（確認画面・辞書の画面）からの要求を受け付けられない時の例外。

待ち受け（local_server）と、画面ごとのモジュール（review など）の両方が使うので、どちらにも
依らないこのモジュールに置く。ソケットも画面ごとの状態も import しない。
"""
from __future__ import annotations


class AppError(Exception):
    """画面からの要求を受け付けられない。code は固定の短い語で、抽出値や例外の内容を含めない。"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
