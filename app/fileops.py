"""フォルダ作成・run_id・連番・移動。

DESIGN.md §14.1・§14.5・§19.2 を実装する。PathsCfg は型ヒントとしてのみ使い
（TYPE_CHECKING）、実行時は属性アクセスだけで動く（app.models への実行時
依存を持たない）。
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import PathsCfg


def ensure_dirs(paths: "PathsCfg") -> None:
    """input/output/done/error/logs を作る（無ければ作成、あれば何もしない）。

    dict・base は対象外（DESIGN.md §2: dict\\ と app\\ は起動時に作らない）。
    """
    for d in (paths.input, paths.output, paths.done, paths.error, paths.logs):
        d.mkdir(parents=True, exist_ok=True)


def _run_id_taken(run_id: str, directory: Path) -> bool:
    if not directory.exists():
        return False
    prefix = f"meishi_{run_id}"
    return any(p.name.startswith(prefix) for p in directory.iterdir())


def make_run_id(now: datetime, output_dir: Path, logs_dir: Path) -> str:
    """output\\・logs\\ に meishi_{run_id} で始まるファイルが無い run_id を返す。"""
    base_id = now.strftime("%Y%m%d_%H%M%S")
    run_id = base_id
    n = 2
    while _run_id_taken(run_id, output_dir) or _run_id_taken(run_id, logs_dir):
        run_id = f"{base_id}_{n}"
        n += 1
    return run_id


def unique_path(directory: Path, name: str) -> Path:
    """directory/name が既にあれば stem_2.ext, stem_3.ext... の空いている名前を返す。"""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    n = 2
    while True:
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def unique_dir(parent: Path, name: str) -> Path:
    """parent/name が既にあれば name_2, name_3... の空いている名前を返す（作成はしない）。

    PDF ごとの出力フォルダ（DESIGN.md §14.1）に使う。同名の PDF を再実行しても
    前回の結果を上書きしない（I8）。
    """
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name}_{n}"
        n += 1
    return candidate


def move_no_overwrite(src: Path, dest_dir: Path) -> Path:
    """src を dest_dir へ移動する。同名があれば連番で再試行し、上書きしない。

    os.rename は移動先に同名ファイルがあると Windows では FileExistsError に
    なるため、これを利用して上書きを防ぐ（DESIGN.md §14.5）。入力は削除しない
    （失敗時は呼び出し元に例外が伝わり、src は input\\ に残る）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    suffix = src.suffix
    name = src.name
    n = 2
    while True:
        dest = dest_dir / name
        try:
            os.rename(src, dest)
            return dest
        except FileExistsError:
            name = f"{stem}_{n}{suffix}"
            n += 1


def list_output_entries(output_dir: Path) -> tuple[list[Path], list[Path]]:
    """output_dir 直下の (フォルダ一覧, ファイル一覧)。無ければ空。clear の対象（§17）。"""
    if not output_dir.exists():
        return [], []
    entries = sorted(output_dir.iterdir(), key=lambda p: p.name.casefold())
    return [p for p in entries if p.is_dir()], [p for p in entries if p.is_file()]


def clear_output(output_dir: Path) -> tuple[int, int]:
    """output_dir の中身（生成した一覧表・分割確認 PDF・PDF ごとのフォルダ）を全部消す。

    output_dir 自体は残す。logs\\・done\\・error\\・input\\ は対象外（§17 clear.bat）。
    戻り値は (消したフォルダ数, 消したファイル数)。
    """
    dirs, files = list_output_entries(output_dir)
    for d in dirs:
        shutil.rmtree(d)
    for f in files:
        f.unlink()
    return len(dirs), len(files)


def list_input_pdfs(input_dir: Path) -> tuple[list[Path], list[str]]:
    """input_dir 直下の *.pdf（大小無視）を名前の casefold 順で返す。

    戻り値は (PDF のパス一覧, 対象外だったファイル名一覧)。サブフォルダは見ない。
    """
    pdfs: list[Path] = []
    ignored: list[str] = []
    if not input_dir.exists():
        return pdfs, ignored
    for p in sorted(input_dir.iterdir(), key=lambda x: x.name.casefold()):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".pdf":
            pdfs.append(p)
        else:
            ignored.append(p.name)
    return pdfs, ignored
