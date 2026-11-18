"""確認画面の待ち受け（この PC の中だけ）。

app\\ の中でソケットを開くのはこのモジュールだけ（I10）。外部へは接続せず、名前解決もしない。

- 127.0.0.1 にだけ bind する。他の PC からは接続できない。127.0.0.1 の同じポートへの
  二重の bind を許さない（別の利用者に要求を横取りされないようにする）
- 実行ごとに作る合言葉（トークン）を URL の先頭に置き、一致しない要求は 404 にする
- Host が `127.0.0.1:<ポート>` でない要求は 403（DNS リバインディング対策）
- 状態を変える要求は、JSON であること・この画面自身からの送信であることを確かめる
- 要求の検査（check）は本文を読む前に行う。通った要求だけ本文を読む
- 要求の内容・合言葉・例外の中身をコンソールやログに出さない（I9）

要求の検査と振り分けは ReviewApp にまとめてある（ソケットを使わずにテストできる）。
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.review import CANCELLED, FINISHED, FINISHING, ReviewError

if TYPE_CHECKING:
    from app.review import ReviewSession

HOST = "127.0.0.1"
MAX_BODY = 256 * 1024          # 要求の本文の上限
MAX_CONNECTIONS = 16           # 同時に処理する接続の上限
_SOCKET_TIMEOUT = 20.0         # 1 回の読み書きを待つ上限 秒
_REQUEST_DEADLINE = 15.0       # 接続してから要求（見出しと本文）を送り終えるまでの上限 秒
_FLUSH_GRACE = 3.0             # 終了状態になってから、応答が書き終わるのを待つ上限 秒
_VIEWER_GRACE = 4.0            # 出力の最中に開き直した画面が、結果を取りに来るのを待つ上限 秒
_UI_DIR = Path(__file__).resolve().parent / "review_ui"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_INDEX_RE = re.compile(r"[0-9]{1,4}")

# 画面の部品。ここに無い名前は返さない。Content-Type は固定の表で付ける
# （Windows の設定によっては .js が text/plain と判定され、nosniff と合わさって動かなくなるため）
_STATIC: dict[str, tuple[str, str]] = {
    "": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
}

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)
_COMMON_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", _CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)


@dataclass(slots=True)
class Response:
    status: int
    content_type: str
    body: bytes
    final: bool = False                       # この応答を返したら待ち受けを止める
    headers: list[tuple[str, str]] = field(default_factory=list)

    def all_headers(self) -> list[tuple[str, str]]:
        return [
            ("Content-Type", self.content_type),
            ("Content-Length", str(len(self.body))),
            *_COMMON_HEADERS,
            *self.headers,
        ]


def _json(status: int, payload: Any, final: bool = False) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status, "application/json; charset=utf-8", body, final=final)


def _error(status: int, code: str) -> Response:
    return _json(status, {"error": code})


def _reject_constant(_name: str) -> Any:
    raise ValueError("constant")   # NaN・Infinity は受け付けない


@dataclass(frozen=True, slots=True)
class _Route:
    kind: str                 # static / state / image / frames / reset / viewed / finish / cancel
    name: str = ""            # static の時の部品名
    fi: int = 0
    pi: int = 0


def _parse_route(method: str, rest: list[str]) -> _Route | None:
    if method == "GET":
        if len(rest) == 1 and rest[0] in _STATIC:
            return _Route("static", name=rest[0])
        if rest == ["api", "state"]:
            return _Route("state")
        want = ("image",)
    else:
        if rest == ["api", "finish"]:
            return _Route("finish")
        if rest == ["api", "cancel"]:
            return _Route("cancel")
        want = ("frames", "reset", "viewed")
    if len(rest) == 4 and rest[0] == "api" and rest[1] in want:
        if _INDEX_RE.fullmatch(rest[2]) and _INDEX_RE.fullmatch(rest[3]):
            return _Route(rest[1], fi=int(rest[2]), pi=int(rest[3]))
    return None


class ReviewApp:
    """要求 1 件を検査して ReviewSession に渡す。ソケットには触らない。"""

    def __init__(self, session: "ReviewSession", token: str, port: int) -> None:
        self._session = session
        self._token = token.encode("ascii")
        self._host = f"{HOST}:{port}"
        self._origin = f"http://{HOST}:{port}"
        self._static = {
            name: ((_UI_DIR / file).read_bytes(), ctype) for name, (file, ctype) in _STATIC.items()
        }
        self.viewer_waiting = False            # 出力の最中に、状態を尋ねてきた画面がある
        self.result_told = threading.Event()   # 出力・中止の後の状態（結果）を、画面に返した

    def check(self, method: str, target: str, headers: Mapping[str, str]) -> "Response | _Route":
        """要求行とヘッダーだけで検査する。受け付けるなら行き先、拒否するなら応答を返す。

        本文を読む前に呼ぶ（合言葉を知らない相手に大きな本文を読ませられないようにする）。
        セッションには触らないので、ロックは要らない。headers のキーは小文字。
        """
        if headers.get("host") != self._host:
            return _error(403, "forbidden")
        parts = target.split("/")
        # 期待する形: /<合言葉>/<残り…>。クエリや # の付いた要求は受け付けない
        if (
            len(parts) < 3 or parts[0] != "" or "?" in target or "#" in target
            or not _TOKEN_RE.fullmatch(parts[1])
            or not hmac.compare_digest(parts[1].encode("ascii"), self._token)
        ):
            return _error(404, "not_found")
        if method not in ("GET", "POST"):
            return _error(405, "method_not_allowed")
        route = _parse_route(method, parts[2:])
        if route is None:
            return _error(404, "not_found")

        site = headers.get("sec-fetch-site")
        if method == "GET":
            # 画面そのものは、アドレス欄からの移動（none）か自分自身から。それ以外は自分自身からだけ
            allowed = (None, "none", "same-origin") if route.kind == "static" else (None, "same-origin")
            if site not in allowed:
                return _error(403, "forbidden")
            return route

        # POST: この画面自身からの JSON だけを受け付ける
        if site not in (None, "same-origin") or headers.get("origin") != self._origin:
            return _error(403, "forbidden")
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error(415, "unsupported_media_type")
        if "transfer-encoding" in headers:
            return _error(400, "bad_request")
        length = headers.get("content-length", "")
        if not (length.isascii() and length.isdigit() and len(length) <= 7):
            return _error(411, "length_required")
        if int(length) > MAX_BODY:
            return _error(413, "too_large")
        return route

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Response:
        """検査してから処理する。例外は外に出さず、固定の短い応答にする。"""
        route = self.check(method, target, headers)
        if isinstance(route, Response):
            return route
        if len(body) > MAX_BODY:
            return _error(413, "too_large")
        if route.kind == "static":
            # 画面の部品はセッションに触らない。出力の最中（ロックを持ったまま数十秒かかりうる）に
            # 開き直されても、すぐ返す
            data, ctype = self._static[route.name]
            return Response(200, ctype, data)
        if route.kind == "state" and getattr(self._session, "state", None) == FINISHING:
            # 出力の最中は、終わるのを待たせずに「出力中」とだけ答える（画面が待ちの表示を出す）
            self.viewer_waiting = True
            return _json(200, {"state": FINISHING, "files": [], "summary": None})
        try:
            with self._session.lock:
                return self._run(method, route, body)
        except ReviewError as exc:
            return _error(exc.status, exc.code)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - 例外の中身（抽出値を含みうる）を画面にもコンソールにも出さない
            self._session.log_error(type(exc).__name__)
            response = _error(500, "internal")
            # 出力・中止の途中で止まった時は、待ち受けを残さない（もう一度出力させない）
            response.final = route.kind in ("finish", "cancel")
            return response

    def _run(self, method: str, route: _Route, body: bytes) -> Response:
        if route.kind == "state":
            state = self._session.state_json()
            if state.get("summary") is not None:
                self.result_told.set()
            return _json(200, state)
        if route.kind == "image":
            return Response(200, "image/jpeg", self._session.page_image(route.fi, route.pi))

        try:
            payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return _error(400, "bad_request")
        if not isinstance(payload, dict):
            return _error(400, "bad_request")
        if route.kind == "finish":
            return _json(200, self._session.finish(payload), final=True)
        if route.kind == "cancel":
            return _json(200, self._session.cancel(), final=True)
        if route.kind == "frames":
            return _json(200, self._session.set_frames(route.fi, route.pi, payload))
        if route.kind == "reset":
            return _json(200, self._session.reset_page(route.fi, route.pi, payload))
        return _json(200, self._session.mark_viewed(route.fi, route.pi))


class _Handler(BaseHTTPRequestHandler):
    """ソケットと ReviewApp の受け渡しだけを行う。"""

    protocol_version = "HTTP/1.0"     # 1 要求ごとに接続を閉じる
    timeout = _SOCKET_TIMEOUT

    def version_string(self) -> str:
        return "meishi"               # Python と http.server の版を出さない

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 親クラスの引数名
        """要求の行（合言葉を含む URL）もエラーも、コンソールに出さない。"""

    def setup(self) -> None:
        super().setup()
        # 少しずつ送って接続を握り続ける相手を切る（1 回ごとの待ち時間だけでは防げない）
        self._deadline = threading.Timer(_REQUEST_DEADLINE, self._abort)
        self._deadline.daemon = True
        self._deadline.start()

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def _abort(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _dispatch(self, method: str) -> None:
        app: ReviewApp = self.server.app          # type: ignore[attr-defined]
        # 同じヘッダーが複数ある要求は、値を連結して検査で落とす（Host・Origin・Content-Length など）
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            key = name.lower()
            headers[key] = f"{headers[key]},{value}" if key in headers else value
        checked = app.check(method, self.path, headers)
        if isinstance(checked, Response):
            # 拒否する要求の本文は処理しない。上限以内と分かっている分だけ読み捨てる
            # （読まずに閉じると、相手に応答が届く前に接続が切れることがある）
            length = headers.get("content-length", "")
            if method == "POST" and length.isascii() and length.isdigit() and len(length) <= 7:
                if int(length) <= MAX_BODY:
                    self.rfile.read(int(length))
            self._send(checked)
            return
        body = self.rfile.read(int(headers["content-length"])) if method == "POST" else b""
        self._deadline.cancel()                   # 要求は受け取り終えた。処理（出力など）は時間で切らない
        response = app.handle(method, self.path, headers, body)
        try:
            self._send(response)
        finally:
            # 相手が接続を切っていて応答を書けなくても、出力・中止の後は必ず待ち受けを止める
            if response.final:
                self.server.done.set()            # type: ignore[attr-defined]

    def _send(self, response: Response) -> None:
        if self.request_version == "HTTP/0.9":
            # 版の無い要求・読み取れない要求には、見出し（CSP など）を付けられないので何も返さない
            self.close_connection = True
            return
        self.send_response(response.status)
        for name, value in response.all_headers():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802 - http.server の決まった名前
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """http.server が自分で返すエラー（未対応のメソッド・壊れた要求）も、同じ形の短い応答にする。"""
        try:
            self._send(_error(code, "bad_request"))
        except OSError:
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows では SO_REUSEADDR が付いていると、別のプロセス（別の利用者を含む）が同じポートに
    # bind して要求を横取りできる。付けない。
    allow_reuse_address = False
    allow_reuse_port = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def server_bind(self) -> None:
        """排他で bind する。親クラスの getfqdn（名前解決）は呼ばない。"""
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = HOST
        self.server_port = self.server_address[1]

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)        # 接続が多すぎる時は受けずに閉じる
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """接続の途中の例外でトレースバックをコンソールに出さない（I9）。"""


class ReviewServer:
    """待ち受けの開始・待機・停止。"""

    def __init__(self, session: "ReviewSession") -> None:
        self._session = session
        self._token = secrets.token_urlsafe(32)
        self._httpd = _Server((HOST, 0), _Handler)
        host, port = self._httpd.server_address[:2]
        if host != HOST:   # 念のため。外向きに開いた状態では動かさない
            self._httpd.server_close()
            raise RuntimeError("loopback only")
        self._httpd.app = ReviewApp(session, self._token, port)   # type: ignore[attr-defined]
        self._httpd.done = threading.Event()                       # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._started = False
        self.url = f"http://{HOST}:{port}/{self._token}/"

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]

    def socket_option(self, option: int) -> int:
        """待ち受けのソケットの設定値（テスト用）。"""
        return self._httpd.socket.getsockopt(socket.SOL_SOCKET, option)

    def start(self) -> None:
        self._started = True
        self._thread.start()

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

    def stop(self) -> None:
        if self._started:
            self._httpd.shutdown()
        self._httpd.server_close()


def open_browser(url: str) -> bool:
    """既定のブラウザで開く。開けなければ False（利用者がアドレスを手で開く）。

    webbrowser モジュールは環境変数 BROWSER のコマンドを先に実行するので使わない。
    渡してよいのは、この待ち受け自身のアドレスだけ。
    """
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/[A-Za-z0-9_-]{43}/", url):
        return False
    startfile = getattr(os, "startfile", None)
    if startfile is None:  # pragma: no cover - Windows 以外
        return False
    try:
        startfile(url)
    except OSError:
        return False
    return True
