const CATEGORY_COLORS = {
  'テクノロジー': '#1E88E5',
  '海外IT': '#FB8C00',
  '科学': '#00ACC1',
  '国際': '#8E24AA',
  '環境': '#43A047',
  'ビジネス・経済': '#E53935',
};
const CATEGORY_EMOJI = {
  'テクノロジー': '💻',
  '海外IT': '🌐',
  '科学': '🔬',
  '国際': '🌍',
  '環境': '🌱',
  'ビジネス・経済': '💴',
};
// カテゴリごとの出典表示
const CATEGORY_SOURCE = {
  'テクノロジー': 'The Guardian',
  '海外IT': 'via Hacker News',
  '科学': 'The Guardian',
  '国際': 'The Guardian',
  '環境': 'The Guardian',
  'ビジネス・経済': 'The Guardian',
};
// テクノロジーを先頭にする並び順
const CATEGORY_ORDER = Object.keys(CATEGORY_COLORS);

const dateSelect = document.getElementById('dateSelect');
const feed = document.getElementById('feed');
const hint = document.getElementById('hint');
const tabs = document.getElementById('tabs');

// ---------- 既読管理（localStorage：この端末・ブラウザ内だけの記録） ----------
const READ_KEY = 'readUrls';
const READ_RETENTION_DAYS = 40;  // 記事データ(30日)より少し長く残して掃除
const SAVED_KEY = 'savedArticles';
// タブの並び（横スワイプはこの順に移動する）
const TAB_ORDER = ['unread', 'read', 'saved'];

// Guardian Open Platform 規約：Guardian由来のコンテンツは取得から24時間を超えて保持しない。
// 「後で見る」で保存した記事も例外ではないので、保存済みでも24時間で自動削除する。
const GUARDIAN_HOST = 'theguardian.com';
const GUARDIAN_SAVE_TTL = 24 * 60 * 60 * 1000;
function isGuardian(url) {
  return (url || '').includes(GUARDIAN_HOST);
}

function loadRead() {
  let map;
  try {
    map = JSON.parse(localStorage.getItem(READ_KEY)) || {};
  } catch (e) {
    map = {};
  }
  const cutoff = Date.now() - READ_RETENTION_DAYS * 24 * 60 * 60 * 1000;
  for (const url of Object.keys(map)) {
    if (map[url] < cutoff) delete map[url];
  }
  return map;
}

function saveRead() {
  try {
    localStorage.setItem(READ_KEY, JSON.stringify(readMap));
  } catch (e) { /* 保存できなくても表示は継続 */ }
}

// ---------- 「後で見る」保存（localStorage：この端末・ブラウザ内だけの記録） ----------
// 記事データは30日で消えるが、保存した記事は本文ごと丸ごと控えるので、消えた後も読める。
// 保存自体は期限で消さない（自分でしおりを外すまで残る）。
function loadSaved() {
  try {
    return JSON.parse(localStorage.getItem(SAVED_KEY)) || {};
  } catch (e) {
    return {};
  }
}

function persistSaved() {
  try {
    localStorage.setItem(SAVED_KEY, JSON.stringify(savedMap));
  } catch (e) {
    console.warn('保存できませんでした', e);
  }
}

let savedMap = loadSaved();   // { url: { a: 記事まるごと, t: 保存した時刻, d: 保存時に見ていた日付 } }

// 保存したGuardian記事のうち、取得から24時間を超えたものを削除する（規約順守）。
// 判定は記事の取得時刻(collected_at)基準。無ければ保存時刻でフォールバック。
function purgeExpiredSaved() {
  const now = Date.now();
  let changed = false;
  for (const url of Object.keys(savedMap)) {
    if (!isGuardian(url)) continue;
    const e = savedMap[url];
    const base = (e.a && e.a.collected_at) ? Date.parse(e.a.collected_at) : (e.t || 0);
    if (now - base > GUARDIAN_SAVE_TTL) {
      delete savedMap[url];
      changed = true;
    }
  }
  if (changed) persistSaved();
}
purgeExpiredSaved();   // 起動時に掃除

function findArticle(url) {
  return currentArticles.find((a) => a.url === url)
      || (savedMap[url] && savedMap[url].a)
      || null;
}

