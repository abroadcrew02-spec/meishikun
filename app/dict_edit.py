"""辞書の編集（画面から語を足す・消す・保存する）。dev/dict_ui/design.md §6・§11。

- 保存するまで辞書ファイルに触らない。保存前の変更は DictSession が持つ
- 語の検査はここで行う（画面は結果を表示するだけ）。足す時と保存の時の 2 回かける
- 保存は「照合 → 検査 → 控え → 一時ファイル → 印 → 置き換え」の順。途中で止まった時は、次の実行の
  最初に recover_interrupted_save が保存前の辞書へ戻す
- 辞書の語は利用者が登録した設定値。ログには、語そのものを書かない（件数だけ）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app import patterns
from app.apperror import AppError
from app.dictionaries import (
    FORM_AREA_CODES,
    FORM_OLD_KANJI,
    FORM_WORDS,
    KINDS,
    DictionaryError,
    DictKind,
    is_blank_word,
    kind_of,
    load_dictionaries,
)
from app.textnorm import normalize

MAX_WORD_CHARS = 40          # 1 語の長さの上限
MAX_NOTE_CHARS = 40          # 市外局番のメモの長さの上限
MAX_WORDS_PER_KIND = 500     # 1 つの辞書の語数の上限
MAX_PENDING = 200            # 保存していない変更の数の上限
BACKUP_KEEP = 10             # 辞書ファイルごとに残す控えの数
BACKUP_DIR = "backup"
MARKER_NAME = "saving.json"  # 置き換えの最中だけ置く印（dict\backup\ の中）
ADD_MARK = "# ---- 画面から追加 ----"
EDITING = "editing"
CLOSED = "closed"

_BOM = b"\xef\xbb\xbf"
_HEADERS = {FORM_OLD_KANJI: "old,new", FORM_AREA_CODES: "area_code,note"}
_STAMP_RE = r"[0-9]{8}_[0-9]{6}(?:_[0-9]{1,3})?"
_TMP_RE = re.compile(r"\.[A-Za-z0-9_.-]{1,60}\.[0-9a-f]{16}\.tmp")
_AREA_CODE_RE = re.compile(r"0[0-9]{1,4}")
_MANY_DIGITS = 6             # 数字がこの数以上ある語は、電話番号などに見える（区切りは数えない）
# 区分では拾えない、見えない文字（結合用の埋め草・ハングルの埋め草・クメール文字の見えない母音・点字の空白）
_INVISIBLE = frozenset(map(chr, (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0)))
# 文字として受け付けない区分: 制御・書式（見えない文字）・サロゲート・私用・未定義・行と段落の区切り
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# 英字の大文字と小文字を区別せずに照合する種類（同じ語かどうかの判定も、区別しない）
_CASEFOLD_KINDS = frozenset({"titles", "corp_suffixes", "tel_prefixes", "fax_prefixes"})
# 2 文字以下の語に注意を出す種類
_SHORT_WARN_KINDS = frozenset({"titles", "corp_suffixes", "dept_suffixes", "branch_labels", "org_suffixes"})
# 既にある語尾の前に 2 文字以上が付いた語（会社名そのものなど）に注意を出す種類
_WHOLE_NAME_KINDS = frozenset({"corp_suffixes", "org_suffixes", "dept_suffixes", "branch_labels"})


@dataclass(frozen=True)
class KindInfo:
    """画面に出す、辞書の種類の説明。"""
    key: str
    title: str
    description: str
    example: str
    hint: str


# 画面に並べる順
SCREEN_KINDS: tuple[KindInfo, ...] = (
    KindInfo("corp_suffixes", "会社の種類", "この語が付いた言葉を「会社名」として読みます。",
             "「株式会社」があると、「株式会社ダミー商事」が会社名になります。",
             "会社名が「未分類」に入っていた時（例: 「弁護士法人」を足す）。"),
    KindInfo("titles", "役職", "この語を含む言葉を「役職」として読みます。「〜長」で終わる短い語は、無くても読みます。",
             "「主任」があると、「営業部 主任」の「主任」が役職になります。",
             "役職が「未分類」や「氏名」に入っていた時。"),
    KindInfo("dept_suffixes", "部署の語尾", "この語で終わる言葉を「部署」として読みます。",
             "「部」があると、「営業部」が部署になります。",
             "部署が「未分類」や「氏名」に入っていた時（例: 「センター」を足す）。"),
    KindInfo("branch_labels", "拠点の語", "住所の前に付く拠点の名前です。住所の頭に付けて読みます。",
             "「本社」があると、「本社 東京都…」の「本社」が住所の一部になります。「東京本社」のような形も拾います。",
             "住所の前の拠点名が「未分類」に入っていた時（例: 「出張所」を足す）。"),
    KindInfo("org_suffixes", "組織名の語尾", "この語で終わる言葉を「氏名」の候補にしません。",
             "「銀行」があると、「見本銀行」を氏名と読み違えません。",
             "会社でない組織の名前が「氏名」に入っていた時（例: 「技研」を足す）。"),
    KindInfo("tel_prefixes", "電話の見出し", "この語の後ろの番号を「電話」として読みます。英字は大文字と小文字を区別しません。",
             "「TEL」があると、「TEL 03-0000-0001」が電話になります。",
             "見出しつきの電話番号が読まれなかった時（例: 「ダイヤルイン」を足す）。"),
    KindInfo("fax_prefixes", "FAX の見出し", "この語の後ろの番号を「FAX」として読みます。英字は大文字と小文字を区別しません。",
             "「FAX」があると、「FAX 03-0000-0002」が FAX になります。",
             "FAX の番号が「電話」に入っていた時（例: 「ファクシミリ」を足す）。"),
    KindInfo("old_kanji", "旧字体", "判定の時だけ、旧字体を新字体として扱います。一覧表には元の字のまま出ます。",
             "「會」→「会」があると、「株式會社」を「株式会社」と同じように読みます。",
             "旧字体で書かれた会社名や役職が読まれなかった時。"),
    KindInfo("area_codes", "市外局番", "区切りの無い電話番号を、どこで区切るかを決めます。",
             "「03」があると、「0300000001」を「03-0000-0001」と読みます。",
             "区切りの無い電話番号が、違う位置で区切られていた時。"),
)


def _is_variation_selector(ch: str) -> bool:
    """異体字セレクター（字の形だけを変える、見えない文字）か。"""
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or 0x180B <= cp <= 0x180F


def _is_bad_text(text: str) -> bool:
    """見えない文字・改行にあたる文字・普通の空白以外の空白を含むか。"""
    for ch in text:
        if unicodedata.category(ch) in _BAD_CATEGORIES or _is_variation_selector(ch) or ch in _INVISIBLE:
            return True
        if ch.isspace() and ch not in " \u3000":
            return True
    return len(text.splitlines()) > 1


def _is_kanji(ch: str) -> bool:
    """割り当てのある漢字 1 文字か（統合漢字・拡張・互換漢字）。"""
    cp = ord(ch)
    in_range = (
        0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x323AF
    )
    return in_range and unicodedata.category(ch) == "Lo"


def _is_link(path: Path) -> bool:
    """シンボリックリンク・ジャンクションなど、別の場所を指すものか（書いた中身が別の場所へ行く）。

    Windows では、ファイルの属性（再解析ポイント）で見る。Path.is_junction は Python 3.12 からなので使わない。
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


