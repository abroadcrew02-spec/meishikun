"""この PC の中だけで動く画面の待ち受け（確認画面と辞書の画面が共用する）。

app\\ の中でソケットを開くのはこのモジュールだけ（I10）。外部へは接続せず、名前解決もしない。

- 127.0.0.1 にだけ bind する。他の PC からは接続できない。127.0.0.1 の同じポートへの
  二重の bind を許さない（別の利用者に要求を横取りされないようにする）
- 実行ごとに作る合言葉（トークン）を URL の先頭に置き、一致しない要求は 404 にする
- Host が `127.0.0.1:<ポート>` でない要求は 403（DNS リバインディング対策）
- 状態を変える要求は、JSON であること・この画面自身からの送信であることを確かめる
- 要求の検査（check）は本文を読む前に行う。通った要求だけ本文を読む
- 要求の内容・合言葉・例外の中身をコンソールやログに出さない（I9）

要求の検査は BaseApp.check と BaseApp.handle にまとめてある（ソケットを使わずにテストできる）。
画面ごとのアプリ（BaseApp の下位クラス）が決めるのは、画面の部品の表・本文の上限・経路の読み方・
処理の中身だけで、検査の順番と中身は変えられない。このモジュールは、画面ごとのモジュール
（app.review など）を import しない。
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.apperror import AppError

HOST = "127.0.0.1"
MAX_BODY_LIMIT = 256 * 1024    # アプリが決められる「要求の本文の上限」の最大
MAX_CONNECTIONS = 16           # 同時に処理する接続の上限
_SOCKET_TIMEOUT = 20.0         # 1 回の読み書きを待つ上限 秒
_REQUEST_DEADLINE = 15.0       # 接続してから要求（見出しと本文）を送り終えるまでの上限 秒
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_STATIC_FILE_RE = re.compile(r"[a-z0-9_-][a-z0-9_.-]{0,39}")   # 画面の部品のファイル名
STATIC = "static"              # 画面の部品を返す経路の種類

# 画面の部品に付けてよい Content-Type。固定の表で付ける
# （Windows の設定によっては .js が text/plain と判定され、nosniff と合わさって動かなくなるため）
_STATIC_TYPES = frozenset({
    "text/html; charset=utf-8", "text/javascript; charset=utf-8", "text/css; charset=utf-8",
})

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


def json_response(status: int, payload: Any, final: bool = False) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status, "application/json; charset=utf-8", body, final=final)


def error_response(status: int, code: str) -> Response:
    return json_response(status, {"error": code})


def _reject_constant(_name: str) -> Any:
    raise ValueError("constant")   # NaN・Infinity は受け付けない


def parse_json_object(body: bytes) -> dict[str, Any] | None:
    """要求の本文を JSON のオブジェクトとして読む。読めない・オブジェクトでない時は None。"""
    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True, slots=True)
class Route:
    """受け付けた要求の行き先。kind は固定の短い語。"""
    kind: str
    name: str = ""                # 画面の部品（STATIC）の時の部品名。それ以外は、経路に含まれる固定の名前
    args: tuple[int, ...] = ()    # 経路に含まれる番号（画面ごとに意味を決める）


class BaseApp:
    """要求 1 件を検査して、画面ごとの処理に渡す。ソケットには触らない。

    下位クラスは、次の差し込み口だけを実装する。check と handle は上書きできない。

    - parse_route: 画面の部品以外の経路を読む
    - lock:        処理の間に持つロック
    - run:         検査を通った要求の処理
    - before_lock・log_error・final_on_error: 必要な時だけ
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in ("check", "handle"):
            # 下位クラス自身が書いた時も、別の親（mixin）から持ち込んだ時も断る
            if name in cls.__dict__ or getattr(cls, name) is not getattr(BaseApp, name):
                raise TypeError(f"{cls.__name__} は {name} を上書きできません（要求の検査は共通）")

    def __init__(
        self, token: str, port: int, *, static: Mapping[str, tuple[Path, str]], max_body: int,
    ) -> None:
        """static は「URL の名前 → (ファイル, Content-Type)」。プログラムの中の固定の表を渡す。

        ファイルは起動時にメモリへ読む（要求の内容からファイルを探すことはしない）。
        """
        if not (0 < max_body <= MAX_BODY_LIMIT):
            raise ValueError("max_body")
        self._token = token.encode("ascii")
        self._host = f"{HOST}:{port}"
        self._origin = f"http://{HOST}:{port}"
        self.__max_body = max_body      # 名前を隠す（下位クラスや外から書き換えにくくする）
        self._static: dict[str, tuple[bytes, str]] = {}
        for name, (file, ctype) in static.items():
            if ctype not in _STATIC_TYPES or not _STATIC_FILE_RE.fullmatch(file.name):
                raise ValueError("static")
            if name and name != file.name:
                raise ValueError("static")      # URL の名前は、ファイル名と同じにする（"" は画面そのもの）
            self._static[name] = (file.read_bytes(), ctype)

    @property
    def max_body(self) -> int:
        """要求の本文の上限（バイト）。"""
        return self.__max_body

    # ---- 差し込み口 ----

    def parse_route(self, method: str, rest: list[str]) -> Route | None:
        """`/<合言葉>/` より後ろ（rest）を読む。受け付けない経路は None。画面の部品はここに来ない。"""
        raise NotImplementedError

    def lock(self) -> AbstractContextManager[Any]:
        """run の間に持つロック。"""
        raise NotImplementedError

    def run(self, method: str, route: Route, body: bytes) -> Response:
        """検査を通った要求を処理する（lock を持った状態で呼ばれる）。"""
        raise NotImplementedError

    def before_lock(self, route: Route) -> Response | None:
        """ロックを取る前に答えられる要求なら、その応答を返す。"""
        return None

    def log_error(self, name: str) -> None:
        """処理の中で起きた例外の型の名前を記録する（中身は記録しない）。"""

    def final_on_error(self, route: Route) -> bool:
        """処理が例外で止まった時に、待ち受けも止める経路か。"""
        return False

    # ---- 検査と振り分け（上書きできない） ----

    def check(self, method: str, target: str, headers: Mapping[str, str]) -> "Response | Route":
        """要求行とヘッダーだけで検査する。受け付けるなら行き先、拒否するなら応答を返す。

        本文を読む前に呼ぶ（合言葉を知らない相手に大きな本文を読ませられないようにする）。
        画面ごとの状態には触らないので、ロックは要らない。headers のキーは小文字。
        """
        if headers.get("host") != self._host:
            return error_response(403, "forbidden")
        parts = target.split("/")
        # 期待する形: /<合言葉>/<残り…>。クエリや # の付いた要求は受け付けない
        if (
            len(parts) < 3 or parts[0] != "" or "?" in target or "#" in target
            or not _TOKEN_RE.fullmatch(parts[1])
            or not hmac.compare_digest(parts[1].encode("ascii"), self._token)
        ):
            return error_response(404, "not_found")
        if method not in ("GET", "POST"):
            return error_response(405, "method_not_allowed")
        rest = parts[2:]
        route: Route | None
        if method == "GET" and len(rest) == 1 and rest[0] in self._static:
            route = Route(STATIC, name=rest[0])
        else:
            route = self.parse_route(method, rest)
            if route is not None and route.kind == STATIC:
                route = None   # 画面の部品は、上の固定の表からだけ返す
        if route is None:
            return error_response(404, "not_found")

        site = headers.get("sec-fetch-site")
        if method == "GET":
            # 画面そのものは、アドレス欄からの移動（none）か自分自身から。それ以外は自分自身からだけ
            allowed = (None, "none", "same-origin") if route.kind == STATIC else (None, "same-origin")
            if site not in allowed:
                return error_response(403, "forbidden")
            return route

        # POST: この画面自身からの JSON だけを受け付ける
        if site not in (None, "same-origin") or headers.get("origin") != self._origin:
            return error_response(403, "forbidden")
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return error_response(415, "unsupported_media_type")
        if "transfer-encoding" in headers:
            return error_response(400, "bad_request")
        length = headers.get("content-length", "")
        if not (length.isascii() and length.isdigit() and len(length) <= 7):
            return error_response(411, "length_required")
        if int(length) > self.__max_body:
            return error_response(413, "too_large")
        return route

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Response:
        """検査してから処理する。例外は外に出さず、固定の短い応答にする。"""
        route = self.check(method, target, headers)
        if isinstance(route, Response):
            return route
        if len(body) > self.__max_body:
            return error_response(413, "too_large")
        if route.kind == STATIC:
            # 画面の部品は、画面ごとの状態に触らない。長い処理の最中（ロックを持ったまま数十秒
            # かかりうる）に開き直されても、すぐ返す
            data, ctype = self._static[route.name]
            return Response(200, ctype, data)
        early = self.before_lock(route)
        if early is not None:
            return early
        try:
            with self.lock():
                return self.run(method, route, body)
        except AppError as exc:
            return error_response(exc.status, exc.code)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - 例外の中身（抽出値を含みうる）を画面にもコンソールにも出さない
            self.log_error(type(exc).__name__)
            response = error_response(500, "internal")
            # 終わりの処理の途中で止まった時は、待ち受けを残さない（もう一度実行させない）
            response.final = self.final_on_error(route)
            return response


