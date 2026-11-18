"""CLI エントリポイント。

DESIGN.md §4.1・§17・§19.2 を実装する。`python -m app review`（枠を画面で確認してから
出力）・`python -m app run`（確認なしで出力）・`python -m app dict`（辞書を画面で編集）・
`python -m app diagnose [--no-split]`・`python -m app clear` を提供する。重い依存（app.config・
app.dictionaries・app.batch・app.diagnose）は実際にコマンドを実行する時
だけ import する（引数パースだけなら依存が揃っていなくても動く）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app")
    # --base はサブコマンドの前でも後でも書ける（.bat は `-m app run %*` と後ろに付ける）
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS: サブコマンド側に書かれなかった時、前に書かれた --base を既定値で上書きしない
    common.add_argument("--base", type=Path, default=argparse.SUPPRESS, help="base フォルダ（既定: このパッケージの親）")
    parser.add_argument("--base", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", parents=[common])
    review = sub.add_parser("review", parents=[common])
    review.add_argument("--no-browser", action="store_true", help="ブラウザを開かず、アドレスだけ表示する")
    dict_cmd = sub.add_parser("dict", parents=[common])
    dict_cmd.add_argument("--no-browser", action="store_true", help="ブラウザを開かず、アドレスだけ表示する")
    diag = sub.add_parser("diagnose", parents=[common])
    diag.add_argument("--no-split", action="store_true", help="分割結果を出さない")
    clear = sub.add_parser("clear", parents=[common])
    clear.add_argument("--yes", action="store_true", help="確認せずに消す")
    return parser


def _run_clear(base: Path, yes: bool) -> int:
    """output\\ の中身を全部消す（clear.bat）。logs\\・done\\・error\\・input\\ は残す。"""
    from app import fileops
    from app.config import ConfigError, load_settings

    try:
        settings = load_settings(base)
    except ConfigError as exc:
        print(f"起動に失敗しました: {exc}")
        return 2
    output = settings.paths.output
    dirs, files = fileops.list_output_entries(output)
    if not dirs and not files:
        print(f"消すものはありません（{output} は空です）。")
        return 0
    print(f"{output} の中身を消します（logs・done・error・input は残します）。")
    for d in dirs:
        print(f"  フォルダ: {d.name}\\")
    for f in files:
        print(f"  ファイル: {f.name}")
    if not yes:
        try:
            answer = input(f"フォルダ {len(dirs)} 件・ファイル {len(files)} 件を消しますか？ [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("中止しました。何も消していません。")
            return 0
    try:
        n_dirs, n_files = fileops.clear_output(output)
    except OSError as exc:
        print(f"消せないものがありました（開いているファイルが無いか確認してください）: {type(exc).__name__}")
        return 1
    print(f"消しました: フォルダ {n_dirs} 件・ファイル {n_files} 件")
    return 0


def _load_dictionaries(settings):
    """辞書を読む。ロックを取った後に呼ぶ。

    前の辞書の保存が置き換えの途中で止まっていたら、保存前の辞書に戻してから読む
    （半端な辞書のまま実行しない）。
    """
    from app.dict_edit import recover_interrupted_save
    from app.dictionaries import load_dictionaries

    for line in recover_interrupted_save(settings.paths.dict_dir).messages():
        print(line)
    return load_dictionaries(settings.paths.dict_dir)


def _run_locked(ns: argparse.Namespace, settings) -> int:
    """run / review / dict を、同じフォルダで同時に 1 つだけ動かす。

    確認画面を開いている間に別の実行が同じ PDF を処理すると、一覧表が二重にできたり、
    入力が先に移動されたりするため。辞書の画面を開いている間は、辞書が書き換わりうるので、
    全自動も確認画面も動かさない。
    """
    from app import fileops
    from app.dictionaries import DictionaryError

    lock = None
    try:
        lock = fileops.acquire_run_lock(settings.paths.logs)
        locked_out = lock is None
    except OSError:
        # logs フォルダを作れない。run・review は、この後の処理が「起動できない」として報告する。
        # 辞書の画面は、ロック無しでは動かさない
        locked_out = ns.command == "dict"
    if locked_out:
        print(
            "別の実行（確認画面・辞書の画面を開いたままの実行を含む）が動いているか、logs フォルダの"
            " meishi.lock を開けません。前の黒い画面が残っていないか確かめてから、もう一度実行してください。"
        )
        return 2
    try:
        if ns.command == "dict":
            from app.dict_edit import run_dict_editor

            try:
                return run_dict_editor(settings, open_browser=not ns.no_browser)
            except Exception as exc:  # noqa: BLE001 - 例外の中身（辞書の語やパスを含みうる）を表示しない
                print(f"辞書の画面を続けられませんでした（{type(exc).__name__}）。")
                return 1

        try:
            dicts = _load_dictionaries(settings)
        except DictionaryError as exc:
            print(f"起動に失敗しました: {exc}")
            return 2

        if ns.command == "review":
            from app.review import run_review

            try:
                return run_review(settings, dicts, open_browser=not ns.no_browser)
            except Exception as exc:  # noqa: BLE001 - 例外の中身（名刺の文字を含みうる）を表示しない（I9）
                print(f"確認画面を続けられませんでした（{type(exc).__name__}）。何も出力していない PDF は input フォルダに残っています。")
                return 1

        from app.batch import run_batch
        from app.summary import format_summary

        summary = run_batch(settings, dicts)
        print(format_summary(summary))
        return summary.exit_code()
    finally:
        if lock is not None:
            lock.close()


def main(argv: list[str] | None = None) -> int:
    """"review [--no-browser]" / "run" / "dict [--no-browser]" / "diagnose [--no-split]" / "clear [--yes]"。

    任意で "--base PATH"。
    """
    args = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        ns = parser.parse_args(args)
    except SystemExit as exc:
        # argparse は不正な引数で SystemExit を投げる。exit code にして返す。
        code = exc.code
        return code if isinstance(code, int) else 2

    try:
        import fitz  # noqa: F401
        import openpyxl  # noqa: F401
    except ImportError:
        print("PyMuPDF / openpyxl が見つかりません。インストールを確認してください。")
        return 2

    base = ns.base if ns.base is not None else Path(__file__).resolve().parent.parent

    if ns.command == "clear":
        return _run_clear(base, ns.yes)

    try:
        from app.config import ConfigError, load_settings
        from app.dictionaries import DictionaryError, load_dictionaries
    except ImportError as exc:
        print(f"起動に失敗しました: {exc}")
        return 2

    try:
        settings = load_settings(base)
    except ConfigError as exc:
        print(f"起動に失敗しました: {exc}")
        return 2

    # 辞書は、ロックを取ってから読む（辞書の画面が保存している最中の辞書を読まないため）
    if ns.command in ("run", "review", "dict"):
        return _run_locked(ns, settings)

    try:
        dicts = load_dictionaries(settings.paths.dict_dir)
    except DictionaryError as exc:
        print(f"起動に失敗しました: {exc}")
        return 2

    if ns.command == "diagnose":
        from app.diagnose import run_diagnose

        return run_diagnose(settings, dicts, with_split=not ns.no_split)

    return 2  # pragma: no cover - argparse の subparsers(required=True) で到達しない


if __name__ == "__main__":
    raise SystemExit(main())