@dataclass
class _Entry:
    line: str               # ファイルに書く 1 行
    key: str                # 同じ語かどうかを見る鍵
    value: dict[str, str]   # 画面に出す値
    index: int = -1         # 元のファイルの行の位置（足した語は -1）


@dataclass
class _KindState:
    kind: DictKind
    info: KindInfo
    path: Path
    exists: bool = False
    raw: bytes = b""                 # 読んだ時のバイト列（照合に使う）
    digest: str = ""
    bom: bool = False
    newline: str = "\r\n"
    lines: list[str] = field(default_factory=list)       # 保存する時の元になる行
    entries: list[_Entry] = field(default_factory=list)  # ファイルにある語
    added: list[_Entry] = field(default_factory=list)
    removed: set[str] = field(default_factory=set)
    from_default: bool = False       # ファイルに語が無く、最初から入っている語で動いている

    def changed(self) -> bool:
        return bool(self.added or self.removed)

    def active(self) -> list[_Entry]:
        """保存した後に効く語（消す語を除き、足す語を含む）。"""
        return [e for e in self.entries if e.key not in self.removed] + self.added


class DictSession:
    """辞書の編集 1 回ぶんの状態。操作は lock を持って行う。"""

    def __init__(self, dict_dir: Path, logger: Any = None, settings: Any = None, run_id: str = "") -> None:
        """settings と run_id は試し読みに使う（無ければ、試し読みはできない）。"""
        self.lock = threading.RLock()
        self.dict_dir = dict_dir
        self.state = EDITING
        self.rev = 0
        self._logger = logger
        self._settings = settings
        self._run_id = run_id
        self._kinds: dict[str, _KindState] = {}
        # 試し読み。busy の間は、足す・消す・保存・読み直し・終わる を受け付けない
        self.busy = False
        self.idle_closed = False                        # 長く操作が無かったので、自動で終えた
        self._trial: dict[str, Any] | None = None       # 画面に返す、試し読みの進み具合と結果
        self._trial_stop = threading.Event()
        self._trial_thread: threading.Thread | None = None
        self._trial_folder = ""                         # この実行で最後に書いた比較表のフォルダ
        self._load()

    # ---------------------------------------------------------------- 読み込み

    def _load(self) -> None:
        # 検査に通らない辞書は編集できない（DictionaryError。呼び出し元が、壊れた行と控えの場所を伝える）
        load_dictionaries(self.dict_dir)
        states: dict[str, _KindState] = {}
        for info in SCREEN_KINDS:
            kind = kind_of(info.key)
            states[info.key] = self._read_kind(kind, info)
        self._kinds = states
        old_kanji = self._old_kanji()
        for st in states.values():
            self._fill_entries(st, old_kanji)

    def _read_kind(self, kind: DictKind, info: KindInfo) -> _KindState:
        st = _KindState(kind=kind, info=info, path=self.dict_dir / kind.filename)
        try:
            st.raw = st.path.read_bytes()
            st.exists = True
        except FileNotFoundError:
            st.raw = b""
        st.digest = hashlib.sha256(st.raw).hexdigest()
        st.bom = st.raw.startswith(_BOM)
        text = st.raw.decode("utf-8-sig")
        if st.exists and "\r\n" not in text and "\n" in text:
            st.newline = "\n"
        st.lines = text.splitlines()
        return st

    def _data_lines(self, st: _KindState) -> list[tuple[int, str]]:
        """語や対応の入った行（行の位置, 中身）。読み込み（dictionaries._data_lines）と同じ読み方にする。"""
        out = []
        for i, raw_line in enumerate(st.lines):
            if i == 0:
                raw_line = raw_line.removeprefix("\ufeff")     # BOM が 2 つ重なったファイル
            line = raw_line.strip()
            if line and not line.startswith("#"):
                out.append((i, line))
        return out

    def _fill_entries(self, st: _KindState, old_kanji: dict[str, str]) -> None:
        data = self._data_lines(st)
        st.entries = []
        form = st.kind.form
        if form == FORM_WORDS:
            # 見えない文字だけの行は、読み込みが語として扱わない。画面でも語にしない
            data = [(i, word) for i, word in data if not is_blank_word(normalize(word, old_kanji).norm)]
            if not data and st.kind.default is not None:
                # ファイルに語が無い: 最初から入っている語で動いている。足す・消す時は、その語もファイルに書く
                st.from_default = True
                first = len(st.lines)
                for word in st.kind.default:
                    st.lines.append(word)
                data = [(first + n, word) for n, word in enumerate(st.kind.default)]
            for index, word in data:
                st.entries.append(_Entry(word, self._word_key(st.kind.key, word, old_kanji), {"value": word}, index))
            return
        for index, content in data[1:]:      # 最初の行は見出し
            parts = content.split(",", 1)
            first = parts[0].strip()
            second = parts[1].strip() if len(parts) > 1 else ""
            if form == FORM_OLD_KANJI:
                st.entries.append(_Entry(content, first, {"old": first, "new": second}, index))
            else:
                st.entries.append(_Entry(content, first, {"code": first, "note": second}, index))

    def _old_kanji(self) -> dict[str, str]:
        """今の変更を当てた後の、旧字体の対応表。"""
        return {e.value["old"]: e.value["new"] for e in self._kinds["old_kanji"].active()}

    @staticmethod
    def _word_key(key: str, word: str, old_kanji: dict[str, str]) -> str:
        norm = normalize(word, old_kanji).norm
        return norm.casefold() if key in _CASEFOLD_KINDS else norm

    def _rekey_words(self) -> None:
        """旧字体の表が変わった時に、語の鍵を付け直す。"""
        old_kanji = self._old_kanji()
        for st in self._kinds.values():
            if st.kind.form != FORM_WORDS:
                continue
            marked = [entry for entry in st.entries if entry.key in st.removed]
            for entry in (*st.entries, *st.added):
                entry.key = self._word_key(st.kind.key, entry.value["value"], old_kanji)
            st.removed = {entry.key for entry in marked}     # 「消す」に入れた語は、新しい鍵で持ち直す

    # ---------------------------------------------------------------- 画面に返す状態

    def pending(self) -> int:
        return sum(len(st.added) + len(st.removed) for st in self._kinds.values())

    def state_json(self) -> dict[str, Any]:
        kinds = []
        for st in self._kinds.values():
            seen: dict[str, int] = {}
            for entry in st.active():
                seen[entry.key] = seen.get(entry.key, 0) + 1
            entries = []
            for entry in st.entries:
                status = "removed" if entry.key in st.removed else "file"
                entries.append({**entry.value, "status": status, "dup": seen.get(entry.key, 0) > 1})
            for entry in st.added:
                entries.append({**entry.value, "status": "added", "dup": seen.get(entry.key, 0) > 1})
            kinds.append({
                "key": st.kind.key, "form": st.kind.form, "file": st.kind.filename,
                "title": st.info.title, "description": st.info.description,
                "example": st.info.example, "hint": st.info.hint,
                "count": len(st.active()), "from_default": st.from_default, "entries": entries,
            })
        trial = None
        if self._trial is not None:
            # stale: 試し読みの後に辞書を変えた（結果は、今の変更の内容と合っていない）
            trial = {**self._trial, "stale": self._trial.get("rev") != self.rev}
        return {"state": self.state, "rev": self.rev, "pending": self.pending(), "kinds": kinds,
                "busy": self.busy, "trial": trial,
                "limits": {"word": MAX_WORD_CHARS, "note": MAX_NOTE_CHARS, "words": MAX_WORDS_PER_KIND}}

    # ---------------------------------------------------------------- 語の検査

    def _state_of(self, key: str) -> _KindState:
        st = self._kinds.get(key)
        if st is None:
            raise AppError(404, "not_found")
        return st

    def _require_editing(self) -> None:
        if self.state != EDITING:
            raise AppError(409, "closed")
        if self.busy:
            raise AppError(409, "busy")      # 試し読みの途中

    @staticmethod
    def _text(payload: dict[str, Any], name: str) -> str:
        value = payload.get(name, "")
        if not isinstance(value, str):
            raise AppError(400, "bad_request")
        return value.strip()

    def _make_entry(self, st: _KindState, payload: dict[str, Any]) -> tuple[_Entry | None, str]:
        """payload から 1 件を作る。受け付けない時は (None, 理由の符号)。"""
        form = st.kind.form
        allowed = {FORM_WORDS: {"value"}, FORM_OLD_KANJI: {"old", "new"}, FORM_AREA_CODES: {"code", "note"}}[form]
        if set(payload) - allowed - {"rev"}:
            raise AppError(400, "bad_request")
        if form == FORM_WORDS:
            word = self._text(payload, "value")
            if not word:
                return None, "empty"
            if _is_bad_text(word):
                return None, "bad_chars"
            if len(word) > MAX_WORD_CHARS:
                return None, "too_long"
            if word.startswith("#"):
                return None, "hash"
            key = self._word_key(st.kind.key, word, self._old_kanji())
            if not key.strip():
                return None, "empty"
            return _Entry(word, key, {"value": word}), ""
        if form == FORM_OLD_KANJI:
            old, new = self._text(payload, "old"), self._text(payload, "new")
            if not old or not new:
                return None, "empty"
            if len(old) != 1 or len(new) != 1 or not _is_kanji(old) or not _is_kanji(new):
                return None, "kanji_only"
            if old == new:
                return None, "same_char"
            return _Entry(f"{old},{new}", old, {"old": old, "new": new}), ""
        code, note = self._text(payload, "code"), self._text(payload, "note")
        if not code:
            return None, "empty"
        if not _AREA_CODE_RE.fullmatch(code):
            return None, "area_code"
        if _is_bad_text(note) or "," in note:
            return None, "bad_chars"
        if note[:1] in ("=", "+", "-", "@"):
            return None, "note_start"      # 表計算ソフトで開いた時に、式として扱われる書き出し
        if len(note) > MAX_NOTE_CHARS:
            return None, "too_long"
        return _Entry(f"{code},{note}", code, {"code": code, "note": note}), ""

    def _refusal(self, st: _KindState, entry: _Entry, *, skip: _Entry | None = None) -> str:
        """entry を足せない理由の符号（足せるなら ""）。skip は、比べる相手から外す 1 件（自分自身）。"""
        others = [e for e in st.active() if e is not skip]
        if any(e.key == entry.key for e in others):
            return "duplicate"
        if len(others) + 1 > MAX_WORDS_PER_KIND:
            return "too_many"
        if self._clashes_with_other_prefix(st, entry.key):
            return "in_other_prefix"
        return ""

    def _clashes_with_other_prefix(self, st: _KindState, key: str) -> bool:
        """電話と FAX の見出しの両方に同じ語があるか（FAX として扱われ、電話の側が効かない）。"""
        if st.kind.key not in ("tel_prefixes", "fax_prefixes"):
            return False
        other = self._kinds["fax_prefixes" if st.kind.key == "tel_prefixes" else "tel_prefixes"]
        return any(e.key == key for e in other.active())

    def _warnings(self, st: _KindState, entry: _Entry) -> list[str]:
        """足した語への注意の符号。`in_other_kind:<辞書の識別子>` は、同じ語がある辞書を添える。"""
        out: list[str] = []
        key = st.kind.key
        if st.kind.form == FORM_OLD_KANJI:
            if unicodedata.normalize("NFKC", entry.value["old"]) != entry.value["old"]:
                out.append("no_effect")       # 先に別の字へ直されるので、この対応は効かない
            return out
        if st.kind.form != FORM_WORDS:
            return out
        word = entry.value["value"]
        compact = word.replace(" ", "").replace("\u3000", "")
        if key in _SHORT_WARN_KINDS and len(compact) <= 2:
            out.append("short")
        if key in ("tel_prefixes", "fax_prefixes") and len(compact) <= 2 and (
            unicodedata.normalize("NFKC", compact).isascii()
        ):
            out.append("short_prefix")
        if compact != word:
            out.append("has_space")           # 途中に空白がある（2 つの言葉を、まとめて貼り付けた時など）
        plain_word = unicodedata.normalize("NFKC", word)
        if (
            "@" in word
            or sum(ch.isdigit() for ch in plain_word) >= _MANY_DIGITS       # 03-0000-0001 のような番号
            or plain_word.replace(" ", "").startswith(patterns.PREFECTURES)   # 住所
        ):
            out.append("card_value")
        elif key in _WHOLE_NAME_KINDS and any(
            entry.key != e.key and len(entry.key) - len(e.key) >= 2 and (
                entry.key.endswith(e.key)
                # 会社の種類・組織名の語尾は、名前の前にも付く（株式会社ダミー商事）
                or (key in ("corp_suffixes", "org_suffixes") and entry.key.startswith(e.key))
            )
            for e in st.active() if e is not entry
        ):
            out.append("card_value")          # 既にある語に言葉が付いている（会社名そのものなど）
        for other in self._kinds.values():
            if other is st or other.kind.form != FORM_WORDS or {key, other.kind.key} == {"tel_prefixes", "fax_prefixes"}:
                continue
            plain = normalize(word, self._old_kanji()).norm.casefold()
            if any(normalize(e.value["value"], self._old_kanji()).norm.casefold() == plain for e in other.active()):
                out.append(f"in_other_kind:{other.kind.key}")
                break
        return out

    # ---------------------------------------------------------------- 足す・消す・戻す

    def add(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_editing()
        st = self._state_of(key)
        entry, code = self._make_entry(st, payload)
        if entry is None:
            return self._answer(False, code)
        # 「消す」に入れた語と同じ行を足す時は、消すのを取りやめるだけにする。同じ鍵でも書く行が違う時
        # （旧字体の新字体・市外局番のメモ・語のつづりを直す）は、「消して足す」として受ける
        if entry.key in st.removed and any(
            e.key == entry.key and e.line.strip() == entry.line for e in st.entries
        ):
            if self._clashes_with_other_prefix(st, entry.key):
                return self._answer(False, "in_other_prefix")
            if any(e.key == entry.key for e in st.added):     # 消している間に、同じ語を別の形で足していた
                return self._answer(False, "duplicate")
            st.removed.discard(entry.key)
            return {**self._changed(st, entry), "kept": True}
        code = self._refusal(st, entry)
        if code:
            return self._answer(False, code)
        if self.pending() + 1 > MAX_PENDING:
            return self._answer(False, "too_many_changes")
        st.added.append(entry)
        return self._changed(st, entry)

    def remove(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_editing()
        st = self._state_of(key)
        target = self._find_key(st, payload)
        added = [e for e in st.added if e.key == target]
        if added:                                  # 足したばかりの語は、取り消すだけ
            if st.kind.form == FORM_WORDS and (st.kind.required or st.kind.default is not None):
                # ほかの語をすべて「消す」に入れた後で、足した語を取り消すと、辞書が空になる。
                # 鍵ではなく語で数える（旧字体の表を変えると、ファイルの語と足した語が同じ鍵になることがある。
                # その時は、取り消してもファイルの語が残る）
                if all(any(e is a for a in added) for e in st.active()):
                    return self._answer(False, "last_word")
            st.added = [e for e in st.added if e.key != target]
            return self._changed(st, None)
        if not any(e.key == target for e in st.entries) or target in st.removed:
            return self._answer(False, "not_found")
        if st.kind.form == FORM_WORDS and (st.kind.required or st.kind.default is not None):
            if len({e.key for e in st.active()} - {target}) == 0:
                return self._answer(False, "last_word")
        if self.pending() + 1 > MAX_PENDING:
            return self._answer(False, "too_many_changes")
        st.removed.add(target)
        return self._changed(st, None)

    def restore(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_editing()
        st = self._state_of(key)
        target = self._find_key(st, payload)
        if target not in st.removed:
            return self._answer(False, "not_found")
        if self._clashes_with_other_prefix(st, target):   # 消している間に、もう一方の見出しに同じ語を足していた
            return self._answer(False, "in_other_prefix")
        if any(e.key == target for e in st.added):        # 消している間に、同じ語を別の形で足していた
            return self._answer(False, "duplicate")
        st.removed.discard(target)
        return self._changed(st, None)

    def _find_key(self, st: _KindState, payload: dict[str, Any]) -> str:
        """消す・戻す相手の鍵。語は、画面に出ている値で指す。"""
        name = {FORM_WORDS: "value", FORM_OLD_KANJI: "old", FORM_AREA_CODES: "code"}[st.kind.form]
        if set(payload) - {name, "rev"}:
            raise AppError(400, "bad_request")
        value = self._text(payload, name)
        if st.kind.form == FORM_WORDS:
            return self._word_key(st.kind.key, value, self._old_kanji())
        return value

    def _changed(self, st: _KindState, entry: _Entry | None) -> dict[str, Any]:
        if st.kind.form == FORM_OLD_KANJI:
            self._rekey_words()
        self.rev += 1
        warnings = self._warnings(st, entry) if entry is not None else []
        return {**self._answer(True, ""), "warnings": warnings}

    def _answer(self, ok: bool, code: str) -> dict[str, Any]:
        # kept: 消す予定だった語を、そのまま残すことにした（足した語は増えていない）
        return {"ok": ok, "reason": code, "warnings": [], "kept": False, "rev": self.rev, "pending": self.pending()}

    # ---------------------------------------------------------------- 保存

    def _render(self, st: _KindState) -> bytes:
        """保存する中身。元の行を残し、消す語の行を除き、足す語を末尾に足す。"""
        drop = {e.index for e in st.entries if e.key in st.removed}
        lines = [line for i, line in enumerate(st.lines) if i not in drop]
        if st.added:
            header = _HEADERS.get(st.kind.form)
            if header is not None and not any(
                line.strip() and not line.strip().startswith("#") for line in lines
            ):
                lines.append(header)               # 説明だけのファイルには、先に見出しを書く
            if ADD_MARK not in (line.strip() for line in lines):
                lines.append(ADD_MARK)
            lines.extend(e.line for e in st.added)
        text = "".join(line + st.newline for line in lines)
        return (_BOM if st.bom else b"") + text.encode("utf-8")

    def _recheck(self) -> None:
        """保存の前に、足す語の全件をもう一度検査する。"""
        for st in self._kinds.values():
            for entry in st.added:
                again, code = self._make_entry(st, dict(entry.value))
                if again is None or again.line != entry.line:
                    raise AppError(422, "invalid")
                refusal = self._refusal(st, entry, skip=entry)
                if refusal == "duplicate":
                    # 旧字体の表を変えたことで、足した語が別の語と同じになった。画面は、印の付いた語を示す
                    raise AppError(422, "duplicate_added")
                if refusal:
                    raise AppError(422, "invalid")
            if st.kind.form == FORM_WORDS and (st.kind.required or st.kind.default is not None):
                if st.changed() and not st.active():
                    raise AppError(422, "invalid")

    def _check_files(self, states: list[_KindState]) -> None:
        """保存・試し読みの前に、辞書ファイルが、この画面が読んだ時のままかを確かめる。"""
        # 0. 前の保存が、戻しきれないまま止まっていないか（辞書ファイルをほかのソフトが掴んでいた時）
        try:
            recover_interrupted_save(self.dict_dir)
        except DictionaryError as exc:
            raise AppError(500, "restart_needed") from exc
        # 1. 照合: 読んだ後に、外（メモ帳など）で書き換えられていないか
        for st in states:
            try:
                now = st.path.read_bytes()
            except FileNotFoundError:
                now = b"" if not st.exists else None
            except OSError as exc:
                raise AppError(500, "save_failed") from exc      # ほかのソフトが掴んでいて読めない
            if now is None or hashlib.sha256(now).hexdigest() != st.digest:
                raise AppError(409, "changed_outside")

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_editing()
        if set(payload) - {"rev"} or payload.get("rev") != self.rev:
            raise AppError(409, "stale")            # 画面が見ている内容と違う
        changed = [st for st in self._kinds.values() if st.changed()]
        if not changed:
            return {"ok": True, "saved": [], "backups": [], "rev": self.rev, "pending": 0}

        self._check_files(changed)
        # 2・3. 語の検査と、書き換えた後の辞書の一式の検査（正規表現の組み立てまで）
        self._recheck()
        new_bytes = {st.kind.key: self._render(st) for st in changed}
        try:
            replace = {kind_of(k).filename: data.decode("utf-8-sig") for k, data in new_bytes.items()}
            patterns.warm(load_dictionaries(self.dict_dir, replace=replace))
        except (DictionaryError, UnicodeError, re.error) as exc:
            raise AppError(422, "invalid") from exc
        # 4. 書ける場所か
        backup_dir = self.dict_dir / BACKUP_DIR
        if _is_link(self.dict_dir) or (backup_dir.exists() and (_is_link(backup_dir) or not backup_dir.is_dir())):
            raise AppError(409, "unsafe_place")
        for st in changed:
            if st.exists and (_is_link(st.path) or not os.access(st.path, os.W_OK)):
                raise AppError(409, "read_only")

        added = sum(len(st.added) for st in changed)
        removed = sum(len(st.removed) for st in changed)
        try:
            backups = _replace_files(self.dict_dir, [(st.kind.filename, st.raw if st.exists else None,
                                                      new_bytes[st.kind.key]) for st in changed])
        except RestoreFailed as exc:
            # 一部だけ新しくなった状態から戻せなかった。印が残っているので、次の起動で戻す
            raise AppError(500, "restart_needed") from exc
        except OSError as exc:
            raise AppError(500, "save_failed") from exc          # 辞書は保存する前のまま。同じ画面でやり直せる
        saved = [st.kind.filename for st in changed]
        self._log(f"DICT_SAVE files={len(saved)} added={added} removed={removed}")
        try:
            self._load()
        except DictionaryError as exc:
            raise AppError(500, "restart_needed") from exc       # 保存は済んでいる。画面を開き直してもらう
        self.rev += 1
        self._trial = None       # 前の試し読みの結果は、保存する前の変更のもの。保存した後は出さない
        return {"ok": True, "saved": saved, "backups": backups, "rev": self.rev, "pending": self.pending()}

    def reload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """辞書ファイルを読み直し、保存していない変更を当て直す。当たらなかった変更の数を返す。

        保存の時に「外で書き換えられていた」と分かった後に使う。読み直した辞書が検査に通らない時は
        AppError（メモ帳で直してもらう）。
        """
        self._require_editing()
        if payload:
            raise AppError(400, "bad_request")
        pending = {key: (list(st.added), set(st.removed)) for key, st in self._kinds.items()}
        try:
            self._load()
        except DictionaryError as exc:
            raise AppError(422, "invalid_file") from exc
        dropped = 0
        order = ["old_kanji", *[key for key in self._kinds if key != "old_kanji"]]   # 語の鍵は旧字体の表で決まる
        for key in order:
            st = self._kinds[key]
            added, removed = pending[key]
            for old in added:
                entry, _code = self._make_entry(st, dict(old.value))
                if entry is None or self._refusal(st, entry):
                    dropped += 1
                    continue
                st.added.append(entry)
            for target in removed:
                if any(e.key == target for e in st.entries):
                    st.removed.add(target)
                else:
                    dropped += 1
            if key == "old_kanji":
                self._rekey_words()
        self.rev += 1
        return {"ok": True, "dropped": dropped, "rev": self.rev, "pending": self.pending()}

    # ---------------------------------------------------------------- 試し読み

    def start_trial(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存していない変更を当てた辞書と、今の辞書で、手元の PDF を読み比べ始める。

        すぐに返す。読み比べは別のスレッドで進み、進み具合と結果は state_json の trial に入る。
        """
        self._require_editing()
        if set(payload) - {"rev"} or payload.get("rev") != self.rev:
            raise AppError(409, "stale")
        if self._settings is None:
            raise AppError(409, "unavailable")
        if self.pending() == 0:
            return {"ok": False, "reason": "no_changes", "rev": self.rev, "pending": 0}
        from app import dict_trial

        # 外で書き換えられた辞書と比べると、その変更を戻す違いまで数えてしまう。全部の辞書を確かめる
        self._check_files(list(self._kinds.values()))
        self._recheck()
        changed = [st for st in self._kinds.values() if st.changed()]
        try:
            # 変更後の辞書は、保存で書くのと同じ中身を、読み込みと同じ道で読んで作る
            replace = {st.kind.filename: self._render(st).decode("utf-8-sig") for st in changed}
            before = load_dictionaries(self.dict_dir)
            after = load_dictionaries(self.dict_dir, replace=replace)
            patterns.warm(after)
        except (DictionaryError, UnicodeError, re.error) as exc:
            raise AppError(422, "invalid") from exc
        pdfs = dict_trial.select_pdfs(self._settings)
        if not pdfs:
            return {"ok": False, "reason": "no_pdfs", "rev": self.rev, "pending": self.pending()}

        rev = self.rev
        self.busy = True
        self._trial_stop.clear()
        self._trial = {"running": True, "done": 0, "total": len(pdfs), "rev": rev, "result": None, "failed": False}

        def progress(done: int, total: int) -> None:
            with self.lock:
                if self._trial is not None and self._trial.get("running"):
                    self._trial.update(done=done, total=total)

        def work() -> None:
            result = None
            try:
                result = dict_trial.run_trial(
                    self._settings, before, after, pdfs, self._run_id,
                    stop=self._trial_stop.is_set, progress=progress,
                )
            except Exception as exc:  # noqa: BLE001 - 例外の中身（名刺の文字を含みうる）を出さない
                self.log_error(type(exc).__name__)
            finally:
                with self.lock:
                    self._finish_trial(rev, result)

        self._trial_thread = threading.Thread(target=work, name="dict-trial", daemon=True)
        self._trial_thread.start()
        return {"ok": True, "reason": "", "rev": self.rev, "pending": self.pending()}

    def _finish_trial(self, rev: int, result: Any) -> None:
        from app import dict_trial

        self.busy = False
        if result is None:
            self._trial = {"running": False, "done": 0, "total": 0, "rev": rev, "result": None, "failed": True}
            return
        left_behind = False
        if not result.cancelled:
            # 新しい比較表を書けたら（または違いが無かったら）、前の比較表は消す（名刺の内容の写しを増やさない）
            if self._trial_folder and self._trial_folder != result.folder:
                left_behind = not dict_trial.remove_folder(self._settings, self._trial_folder)
            self._trial_folder = result.folder
            self._log(f"DICT_TRIAL pdfs={result.pdfs} cards={result.cards} changed_cards={result.changed_cards} "
                      f"changed_pages={result.changed_pages} unreadable={result.unreadable}")
        self._trial = {"running": False, "done": result.pdfs + result.unreadable, "total": result.pdfs + result.unreadable,
                       "rev": rev, "result": {**result.to_json(), "old_folder_left": left_behind}, "failed": False}

    def cancel_trial(self, payload: dict[str, Any]) -> dict[str, Any]:
        """試し読みを止める（読んでいる PDF が終わった所で止まる）。"""
        if payload:
            raise AppError(400, "bad_request")
        if self.busy:
            self._trial_stop.set()
        return {"ok": True, "busy": self.busy}

    def stop_trial(self, timeout: float = 60.0) -> None:
        """試し読みを止めて、終わるのを待つ（黒い画面を閉じる前・Ctrl+C の時）。ロックを持たずに呼ぶ。"""
        self._trial_stop.set()
        thread = self._trial_thread
        if thread is not None:
            thread.join(timeout)

    def close(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"discard"}:
            raise AppError(400, "bad_request")
        if self.busy:
            raise AppError(409, "busy")
        if self.state == EDITING and self.pending() and payload.get("discard") is not True:
            raise AppError(409, "unsaved")
        self.state = CLOSED
        self._log("DICT_CLOSE")
        return {"ok": True, "state": CLOSED}

    def log_error(self, name: str) -> None:
        self._log(f"DICT_ERROR type={name}")

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)


# ---------------------------------------------------------------------------
# ファイルの置き換え（控え・印・復旧）
# ---------------------------------------------------------------------------

def _write_new(path: Path, data: bytes) -> None:
    """無い名前にだけ書く（既にある名前・リンクには書かない）。書き終えたらディスクへ流す。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _backup_name(filename: str, stamp: str, n: int) -> str:
    stem, dot, ext = filename.rpartition(".")
    return f"{stem}_{stamp}{'' if n == 0 else f'_{n}'}{dot}{ext}"


def _backup_re(filename: str) -> re.Pattern[str]:
    stem, dot, ext = filename.rpartition(".")
    return re.compile(re.escape(stem) + "_" + _STAMP_RE + re.escape(dot + ext))


def _tmp_path(dict_dir: Path, filename: str) -> Path:
    return dict_dir / f".{filename}.{secrets.token_hex(8)}.tmp"


class RestoreFailed(OSError):
    """置き換えの途中で失敗し、置き換えたファイルを控えから戻すこともできなかった。

    印（saving.json）を残してある。次の起動で recover_interrupted_save が戻す。
    """


def _replace_files(
    dict_dir: Path, files: list[tuple[str, bytes | None, bytes]], now: datetime | None = None,
) -> list[str]:
    """files（ファイル名・今の中身（無ければ None）・新しい中身）を、まとめて置き換える。

    戻り値は、作った控えの名前（dict\\backup\\ の中）。失敗した時は、置き換えたものを控えから戻して OSError。
    """
    backup_dir = dict_dir / BACKUP_DIR
    backup_dir.mkdir(exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    plan: list[dict[str, Any]] = []
    marker = backup_dir / MARKER_NAME
    tmps: list[Path] = []
    try:
        # 控え（全ファイル）→ 一時ファイル（全ファイル）→ 印 → 置き換え（全ファイル）
        for filename, old, _new in files:
            backup = None
            if old is not None:
                n = 0
                while (backup_dir / _backup_name(filename, stamp, n)).exists():
                    n += 1
                backup = _backup_name(filename, stamp, n)
                _write_new(backup_dir / backup, old)
            plan.append({"name": filename, "backup": backup, "sha256": hashlib.sha256(_new).hexdigest()})
        for filename, _old, new in files:
            tmp = _tmp_path(dict_dir, filename)
            _write_new(tmp, new)
            tmps.append(tmp)
        _write_new(marker, json.dumps({"files": plan}, ensure_ascii=True).encode("ascii"))
    except OSError:
        for tmp in tmps:
            tmp.unlink(missing_ok=True)
        for item in plan:                       # 何も置き換えていないので、今作った控えは要らない
            if item["backup"]:
                (backup_dir / item["backup"]).unlink(missing_ok=True)
        raise
    replaced: list[dict[str, Any]] = []
    try:
        for (filename, _old, _new), tmp, item in zip(files, tmps, plan):
            os.replace(tmp, dict_dir / filename)
            replaced.append(item)
    except OSError as exc:
        for tmp in tmps:
            tmp.unlink(missing_ok=True)
        try:
            # 一部だけ新しくなった状態を残さない。戻すのは、置き換えが済んだファイルだけ
            # （置き換えられなかったファイルは元のまま。ほかのソフトが掴んでいると、触ることもできない）
            _restore(dict_dir, replaced)
        except OSError as restore_exc:
            raise RestoreFailed(str(restore_exc)) from exc      # 印は残す
        marker.unlink(missing_ok=True)
        for item in plan:                       # 辞書は保存の前のままなので、今作った控えは要らない
            if item["backup"]:
                (backup_dir / item["backup"]).unlink(missing_ok=True)
        raise
    marker.unlink(missing_ok=True)
    _prune_backups(backup_dir, [filename for filename, _old, _new in files])
    return [item["backup"] for item in plan if item["backup"]]


def _restore(dict_dir: Path, plan: list[dict[str, Any]]) -> None:
    """plan に書かれた控えの中身に戻す。控えそのものは動かさない。"""
    backup_dir = dict_dir / BACKUP_DIR
    for item in plan:
        target = dict_dir / item["name"]
        if item["backup"] is None:
            target.unlink(missing_ok=True)      # 保存の前は無かったファイル
            continue
        data = (backup_dir / item["backup"]).read_bytes()
        tmp = _tmp_path(dict_dir, item["name"])
        _write_new(tmp, data)
        try:
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)         # 戻せなかった時に、一時ファイルを残さない
            raise


def _prune_backups(backup_dir: Path, filenames: list[str]) -> None:
    """古い控えを消す（ファイルごとに新しい順で BACKUP_KEEP 件を残す）。消せなくても失敗にしない。"""
    for filename in filenames:
        pattern = _backup_re(filename)
        try:
            names = sorted(p.name for p in backup_dir.iterdir() if p.is_file() and pattern.fullmatch(p.name))
        except OSError:
            continue
        for name in names[:-BACKUP_KEEP] if len(names) > BACKUP_KEEP else []:
            try:
                (backup_dir / name).unlink()
            except OSError:
                pass


@dataclass(frozen=True)
class Recovery:
    """前の保存の後始末の結果。何もしなかった時は偽。"""
    restored: tuple[str, ...] = ()   # 保存前の中身に戻したファイル
    kept: tuple[str, ...] = ()       # 保存が止まった後に書き換えられていたので、そのままにしたファイル

    def __bool__(self) -> bool:
        return bool(self.restored or self.kept)

    def log_line(self) -> str:
        """ログに残す 1 行（件数だけ）。"""
        return f"DICT_RECOVER restored={len(self.restored)} kept={len(self.kept)}"

    def messages(self) -> list[str]:
        """利用者に伝える文。"""
        out = []
        if self.restored:
            out.append("前回の辞書の保存が途中で止まっていたため、保存前の辞書に戻しました。")
        if self.kept:
            out.append(
                "前回の辞書の保存が途中で止まっていました。次のファイルは、その後に書き換えられていたか、"
                f"控えが無くて戻せなかったので、そのままにしてあります: {'・'.join(self.kept)}"
            )
        return out


def _remove_stale_tmp(dict_dir: Path) -> None:
    """前の実行が残した一時ファイル（足した語が入っている）を消す。消せなくても続ける。"""
    try:
        paths = [p for p in dict_dir.iterdir() if p.is_file() and _TMP_RE.fullmatch(p.name)]
    except OSError:
        return
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def recover_interrupted_save(dict_dir: Path) -> Recovery:
    """前の保存が置き換えの途中で止まっていたら、保存前の辞書に戻す。何かした時は、真になる結果を返す。

    全自動・確認画面・辞書の画面のどれも、ロックを取った後・辞書を読む前に呼ぶ。
    戻すのは、中身が「止まった保存が書こうとした中身」のままのファイルだけ。止まった後に利用者が
    書き換えたファイルは、上書きしない（控えにも残っていない中身を消さないため）。
    戻せない時は DictionaryError（半端な辞書のまま実行しない）。
    """
    backup_dir = dict_dir / BACKUP_DIR
    marker = backup_dir / MARKER_NAME
    _remove_stale_tmp(dict_dir)
    try:
        raw = marker.read_bytes()
    except FileNotFoundError:
        return Recovery()
    except OSError as exc:
        raise DictionaryError(f"{MARKER_NAME}: 前の保存の記録を読めません") from exc
    known = {kind.filename for kind in KINDS}
    restored: list[dict[str, Any]] = []
    kept: list[str] = []
    try:
        plan = json.loads(raw.decode("ascii"))["files"]
        for item in plan:
            name, backup, digest = item["name"], item["backup"], item["sha256"]
            if name not in known or not (backup is None or _backup_re(name).fullmatch(backup)):
                raise ValueError(name)
            if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("sha256")
        if _is_link(dict_dir) or _is_link(backup_dir):
            raise ValueError("link")
        for item in plan:
            target = dict_dir / item["name"]
            try:
                current = target.read_bytes()
            except FileNotFoundError:
                current = None
            try:
                original = None if item["backup"] is None else (backup_dir / item["backup"]).read_bytes()
            except FileNotFoundError:
                # 控えが消されている。今の中身が、保存しようとした中身（保存前の検査を通っている）なら、
                # 戻せないのでそのままにして続ける。それ以外は、利用者が直した中身なので、やはりそのままにする
                kept.append(item["name"])
                continue
            if current == original:
                continue                                    # 置き換わる前に止まった
            if current is not None and hashlib.sha256(current).hexdigest() == item["sha256"]:
                restored.append(item)                       # 置き換わった所で止まった
            else:
                kept.append(item["name"])                   # その後に、外で書き換えられた
        _restore(dict_dir, restored)
    except (ValueError, KeyError, TypeError, UnicodeError, OSError) as exc:
        raise DictionaryError(
            f"{MARKER_NAME}: 前の保存が途中で止まっていますが、保存前の辞書に戻せません"
            f"（dict\\{BACKUP_DIR} の控えを確かめてください）"
        ) from exc
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        raise DictionaryError(f"{MARKER_NAME}: 前の保存の記録を消せません（dict\\{BACKUP_DIR} を確かめてください）") from exc
    _remove_stale_tmp(dict_dir)
    return Recovery(restored=tuple(item["name"] for item in restored), kept=tuple(kept))


def latest_backup(dict_dir: Path, filename: str) -> str | None:
    """filename の最新の控えの名前（無ければ None）。"""
    backup_dir = dict_dir / BACKUP_DIR
    pattern = _backup_re(filename)
    try:
        names = sorted(p.name for p in backup_dir.iterdir() if p.is_file() and pattern.fullmatch(p.name))
    except OSError:
        return None
    return names[-1] if names else None


# ---------------------------------------------------------------------------
# 全体の流れ（python -m app dict）
# ---------------------------------------------------------------------------

def run_dict_editor(settings: Any, open_browser: bool = True, echo: Any = None) -> int:
    """辞書の画面を開き、「終わる」が押されるまで待つ。呼び出し元がロックを持っていること。"""
    from app import fileops, runlog

    if echo is None:
        def echo(text: str) -> None:
            print(text, flush=True)

    paths = settings.paths
    dict_dir = paths.dict_dir
    try:
        inside = dict_dir.resolve().is_relative_to(paths.base.resolve())
    except OSError:
        inside = False
    if not inside:
        # ロックは logs フォルダごと。辞書を別の場所に置いて共有していると、ほかのフォルダの実行と重なる
        echo("config.ini の辞書のフォルダが、このツールのフォルダの外にあります。画面からは編集できません。")
        return 2
    try:
        fileops.ensure_dirs(paths)
    except OSError:
        echo("起動に失敗しました: 必要なフォルダを作れません。")
        return 2
    run_id = fileops.make_run_id(datetime.now(), paths.output, paths.logs)
    logger = runlog.setup_run_logger(paths.logs / f"meishi_{run_id}.log")

    try:
        recovery = recover_interrupted_save(dict_dir)
        for line in recovery.messages():
            echo(line)
        if recovery:
            logger.info(recovery.log_line())
        session = DictSession(dict_dir, logger, settings=settings, run_id=run_id)
    except DictionaryError as exc:
        echo(f"辞書を読めません: {exc}")
        echo("メモ帳で直すか、控えから戻してください。控えは dict\\backup フォルダにあります（保存のたびに作られます）。")
        name = str(exc).split(":", 1)[0]
        newest = latest_backup(dict_dir, name) if name in {kind.filename for kind in KINDS} else None
        if newest is not None:
            echo(f"いちばん新しい控え: dict\\backup\\{newest}")
        return 2
    logger.info("DICT_START")

    from app import dict_server   # 待ち受けを使う時だけ読み込む

    server = dict_server.DictServer(session)
    server.start()
    echo("")
    echo("ブラウザで辞書の画面を開きます。言葉を足したら「保存する」を、終わる時は「終わる」を押してください。")
    echo("ブラウザを閉じただけでは終わりません。画面が開かない時、閉じてしまった時は、")
    echo("次のアドレスをブラウザで開いてください:")
    echo(f"  {server.url}")
    echo("（この黒い画面を開いている間は、名刺の読み取りを実行できません）")
    echo("")
    if open_browser and not dict_server.open_browser(server.url):
        echo("ブラウザを自動で開けませんでした。上のアドレスを手で開いてください。")
    try:
        server.wait()
    except KeyboardInterrupt:
        session.stop_trial()   # 試し読みの途中なら、読んでいる PDF が終わる所で止める
        with session.lock:     # 保存の途中なら、終わるまで待ってから抜ける
            pass
    finally:
        server.stop()
        session.stop_trial()
    if session.idle_closed:
        echo("長い間、操作が無かったので、辞書の画面を終わりました（開いている間は、名刺の読み取りを実行できないため）。")
    with session.lock:
        left = session.pending() if session.state != CLOSED else 0
        if session.state != CLOSED:
            session.busy = False     # 試し読みが止まるのを待ちきれなかった時も、ここで終わる
            session.close({"discard": True})
    echo("辞書の画面を終わりました。" + (f"保存していない変更 {left} 件は、取り消しました。" if left else ""))
    return 0