class _Handler(BaseHTTPRequestHandler):
    """ソケットとアプリ（BaseApp）の受け渡しだけを行う。"""

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
        app: BaseApp = self.server.app            # type: ignore[attr-defined]
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
                if int(length) <= app.max_body:
                    self.rfile.read(int(length))
            self._send(checked)
            return
        body = self.rfile.read(int(headers["content-length"])) if method == "POST" else b""
        self._deadline.cancel()                   # 要求は受け取り終えた。処理（出力など）は時間で切らない
        response = app.handle(method, self.path, headers, body)
        try:
            self._send(response)
        finally:
            # 相手が接続を切っていて応答を書けなくても、終わりの応答の後は必ず待ち受けを止める
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
            self._send(error_response(code, "bad_request"))
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


class LocalServer:
    """待ち受けの開始・停止。1 つの待ち受けに載せるアプリは 1 つだけ。

    いつ終わったとみなすか（wait）は画面ごとに違うので、ここには置かない。下位クラスが、
    done（終わりの応答を返し終えた合図）と自分の画面の状態を見て決める。
    """

    def __init__(self, make_app: Callable[[str, int], BaseApp]) -> None:
        """make_app は (合言葉, ポート) からアプリを作る。合言葉は実行ごとに作る。"""
        self._token = secrets.token_urlsafe(32)
        self._httpd = _Server((HOST, 0), _Handler)
        try:
            host, port = self._httpd.server_address[:2]
            if host != HOST:   # 念のため。外向きに開いた状態では動かさない
                raise RuntimeError("loopback only")
            app = make_app(self._token, port)
            if not isinstance(app, BaseApp):
                raise TypeError("app")
        except BaseException:
            self._httpd.server_close()
            raise
        self._httpd.app = app                                      # type: ignore[attr-defined]
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
