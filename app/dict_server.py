"""辞書の画面の要求の振り分けと、待ち受けの終わりの見方。

待ち受けそのもの（ソケット・要求の検査・共通のヘッダー）は local_server にある。このモジュールは
ソケットに触らない（I10）。ここにあるのは、辞書の画面の経路・画面の部品の表・DictSession への
受け渡しと、「終わる」が押されたら（または長く操作が無ければ）待ち受けを止める判断だけ。
"""
from __future__ import annotations

import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.dict_edit import CLOSED, SCREEN_KINDS
from app.local_server import (  # noqa: F401 - open_browser は、このモジュールの名前としても使われる
    BaseApp,
    LocalServer,
    Response,
    Route,
    error_response,
    json_response,
    open_browser,
    parse_json_object,
)

if TYPE_CHECKING:
    from app.dict_edit import DictSession

MAX_BODY = 64 * 1024           # 要求の本文の上限（語 1 件ぶんの JSON には十分）
IDLE_LIMIT = 60 * 60.0         # 保存していない変更が無いまま、この秒数だけ要求が無ければ止める
_FLUSH_GRACE = 3.0             # 終了状態になってから、応答が書き終わるのを待つ上限 秒
_APP_DIR = Path(__file__).resolve().parent
_UI_DIR = _APP_DIR / "dict_ui"

# 画面の部品。ここに無い名前は返さない。色・書体・ボタンは確認画面の style.css を共用する
_STATIC: dict[str, tuple[Path, str]] = {
    "": (_UI_DIR / "index.html", "text/html; charset=utf-8"),
    "app.js": (_UI_DIR / "app.js", "text/javascript; charset=utf-8"),
    "style.css": (_APP_DIR / "review_ui" / "style.css", "text/css; charset=utf-8"),
    "dict.css": (_UI_DIR / "dict.css", "text/css; charset=utf-8"),
}
_KIND_KEYS = frozenset(info.key for info in SCREEN_KINDS)
_KIND_ACTIONS = ("add", "remove", "restore")


class DictApp(BaseApp):
    """辞書の画面の要求 1 件を DictSession に渡す。要求の検査は BaseApp が行う。

    経路の種類: state / add / remove / restore / save / reload / trial / trial_cancel / close。
    add・remove・restore の辞書の種類は、固定の名前の表（_KIND_KEYS）にあるものだけ。
    """

    def __init__(self, session: "DictSession", token: str, port: int) -> None:
        super().__init__(token, port, static=_STATIC, max_body=MAX_BODY)
        self._session = session
        self.last_request = time.monotonic()   # 最後に要求を受けた時刻（放置の判定に使う）

    def parse_route(self, method: str, rest: list[str]) -> Route | None:
        if method == "GET":
            return Route("state") if rest == ["api", "state"] else None
        if len(rest) == 2 and rest[0] == "api" and rest[1] in ("save", "reload", "trial", "close"):
            return Route(rest[1])
        if rest == ["api", "trial", "cancel"]:
            return Route("trial_cancel")
        if len(rest) == 3 and rest[0] == "api" and rest[1] in _KIND_ACTIONS and rest[2] in _KIND_KEYS:
            return Route(rest[1], name=rest[2])
        return None

    def lock(self) -> AbstractContextManager[Any]:
        return self._session.lock

    def before_lock(self, route: Route) -> Response | None:
        self.last_request = time.monotonic()
        return None

    def log_error(self, name: str) -> None:
        self._session.log_error(name)

    def final_on_error(self, route: Route) -> bool:
        return False                           # 保存や「終わる」が失敗しても、画面は開いたままにする

    def run(self, method: str, route: Route, body: bytes) -> Response:
        session = self._session
        if route.kind == "state":
            return json_response(200, session.state_json())
        payload = parse_json_object(body)
        if payload is None:
            return error_response(400, "bad_request")
        if route.kind == "add":
            return json_response(200, session.add(route.name, payload))
        if route.kind == "remove":
            return json_response(200, session.remove(route.name, payload))
        if route.kind == "restore":
            return json_response(200, session.restore(route.name, payload))
        if route.kind == "save":
            return json_response(200, session.save(payload))
        if route.kind == "reload":
            return json_response(200, session.reload(payload))
        if route.kind == "trial":
            return json_response(200, session.start_trial(payload))
        if route.kind == "trial_cancel":
            return json_response(200, session.cancel_trial(payload))
        return json_response(200, session.close(payload), final=True)


class DictServer(LocalServer):
    """辞書の画面の待ち受けの開始・待機・停止。"""

    def __init__(self, session: "DictSession") -> None:
        self._session = session
        super().__init__(lambda token, port: DictApp(session, token, port))

    def wait(self) -> None:
        """「終わる」の応答を返し終えるまで待つ。Ctrl+C を受け取れるよう、短い間隔で見る。"""
        done: threading.Event = self._httpd.done   # type: ignore[attr-defined]
        app: DictApp = self._httpd.app             # type: ignore[attr-defined]
        while not done.wait(0.5):
            if self._session.state == CLOSED:      # 二重の守り: 合図が立たなくても、終わっていれば抜ける
                done.wait(_FLUSH_GRACE)
                break
            if time.monotonic() - app.last_request > IDLE_LIMIT:
                with self._session.lock:
                    # 開いたまま放置されると、全自動も確認画面も動かせない。変更が無ければ閉じる
                    if self._session.pending() == 0 and not self._session.busy:
                        self._session.idle_closed = True
                        self._session.close({})
                        break
                    app.last_request = time.monotonic()
