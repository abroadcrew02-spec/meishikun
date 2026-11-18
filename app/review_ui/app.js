// 名刺の枠の確認画面。座標はすべてページの表示空間の pt で持ち、描く時だけ百分率に直す。
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);

  const MIN_SIDE = 14;                 // 枠の一辺の下限 pt（約 5mm）
  const CARD_SIZE = [257.95, 155.91];  // 標準の名刺 91 x 55 mm
  const SAVE_DELAY = 250;              // 修正をまとめて送るまでの待ち ms
  const BUSY_FINISH = '出力しています。\nPDF が多い時は数十秒かかります。\nこのままお待ちください。';
  const NUDGE_GROUP = 800;             // 続けて押した矢印キーを 1 回の操作にまとめる ms
  const GRIPS = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
  const FLAG_TEXT = {
    SPLIT_SUSPECT_MERGED: '2 枚がくっついている疑い',
    SPLIT_WEAK: '名刺らしくない（文字や連絡先が少ない）',
    SPLIT_ORPHAN_TEXT: '枠の外の文字をこの枠に含めた',
    SPLIT_FALLBACK: 'ページを名刺ごとに区切れなかった',
    REGION_NO_TEXT: '枠の中に文字がない',
  };
  const FILE_NOTE = {
    no_text: '読めません（文字情報がありません）',
    error: '読めません（PDF として開けない、または読み取りに失敗）',
  };
  const RESULT_TEXT = {
    ok: '出力しました',
    partial: '出力しました（文字情報の無いページを除く）',
    no_text: '読めません（文字情報なし）',
    error: '読めません',
    output_failed: '一覧表を書けませんでした',
    move_failed: '出力しましたが、PDF を移動できませんでした',
    input_changed: '確認中に PDF が変わった（または無くなった）ため、出力していません',
  };

  const st = { files: [], f: -1, p: -1, sel: null, draw: false, zoom: 'page', busy: false, ended: false };
  const hist = new Map();     // ページごとの取り消し履歴
  const timers = new Map();   // ページごとの送信待ち
  const chains = new Map();   // ページごとの送信の順番
  const unsaved = new Set();  // まだ送り終えていないページ
  const failed = new Set();   // 送信に失敗したページ
  const frameEls = new Map(); // 表示中のページの枠の要素
  let drag = null;
  let rubber = null;
  let uid = 0;
  let imageSeq = 0;
  let lastFocus = null;
  let stickyNotice = false;

  // ---------------------------------------------------------------- 通信

  async function api(path, body, keepalive) {
    const opt = { cache: 'no-store' };
    if (keepalive) opt.keepalive = true;
    if (body !== undefined) {
      opt.method = 'POST';
      opt.headers = { 'Content-Type': 'application/json' };
      opt.body = JSON.stringify(body);
    }
    const res = await fetch('api/' + path, opt);
    if (!res.ok) {
      const err = new Error('HTTP ' + res.status);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  // ---------------------------------------------------------------- 補助

  const key = (f, p) => f + '/' + p;
  const page = (f, p) => (st.files[f] ? st.files[f].pages[p] : undefined);
  const cur = () => page(st.f, st.p);
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  const round2 = (v) => Math.round(v * 100) / 100;
  const frameById = (id) => { const pg = cur(); return pg ? pg.frames.find((fr) => fr.id === id) : undefined; };
  const dialogOpen = () => document.querySelector('dialog[open]') !== null;
  const dragging = (f, p) => drag !== null && f === st.f && p === st.p;
  const reviewable = (pg) => pg.kind === 'text';

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

  const labelsOf = (file) => {
    const out = {};
    file.pages.forEach((pg, i) => { out[i] = Object.fromEntries(pg.frames.map((fr) => [fr.id, fr.label])); });
    return out;
  };

  const sameFrames = (a, b) => a.length === b.length && a.every((fr) => {
    const other = b.find((x) => x.id === fr.id);
    return Boolean(other) && fr.rect.every((v, i) => Math.abs(v - other.rect[i]) < 0.011);
  });

  function eachPage(fn) {
    st.files.forEach((file, f) => file.pages.forEach((pg, p) => { if (reviewable(pg)) fn(pg, f, p, file); }));
  }

  function historyOf(k) {
    if (!hist.has(k)) hist.set(k, { undo: [], redo: [], groupId: null, groupAt: 0 });
    return hist.get(k);
  }

  const history = () => historyOf(key(st.f, st.p));

  const snapshot = (pg) => pg.frames.map((fr) => ({ id: fr.id, label: fr.label, rect: fr.rect.slice(), flags: fr.flags.slice() }));

  function newId() {
    uid += 1;
    return 'n' + uid + '-' + Math.random().toString(36).slice(2, 8);
  }

  function warnText(fr) {
    return fr.flags.map((code) => FLAG_TEXT[code] || code).join('、');
  }

  // ---------------------------------------------------------------- 表示: 一覧

  function renderList() {
    const list = $('page-list');
    const at = document.activeElement;
    const keep = at && at.classList.contains('page-btn') ? [at.dataset.f, at.dataset.p] : null;
    list.textContent = '';
    st.files.forEach((file, f) => {
      const li = el('li', 'file');
      li.append(el('p', 'file-name', file.name));
      if (FILE_NOTE[file.status]) {
        li.append(el('p', 'file-note', FILE_NOTE[file.status]));
        li.append(el('p', 'page-off', '出力すると error フォルダへ移ります'));
        list.append(li);
        return;
      }
      const pages = el('ol', 'file-pages');
      file.pages.forEach((pg, p) => {
        const item = el('li');
        if (!reviewable(pg)) {
          item.append(el('p', 'page-off', pg.no + ' ページ目: ' + (pg.kind === 'blank' ? '白紙' : '文字情報なし（対象外）')));
          pages.append(item);
          return;
        }
        const btn = el('button', 'page-btn');
        btn.type = 'button';
        btn.dataset.f = f;
        btn.dataset.p = p;
        if (f === st.f && p === st.p) btn.setAttribute('aria-current', 'true');
        btn.append(el('span', 'page-no', pg.no + ' ページ目'));
        const chips = el('span', 'chips');
        const warns = pg.frames.filter((fr) => fr.flags.length).length;
        if (warns) chips.append(el('span', 'chip chip-warn', '要確認 ' + warns));
        if (pg.outside_count) chips.append(el('span', 'chip chip-out', '枠の外に文字'));
        if (pg.edited) chips.append(el('span', 'chip', '修正あり'));
        if (!pg.viewed) chips.append(el('span', 'chip chip-new', 'まだ見ていない'));
        btn.append(chips);
        // どの PDF のページかが名前で分かるようにする（「1 ページ目」が PDF ごとに並ぶため）
        const notes = Array.from(chips.children, (chip) => chip.textContent);
        btn.setAttribute('aria-label', [file.name + ' ' + pg.no + ' ページ目', ...notes].join('、'));
        item.append(btn);
        pages.append(item);
      });
      li.append(pages);
      list.append(li);
    });
    if (keep) {
      for (const btn of list.querySelectorAll('.page-btn')) {
        if (btn.dataset.f === keep[0] && btn.dataset.p === keep[1]) btn.focus({ preventScroll: true });
      }
    }
  }

  // ---------------------------------------------------------------- 表示: 原稿と枠

  function layoutSheet() {
    const pg = cur();
    if (!pg) return;
    const area = $('sheet-area');
    const availW = area.clientWidth - 48;
    const availH = area.clientHeight - 48;
    const actual = pg.width * 96 / 72;
    let w;
    if (st.zoom === 'page') w = Math.min(availW, availH * pg.width / pg.height);
    else if (st.zoom === 'width') w = availW;
    else w = actual * st.zoom;
    w = Math.max(240, w);
    const sheet = $('sheet');
    sheet.style.width = w + 'px';
    sheet.style.height = (w * pg.height / pg.width) + 'px';
    $('zoom-value').textContent = Math.round(w / actual * 100) + '%';
  }

  function place(node, rect, pg) {
    node.style.left = (rect[0] / pg.width * 100) + '%';
    node.style.top = (rect[1] / pg.height * 100) + '%';
    node.style.width = ((rect[2] - rect[0]) / pg.width * 100) + '%';
    node.style.height = ((rect[3] - rect[1]) / pg.height * 100) + '%';
  }

  function buildFrame(fr) {
    const node = el('div', 'frame');
    node.tabIndex = 0;
    node.dataset.id = fr.id;
    node.setAttribute('role', 'group');
    node.setAttribute('aria-describedby', 'frame-help');
    node.append(el('span', 'tag'), el('span', 'bang', '!'));
    node.firstChild.setAttribute('aria-hidden', 'true');
    node.lastChild.setAttribute('aria-hidden', 'true');
    for (const dir of GRIPS) {
      const grip = el('span', 'grip');
      grip.dataset.dir = dir;
      node.append(grip);
    }
    return node;
  }

  function syncFrames() {
    const pg = cur();
    const box = $('frames');
    const focusedId = document.activeElement && document.activeElement.classList.contains('frame')
      ? document.activeElement.dataset.id : null;
    const want = pg ? pg.frames : [];
    const ids = new Set(want.map((fr) => fr.id));
    for (const [id, node] of frameEls) {
      if (!ids.has(id)) { node.remove(); frameEls.delete(id); }
    }
    let reordered = false;
    want.forEach((fr, i) => {
      let node = frameEls.get(fr.id);
      if (!node) { node = buildFrame(fr); frameEls.set(fr.id, node); }
      if (box.children[i] !== node) { box.insertBefore(node, box.children[i] || null); reordered = true; }
      place(node, fr.rect, pg);
      node.classList.toggle('warn', fr.flags.length > 0);
      node.classList.toggle('selected', fr.id === st.sel);
      node.querySelector('.tag').textContent = fr.label || '＋';
      const why = warnText(fr);
      node.setAttribute('aria-label', '枠 ' + (fr.label || '（新しい枠）') + (why ? '、確認してください: ' + why : ''));
    });
    if (reordered && focusedId && frameEls.has(focusedId)) frameEls.get(focusedId).focus({ preventScroll: true });
  }

  function renderMarks() {
    const pg = cur();
    const box = $('marks');
    box.textContent = '';
    if (!pg) return;
    for (const rect of pg.outside) {
      const mark = el('div', 'out-mark');
      place(mark, rect, pg);
      box.append(mark);
    }
  }

  function renderPanel() {
    const pg = cur();
    const list = $('frame-list');
    const at = document.activeElement;
    const keep = at && at.classList.contains('frame-item') ? at.dataset.id : null;
    list.textContent = '';
    if (!pg) { $('page-summary').textContent = ''; return; }
    const warns = pg.frames.filter((fr) => fr.flags.length).length;
    let text = '枠 ' + pg.frames.length + ' 個';
    if (warns) text += '、うち確認してほしい枠 ' + warns + ' 個';
    if (pg.outside_count) text += '。どの枠にも入っていない文字があります（青い点線）';
    setText($('page-summary'), text);
    for (const fr of pg.frames) {
      const li = el('li');
      const btn = el('button', 'frame-item' + (fr.flags.length ? ' warn' : ''));
      btn.type = 'button';
      btn.dataset.id = fr.id;
      if (fr.id === st.sel) btn.setAttribute('aria-current', 'true');
      btn.append(el('span', 'mark', fr.label || '＋'));
      btn.append(fr.flags.length ? el('span', 'why', warnText(fr)) : el('span', 'fine', '問題なし'));
      btn.setAttribute('aria-label', '枠 ' + (fr.label || '＋') + '、' + (fr.flags.length ? warnText(fr) : '問題なし'));
      li.append(btn);
      list.append(li);
      if (keep === fr.id) btn.focus({ preventScroll: true });
    }
    const msg = $('sheet-msg');
    if (!pg.frames.length && !$('sheet').classList.contains('loading')) {
      msg.textContent = 'このページには枠がありません。「枠を追加」で名刺を囲んでください。';
      msg.hidden = false;
    } else if (!$('sheet').classList.contains('loading')) {
      msg.hidden = true;
    }
  }

  function renderControls() {
    const pg = cur();
    const h = pg ? history() : { undo: [], redo: [] };
    const has = Boolean(pg && frameById(st.sel));
    const lock = st.busy || !pg;
    $('undo').disabled = lock || !h.undo.length;
    $('redo').disabled = lock || !h.redo.length;
    $('add-frame').disabled = lock;
    $('add-frame').setAttribute('aria-pressed', String(st.draw));
    const sel = has ? frameById(st.sel).rect : [0, 0, 0, 0];
    $('split-lr').disabled = lock || !has || sel[2] - sel[0] < MIN_SIDE * 2 + 2;
    $('split-tb').disabled = lock || !has || sel[3] - sel[1] < MIN_SIDE * 2 + 2;
    $('delete-frame').disabled = lock || !has;
    $('reset-page').disabled = lock || !pg.edited;
    $('draw-layer').hidden = !st.draw;
    $('draw-hint').hidden = !st.draw;
    $('finish').disabled = st.busy;
    $('cancel').disabled = st.busy;
    const order = [];
    eachPage((_pg, f, p) => order.push(key(f, p)));
    const at = order.indexOf(key(st.f, st.p));
    $('prev-page').disabled = at <= 0;
    $('next-page').disabled = at < 0 || at >= order.length - 1;
    $('next-warn').disabled = !nextWarnPage();
    keepFocus();
  }

  // 押したボタンが使えなくなった時（取り消す操作が無くなった等）に、フォーカスを見失わない
  function keepFocus() {
    const at = document.activeElement;
    const nowhere = !at || at === document.body;
    const lost = (at && at.disabled) || (nowhere && lastFocus && (lastFocus.disabled || !lastFocus.isConnected));
    if (!lost || dialogOpen() || st.ended || !cur()) return;
    $('sheet-area').focus({ preventScroll: true });
  }

  function totals() {
    const t = { files: 0, pages: 0, frames: 0, warns: 0, empty: 0, unviewed: 0, outside: 0, unreadable: 0 };
    t.noFrames = 0;
    st.files.forEach((file) => {
      if (FILE_NOTE[file.status]) { t.unreadable += 1; return; }
      t.files += 1;
      if (!file.pages.some((pg) => reviewable(pg) && pg.frames.length)) t.noFrames += 1;
    });
    eachPage((pg) => {
      t.pages += 1;
      t.frames += pg.frames.length;
      t.warns += pg.frames.filter((fr) => fr.flags.length).length;
      t.empty += pg.frames.filter((fr) => fr.flags.includes('REGION_NO_TEXT')).length;
      if (!pg.viewed) t.unviewed += 1;
      if (pg.outside_count) t.outside += 1;
    });
    return t;
  }

  function renderTotals() {
    const t = totals();
    const parts = ['PDF ' + t.files + ' 本', '枠 ' + t.frames + ' 個'];
    parts.push('確認してほしい枠 ' + t.warns + ' 個');
    if (t.unviewed) parts.push('まだ見ていないページ ' + t.unviewed);
    if (t.outside) parts.push('枠の外に文字があるページ ' + t.outside);
    if (t.unreadable) parts.push('読めない PDF ' + t.unreadable + ' 本');
    setText($('totals'), parts.join(' ／ '));
  }

  function renderSaveState() {
    let text = '';
    if (failed.size) text = '保存できていません';
    else if (unsaved.size) text = '保存しています';
    else if (st.files.some((file) => file.pages.some((pg) => pg.edited))) text = '修正を保存しました';
    setText($('save-state'), text);
  }

  function renderWhere() {
    const pg = cur();
    setText($('where'), pg ? st.files[st.f].name + '　' + pg.no + ' ページ目' : '');
  }

  function renderAll() {
    renderList();
    renderWhere();
    syncFrames();
    renderMarks();
    renderPanel();
    renderControls();
    renderTotals();
    renderSaveState();
  }

  function select(id, focus) {
    st.sel = id;
    syncFrames();
    renderPanel();
    renderControls();
    if (focus && id && frameEls.has(id)) frameEls.get(id).focus({ preventScroll: false });
  }

  // ---------------------------------------------------------------- ページの切り替え

  function openPage(f, p) {
    const pg = page(f, p);
    if (!pg || !reviewable(pg)) return;
    st.f = f;
    st.p = p;
    st.sel = null;
    st.draw = false;
    for (const node of frameEls.values()) node.remove();
    frameEls.clear();
    const sheet = $('sheet');
    sheet.classList.add('loading');
    const msg = $('sheet-msg');
    msg.textContent = 'ページを読み込んでいます';
    msg.hidden = false;
    loadImage(f, p, pg);
    layoutSheet();
    if (!pg.viewed) {
      pg.viewed = true;
      api('viewed/' + f + '/' + p, {}).catch(() => showNotice('ツールとの接続が切れました。黒い画面を閉じていないか確かめてください。'));
    }
    renderAll();
    $('sheet-area').scrollTo(0, 0);
    announce(st.files[f].name + ' の ' + pg.no + ' ページ目を表示しました。枠は ' + pg.frames.length + ' 個です。');
  }

  // ページの画像は <img> ではなく背景として描く。読み込みの成否は見えない Image で確かめる
  function loadImage(f, p, pg) {
    const view = $('page-img');
    const url = 'api/image/' + f + '/' + p;
    imageSeq += 1;
    const seq = imageSeq;
    view.style.backgroundImage = 'none';
    view.setAttribute('aria-label', st.files[f].name + ' の ' + pg.no + ' ページ目のスキャン画像');
    const probe = new Image();
    probe.onload = () => {
      if (seq !== imageSeq) return;
      view.style.backgroundImage = 'url("' + url + '")';
      $('sheet').classList.remove('loading');
      $('sheet-msg').hidden = true;
      renderPanel();
    };
    probe.onerror = () => {
      if (seq !== imageSeq) return;
      $('sheet').classList.remove('loading');
      const msg = $('sheet-msg');
      msg.textContent = 'ページの画像を表示できませんでした。黒い画面を閉じていないか確かめ、左の一覧からこのページを開き直してください。';
      msg.hidden = false;
      announce(msg.textContent);
    };
    probe.src = url;
  }

  function nextWarnPage() {
    const order = [];
    eachPage((pg, f, p) => order.push({ pg, f, p }));
    const at = order.findIndex((o) => o.f === st.f && o.p === st.p);
    const needs = (o) => o.pg.frames.some((fr) => fr.flags.length) || o.pg.outside_count > 0;
    for (let i = 1; i <= order.length; i += 1) {
      const o = order[(at + i) % order.length];
      if (o && needs(o) && !(o.f === st.f && o.p === st.p)) return o;
    }
    return null;
  }

  function stepPage(delta) {
    const order = [];
    eachPage((_pg, f, p) => order.push([f, p]));
    const at = order.findIndex(([f, p]) => f === st.f && p === st.p);
    const next = order[at + delta];
    if (next) openPage(next[0], next[1]);
  }

  // ---------------------------------------------------------------- 保存

  function scheduleSave(f, p, delay) {
    const k = key(f, p);
    unsaved.add(k);
    clearTimeout(timers.get(k));
    timers.set(k, setTimeout(() => { timers.delete(k); enqueue(f, p); }, delay === undefined ? SAVE_DELAY : delay));
    renderSaveState();
  }

  // 同じページの送信は前の送信が終わってから行う（順番が入れ替わらないようにする）
  function enqueue(f, p, task) {
    const k = key(f, p);
    const run = () => (task ? task() : sendFrames(f, p));
    const next = (chains.get(k) || Promise.resolve()).then(run, run);
    chains.set(k, next);
    return next;
  }

  async function sendFrames(f, p) {
    const k = key(f, p);
    const pg = page(f, p);
    if (dragging(f, p)) { scheduleSave(f, p); return; }   // ドラッグの途中の枠は送らない。離した後に送る
    const rev = pg.rev;
    try {
      const body = { rev, frames: pg.frames.map((fr) => ({ id: fr.id, rect: fr.rect.map(round2) })) };
      const res = await api('frames/' + f + '/' + p, body);
      failed.delete(k);
      if (pg.rev === rev) {
        if (!timers.has(k)) unsaved.delete(k);
        applyServerPage(f, p, res);
      }
      if (!failed.size) hideNotice();
    } catch (err) {
      if (err.status === 409) {
        failed.delete(k);
        await resync(f, p, rev, false);
      } else {
        failed.add(k);
        showNotice('修正を保存できませんでした。黒い画面を閉じていないか確かめ、「もう一度つなぐ」を押してください。');
      }
    }
    renderSaveState();
  }

  // 出力が済んでいる・出力の途中だと分かった時は、そちらの表示へ移る
  function handleEnded(state) {
    if (state.summary) { showDone(state.summary); return true; }
    if (state.state === 'finishing') { waitForFinish(); return true; }
    return false;
  }

  // 送った番号がツール側の番号以下で断られた時（409）。
  // ツール側の内容が画面と同じなら、送信が二重に届いただけなので、ラベルや旗を取り込む。
  // 違う内容なら（再読み込みの直前の送信や、別のタブの修正が先に届いていた）、画面に出ている枠を正として
  // 新しい番号で送り直す。takeTool: 「自動の枠に戻す」の時は、ツール側の内容を採る
  async function resync(f, p, rev, takeTool) {
    const k = key(f, p);
    try {
      const state = await api('state');
      if (handleEnded(state)) return;
      const pg = page(f, p);
      const fresh = state.files[f].pages[p];
      if (pg.rev !== rev) return;   // 待っている間に利用者がさらに直した。その送信に任せる
      if (takeTool || sameFrames(pg.frames, fresh.frames)) {
        if (!timers.has(k)) unsaved.delete(k);
        applyServerPage(f, p, { page: fresh, labels: labelsOf(state.files[f]) });
      } else {
        pg.rev = Math.max(pg.rev, fresh.rev) + 1;
        scheduleSave(f, p, 0);
      }
    } catch (err) {
      failed.add(k);
      showNotice('ツールとの接続が切れました。黒い画面を閉じていないか確かめ、「もう一度つなぐ」を押してください。');
    }
  }

  function applyServerPage(f, p, res) {
    const pg = page(f, p);
    if (dragging(f, p)) {
      // 掴んでいる枠を応答で差し替えない。離した後に取り直す
      drag.stale = true;
      return;
    }
    pg.frames = res.page.frames;
    pg.outside = res.page.outside;
    pg.outside_count = res.page.outside_count;
    pg.edited = res.page.edited;
    pg.rev = Math.max(pg.rev, res.page.rev);
    // ラベルは PDF 内の通し番号なので、同じ PDF の他のページも付け直す
    for (const [index, mapping] of Object.entries(res.labels)) {
      const other = page(f, Number(index));
      if (!other) continue;
      for (const fr of other.frames) {
        if (mapping[fr.id] !== undefined) fr.label = mapping[fr.id];
      }
    }
    if (f === st.f && p === st.p) {
      if (st.sel && !frameById(st.sel)) st.sel = null;
      renderAll();
    } else {
      renderList();
      renderTotals();
    }
  }

  async function flush() {
    const jobs = [];
    for (const k of Array.from(new Set([...timers.keys(), ...failed]))) {
      const [f, p] = k.split('/').map(Number);
      clearTimeout(timers.get(k));
      timers.delete(k);
      jobs.push(enqueue(f, p));
    }
    await Promise.all(jobs);
    await Promise.all(Array.from(chains.values()));
    return failed.size === 0;
  }

  // ---------------------------------------------------------------- 編集

  // before: 変更前の枠の控え。groupId を渡すと、続けて行った同じ操作を 1 回の取り消しにまとめる
  function commit(before, groupId) {
    const pg = cur();
    const h = history();
    const now = Date.now();
    if (!(groupId && h.groupId === groupId && now - h.groupAt < NUDGE_GROUP)) {
      h.undo.push(before);
      if (h.undo.length > 100) h.undo.shift();
    }
    h.groupId = groupId || null;
    h.groupAt = now;
    h.redo.length = 0;
    pg.edited = true;
    pg.rev += 1;
    scheduleSave(st.f, st.p);
    renderAll();
  }

  function restore(frames) {
    const pg = cur();
    pg.frames = frames.map((fr) => ({ id: fr.id, label: fr.label, rect: fr.rect.slice(), flags: fr.flags.slice() }));
    pg.edited = true;
    pg.rev += 1;
    if (st.sel && !frameById(st.sel)) st.sel = null;
    history().groupId = null;
    scheduleSave(st.f, st.p);
    renderAll();
  }

  function undo() {
    const pg = cur();
    if (!pg || st.busy) return;
    const h = history();
    if (!h.undo.length) return;
    h.redo.push(snapshot(pg));
    restore(h.undo.pop());
  }

  function redo() {
    const pg = cur();
    if (!pg || st.busy) return;
    const h = history();
    if (!h.redo.length) return;
    h.undo.push(snapshot(pg));
    restore(h.redo.pop());
  }

  function adjust(rect, dir, dx, dy, pg) {
    let [x0, y0, x1, y1] = rect;
    if (dir === 'move') {
      const w = x1 - x0;
      const h = y1 - y0;
      x0 = clamp(x0 + dx, 0, pg.width - w);
      y0 = clamp(y0 + dy, 0, pg.height - h);
      return [x0, y0, x0 + w, y0 + h];
    }
    if (dir.includes('w')) x0 = clamp(x0 + dx, 0, x1 - MIN_SIDE);
    if (dir.includes('e')) x1 = clamp(x1 + dx, x0 + MIN_SIDE, pg.width);
    if (dir.includes('n')) y0 = clamp(y0 + dy, 0, y1 - MIN_SIDE);
    if (dir.includes('s')) y1 = clamp(y1 + dy, y0 + MIN_SIDE, pg.height);
    return [x0, y0, x1, y1];
  }

  function typicalSize(pg) {
    if (!pg.frames.length) return CARD_SIZE;
    const mid = (values) => values.sort((a, b) => a - b)[Math.floor(values.length / 2)];
    return [mid(pg.frames.map((fr) => fr.rect[2] - fr.rect[0])), mid(pg.frames.map((fr) => fr.rect[3] - fr.rect[1]))];
  }

  function addFrame(rect) {
    const pg = cur();
    const before = snapshot(pg);
    const fr = { id: newId(), label: '', rect, flags: [] };
    pg.frames.push(fr);
    st.sel = fr.id;
    st.draw = false;
    commit(before);
    frameEls.get(fr.id).focus({ preventScroll: true });
  }

  function addDefaultFrame(cx, cy) {
    const pg = cur();
    const [w, h] = typicalSize(pg).map((v, i) => Math.min(v, i === 0 ? pg.width : pg.height));
    const x0 = clamp(cx - w / 2, 0, pg.width - w);
    const y0 = clamp(cy - h / 2, 0, pg.height - h);
    addFrame([x0, y0, x0 + w, y0 + h]);
  }

  function viewCenter(pg) {
    const area = $('sheet-area').getBoundingClientRect();
    const sheet = $('sheet').getBoundingClientRect();
    const cx = clamp((area.left + area.width / 2 - sheet.left) / sheet.width, 0.1, 0.9) * pg.width;
    const cy = clamp((area.top + area.height / 2 - sheet.top) / sheet.height, 0.1, 0.9) * pg.height;
    return [cx, cy];
  }

  function deleteSelected() {
    const pg = cur();
    const at = pg ? pg.frames.findIndex((fr) => fr.id === st.sel) : -1;
    if (at < 0 || st.busy) return;
    const before = snapshot(pg);
    pg.frames.splice(at, 1);
    const next = pg.frames[Math.min(at, pg.frames.length - 1)];
    st.sel = next ? next.id : null;
    commit(before);
    if (next) frameEls.get(next.id).focus({ preventScroll: true });
    else $('add-frame').focus();
  }

  function splitSelected(axis) {
    const pg = cur();
    const at = pg ? pg.frames.findIndex((fr) => fr.id === st.sel) : -1;
    if (at < 0 || st.busy) return;
    const [x0, y0, x1, y1] = pg.frames[at].rect;
    const gap = 1;
    let a;
    let b;
    if (axis === 'lr') {
      if (x1 - x0 < MIN_SIDE * 2 + gap * 2) return;
      const mid = (x0 + x1) / 2;
      a = [x0, y0, mid - gap, y1];
      b = [mid + gap, y0, x1, y1];
    } else {
      if (y1 - y0 < MIN_SIDE * 2 + gap * 2) return;
      const mid = (y0 + y1) / 2;
      a = [x0, y0, x1, mid - gap];
      b = [x0, mid + gap, x1, y1];
    }
    const before = snapshot(pg);
    const first = { id: newId(), label: '', rect: a, flags: [] };
    pg.frames.splice(at, 1, first, { id: newId(), label: '', rect: b, flags: [] });
    st.sel = first.id;
    commit(before);
    frameEls.get(first.id).focus({ preventScroll: true });
  }

  async function resetPage() {
    const pg = cur();
    if (!pg || st.busy) return;
    const f = st.f;
    const p = st.p;
    const k = key(f, p);
    const before = snapshot(pg);
    clearTimeout(timers.get(k));
    timers.delete(k);
    pg.rev += 1;
    const rev = pg.rev;
    unsaved.add(k);
    renderSaveState();
    await enqueue(f, p, async () => {
      try {
        const res = await api('reset/' + f + '/' + p, { rev });
        failed.delete(k);
        if (pg.rev === rev) {
          unsaved.delete(k);
          const h = historyOf(k);
          h.undo.push(before);
          h.redo.length = 0;
          h.groupId = null;
          if (f === st.f && p === st.p) st.sel = null;
          applyServerPage(f, p, res);
        }
      } catch (err) {
        if (err.status === 409) {
          failed.delete(k);
          await resync(f, p, rev, true);
        } else {
          // 「もう一度つなぐ」で、画面に出ている枠を送り直す（戻す操作は取り消された扱いになる）
          failed.add(k);
          showNotice('自動の枠に戻せませんでした。黒い画面を閉じていないか確かめ、「もう一度つなぐ」を押してから、もう一度お試しください。');
        }
      }
    });
    renderSaveState();
  }

  function setDraw(on) {
    if (st.busy || !cur()) return;
    st.draw = on;
    renderControls();
  }

  // ---------------------------------------------------------------- マウス操作

  const ptPerPx = () => cur().width / $('sheet').getBoundingClientRect().width;

  function onFramesDown(e) {
    if (e.button !== 0 || st.draw || st.busy) return;
    const node = e.target.closest('.frame');
    if (!node) { select(null); return; }
    const fr = frameById(node.dataset.id);
    if (!fr) return;
    const pg = cur();
    drag = {
      id: fr.id, dir: e.target.dataset.dir || 'move', x: e.clientX, y: e.clientY,
      start: fr.rect.slice(), before: snapshot(pg), moved: false, stale: false,
      scale: ptPerPx(), pointer: e.pointerId, node,
    };
    node.setPointerCapture(e.pointerId);
    e.preventDefault();
    select(fr.id);
    node.focus({ preventScroll: true });
  }

  function onFramesMove(e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    const pg = cur();
    const fr = frameById(drag.id);
    if (!fr) return;
    const dx = (e.clientX - drag.x) * drag.scale;
    const dy = (e.clientY - drag.y) * drag.scale;
    if (!drag.moved && Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 3) return;
    drag.moved = true;
    fr.rect = adjust(drag.start, drag.dir, dx, dy, pg);
    place(drag.node, fr.rect, pg);
  }

  function onFramesUp(e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    const done = drag;
    drag = null;
    if (done.node.isConnected && done.node.hasPointerCapture(e.pointerId)) done.node.releasePointerCapture(e.pointerId);
    if (done.moved) commit(done.before);
    else if (done.stale) scheduleSave(st.f, st.p, 0);   // 掴んでいる間に見送った応答を取り直す
  }

  function sheetPoint(e) {
    const pg = cur();
    const box = $('sheet').getBoundingClientRect();
    return [
      clamp((e.clientX - box.left) / box.width, 0, 1) * pg.width,
      clamp((e.clientY - box.top) / box.height, 0, 1) * pg.height,
    ];
  }

  function onDrawDown(e) {
    if (e.button !== 0 || st.busy) return;
    const [x, y] = sheetPoint(e);
    rubber = { x, y, pointer: e.pointerId };
    $('draw-layer').setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function onDrawMove(e) {
    if (!rubber || e.pointerId !== rubber.pointer) return;
    const [x, y] = sheetPoint(e);
    const box = $('rubber');
    box.hidden = false;
    place(box, [Math.min(rubber.x, x), Math.min(rubber.y, y), Math.max(rubber.x, x), Math.max(rubber.y, y)], cur());
  }

  function onDrawUp(e) {
    if (!rubber || e.pointerId !== rubber.pointer) return;
    const [x, y] = sheetPoint(e);
    const start = rubber;
    rubber = null;
    $('rubber').hidden = true;
    const rect = [Math.min(start.x, x), Math.min(start.y, y), Math.max(start.x, x), Math.max(start.y, y)];
    if (rect[2] - rect[0] >= MIN_SIDE && rect[3] - rect[1] >= MIN_SIDE) addFrame(rect);
    else addDefaultFrame(x, y);   // ドラッグせずに押した時は、その位置に標準の大きさで置く
  }

  // ---------------------------------------------------------------- キーボード操作

  const ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

  function onFrameKey(e) {
    const node = e.target.closest('.frame');
    if (!node || st.busy || st.draw) return;
    const fr = frameById(node.dataset.id);
    if (!fr) return;
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      st.sel = fr.id;
      deleteSelected();
      return;
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      select(fr.id);
      return;
    }
    const move = ARROWS[e.key];
    if (!move || e.altKey || e.metaKey) return;
    e.preventDefault();
    const pg = cur();
    const step = e.shiftKey ? 10 : 1;
    const before = snapshot(pg);
    const dir = e.ctrlKey ? (move[0] ? 'e' : 's') : 'move';
    const next = adjust(fr.rect, dir, move[0] * step, move[1] * step, pg);
    if (next.every((v, i) => v === fr.rect[i])) return;
    fr.rect = next;
    st.sel = fr.id;
    commit(before, fr.id + ':' + dir);
  }

  function onGlobalKey(e) {
    if (dialogOpen() || st.ended) return;
    const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement;
    if (typing) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && !e.altKey && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
    } else if (mod && !e.altKey && e.key.toLowerCase() === 'y') {
      e.preventDefault();
      redo();
    } else if (!mod && !e.altKey && (e.key.toLowerCase() === 'n' || e.code === 'KeyN') && $('sheet-area').contains(e.target)) {
      e.preventDefault();
      setDraw(true);
      $('add-frame').focus();
    } else if (e.key === 'Escape') {
      if (st.draw) { rubber = null; $('rubber').hidden = true; setDraw(false); }
      else if (st.sel) select(null);
    } else if (e.key === 'Enter' && st.draw && e.target === $('add-frame')) {
      e.preventDefault();
      const [cx, cy] = viewCenter(cur());
      addDefaultFrame(cx, cy);
    }
  }

  // ---------------------------------------------------------------- お知らせ・出力・中止

  // sticky: 利用者が次の操作をするまで残す（保存の成功などで自動的に消さない）
  function showNotice(text, sticky) {
    stickyNotice = Boolean(sticky);
    $('notice-text').textContent = text;
    if ($('notice').hidden) { $('notice').hidden = false; layoutSheet(); }
  }

  // force: 利用者の操作で消す時
  function hideNotice(force) {
    if ($('notice').hidden || (stickyNotice && !force)) return;
    const inside = $('notice').contains(document.activeElement);
    stickyNotice = false;
    $('notice').hidden = true;
    layoutSheet();
    if (inside && !st.ended) $('sheet-area').focus({ preventScroll: true });
  }

  async function reconnect() {
    try {
      if (!st.files.length) {   // 起動の時につながらなかった。最初から読み直す
        hideNotice(true);
        await load();
        return;
      }
      if (handleEnded(await api('state'))) return;
      const ok = await flush();
      if (ok) hideNotice(true);
    } catch (err) {
      showNotice('まだつながりません。黒い画面（run.bat）を閉じてしまった時は、もう一度 run.bat を実行してください。');
    }
    renderSaveState();
  }

  function openConfirm() {
    hideNotice(true);
    const t = totals();
    $('confirm-text').textContent = t.frames
      ? 'PDF ' + (t.files - t.noFrames) + ' 本を、枠 ' + t.frames + ' 個（名刺 ' + t.frames + ' 枚ぶん）で一覧表にします。'
        + '出力した後は、この画面で枠を直せません。'
      : '一覧表にできる枠がありません。出力しても一覧表は作られません。';
    const list = $('confirm-warnings');
    list.textContent = '';
    if (t.noFrames) list.append(el('li', '', '枠が 1 つも無い PDF が ' + t.noFrames + ' 本あります。その PDF の一覧表は作られず、PDF は done フォルダへ移ります。'));
    if (t.unviewed) list.append(el('li', '', 'まだ見ていないページが ' + t.unviewed + ' ページあります。'));
    if (t.outside) list.append(el('li', '', 'どの枠にも入っていない文字が ' + t.outside + ' ページにあります。その文字は一覧表に入りません。'));
    if (t.warns) list.append(el('li', '', '確認してほしい枠が ' + t.warns + ' 個残っています。一覧表の「要確認」列に印が付きます。'));
    if (t.empty) list.append(el('li', '', '文字の無い枠が ' + t.empty + ' 個あります。一覧表には中身の無い行として入ります。'));
    if (t.unreadable) list.append(el('li', '', '読めない PDF ' + t.unreadable + ' 本は error フォルダへ移ります。'));
    $('confirm').showModal();
  }

  function setBusy(on, text) {
    st.busy = on;
    if (on) { st.draw = false; }
    renderControls();
    for (const part of document.querySelectorAll('.bar, .work, .finish')) part.inert = on;
    $('busy').hidden = !on;
    $('busy-text').textContent = on ? text : '';
    if (on) {
      $('busy-text').focus({ preventScroll: true });
      announce(text.replace(/\n/g, ' '));
    } else if (!st.ended) {
      $('finish').focus({ preventScroll: true });
    }
  }

  // 出力の途中で画面を開き直した時。終わるまで待って結果を出す
  async function waitForFinish() {
    if (st.waiting) return;
    st.waiting = true;
    setBusy(true, BUSY_FINISH);
    for (;;) {
      await new Promise((resolve) => { window.setTimeout(resolve, 1000); });
      try {
        const state = await api('state');
        if (state.summary) { showDone(state.summary); return; }
      } catch (err) {
        // 出力が済むとツールは終わる。結果を受け取る前に終わっていたら、黒い画面を見てもらう
        showEnd('出力は終わりました', '結果は、黒い画面と output フォルダで確かめてください。');
        return;
      }
    }
  }

  // 出力しようとしたら「保存が終わっていない」と断られた時。ツール側と番号が合わないページを送り直す
  async function recoverUnsaved() {
    try {
      const state = await api('state');
      if (state.summary) { showDone(state.summary); return; }
      if (state.state === 'finishing') { waitForFinish(); return; }
      eachPage((pg, f, p) => {
        const fresh = state.files[f].pages[p];
        if (fresh.rev === pg.rev) return;
        pg.rev = Math.max(pg.rev, fresh.rev) + 1;
        scheduleSave(f, p, 0);
      });
      showNotice('保存が終わっていない修正がありました。保存し直したので、もう一度「出力する」を押してください。', true);
    } catch (err) {
      showNotice('出力できませんでした。黒い画面を閉じていないか確かめ、「もう一度つなぐ」を押してください。');
    }
  }

  async function finish() {
    $('confirm').close();
    setBusy(true, BUSY_FINISH);
    const ok = await flush();
    if (!ok) {
      setBusy(false);
      showNotice('保存できていない修正があるため、出力していません。「もう一度つなぐ」を押してからやり直してください。');
      renderSaveState();
      return;
    }
    const revs = {};
    eachPage((pg, f, p) => { revs[key(f, p)] = pg.rev; });
    try {
      showDone(await api('finish', { revs }));
    } catch (err) {
      setBusy(false);
      if (err.status === 409) await recoverUnsaved();
      else showNotice('出力できませんでした。黒い画面を閉じていないか確かめてください。出力が済んでいる時は、黒い画面に結果が出ています。');
      renderSaveState();
    }
  }

  async function cancel() {
    $('cancel-confirm').close();
    setBusy(true, '中止しています。');
    try {
      showDone(await api('cancel', {}));
    } catch (err) {
      setBusy(false);
      showNotice('中止を伝えられませんでした。黒い画面（run.bat）を閉じると中止できます。');
      renderSaveState();
    }
  }

  // 表の無い終わりの画面（中止した時・結果を受け取れなかった時）
  function showEnd(title, lead) {
    st.ended = true;
    $('app').hidden = true;
    $('skip').hidden = true;
    $('done-table').hidden = true;
    $('done-title').textContent = title;
    $('done-lead').textContent = lead;
    $('done-foot').textContent = 'このタブは閉じてかまいません。';
    document.title = title + ' - 名刺の枠の確認';
    $('done').hidden = false;
    $('done').focus();
  }

  function showDone(sum) {
    if (sum.cancelled) {
      showEnd('中止しました', '何も出力していません。PDF は input フォルダに残っています。');
      return;
    }
    st.ended = true;
    $('app').hidden = true;
    $('skip').hidden = true;
    const done = $('done');
    done.hidden = false;
    const troubled = sum.files.filter((file) => ['input_changed', 'output_failed', 'move_failed'].includes(file.result)).length;
    let title = sum.rows ? '出力しました' : '終わりました';
    if (sum.rows && troubled) title = '出力しました（一部の PDF を除く）';
    let lead = sum.rows
      ? '名刺 ' + sum.rows + ' 枚を一覧表にしました（要確認 ' + sum.review_rows + ' 枚）。'
        + '一覧表の「要確認」列に 1 が付いた行は、元の名刺と見比べてください。'
      : '一覧表にする名刺はありませんでした。PDF ごとの結果は下の表のとおりです。';
    if (troubled) lead += ' うまくいかなかった PDF が ' + troubled + ' 本あります。下の表を確かめてください。';
    $('done-title').textContent = title;
    $('done-lead').textContent = lead;
    const rows = $('done-rows');
    rows.textContent = '';
    for (const file of sum.files) {
      const tr = el('tr');
      const head = el('th', '', file.name);
      head.scope = 'row';
      // 読めた PDF なのに一覧表が無いのは、枠が 1 つも無かった時
      const noTable = (file.result === 'ok' || file.result === 'partial') && !file.folder;
      tr.append(head, el('td', '', noTable ? '一覧表は作っていません（枠が 0 個）' : (RESULT_TEXT[file.result] || file.result)));
      tr.append(el('td', 'num', file.folder ? String(file.rows) : '―'), el('td', 'num', file.folder ? String(file.review_rows) : '―'));
      let where = 'input フォルダに残っています';
      if (file.folder) where = file.folder;
      else if (file.result === 'input_changed') where = 'input フォルダを確かめてください';
      else if (file.moved) where = file.moved + ' フォルダへ移しました';
      tr.append(el('td', '', where));
      rows.append(tr);
    }
    $('done-table').hidden = sum.files.length === 0;
    $('done-foot').textContent = 'このタブは閉じてかまいません。黒い画面にも同じ結果が出ています。';
    document.title = title + ' - 名刺の枠の確認';
    done.focus();
  }

  // ---------------------------------------------------------------- 起動

  function bind() {
    $('page-list').addEventListener('click', (e) => {
      const btn = e.target.closest('.page-btn');
      if (btn) openPage(Number(btn.dataset.f), Number(btn.dataset.p));
    });
    $('frame-list').addEventListener('click', (e) => {
      const btn = e.target.closest('.frame-item');
      if (btn) select(btn.dataset.id, true);
    });
    const frames = $('frames');
    frames.addEventListener('pointerdown', onFramesDown);
    frames.addEventListener('pointermove', onFramesMove);
    frames.addEventListener('pointerup', onFramesUp);
    frames.addEventListener('pointercancel', onFramesUp);
    frames.addEventListener('lostpointercapture', onFramesUp);   // 離した合図が届かなかった時もここで確定する
    frames.addEventListener('keydown', onFrameKey);
    frames.addEventListener('focusin', (e) => {
      const node = e.target.closest('.frame');
      if (node && node.dataset.id !== st.sel && !drag) select(node.dataset.id);
    });
    const layer = $('draw-layer');
    layer.addEventListener('pointerdown', onDrawDown);
    layer.addEventListener('pointermove', onDrawMove);
    layer.addEventListener('pointerup', onDrawUp);
    layer.addEventListener('pointercancel', () => { rubber = null; $('rubber').hidden = true; });
    document.addEventListener('keydown', onGlobalKey);
    document.addEventListener('focusin', (e) => { lastFocus = e.target; });

    $('undo').addEventListener('click', undo);
    $('redo').addEventListener('click', redo);
    $('add-frame').addEventListener('click', () => setDraw(!st.draw));
    $('split-lr').addEventListener('click', () => splitSelected('lr'));
    $('split-tb').addEventListener('click', () => splitSelected('tb'));
    $('delete-frame').addEventListener('click', deleteSelected);
    $('reset-page').addEventListener('click', resetPage);
    $('prev-page').addEventListener('click', () => stepPage(-1));
    $('next-page').addEventListener('click', () => stepPage(1));
    $('next-warn').addEventListener('click', () => { const o = nextWarnPage(); if (o) openPage(o.f, o.p); });

    const zoomTo = (zoom) => {
      st.zoom = zoom;
      layoutSheet();
      announce('表示の大きさ ' + $('zoom-value').textContent);
    };
    const zoomBy = (factor) => {
      const pg = cur();
      if (!pg) return;
      const now = $('sheet').getBoundingClientRect().width / (pg.width * 96 / 72);
      zoomTo(clamp(now * factor, 0.25, 4));
    };
    $('zoom-in').addEventListener('click', () => zoomBy(1.25));
    $('zoom-out').addEventListener('click', () => zoomBy(0.8));
    $('zoom-page').addEventListener('click', () => zoomTo('page'));
    $('zoom-width').addEventListener('click', () => zoomTo('width'));
    window.addEventListener('resize', layoutSheet);
    window.addEventListener('pagehide', () => {
      // 再読み込みやタブを閉じる直前の修正を取りこぼさない
      for (const k of Array.from(timers.keys())) {
        const [f, p] = k.split('/').map(Number);
        const pg = page(f, p);
        clearTimeout(timers.get(k));
        timers.delete(k);
        const body = { rev: pg.rev, frames: pg.frames.map((fr) => ({ id: fr.id, rect: fr.rect.map(round2) })) };
        api('frames/' + f + '/' + p, body, true).catch(() => {});
      }
    });

    $('notice-retry').addEventListener('click', reconnect);
    $('finish').addEventListener('click', openConfirm);
    $('confirm-back').addEventListener('click', () => $('confirm').close());
    $('confirm-ok').addEventListener('click', finish);
    $('cancel').addEventListener('click', () => $('cancel-confirm').showModal());
    $('cancel-back').addEventListener('click', () => $('cancel-confirm').close());
    $('cancel-ok').addEventListener('click', cancel);
  }

  async function load() {
    let state;
    try {
      state = await api('state');
    } catch (err) {
      showNotice('ツールにつながりません。黒い画面（run.bat）を閉じてしまった時は、もう一度 run.bat を実行してください。');
      return;
    }
    if (handleEnded(state)) return;
    st.files = state.files;
    window.setTimeout(refreshUntouched, 1500);
    let first = null;
    eachPage((pg, f, p) => { if (!first) first = [f, p]; });
    if (first) openPage(first[0], first[1]);
    else {
      renderAll();
      const msg = $('sheet-msg');
      msg.textContent = '枠を確認できるページがありません。「出力する」を押すと、読めない PDF を error フォルダへ移します。';
      msg.hidden = false;
    }
  }

  // 再読み込みの直前に送った修正が、最初の読み込みより後に届くことがある。
  // まだ触っていないページは、少し待ってからツール側の内容を取り込む
  async function refreshUntouched() {
    if (st.ended || st.busy) return;
    let state;
    try {
      state = await api('state');
    } catch (err) {
      return;
    }
    if (st.ended || st.busy || handleEnded(state)) return;
    eachPage((pg, f, p) => {
      const fresh = state.files[f] && state.files[f].pages[p];
      const k = key(f, p);
      if (!fresh || fresh.rev <= pg.rev || unsaved.has(k) || failed.has(k) || dragging(f, p)) return;
      applyServerPage(f, p, { page: fresh, labels: labelsOf(state.files[f]) });
    });
  }

  bind();
  load();
})();