function toggleSaved(url) {
  if (savedMap[url]) {
    delete savedMap[url];
  } else {
    const a = findArticle(url);
    if (!a) return;
    // 記事は日付を持たない（日付は日ごとのファイル側）ので、保存する瞬間に見ている日付を控える。
    savedMap[url] = { a: Object.assign({}, a), t: Date.now(), d: dateSelect.value };
  }
  persistSaved();
  updateTabCounts();
  if (currentTab === 'saved') {
    render();
  } else {
    refreshSaveButtons();
  }
}

function refreshSaveButtons() {
  for (const btn of feed.querySelectorAll('.save-btn')) {
    const on = !!savedMap[btn.dataset.save];
    btn.classList.toggle('saved', on);
    btn.setAttribute('aria-pressed', String(on));
  }
}

let readMap = loadRead();
// 案A：タブの振り分けは「その日を開いた時点」で固定する（読んでも記事が抜けず位置を見失わない）。
let tabSnapshot = {};       // { url: true } ＝ この日を開いた時点で既読だった記事
let manifest = {};          // 日付→[{u:短縮url, c:エポック秒}]（バッジ計算用の軽量一覧・index.json由来）
let urlPre = '';            // マニフェストで省いたURLの接頭辞
let urlSuf = '';            // 同・末尾
let dayCache = {};          // 開いた日の中身をキャッシュして再取得を避ける
let currentArticles = [];   // 表示中の日の全記事（カテゴリ順ソート済み）
let currentTab = 'unread';  // 'unread' | 'read' | 'saved'

// 「読んだ後に内容が更新された（＝翻訳・要約が改善された）記事」は未読に戻す。
// url と時刻(ms)だけで判定できるので、軽量マニフェストでも記事本体でも使える。
function isReadAt(url, contentMs) {
  const t = readMap[url];
  if (!t) return false;                      // まだ読んでいない＝未読
  return contentMs <= t;                     // 読んだ後に更新されていれば未読扱い
}
function isRead(a) {
  return isReadAt(a.url, a.content_at ? Date.parse(a.content_at) : 0);
}
function isUpdatedSinceRead(a) {
  const t = readMap[a.url];
  const c = a.content_at ? Date.parse(a.content_at) : 0;
  return t && c > t;                         // 一度読んだが、その後に改善された
}

function markRead(url) {
  if (!url) return;
  readMap[url] = Date.now();                 // 常に最新の読了時刻に更新（案A：カードは動かさない）
  saveRead();
  updateTabCounts();
  // 未読タブに残したカードへ「既読」印をその場で付ける（読まれたことが見て分かるように）
  if (currentTab !== 'unread') return;
  const card = feed.querySelector(`.card[data-url="${CSS.escape(url)}"]`);
  if (!card || card.querySelector('.read-mark')) return;
  const badges = card.querySelector('.badges');
  if (!badges) return;
  const mark = document.createElement('span');
  mark.className = 'read-mark';
  mark.textContent = '既読';
  badges.insertBefore(mark, badges.querySelector('.save-btn'));
}

// カードが画面の6割以上・1秒以上表示されたら既読にする
const observer = new IntersectionObserver((entries) => {
  for (const en of entries) {
    const el = en.target;
    if (en.isIntersecting) {
      el._readTimer = setTimeout(() => markRead(el.dataset.url), 1000);
    } else if (el._readTimer) {
      clearTimeout(el._readTimer);
      el._readTimer = null;
    }
  }
}, { root: feed, threshold: 0.6 });

// ---------- タブ（記事 | 既読 | 保存）：タップ or 横スワイプで切替 ----------
function setTab(tab) {
  if (tab === currentTab) return;
  const anchor = currentAnchorUrl();   // 案C：移動前に「今見ている記事」を覚える
  currentTab = tab;
  for (const b of tabs.querySelectorAll('button')) {
    b.classList.toggle('active', b.dataset.tab === tab);
  }
  render(anchor);
}

// index.json は毎回読むので、URLの共通部分（接頭辞・末尾）を省いて配信している。
// 省かれていれば復元する（'http' で始まっていれば省かれていない＝そのまま使う）。
function expandUrl(u) {
  return u.startsWith('http') ? u : urlPre + u + urlSuf;
}

