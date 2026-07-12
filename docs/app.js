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

let readMap = loadRead();
let manifest = {};          // 日付→[{u:url, c:content_at}]（バッジ計算用の軽量一覧・index.json由来）
let dayCache = {};          // 開いた日の中身をキャッシュして再取得を避ける
let currentArticles = [];   // 表示中の日の全記事（カテゴリ順ソート済み）
let currentTab = 'unread';  // 'unread' | 'read'

// 「読んだ後に内容が更新された（＝翻訳・要約が改善された）記事」は未読に戻す。
// url と contentAt だけで判定できるので、軽量マニフェストでも記事本体でも使える。
function isReadKey(url, contentAt) {
  const t = readMap[url];
  if (!t) return false;                      // まだ読んでいない＝未読
  const c = contentAt ? Date.parse(contentAt) : 0;
  return c <= t;                             // 読んだ後に更新されていれば未読扱い
}
function isRead(a) {
  return isReadKey(a.url, a.content_at);
}
function isUpdatedSinceRead(a) {
  const t = readMap[a.url];
  const c = a.content_at ? Date.parse(a.content_at) : 0;
  return t && c > t;                         // 一度読んだが、その後に改善された
}

function markRead(url) {
  if (!url) return;
  readMap[url] = Date.now();                 // 常に最新の読了時刻に更新
  saveRead();
  updateTabCounts();
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

// ---------- タブ（未読 | 既読）：タップ or 横スワイプで切替 ----------
function setTab(tab) {
  if (tab === currentTab) return;
  currentTab = tab;
  for (const b of tabs.querySelectorAll('button')) {
    b.classList.toggle('active', b.dataset.tab === tab);
  }
  render();
}

// 未読数は軽量マニフェスト（url+content_at）だけで計算できる。
// → 中身を取得しなくても、全日付のバッジが正確に出せる。
function unreadCount(dateStr) {
  const list = manifest[dateStr] || [];
  return list.filter((e) => !isReadKey(e.u, e.c)).length;
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
  refreshDateOptions();
}

tabs.addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) setTab(b.dataset.tab);
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
    setTab(dx < 0 ? 'read' : 'unread');  // 左へスワイプ→既読 / 右へ→未読
  }
}, { passive: true });

// ---------- 表示 ----------
function formatLabel(dateStr) {
  const [y, m, d] = dateStr.split('-');
  return `${y}/${Number(m)}/${Number(d)}`;
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

  updateTabCounts();
  render();
}

function render() {
  observer.disconnect();

  const showRead = currentTab === 'read';
  const articles = currentArticles.filter((a) => isRead(a) === showRead);

  if (articles.length === 0) {
    feed.innerHTML = showRead
      ? '<p class="empty">まだ既読の記事はありません。</p>'
      : (currentArticles.length > 0
          ? '<p class="empty">🎉 この日の記事はすべて読みました。</p>'
          : '<p class="empty">この日の記事はありません。</p>');
    return;
  }

  feed.innerHTML = articles.map(renderCard).join('');
  feed.scrollTop = 0;

  // 未読タブのカードだけ「表示されたら既読」を監視
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
        ${updatedNote}
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
