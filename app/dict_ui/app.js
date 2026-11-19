// 読み取りの辞書の画面。サーバーが返す状態をそのまま描き、検査はサーバーに任せる。
// 語は textContent・input.value・aria-label だけで入れる。語を要素の id・セレクター・URL には使わない（一覧の行は添字で引く）。
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);

  // 足す・消す・戻す・試し読みの、受け付けなかった理由（応答の reason）
  const REASON_TEXT = {
    empty: '言葉を入れてください',
    bad_chars: '改行や、見えない文字が入っています。入れ直してください',
    too_long: '長すぎます（40 文字まで）',
    hash: '# で始まる言葉は足せません',
    duplicate: 'この言葉は、もう入っています',
    too_many: 'この辞書には、これ以上足せません（500 語まで）',
    in_other_prefix: '同じ言葉を、電話の見出しと FAX の見出しの両方には入れられません',
    too_many_changes: '保存していない変更が多すぎます。いったん保存してください',
    kanji_only: '旧字体と新字体は、漢字 1 文字ずつで入れてください',
    same_char: '同じ字は登録できません',
    area_code: '市外局番は、0 で始まる 2〜5 桁の半角の数字で入れてください',
    note_start: 'メモは、= + - @ のどれかで始めないでください',
    last_word: '最後の 1 語は消せません',
    not_found: '見つかりませんでした。画面を読み込み直してください',
    no_changes: '保存していない変更がありません。言葉を足すか消してから、試し読みをしてください',
    no_pdfs: '試し読みに使える PDF がありません（done フォルダと input フォルダ）',
  };
  // 足せたが気をつけたい点（応答の warnings）。in_other_kind は「in_other_kind:<辞書の識別子>」の形で来る
  const WARNING_TEXT = {
    short: '短い言葉は、氏名や部署の一部に一致して、読み違いが増えることがあります',
    short_prefix: '短い見出しは、番号の前にあるほかの文字に一致して、読み違いが増えることがあります',
    has_space: '言葉の途中に空白が入っています。2 つの言葉なら、1 つずつ足してください',
    card_value: '名刺の内容そのもの（会社名・氏名・番号）に見えます。足すのは語尾や見出しだけにしてください',
    no_effect: 'この字は、読み取りの時に先に別の字へ直されるので、登録しても効きません',
  };
  const OTHER_KIND_CODE = 'in_other_kind';
  // 通信の失敗（HTTP のエラーの error）
  const ERROR_TEXT = {
    stale: '画面の内容が古くなっていました。もう一度押してください',
    changed_outside: '辞書ファイルが、この画面の外で書き換えられています。「読み直す」を押すと、今のファイルを読み直します。保存していない変更は、そのまま残ります',
    read_only: '辞書ファイルが読み取り専用になっています。保存できません',
    unsafe_place: '辞書の控えを置くフォルダ（dict の backup）を使えません。保存できません',
    invalid: '保存できない言葉が含まれています。足した言葉を確かめてください',
    invalid_file: '辞書ファイルに読めない行があります。メモ帳で直してから、もう一度読み直してください',
    save_failed: '保存できませんでした。辞書ファイルをほかのソフト（Excel など）で開いていたら閉じて、もう一度「保存する」を押してください。辞書は保存する前のままです',
    restart_needed: '保存の途中で問題が起きました。辞書ファイルをほかのソフト（Excel など）で開いていたら閉じて、もう一度「保存する」を押してください。それでも同じ文が出る時は、「終わる」を押してこの画面を終わり、dict.bat をもう一度実行してください（開き直すと、辞書は正しい状態に戻ります）',
    duplicate_added: '足した言葉の中に、同じ言葉が 2 つあります（印の付いた言葉）。片方を取り消してから、保存してください',
    busy: '試し読みの途中です。終わるのを待つか、中止してから操作してください',
    unavailable: 'この画面では、試し読みを使えません',
    too_large: '入力が長すぎます',
    internal: 'ツールの中で問題が起きました。続けられない時は、黒い画面（dict.bat）を閉じて、もう一度 dict.bat を実行してください。',
  };
  const OTHER_4XX_TEXT = 'この操作は受け付けられませんでした。画面を読み込み直してください';
  // 「試し読み」を押した時の最初の確かめで止まった時の文（保存はしていないので、「保存する」を押す案内にしない）
  const TRIAL_ERROR_TEXT = {
    save_failed: '試し読みを始められませんでした。辞書ファイルをほかのソフト（Excel など）で開いていたら閉じて、もう一度「試し読み」を押してください。保存していない変更は、辞書ファイルに書いていません',
    restart_needed: '試し読みを始められませんでした。前回の保存が、途中で止まったままになっています（保存していない変更は、辞書ファイルに書いていません）。辞書ファイルをほかのソフト（Excel など）で開いていたら閉じて、もう一度「試し読み」を押してください。それでも同じ文が出る時は、「終わる」を押してこの画面を終わり、dict.bat をもう一度実行してください（開き直すと、辞書は正しい状態に戻ります）',
    duplicate_added: '足した言葉の中に、同じ言葉が 2 つあります（印の付いた言葉）。片方を取り消してから、もう一度「試し読み」を押してください',
  };
  const TRIAL_FAILED_TEXT = '試し読みを続けられませんでした。もう一度試してください。';
  const TRIAL_NO_CARDS_TEXT = '名刺が 1 枚も見つかりませんでした。結果は確かめられていません。';
  const SHEET_LEFT_HEAD = '試し読みの比較表が、output フォルダの';
  const SHEET_LEFT_TAIL = 'に残っています。比較表には名刺の内容が入っています。確かめたら、clear.bat で消してください。';
  const TRIAL_CANCELLED_TEXT = '試し読みを中止しました。';
  const TRIAL_STALE_TEXT = 'この後に辞書を変えています。もう一度試し読みをしてください。';
  const TRIAL_CANCELLING_TEXT = '中止しています。読んでいる PDF が終わるまで、お待ちください。';
  const CLOSED_TITLE = 'この画面は、もう終わっています。';
  const CLOSED_LEAD = '辞書を編集する時は、dict.bat をもう一度実行してください';
  const CONNECT_TEXT = 'ツールとの接続が切れました。黒い画面（dict.bat）を閉じていないか確かめ、「もう一度つなぐ」を押してください。';
  const START_TEXT = 'ツールにつながりません。黒い画面（dict.bat）を閉じてしまった時は、もう一度 dict.bat を実行してください。';
  const RETRY_FAIL_TEXT = 'まだつながりません。黒い画面（dict.bat）を閉じてしまった時は、もう一度 dict.bat を実行してください。';
  const UNIT = { words: '語', old_kanji: '件', area_codes: '件' };
  const FIELD_LABELS = {
    words: ['足す語', ''],
    old_kanji: ['旧字体', '新字体'],
    area_codes: ['市外局番', 'メモ'],
  };
  const SEARCH_ANNOUNCE_DELAY = 500;   // 検索の件数を読み上げるまでの待ち ms（入力のたびに鳴らさない）
  const POLL_INTERVAL = 1000;          // 試し読みの間だけ、状態を取り直す間隔 ms

  const st = {
    kinds: [], sel: '', rev: 0, pending: 0, limits: { word: 40, note: 40, words: 500 },
    filter: '', loaded: false, working: false, ended: false,
    busy: false, trial: null, cancelling: false, overlay: '',   // overlay: 画面の覆いの種類（'' なし・'save' 保存などの出力中・'trial' 試し読み中）
    trialKey: '', focusResult: false,
    sheets: [],               // output に残っている比較表のフォルダの名前
  };
  const timers = new Map();
  let searchTimer = 0;
  let pollTimer = 0;

  // ---------------------------------------------------------------- 通信

  async function api(path, body) {
    const opt = { cache: 'no-store' };
    if (body !== undefined) {
      opt.method = 'POST';
      opt.headers = { 'Content-Type': 'application/json' };
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch('api/' + path, opt);
    } catch (cause) {
      const err = new Error('network');
      err.status = 0;
      err.code = '';
      throw err;
    }
    if (!res.ok) {
      let code = '';
      try {
        const info = await res.json();
        if (info && typeof info.error === 'string') code = info.error;
      } catch (cause) {
        code = '';
      }
      const err = new Error('HTTP ' + res.status);
      err.status = res.status;
      err.code = code;
      throw err;
    }
    return res.json();
  }

  // ---------------------------------------------------------------- 補助

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // 文字が変わった時だけ書く（読み上げの対象を、同じ文で何度も鳴らさない）
  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  // 画面を見ていない利用者へ、状態の変化を伝える
  function announce(text) {
    const box = $('announce');
    box.textContent = '';
    window.setTimeout(() => { box.textContent = text; }, 30);
  }

  // 画面の中の知らせ（role="status"）。同じ文を続けて出しても読み上げ直されるよう、いったん空にしてから書く
  function setMsg(node, tone, head, lines) {
    window.clearTimeout(timers.get(node));
    node.textContent = '';
    node.className = 'msg';
    if (!head) return;
    timers.set(node, window.setTimeout(() => {
      node.className = 'msg msg-' + tone;
      node.append(el('span', 'msg-head', head));
      if (lines && lines.length === 1) {
        node.append(document.createTextNode(' '), el('span', '', lines[0]));
      } else if (lines && lines.length > 1) {
        const ul = el('ul');
        for (const line of lines) ul.append(el('li', '', line));
        node.append(ul);
      }
    }, 30));
  }

  const cur = () => st.kinds.find((k) => k.key === st.sel);

  // 注意の符号を文にする。in_other_kind:<辞書の識別子> は、その辞書の名前を状態から引く
  function warningText(code) {
    if (code === OTHER_KIND_CODE || code.startsWith(OTHER_KIND_CODE + ':')) {
      const other = st.kinds.find((k) => k.key === code.slice(OTHER_KIND_CODE.length + 1));
      return other ? '同じ言葉が、「' + other.title + '」の辞書にもあります' : '同じ言葉が、ほかの辞書にもあります';
    }
    return WARNING_TEXT[code] || '';
  }
  const fold = (text) => text.normalize('NFKC').toLowerCase();
  const unitOf = (k) => UNIT[k.form] || '語';

  function entryLabel(form, e) {
    if (form === 'old_kanji') return e.old + ' → ' + e.new;
    if (form === 'area_codes') return e.note ? e.code + '（' + e.note + '）' : e.code;
    return e.value;
  }

  function searchText(form, e) {
    if (form === 'old_kanji') return e.old + ' ' + e.new;
    if (form === 'area_codes') return e.code + ' ' + e.note;
    return e.value;
  }

  function entryPayload(form, e) {
    if (form === 'old_kanji') return { old: e.old };
    if (form === 'area_codes') return { code: e.code };
    return { value: e.value };
  }

  const changesOf = (k) => k.entries.filter((e) => e.status !== 'file').length;
  const removedOf = (k) => k.entries.filter((e) => e.status === 'removed').length;

  // ---------------------------------------------------------------- 表示

  function renderNav() {
    const list = $('kind-list');
    if (list.children.length !== st.kinds.length) {
      list.textContent = '';
      st.kinds.forEach((k, i) => {
        const li = el('li');
        const btn = el('button', 'kind-btn');
        btn.type = 'button';
        btn.dataset.i = String(i);
        // 要素の間に空白を入れる（読み上げで「役職201 語」と続けて読まれないように）
        btn.append(el('span', 'kind-name'), document.createTextNode(' '), el('span', 'kind-count'),
          document.createTextNode(' '), el('span', 'kind-changes'));
        li.append(btn);
        list.append(li);
      });
    }
    st.kinds.forEach((k, i) => {
      const btn = list.children[i].firstChild;
      const [name, count, changes] = btn.children;
      setText(name, k.title);
      setText(count, k.count + ' ' + unitOf(k));
      const n = changesOf(k);
      setText(changes, n ? '変更 ' + n + ' 件' : '');
      changes.hidden = n === 0;
      if (k.key === st.sel) btn.setAttribute('aria-current', 'true');
      else btn.removeAttribute('aria-current');
    });
  }

  function renderDetail() {
    const k = cur();
    if (!k) return;
    setText($('detail-title'), k.title);
    setText($('kind-desc'), k.description);
    setText($('kind-example'), k.example);
    setText($('kind-hint'), k.hint);
    setText($('kind-file'), k.file);
    $('kind-default').hidden = !k.from_default;
    const labels = FIELD_LABELS[k.form] || FIELD_LABELS.words;
    setText($('label-a'), labels[0]);
    setText($('label-b'), labels[1]);
    $('field-b').hidden = !labels[1];
    const lim = st.limits;
    let help = '1 語は ' + lim.word + ' 文字まで、1 つの辞書に ' + lim.words + ' 語まで入ります。';
    if (k.form === 'old_kanji') help = '旧字体と新字体を、漢字 1 文字ずつ入れます。';
    if (k.form === 'area_codes') help = '市外局番は、0 で始まる 2〜5 桁の半角の数字で入れます。メモは ' + lim.note + ' 文字までで、省略できます。';
    setText($('input-help'), help);
    // 入力の長さの上限。maxlength は UTF-16 の単位で数えるため、1 文字で 2 つ分の字（𠮷 など）が入らなくなる。
    // 旧字体と新字体は 2 にして、1 文字までへの切り詰めは limitOneChar で行う
    // 語・市外局番・メモは、上限より長く入れられるようにしておく（上限ちょうどで止めると、長すぎる入力が
    // 理由も出さずに切り詰められて足される。長すぎる時は、サーバーが理由を返す）
    const lens = k.form === 'old_kanji' ? [2, 2] : (k.form === 'area_codes' ? [20, 200] : [200, 200]);
    $('add-a').maxLength = lens[0];
    $('add-b').maxLength = lens[1];
    // 名刺の内容を登録しないでほしい案内は、語の辞書だけに出す。隠した要素も説明に読まれるので、参照も外す
    const wordsKind = k.form === 'words';
    $('card-note').hidden = !wordsKind;
    const desc = (wordsKind ? 'card-note ' : '') + 'input-help add-msg';
    $('add-a').setAttribute('aria-describedby', desc);
    $('add-b').setAttribute('aria-describedby', 'input-help add-msg');
  }

  // 旧字体と新字体の欄は 1 文字まで。字の途中（サロゲート対の間）では切らない
  function limitOneChar(input) {
    const k = cur();
    if (!k || k.form !== 'old_kanji') return;
    const chars = Array.from(input.value);
    if (chars.length > 1) input.value = chars[0];
  }

  function focusEntry(i) {
    const li = $('entries').querySelector('li[data-i="' + i + '"]');
    const btn = li ? li.querySelector('button') : null;
    if (btn) btn.focus();
    return Boolean(btn);
  }

  function buildRow(k, e, i) {
    const tone = e.status === 'added' ? 'added' : (e.status === 'removed' ? 'removed' : 'file');
    const li = el('li', 'entry is-' + tone);
    li.dataset.i = String(i);
    const label = entryLabel(k.form, e);
    li.append(el('span', 'entry-text', label));
    if (tone === 'added') li.append(el('span', 'mark mark-added', '追加（未保存）'));
    if (tone === 'removed') li.append(el('span', 'mark mark-removed', '消す（未保存）'));
    if (e.dup) li.append(el('span', 'mark mark-dup', '同じ言葉が 2 行あります'));
    const btn = el('button', 'entry-btn');
    btn.type = 'button';
    btn.dataset.act = tone === 'removed' ? 'restore' : 'remove';
    if (tone === 'removed') {
      btn.textContent = '戻す';
      btn.setAttribute('aria-label', '「' + label + '」を戻す');
    } else if (tone === 'added') {
      btn.textContent = '取り消す';
      btn.setAttribute('aria-label', '「' + label + '」の追加を取り消す');
    } else {
      btn.textContent = '消す';
      btn.setAttribute('aria-label', '「' + label + '」を消す');
    }
    li.append(btn);
    return li;
  }

  function renderEntries() {
    const k = cur();
    const list = $('entries');
    const at = document.activeElement;
    const row = at && list.contains(at) ? at.closest('li') : null;
    const keep = row ? row.dataset.i : null;
    list.textContent = '';
    if (!k) return;
    const q = fold(st.filter.trim());
    let shown = 0;
    k.entries.forEach((e, i) => {
      if (q && !fold(searchText(k.form, e)).includes(q)) return;
      shown += 1;
      list.append(buildRow(k, e, i));
    });
    const unit = unitOf(k);
    const removed = removedOf(k);
    let count = k.count + ' ' + unit;
    if (removed) count += '（消す予定の ' + removed + ' ' + unit + ' を除く）';
    if (q) count = k.count + ' ' + unit + ' のうち、' + shown + ' 件が一致';
    setText($('count'), count);
    const empty = $('empty');
    if (shown === 0) {
      setText(empty, q ? '一致する言葉がありません' : 'この辞書には、まだ言葉がありません');
      empty.hidden = false;
    } else {
      empty.hidden = true;
    }
    if (keep !== null) focusEntry(keep);
  }

  function renderFooter() {
    const n = st.pending;
    setText($('pending'), n ? '保存していない変更 ' + n + ' 件' : '保存していない変更はありません');
    $('pending').classList.toggle('has-changes', n > 0);
    $('save').disabled = n === 0;
    $('trial').disabled = n === 0 || st.busy;
  }

  function render() {
    $('loading').hidden = true;
    $('kinds').hidden = false;
    $('detail').hidden = false;
    renderNav();
    renderDetail();
    renderEntries();
    renderFooter();
  }

  // ---------------------------------------------------------------- 状態の取得

  function applyState(s) {
    st.kinds = s.kinds;
    st.rev = s.rev;
    st.pending = s.pending;
    if (s.limits) st.limits = s.limits;
    if (!st.kinds.some((k) => k.key === st.sel)) st.sel = st.kinds.length ? st.kinds[0].key : '';
    const wasBusy = st.busy;
    st.busy = s.busy === true;
    st.trial = s.trial || null;
    st.sheets = Array.isArray(s.sheets) ? s.sheets.filter((name) => typeof name === 'string') : [];
    st.loaded = true;
    render();
    renderTrial(wasBusy);
  }

  // 戻り値: 続けてよいか（サーバーが終わっていたら false）
  async function refreshState() {
    const s = await api('state');
    if (s.state === 'closed') {
      showEnd(CLOSED_TITLE, CLOSED_LEAD);
      return false;
    }
    applyState(s);
    return true;
  }

  // ---------------------------------------------------------------- 試し読み

  function stopPoll() {
    window.clearTimeout(pollTimer);
    pollTimer = 0;
  }

  // 試し読みの間だけ、1 秒おきに状態を取り直す
  function schedulePoll() {
    if (pollTimer || !st.busy || st.ended) return;
    pollTimer = window.setTimeout(poll, POLL_INTERVAL);
  }

  async function poll() {
    pollTimer = 0;
    if (!st.busy || st.ended) return;
    try {
      const s = await api('state');
      if (s.state === 'closed') {
        showEnd(CLOSED_TITLE, CLOSED_LEAD);
        return;
      }
      if (s.busy === true) {
        // まだ読んでいる間は、進み具合だけ書き換える（辞書の一覧は描き直さない）
        st.trial = s.trial || null;
        renderTrial(true);
      } else {
        applyState(s);
      }
    } catch (err) {
      // つながらない時は、覆いを外して、「もう一度つなぐ」の出る帯を使えるようにする
      st.busy = false;
      renderTrial(true, true);
      await handleError(err);
      if (!st.ended && !$('notice-retry').hidden) $('notice-retry').focus();
    }
  }

  // 試し読みの進み具合の文
  function progressText() {
    if (st.cancelling) return TRIAL_CANCELLING_TEXT;
    const t = st.trial;
    const total = t && t.total ? t.total : 0;
    const n = total ? '（' + Math.min(t.done, total) + '／' + total + ' 本）' : '';
    return '読み比べています' + n + '。PDF が多い時は数十秒かかります。';
  }

  // 試し読みの結果の中身。戻り値は、読み上げに使う 1 行目
  function fillTrialResult(t) {
    const body = $('trial-result-body');
    body.textContent = '';
    if (t.stale) body.append(el('p', 'trial-stale', TRIAL_STALE_TEXT));
    const r = t.result;
    if (t.failed || !r) {
      body.append(el('p', '', TRIAL_FAILED_TEXT));
      return TRIAL_FAILED_TEXT;
    }
    if (r.cancelled) {
      body.append(el('p', '', TRIAL_CANCELLED_TEXT));
      return TRIAL_CANCELLED_TEXT;
    }
    const first = 'PDF ' + r.pdfs + ' 本・名刺 ' + r.cards + ' 枚を読み比べました。';
    body.append(el('p', '', first));
    if (r.unreadable >= 1) {
      body.append(el('p', '', '読めなかった PDF が ' + r.unreadable + ' 本ありました（飛ばしました）。'));
    }
    if (r.pdfs === 0) {
      body.append(el('p', '', '読める PDF がありませんでした。結果は確かめられていません。'));
    } else if (r.cards === 0 && r.changed_cards === 0 && r.changed_pages === 0) {
      body.append(el('p', '', TRIAL_NO_CARDS_TEXT));      // 比べる名刺が無かった（「変わりません」とは言えない）
    } else if (r.changed_cards === 0 && r.changed_pages === 0) {
      body.append(el('p', '', '読み取りの結果は変わりません。'));
    } else {
      body.append(el('p', 'trial-count', '結果が変わる名刺: ' + r.changed_cards + ' 枚'));
      if (Array.isArray(r.fields) && r.fields.length) {
        const table = el('table', 'trial-table');
        const head = el('tr');
        for (const name of ['項目', '変わる名刺の枚数']) {
          const th = el('th', '', name);
          th.scope = 'col';
          head.append(th);
        }
        const thead = el('thead');
        thead.append(head);
        const tbody = el('tbody');
        for (const f of r.fields) {
          const tr = el('tr');
          const th = el('th', '', f.name);
          th.scope = 'row';
          tr.append(th, el('td', 'num', String(f.cards)));
          tbody.append(tr);
        }
        table.append(thead, tbody);
        const wrap = el('div', 'trial-table-wrap');
        wrap.append(table);
        body.append(wrap);
      }
      if (r.changed_pages >= 1) {
        body.append(el('p', '', '名刺の分け方が変わるページ: ' + r.changed_pages + ' ページ'));
      }
    }
    if (r.folder) {
      body.append(el('p', '', 'どの名刺のどの項目がどう変わるかは、output フォルダの「' + r.folder + '」にある ' + r.file + ' に書きました。'));
      body.append(el('p', 'trial-care', '比較表には名刺の内容が入っています。確かめたら、clear.bat で消してください。'));
    }
    if (r.old_folder_left) {
      body.append(el('p', '', '前の比較表のフォルダを消せませんでした（Excel で開いていると消せません）。output フォルダに残っています。確かめたら、clear.bat で消してください。'));
    }
    return first;
  }

  // 状態の trial を画面に出す。wasBusy: 取り直す前は読み比べの途中だった（終わった時だけ、結果へフォーカスを移す）
  function renderTrial(wasBusy, keepResult) {
    try {
      renderTrialBody(wasBusy, keepResult);
    } finally {
      renderSheetNote();
    }
  }

  // 比較表のフォルダが output に残っている時の案内。結果の欄が同じフォルダを案内している間は出さない
  function renderSheetNote() {
    const note = $('sheet-left');
    const r = st.trial && st.trial.result;
    const shown = !$('trial-result').hidden && r && r.folder ? r.folder : '';
    const names = st.busy ? [] : st.sheets.filter((name) => name !== shown);
    if (!names.length) {
      note.hidden = true;
      return;
    }
    setText(note, SHEET_LEFT_HEAD + names.map((name) => '「' + name + '」').join('') + SHEET_LEFT_TAIL);
    note.hidden = false;
  }

  function renderTrialBody(wasBusy, keepResult) {
    const t = st.trial;
    if (st.busy) {
      // 読み比べの途中: 覆いを出す（保存などの出力中の覆いがある間は、そちらを優先する）
      if (st.overlay === '') {
        setBusy(true, progressText(), 'trial');
        announce('試し読みを始めました。');
      } else if (st.overlay === 'trial') {
        setText($('busy-text'), progressText());
      }
      $('trial-result').hidden = true;
      st.trialKey = '';
      schedulePoll();
      return;
    }
    stopPoll();
    st.cancelling = false;
    const wasTrialOverlay = st.overlay === 'trial';
    if (wasTrialOverlay) setBusy(false);
    if (keepResult) return;
    if (!t || t.running) {
      $('trial-result').hidden = true;
      st.trialKey = '';
      return;
    }
    // 結果。同じ中身なら描き直さない（取り直すたびに読み上げ直さない）
    const key = JSON.stringify(t);
    if (key === st.trialKey) return;
    st.trialKey = key;
    const lead = fillTrialResult(t);
    $('trial-result').hidden = false;
    if (wasBusy || wasTrialOverlay || st.focusResult) {
      $('trial-result').focus();
      announce(lead);
    }
  }

  function startTrial() {
    return exclusive(async () => {
      hideNotice();
      let res;
      try {
        res = await api('trial', { rev: st.rev });
      } catch (err) {
        await handleError(err, TRIAL_ERROR_TEXT);
        return;
      }
      if (!res.ok) {
        showNotice(REASON_TEXT[res.reason] || '試し読みを始められませんでした。画面を読み込み直してください');
        return;
      }
      st.cancelling = false;
      st.focusResult = true;     // 読み比べがすぐ終わった時も、結果へフォーカスを移す
      st.trialKey = '';
      try {
        if (!(await refreshState())) return;
      } finally {
        st.focusResult = false;
      }
      if (!st.busy && $('trial-result').hidden) $('trial').focus();
    });
  }

  async function cancelTrial() {
    if (st.cancelling || !st.busy) return;
    st.cancelling = true;
    setText($('busy-text'), TRIAL_CANCELLING_TEXT);
    $('trial-cancel').hidden = true;
    $('busy-text').focus({ preventScroll: true });
    try {
      const res = await api('trial/cancel', {});
      if (res && res.busy === false) await refreshState();
    } catch (err) {
      st.cancelling = false;
      st.busy = false;
      stopPoll();
      renderTrial(true, true);
      await handleError(err);
      if (!st.ended && !$('notice-retry').hidden) $('notice-retry').focus();
    }
  }

  // ---------------------------------------------------------------- お知らせ・結果・出力中

  function showNotice(text, opt) {
    const o = opt || {};
    $('notice-text').textContent = text;
    $('notice-retry').hidden = !o.retry;
    $('notice-reload').hidden = !o.reload;
    $('notice').hidden = false;
  }

  function hideNotice() {
    if ($('notice').hidden) return;
    const inside = $('notice').contains(document.activeElement);
    $('notice').hidden = true;
    if (inside && !st.ended && st.loaded) $('add-a').focus();
  }

  function showResult(title, lines, focus) {
    $('result-title').textContent = title;
    const body = $('result-body');
    body.textContent = '';
    for (const line of lines) body.append(el('p', '', line));
    $('result').hidden = false;
    if (focus) $('result').focus({ preventScroll: true });
  }

  function hideResult() {
    $('result').hidden = true;
  }

  function clearMsgs() {
    setMsg($('add-msg'), '', '');
    setMsg($('row-msg'), '', '');
    setInvalid(false);
  }

  function setInvalid(on) {
    for (const id of ['add-a', 'add-b']) {
      if (on) $(id).setAttribute('aria-invalid', 'true');
      else $(id).removeAttribute('aria-invalid');
    }
  }

  // 画面全体の覆い。背面は操作できなくする。kind が 'trial' の時だけ「中止」を出す（試し読み）
  function setBusy(on, text, kind) {
    for (const part of document.querySelectorAll('.bar, .notice, .result, .dict-work, .finish')) part.inert = on;
    $('busy').hidden = !on;
    setText($('busy-text'), on ? text : '');
    $('trial-cancel').hidden = !on || kind !== 'trial';
    $('skip').hidden = on;
    st.overlay = on ? (kind || 'save') : '';
    if (on) $('busy-text').focus({ preventScroll: true });
  }

  function showEnd(title, lead) {
    st.ended = true;
    for (const dlg of document.querySelectorAll('dialog[open]')) dlg.close();
    $('app').hidden = true;
    $('skip').hidden = true;
    $('done-title').textContent = title;
    $('done-lead').textContent = lead;
    $('done').hidden = false;
    $('done').focus();
  }

  // 足した言葉の中の同じ言葉（dup の印の付いた行）へ移る。見つかったら true
  function focusDuplicateAdded() {
    for (const k of st.kinds) {
      const at = k.entries.findIndex((e) => e.status === 'added' && e.dup);
      if (at < 0) continue;
      if (k.key !== st.sel) selectKind(st.kinds.indexOf(k));
      st.filter = '';
      $('filter').value = '';
      renderEntries();
      return focusEntry(at);
    }
    return false;
  }

  // 通信や保存の失敗を、利用者が次にすることの分かる文にして帯に出す。戻り値: フォーカスを移したか
  // texts: この操作だけの文（符号 → 文）。無い符号は、共通の文を使う
  async function handleError(err, texts) {
    const code = err.code || '';
    const status = err.status || 0;
    if (code === 'closed') {
      showEnd(CLOSED_TITLE, CLOSED_LEAD);
    } else if (code === 'stale') {
      try { await refreshState(); } catch (cause) { /* 取り直せない時は、下の帯だけ出す */ }
      if (!st.ended) showNotice(ERROR_TEXT.stale);
    } else if (code === 'busy') {
      // 試し読みの途中だった（ほかの画面で始めた・再読み込みした）。取り直すと覆いが出る
      try { await refreshState(); } catch (cause) { /* 取り直せない時は、下の帯だけ出す */ }
      if (!st.ended && !st.busy) showNotice(ERROR_TEXT.busy);
    } else if (code === 'changed_outside' || code === 'invalid_file') {
      showNotice(ERROR_TEXT[code], { reload: true });
      $('notice-reload').focus();
      return true;
    } else if (code === 'duplicate_added') {
      try { await refreshState(); } catch (cause) { /* 取り直せなくても、帯は出す */ }
      if (!st.ended) {
        showNotice((texts && texts.duplicate_added) || ERROR_TEXT.duplicate_added);
        return focusDuplicateAdded();
      }
    } else if (ERROR_TEXT[code]) {
      showNotice((texts && texts[code]) || ERROR_TEXT[code]);
    } else if (status === 413) {
      showNotice(ERROR_TEXT.too_large);
    } else if (status >= 400 && status < 500) {
      showNotice(OTHER_4XX_TEXT);       // 符号の表に無い 4xx。接続の失敗ではない
    } else if (status >= 500) {
      showNotice(ERROR_TEXT.internal);
    } else {
      showNotice(CONNECT_TEXT, { retry: true });   // 通信そのものが失敗した時だけ
    }
    return false;
  }

  // 変更・保存・読み直し・終わるは、1 件ずつ順に送る（前の応答が返るまで次を送らない）
  async function exclusive(task) {
    if (st.working || st.ended) return;
    st.working = true;
    try {
      await task();
    } catch (err) {
      await handleError(err);
    } finally {
      st.working = false;
    }
  }

  // ---------------------------------------------------------------- 操作

  function selectKind(i) {
    const k = st.kinds[i];
    if (!k || k.key === st.sel) return;
    st.sel = k.key;
    st.filter = '';
    $('filter').value = '';
    $('add-a').value = '';
    $('add-b').value = '';
    clearMsgs();
    render();
    announce(k.title + ' を表示しました。' + k.count + ' ' + unitOf(k));
  }

  function addEntry() {
    return exclusive(async () => {
      const k = cur();
      if (!k) return;
      hideNotice();
      hideResult();
      setMsg($('row-msg'), '', '');
      const a = $('add-a').value;
      const b = $('add-b').value;
      let body = { value: a };
      if (k.form === 'old_kanji') body = { old: a, new: b };
      if (k.form === 'area_codes') body = { code: a, note: b };
      let res;
      try {
        res = await api('add/' + k.key, body);
      } catch (err) {
        await handleError(err);
        if (!st.ended) $('add-a').focus();
        return;
      }
      if (!res.ok) {
        setInvalid(true);
        setMsg($('add-msg'), 'error', '追加できませんでした。', [REASON_TEXT[res.reason] || '受け付けられませんでした。入れ直してください']);
        $('add-a').focus();
        return;
      }
      setInvalid(false);
      $('add-a').value = '';
      $('add-b').value = '';
      if (!(await refreshState())) return;
      const shownLabel = entryLabel(k.form, { value: a.trim(), old: a.trim(), new: b.trim(), code: a.trim(), note: b.trim() });
      const warnings = (res.warnings || []).map(warningText).filter(Boolean);
      let head = '「' + shownLabel + '」を追加しました。保存するまで、辞書ファイルは変わりません。';
      if (res.kept === true) head = '消す予定だった「' + shownLabel + '」を、そのまま残すことにしました。';
      const q = fold(st.filter.trim());
      if (q && !fold(searchText(k.form, { value: a, old: a, new: b, code: a, note: b })).includes(q)) {
        head += '（検索で絞っているため、一覧には出ていません。）';
      }
      setMsg($('add-msg'), warnings.length ? 'warn' : 'ok', head, warnings);
      $('add-a').focus();   // 続けて足せるよう入力欄に戻す。知らせが見える位置のままにするため、一覧は動かさない
    });
  }

  // 行のボタン（消す・取り消す・戻す）
  function rowAction(i) {
    return exclusive(async () => {
      const k = cur();
      const entry = k ? k.entries[i] : undefined;
      if (!entry) return;
      hideNotice();
      hideResult();
      clearMsgs();
      const act = entry.status === 'removed' ? 'restore' : 'remove';
      const label = entryLabel(k.form, entry);
      const list = $('entries');
      const row = list.querySelector('li[data-i="' + i + '"]');
      const pos = row ? Array.prototype.indexOf.call(list.children, row) : -1;
      const wasAdded = entry.status === 'added';
      let res;
      try {
        res = await api(act + '/' + k.key, entryPayload(k.form, entry));
      } catch (err) {
        await handleError(err);
        return;
      }
      if (!(await refreshState())) return;
      if (!res.ok) {
        setMsg($('row-msg'), 'error', '変えられませんでした。', [REASON_TEXT[res.reason] || '受け付けられませんでした。画面を読み込み直してください']);
        focusEntry(i);
        return;
      }
      if (act === 'restore') setMsg($('row-msg'), 'ok', '「' + label + '」を戻しました。', []);
      else if (wasAdded) setMsg($('row-msg'), 'ok', '「' + label + '」の追加を取り消しました。', []);
      else setMsg($('row-msg'), 'ok', '「' + label + '」を、消す（未保存）にしました。保存するまで、辞書ファイルは変わりません。', []);
      // フォーカスの行き先: 消す・戻すは同じ行（ボタンが「戻す」「消す」に変わる）。取り消した行は消えるので、次の行（最後なら前の行）
      if (wasAdded) {
        const rows = $('entries').children;
        const next = rows[pos] || rows[rows.length - 1];
        const btn = next ? next.querySelector('button') : null;
        if (btn) btn.focus();
        else if (st.filter) $('filter').focus();
        else $('add-a').focus();
      } else if (!focusEntry(i)) {
        $('add-a').focus();
      }
    });
  }

  function save() {
    return exclusive(async () => {
      hideNotice();
      hideResult();
      clearMsgs();
      setBusy(true, '保存しています。');
      try {
        const res = await api('save', { rev: st.rev });
        const alive = await refreshState();
        setBusy(false);
        if (!alive) return;
        if (!res.saved || !res.saved.length) {
          showResult('保存する変更はありませんでした。', [], true);
        } else {
          const lines = ['書き換えたファイル: ' + res.saved.join('、')];
          if (res.backups && res.backups.length) {
            lines.push('書き換える前の控え: dict フォルダの backup に置きました（' + res.backups.join('、') + '）');
          }
          showResult('保存しました。', lines, true);
        }
      } catch (err) {
        setBusy(false);
        const moved = await handleError(err);
        if (!st.ended && !moved) $('save').focus();
      }
    });
  }

  function reload() {
    return exclusive(async () => {
      hideNotice();
      hideResult();
      clearMsgs();
      setBusy(true, '読み直しています。');
      try {
        const res = await api('reload', {});
        const alive = await refreshState();
        setBusy(false);
        if (!alive) return;
        const lines = [];
        if (res.dropped >= 1) lines.push('当て直せなかった変更が ' + res.dropped + ' 件あります。');
        showResult('読み直しました。', lines, true);
      } catch (err) {
        setBusy(false);
        await handleError(err);
      }
    });
  }

  async function endEditing(discard) {
    setBusy(true, '終わっています。');
    try {
      await api('close', discard ? { discard: true } : {});
      setBusy(false);
      showEnd('終わりました。', 'このタブは閉じてかまいません。');
    } catch (err) {
      setBusy(false);
      if (err.code === 'unsaved') {
        try { await refreshState(); } catch (cause) { /* 件数が取り直せなくても、確かめる画面は出す */ }
        if (!st.ended) openCloseConfirm();
        return;
      }
      await handleError(err);
      if (!st.ended) $('quit').focus();
    }
  }

  function openCloseConfirm() {
    $('close-text').textContent = '保存していない変更が ' + st.pending + ' 件あります。取り消して終わりますか？';
    $('close-confirm').showModal();
  }

  function quit() {
    if (st.pending > 0) {   // 保存していない変更がある時は、送る前に確かめる
      openCloseConfirm();
      return;
    }
    exclusive(() => endEditing(false));
  }

  async function reconnect() {
    try {
      const alive = await refreshState();
      if (alive) hideNotice();
    } catch (err) {
      showNotice(RETRY_FAIL_TEXT, { retry: true });
    }
  }

  // ---------------------------------------------------------------- 起動

  function bind() {
    $('kind-list').addEventListener('click', (e) => {
      const btn = e.target.closest('.kind-btn');
      if (btn) selectKind(Number(btn.dataset.i));
    });
    $('kind-list').addEventListener('keydown', (e) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
      const btns = Array.from($('kind-list').querySelectorAll('.kind-btn'));
      const at = btns.indexOf(document.activeElement);
      if (at < 0) return;
      let to = at;
      if (e.key === 'ArrowDown') to = Math.min(at + 1, btns.length - 1);
      else if (e.key === 'ArrowUp') to = Math.max(at - 1, 0);
      else if (e.key === 'Home') to = 0;
      else to = btns.length - 1;
      e.preventDefault();
      btns[to].focus();
    });

    $('add-form').addEventListener('submit', (e) => {
      e.preventDefault();
      addEntry();
    });
    for (const id of ['add-a', 'add-b']) {
      $(id).addEventListener('input', (e) => {
        setInvalid(false);
        if (!e.isComposing) limitOneChar($(id));
      });
      $(id).addEventListener('compositionend', () => limitOneChar($(id)));
    }

    $('filter').addEventListener('input', () => {
      st.filter = $('filter').value;
      renderEntries();
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        const q = st.filter.trim();
        if (!q) return;
        const k = cur();
        const hit = k ? k.entries.filter((e) => fold(searchText(k.form, e)).includes(fold(q))).length : 0;
        announce(hit ? hit + ' 件が一致しました' : '一致する言葉がありません');
      }, SEARCH_ANNOUNCE_DELAY);
    });

    $('entries').addEventListener('click', (e) => {
      const btn = e.target.closest('.entry-btn');
      if (!btn) return;
      const li = btn.closest('li');
      if (li) rowAction(Number(li.dataset.i));
    });

    $('trial').addEventListener('click', startTrial);
    $('trial-cancel').addEventListener('click', cancelTrial);
    $('save').addEventListener('click', save);
    $('quit').addEventListener('click', quit);
    $('close-back').addEventListener('click', () => $('close-confirm').close());
    $('close-ok').addEventListener('click', () => {
      $('close-confirm').close();
      exclusive(() => endEditing(true));
    });
    $('notice-retry').addEventListener('click', reconnect);
    $('notice-reload').addEventListener('click', reload);
  }

  async function load() {
    try {
      await refreshState();
    } catch (err) {
      $('loading').hidden = true;
      showNotice(START_TEXT, { retry: true });
    }
  }

  bind();
  load();
})();
