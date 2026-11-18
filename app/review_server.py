"""確認画面の要求の振り分けと、待ち受けの終わりの見方。

待ち受けそのもの（ソケット・要求の検査・共通のヘッダー）は local_server にある。このモジュールは
ソケットに触らない（I10）。ここにあるのは、確認画面の経路・画面の部品の表・ReviewSession への
受け渡しと、「出力か中止が済んだら待ち受けを止める」の判断だけ。
"""
from __future__ import annotations

import os  # noqa: F401 - open_browser の差し替え（テスト）が、このモジュールの os を通して行われる
import re
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.local_server import (  # noqa: F401 - _Handler・open_browser は、このモジュールの名前としても使われる
    BaseApp,
    LocalServer,
    Response,
    Route,
    _Handler,
    error_response,
    json_response,
    open_browser,
    parse_json_object,
)
from app.review import CANCELLED, FINISHED, FINISHING

if TYPE_CHECKING:
    from app.review import ReviewSession

MAX_BODY = 256 * 1024          # 要求の本文の上限
_FLUSH_GRACE = 3.0             # 終了状態になってから、応答が書き終わるのを待つ上限 秒
_VIEWER_GRACE = 4.0            # 出力の最中に開き直した画面が、結果を取りに来るのを待つ上限 秒
_UI_DIR = Path(__file__).resolve().parent / "review_ui"
_INDEX_RE = re.compile(r"[0-9]{1,4}")

# 画面の部品。ここに無い名前は返さない
_STATIC: dict[str, tuple[Path, str]] = {
    "": (_UI_DIR / "index.html", "text/html; charset=utf-8"),
    "app.js": (_UI_DIR / "app.js", "text/javascript; charset=utf-8"),
    "style.css": (_UI_DIR / "style.css", "text/css; charset=utf-8"),
}


class ReviewApp(BaseApp):
    """確認画面の要求 1 件を ReviewSession に渡す。要求の検査は BaseApp が行う。

    経路の種類: state / image / frames / reset / viewed / finish / cancel。
    """

    def __init__(self, session: "ReviewSession", token: str, port: int) -> None:
        super().__init__(token, port, static=_STATIC, max_body=MAX_BODY)
        self._session = session
        self.viewer_waiting = False            # 出力の最中に、状態を尋ねてきた画面がある
        self.result_told = threading.Event()   # 出力・中止の後の状態（結果）を、画面に返した

    def parse_route(self, method: str, rest: list[str]) -> Route | None:
        if method == "GET":
            if rest == ["api", "state"]:
                return Route("state")
            want: tuple[str, ...] = ("image",)
        else:
            if rest == ["api", "finish"]:
                return Route("finish")
            if rest == ["api", "cancel"]:
                return Route("cancel")
            want = ("frames", "reset", "viewed")
        if len(rest) == 4 and rest[0] == "api" and rest[1] in want:
            if _INDEX_RE.fullmatch(rest[2]) and _INDEX_RE.fullmatch(rest[3]):
                return Route(rest[1], args=(int(rest[2]), int(rest[3])))
        return None

    def lock(self) -> AbstractContextManager[Any]:
        return self._session.lock

    def before_lock(self, route: Route) -> Response | None:
        if route.kind == "state" and getattr(self._session, "state", None) == FINISHING:
            # 出力の最中は、終わるのを待たせずに「出力中」とだけ答える（画面が待ちの表示を出す）
            self.viewer_waiting = True
            return json_response(200, {"state": FINISHING, "files": [], "summary": None})
        return None

    def log_error(self, name: str) -> None:
        self._session.log_error(name)

    def final_on_error(self, route: Route) -> bool:
        # 出力・中止の途中で止まった時は、待ち受けを残さない（もう一度出力させない）
        return route.kind in ("finish", "cancel")

    def run(self, method: str, route: Route, body: bytes) -> Response:
        if route.kind == "state":
            state = self._session.state_json()
            if state.get("summary") is not None:
                self.result_told.set()
            return json_response(200, state)
        if route.kind == "image":
            fi, pi = route.args
            return Response(200, "image/jpeg", self._session.page_image(fi, pi))

        payload = parse_json_object(body)
        if payload is None:
            return error_response(400, "bad_request")
        if route.kind == "finish":
            return json_response(200, self._session.finish(payload), final=True)
        if route.kind == "cancel":
            return json_response(200, self._session.cancel(), final=True)
        fi, pi = route.args
        if route.kind == "frames":
            return json_response(200, self._session.set_frames(fi, pi, payload))
        if route.kind == "reset":
            return json_response(200, self._session.reset_page(fi, pi, payload))
        return json_response(200, self._session.mark_viewed(fi, pi))


class ReviewServer(LocalServer):
    """確認画面の待ち受けの開始・待機・停止。"""

    def __init__(self, session: "ReviewSession") -> None:
        self._session = session
        super().__init__(lambda token, port: ReviewApp(session, token, port))

    def wait(self) -> None:
        """出力か中止の応答を返し終えるまで待つ。Ctrl+C を受け取れるよう、短い間隔で見る。"""
        done: threading.Event = self._httpd.done   # type: ignore[attr-defined]
        while not done.wait(0.5):
            # 二重の守り: 合図が立たなくても、出力・中止が済んでいれば抜ける
            if getattr(self._session, "state", None) in (FINISHED, CANCELLED):
                done.wait(_FLUSH_GRACE)
                break
        app: ReviewApp = self._httpd.app           # type: ignore[attr-defined]
        if app.viewer_waiting:
            # 出力の最中に開き直した画面は 1 秒おきに状態を尋ねている。結果を渡してから止める
            app.result_told.wait(_VIEWER_GRACE)