// 未読数は軽量マニフェスト（url+時刻）だけで計算できる。c はエポック秒なのでmsに直す。
function unreadCount(dateStr) {
  const list = manifest[dateStr] || [];
  return list.filter((e) => !isReadAt(expandUrl(e.u), (e.c || 0) * 1000)).length;
}

// 日付ドロップダウンに、その日の未読件数（通知バッジ風）を出す
function refreshDateOptions() {
  for (const opt of dateSelect.options) {
    const n = unreadCount(opt.value);
    opt.textContent = n > 0 ? `${formatLabel(opt.value)} ●${n}` : formatLabel(opt.value);
  }
}

function updateTabCounts() {
  const unread = currentArticles.filter((a) => !isRead(a)).length;
  const badge = document.getElementById('unreadBadge');
  badge.textContent = unread;
  badge.classList.toggle('hidden', unread === 0);
  tabs.querySelector('[data-tab="read"]').textContent = `既読 ${currentArticles.length - unread}`;
  const savedCount = Object.keys(savedMap).length;
  tabs.querySelector('[data-tab="saved"]').textContent = savedCount ? `保存 ${savedCount}` : '保存';
  refreshDateOptions();
}

tabs.addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) setTab(b.dataset.tab);
});

// カードは差し替わるので、親に1つだけ委譲して受ける
feed.addEventListener('click', (e) => {
  const btn = e.target.closest('.save-btn');
  if (!btn) return;
  e.preventDefault();
  toggleSaved(btn.dataset.save);
});

// 横スワイプ検出（縦スクロールと区別：横の動きが縦の1.5倍以上のときだけ）
let touchX = 0, touchY = 0;
feed.addEventListener('touchstart', (e) => {
  touchX = e.touches[0].clientX;
  touchY = e.touches[0].clientY;
}, { passive: true });
feed.addEventListener('touchend', (e) => {
  const dx = e.changedTouches[0].clientX - touchX;
  const dy = e.changedTouches[0].clientY - touchY;
  if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    const i = TAB_ORDER.indexOf(currentTab);
    setTab(TAB_ORDER[dx < 0 ? Math.min(i + 1, TAB_ORDER.length - 1) : Math.max(i - 1, 0)]);
  }
}, { passive: true });

// ---------- 先頭に戻るボタン ----------
const toTop = document.getElementById('toTop');
toTop.addEventListener('click', () => {
  feed.scrollTo({ top: 0, behavior: 'smooth' });
});
function syncToTop() {
  toTop.classList.toggle('hidden', feed.scrollTop < feed.clientHeight * 0.8);
}
feed.addEventListener('scroll', syncToTop, { passive: true });

// ---------- 表示 ----------
function formatLabel(dateStr) {
  const [y, m, d] = dateStr.split('-');
  return `${y}/${Number(m)}/${Number(d)}`;
}
function formatShortDate(dateStr) {
  const [, m, d] = dateStr.split('-');
  return `${Number(m)}/${Number(d)}`;
}

function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function init() {
  let dates;
  try {
    // index.json 1ファイルだけで、日付一覧＋バッジ計算用の軽量マニフェストが手に入る。
    const res = await fetch('data/index.json', { cache: 'no-store' });
    const data = await res.json();
    dates = data.dates || [];
    manifest = data.manifest || {};
    urlPre = data.pre || '';   // 省いたURLの共通部分を受け取る
    urlSuf = data.suf || '';
  } catch (e) {
    feed.innerHTML = '<p class="empty">まだデータがありません。</p>';
    return;
  }
  if (dates.length === 0) {
    feed.innerHTML = '<p class="empty">まだデータがありません。</p>';
    return;
  }

  dateSelect.innerHTML = dates.map((d) => `<option value="${d}"></option>`).join('');
  refreshDateOptions();  // マニフェストから全日付のバッジを計算（中身の取得は不要）
  dateSelect.addEventListener('change', () => loadDate(dateSelect.value));

  // LINE通知の「?d=YYYY-MM-DD」リンクから来たら、その日を最初に表示する。無ければ最新の日。
  const wanted = new URLSearchParams(location.search).get('d');
  const initial = wanted && dates.includes(wanted) ? wanted : dates[0];
  dateSelect.value = initial;
  loadDate(initial);  // 中身は開いた日だけ取得
}

