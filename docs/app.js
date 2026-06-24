const CATEGORY_COLORS = {
  '重大ニュース': '#E53935',
  'テクノロジー': '#1E88E5',
  'ビジネス・経済': '#43A047',
  '国際': '#8E24AA',
  '政治': '#5E35B1',
  '科学': '#00ACC1',
};
const CATEGORY_EMOJI = {
  '重大ニュース': '🔥',
  'テクノロジー': '💻',
  'ビジネス・経済': '💴',
  '国際': '🌍',
  '政治': '🏛',
  '科学': '🔬',
};
// 重大ニュースを先頭にする並び順
const CATEGORY_ORDER = Object.keys(CATEGORY_COLORS);

const dateSelect = document.getElementById('dateSelect');
const feed = document.getElementById('feed');
const hint = document.getElementById('hint');

function formatLabel(dateStr) {
  const [y, m, d] = dateStr.split('-');
  return `${y}/${Number(m)}/${Number(d)}`;
}

function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function init() {
  let dates;
  try {
    const res = await fetch('data/index.json', { cache: 'no-store' });
    dates = (await res.json()).dates || [];
  } catch (e) {
    feed.innerHTML = '<p class="empty">まだデータがありません。</p>';
    return;
  }
  if (dates.length === 0) {
    feed.innerHTML = '<p class="empty">まだデータがありません。</p>';
    return;
  }

  dateSelect.innerHTML = dates
    .map((d) => `<option value="${d}">${formatLabel(d)}</option>`)
    .join('');
  dateSelect.addEventListener('change', () => renderDate(dateSelect.value));
  renderDate(dates[0]);
}

async function renderDate(dateStr) {
  feed.innerHTML = '<p class="loading">読み込み中…</p>';
  let day;
  try {
    const res = await fetch(`data/${dateStr}.json`, { cache: 'no-store' });
    day = await res.json();
  } catch (e) {
    feed.innerHTML = '<p class="empty">この日のデータを読み込めませんでした。</p>';
    return;
  }

  const articles = (day.articles || []).slice().sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a.category);
    const ib = CATEGORY_ORDER.indexOf(b.category);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  if (articles.length === 0) {
    feed.innerHTML = '<p class="empty">この日の記事はありません。</p>';
    return;
  }

  feed.innerHTML = articles.map(renderCard).join('');
  feed.scrollTop = 0;

  // スワイプヒントは最初の操作で消す
  hint.style.opacity = '1';
  feed.addEventListener('scroll', () => { hint.style.opacity = '0'; }, { once: true });
}

function renderCard(a) {
  const color = CATEGORY_COLORS[a.category] || '#757575';
  const emoji = CATEGORY_EMOJI[a.category] || '📰';

  const thumb = a.image
    ? `<img class="thumb" src="${a.image}" alt="" loading="lazy"
         onerror="this.classList.add('placeholder');this.removeAttribute('src');this.textContent='🖼';">`
    : `<div class="thumb placeholder">🖼</div>`;

  const impact = a.impact
    ? `<div class="impact">
         <div class="label">💡 社会への影響</div>
         <p>${escapeHtml(a.impact)}</p>
       </div>`
    : '';

  return `
    <article class="card">
      <span class="cat" style="background:${color}">${emoji} ${escapeHtml(a.category)}</span>
      <h2 class="title">${escapeHtml(a.title)}</h2>
      ${thumb}
      <p class="summary">${escapeHtml(a.summary || '')}</p>
      ${impact}
      <a class="read" href="${a.url}" target="_blank" rel="noopener">元記事を読む →</a>
      <p class="meta">${a.time || ''}</p>
    </article>`;
}

init();