// 1日分のデータを取得してキャッシュ（取得済みなら再取得しない）
async function fetchDay(dateStr) {
  if (dayCache[dateStr]) return dayCache[dateStr];
  try {
    const res = await fetch(`data/${dateStr}.json`, { cache: 'no-store' });
    dayCache[dateStr] = await res.json();
  } catch (e) {
    dayCache[dateStr] = null;
  }
  return dayCache[dateStr];
}

async function loadDate(dateStr) {
  let day = dayCache[dateStr];
  if (!day) {
    feed.innerHTML = '<p class="loading">読み込み中…</p>';
    day = await fetchDay(dateStr);
    refreshDateOptions();
  }
  if (!day) {
    feed.innerHTML = '<p class="empty">この日のデータを読み込めませんでした。</p>';
    return;
  }

  currentArticles = (day.articles || []).slice().sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a.category);
    const ib = CATEGORY_ORDER.indexOf(b.category);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  // 案A：この日を開いた時点の既読/未読で振り分けを固定する
  tabSnapshot = {};
  for (const a of currentArticles) tabSnapshot[a.url] = isRead(a);

  updateTabCounts();
  render();
}

// 案A：未読タブだけ「開いた時点」で固定する（読んでも記事が抜けないので位置を見失わない）。
// 既読タブは最新の状態にする＝読んだ記事はすぐそこに現れる。
function inTab(a, tab) {
  if (tab === 'read') return isRead(a);   // 既読タブ：常に最新
  return !tabSnapshot[a.url];             // 記事タブ：開いた時点で未読だったもの（読んでも残る）
}

// 案C：今いちばん画面に映っているカードのURL。タブ移動後、同じ記事に合わせるために使う。
function currentAnchorUrl() {
  const cards = feed.querySelectorAll('.card');
  const mid = feed.clientHeight / 2;
  for (const c of cards) {
    const top = c.offsetTop - feed.scrollTop;
    if (top + c.offsetHeight > mid) return c.dataset.url;
  }
  return null;
}

// 案C：移動先に同じ記事があればその位置へ。無ければ先頭へ。
function scrollToArticle(url) {
  const el = url ? feed.querySelector(`.card[data-url="${CSS.escape(url)}"]`) : null;
  feed.scrollTop = el ? el.offsetTop : 0;
  syncToTop();
}

// 保存した記事がどの日のものか。保存時に控えた日付(d)を使う。
function savedDate(url) {
  const e = savedMap[url];
  if (!e) return '';
  if (e.d) return e.d;
  const c = e.a && e.a.content_at;
  return c ? c.slice(0, 10) : '';
}

function render(anchor) {
  observer.disconnect();

  // 保存タブは日付をまたいだ一覧（日付の新しい順、同じ日はカテゴリ順）。日付選択は無効化する。
  dateSelect.disabled = (currentTab === 'saved');
  if (currentTab === 'saved') {
    purgeExpiredSaved();   // 開いた時にも期限切れGuardianを掃除
    updateTabCounts();     // 掃除で減った保存数をタブに反映
    const list = Object.values(savedMap).sort((x, y) => {
      const dx = savedDate(x.a.url), dy = savedDate(y.a.url);
      if (dx !== dy) return dx < dy ? 1 : -1;
      const ia = CATEGORY_ORDER.indexOf(x.a.category);
      const ib = CATEGORY_ORDER.indexOf(y.a.category);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    }).map((v) => v.a);
    if (list.length === 0) {
      feed.innerHTML = '<p class="empty">保存した記事はありません。<br>'
                     + '記事の右上のしおりボタンで「後で見る」に追加できます。</p>';
      return;
    }
    feed.innerHTML = list.map(renderCard).join('');
    scrollToArticle(anchor);
    for (const card of feed.querySelectorAll('.card')) observer.observe(card);
    hint.style.opacity = '1';
    feed.addEventListener('scroll', () => { hint.style.opacity = '0'; }, { once: true });
    return;
  }

  const showRead = currentTab === 'read';
  // 案A：今の既読状態ではなく「開いた時点のスナップショット」で振り分ける＝読んでも記事は消えない
  const articles = currentArticles.filter((a) => inTab(a, currentTab));

  if (articles.length === 0) {
    feed.innerHTML = showRead
      ? '<p class="empty">まだ既読の記事はありません。</p>'
      : (currentArticles.length > 0
          ? '<p class="empty">🎉 この日の記事はすべて読みました。</p>'
          : '<p class="empty">この日の記事はありません。</p>');
    return;
  }

  feed.innerHTML = articles.map(renderCard).join('');
  scrollToArticle(anchor);   // 案C：移動先に同じ記事があればその位置へ（無ければ先頭）

  // 記事タブのカードだけ「表示されたら既読」を監視（既読タブでは不要）
  if (!showRead) {
    for (const card of feed.querySelectorAll('.card')) observer.observe(card);
  }

  hint.style.opacity = '1';
  feed.addEventListener('scroll', () => { hint.style.opacity = '0'; }, { once: true });
}

function renderCard(a) {
  const color = CATEGORY_COLORS[a.category] || '#757575';
  const emoji = CATEGORY_EMOJI[a.category] || '📰';

  // まだ日本語化できていない記事（英語のまま暫定表示中）。次回更新で改善予定。
  const pendingNote = a.pending
    ? `<div class="pending">⏳ 日本語化が未完了です（原文を暫定表示中）。次回の更新で改善予定です。</div>`
    : '';
  // 一度読んだ後に内容が改善され、未読に戻ってきた記事の目印。
  const updatedNote = (!a.pending && isUpdatedSinceRead(a))
    ? `<span class="updated">✨ 内容を更新</span>`
    : '';
  // 案A：記事タブに残したまま既読になった記事の目印。
  const readNote = (currentTab === 'unread' && isRead(a))
    ? `<span class="read-mark">既読</span>`
    : '';
  // 保存タブは日をまたぐので、いつの記事かを見出しに出す（他のタブは上の日付選択で分かる）。
  const sd = currentTab === 'saved' ? savedDate(a.url) : '';
  const dateChip = sd ? `<span class="date-chip">📅 ${formatShortDate(sd)}</span>` : '';
  // Guardian記事は規約で24時間以内に消えるので、保存タブでその旨を明示する。
  const expiryChip = (currentTab === 'saved' && isGuardian(a.url))
    ? `<span class="expiry-chip">⏳ 24時間で削除</span>` : '';
  // しおり（正面から見たリボン型）。未保存＝枠線だけ、保存済み＝塗りつぶし。
  const isSaved = !!savedMap[a.url];
  const saveBtn = `
    <button class="save-btn${isSaved ? ' saved' : ''}" data-save="${escapeHtml(a.url)}"
            aria-pressed="${isSaved}" aria-label="${isSaved ? '保存を解除' : '後で見るに保存'}"
            title="${isSaved ? '保存を解除' : '後で見る'}">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/>
      </svg>
    </button>`;

  const thumb = a.image
    ? `<img class="thumb" src="${escapeHtml(a.image)}" alt="" loading="lazy"
         onerror="this.classList.add('placeholder');this.removeAttribute('src');this.textContent='🖼';">`
    : `<div class="thumb placeholder">🖼</div>`;

  const impact = a.impact
    ? `<div class="impact">
         <div class="label">💡 社会への影響</div>
         <p>${escapeHtml(a.impact)}</p>
       </div>`
    : '';

  return `
    <article class="card" data-url="${escapeHtml(a.url)}">
      <div class="badges">
        <span class="cat" style="background:${color}">${emoji} ${escapeHtml(a.category)}</span>
        ${dateChip}
        ${expiryChip}
        ${updatedNote}
        ${readNote}
        ${saveBtn}
      </div>
      <h2 class="title">${escapeHtml(a.title)}</h2>
      ${thumb}
      <p class="summary">${escapeHtml(a.summary || '')}</p>
      ${pendingNote}
      ${impact}
      <a class="read" href="${escapeHtml(a.url)}" target="_blank" rel="noopener">元記事を読む →</a>
      <p class="meta">${escapeHtml(a.time || '')} ・ 出典: ${escapeHtml(CATEGORY_SOURCE[a.category] || '')}</p>
    </article>`;
}

init();
