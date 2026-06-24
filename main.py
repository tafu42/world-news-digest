"""定期実行：The Guardian APIから国際ニュースを収集し、Geminiで日本語に翻訳・要約して
日付ごとのデータファイルに追記する。重大ニュースには「社会への影響」も生成する。
LINE通知は notify.py が別途担当する。

ライセンス：The Guardian Open Platform（非商用・要出典明記・元記事リンク）。
"""
import os
import re
import json
import time
import requests
from google import genai
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GUARDIAN_API_KEY = os.getenv('GUARDIAN_API_KEY')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_USER_ID = os.getenv('LINE_USER_ID')

gemini_errors = []

JST = timezone(timedelta(hours=9))

DATA_DIR = os.path.join('docs', 'data')
INDEX_FILE = os.path.join(DATA_DIR, 'index.json')
RETENTION_DAYS = 30

GUARDIAN_ENDPOINT = 'https://content.guardianapis.com/search'

# テック中心。トップ（テクノロジー）に社会への影響を付ける
TOP_CATEGORY = 'テクノロジー'
TOP_SECTION = 'technology'
TOP_COUNT = 5
SECTIONS = {
    '科学':           ('science', 3),
    '国際':           ('world', 3),
    '環境':           ('environment', 2),
    'ビジネス・経済': ('business', 2),
}

BATCH_SIZE = 8   # 要約バッチ（呼び出し回数を抑える）
TITLE_BATCH = 16  # タイトルは短いので一括翻訳（最優先・低コスト）


# ---------- Gemini ----------
def gemini_call(prompt, retries=3):
    client = genai.Client(api_key=GEMINI_API_KEY)
    for attempt in range(retries):
        try:
            res = client.models.generate_content(model='gemini-2.5-flash-lite', contents=prompt)
            return res.text.strip()
        except Exception:
            if attempt < retries - 1:
                time.sleep(30 * (attempt + 1))
            else:
                raise


def is_quota_error(e):
    m = str(e)
    return '429' in m or 'RESOURCE_EXHAUSTED' in m or 'quota' in m.lower()


def note_error(e):
    gemini_errors.append('quota' if is_quota_error(e) else str(e)[:120])


def parse_json(text):
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    return json.loads(text.strip())


def _gen_group(group, build_prompt, build_one):
    try:
        arr = parse_json(gemini_call(build_prompt(group)))
        if isinstance(arr, list) and len(arr) == len(group):
            return arr
    except Exception:
        pass
    return [build_one(a) for a in group]


def _gen_all(articles, build_prompt, build_one):
    out = []
    for i in range(0, len(articles), BATCH_SIZE):
        out.extend(_gen_group(articles[i:i + BATCH_SIZE], build_prompt, build_one))
    return out


def _gen_sized(articles, build_prompt, build_one, size):
    out = []
    for i in range(0, len(articles), size):
        out.extend(_gen_group(articles[i:i + size], build_prompt, build_one))
    return out


# --- タイトル翻訳（最優先・短いので大きめバッチ） ---
def _title_prompt(group):
    items = "\n".join(f"{i+1}. {a['en_title']}" for i, a in enumerate(group))
    return (f"次の{len(group)}件の英語ニュース見出しを、それぞれ自然な日本語タイトルに翻訳してください。\n"
            f"必ず{len(group)}件を順番どおりにJSON配列で返してください。\n"
            '例: ["日本語タイトル1", "日本語タイトル2"]\n\n' + items)


def _title_one(a):
    try:
        return gemini_call(
            "次の英語見出しを自然な日本語タイトルに翻訳してください。訳のみ返してください。\n"
            f"{a['en_title']}"
        ).strip()
    except Exception as e:
        note_error(e)
        return a['en_title']


# --- 要約翻訳（タイトルの次） ---
def _summary_prompt(group):
    items = "\n\n".join(f"記事{i+1}:\n{a['en_text'][:300]}" for i, a in enumerate(group))
    return (f"次の{len(group)}件の英語ニュース本文を、それぞれ日本語で2文以内に要約してください。\n"
            "見出しの言い換えではなく、本文の具体的な内容を含めてください。\n"
            f"必ず{len(group)}件を順番どおりにJSON配列で返してください。\n"
            '例: ["要約1", "要約2"]\n\n' + items)


def _summary_one(a):
    try:
        return gemini_call(
            "次の英語ニュースを日本語で2文以内に要約してください。要約のみ返してください。\n"
            f"{a['en_text'][:300]}"
        ).strip()
    except Exception as e:
        note_error(e)
        return a.get('en_text', '')[:120]


def _impact_prompt(group):
    items = "\n\n".join(
        f"記事{i+1}:\nタイトル: {a['title']}\n要約: {a.get('summary','')}"
        for i, a in enumerate(group)
    )
    return (f"次の{len(group)}件の重要ニュースについて、それぞれ"
            "『このニュースで起こりうる社会への影響』を日本語で1〜2文で具体的に書いてください。\n"
            f"必ず{len(group)}件を記事の順番どおりにJSON配列で返してください。\n"
            '例: ["影響1", "影響2"]\n\n' + items)


def _impact_one(a):
    try:
        return gemini_call(
            "次のニュースで起こりうる社会への影響を日本語1〜2文で書いてください。本文のみ返してください。\n"
            f"タイトル: {a['title']}\n要約: {a.get('summary','')}"
        ).strip()
    except Exception as e:
        note_error(e)
        return ''


def line_alert(text):
    if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID):
        return
    try:
        requests.post('https://api.line.me/v2/bot/message/push',
                      headers={'Content-Type': 'application/json',
                               'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'},
                      json={'to': LINE_USER_ID, 'messages': [{'type': 'text', 'text': text}]},
                      timeout=10)
    except Exception:
        pass


# ---------- Guardian 収集 ----------
def strip_html(s):
    return re.sub(r'<[^>]+>', '', s or '').strip()


# ライブ速報・動画・ギャラリー等は要約に向かないので除外する
EXCLUDE_URL_RE = re.compile(r'/(live|video|audio|gallery|ng-interactive|picture)/')


def guardian_fetch(section, count):
    params = {
        'api-key': GUARDIAN_API_KEY,
        'show-fields': 'headline,trailText,thumbnail',
        'order-by': 'newest',
        'page-size': count * 3,  # 除外する分を見越して多めに取る
    }
    if section:
        params['section'] = section
    r = requests.get(GUARDIAN_ENDPOINT, params=params, timeout=15)
    r.raise_for_status()
    results = r.json().get('response', {}).get('results', [])
    items = []
    for x in results:
        if len(items) >= count:
            break
        url = x.get('webUrl', '')
        if not url or EXCLUDE_URL_RE.search(url):
            continue
        f = x.get('fields', {})
        trail = strip_html(f.get('trailText', ''))
        if len(trail) < 25:  # 要約材料が薄すぎる記事は飛ばす
            continue
        items.append({
            'en_title': f.get('headline', x.get('webTitle', '')),
            'en_text': trail,
            'image': f.get('thumbnail', ''),
            'url': url,
        })
    return items


def collect(seen_urls):
    plan = [(TOP_CATEGORY, TOP_SECTION, TOP_COUNT)] + \
           [(name, sec, n) for name, (sec, n) in SECTIONS.items()]
    new_articles = []
    for category, section, count in plan:
        for a in guardian_fetch(section, count):
            if not a['url'] or a['url'] in seen_urls:
                continue
            a['category'] = category
            new_articles.append(a)
            seen_urls.add(a['url'])
    return new_articles


# ---------- 保存 ----------
def date_path(d):
    return os.path.join(DATA_DIR, f'{d}.json')


def load_day(d):
    p = date_path(d)
    if not os.path.exists(p):
        return {'date': d, 'articles': []}
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_day(day):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(date_path(day['date']), 'w', encoding='utf-8') as f:
        json.dump(day, f, ensure_ascii=False, indent=2)


def known_urls(dates):
    urls = set()
    for d in dates:
        p = date_path(d)
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                for a in json.load(f).get('articles', []):
                    urls.add(a['url'])
    return urls


def rebuild_index():
    dates = [n[:-5] for n in os.listdir(DATA_DIR)
             if n.endswith('.json') and n != 'index.json']
    dates.sort(reverse=True)
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        json.dump({'dates': dates}, f, ensure_ascii=False, indent=2)


def cleanup_old_days():
    if not os.path.isdir(DATA_DIR):
        return
    cutoff = (datetime.now(JST) - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%d')
    for n in os.listdir(DATA_DIR):
        if n.endswith('.json') and n != 'index.json' and n[:-5] < cutoff:
            os.remove(os.path.join(DATA_DIR, n))


def main():
    today = datetime.now(JST).strftime('%Y-%m-%d')
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime('%Y-%m-%d')

    seen = known_urls([today, yesterday])
    new_articles = collect(seen)

    if not new_articles:
        print('新着記事なし。')
        cleanup_old_days()
        rebuild_index()
        return

    now_hm = datetime.now(JST).strftime('%H:%M')
    for a in new_articles:
        a['time'] = now_hm

    # ① タイトル翻訳（最優先：枠が厳しくても一覧で読めるように先に確保）
    titles = _gen_sized(new_articles, _title_prompt, _title_one, TITLE_BATCH)
    for a, t in zip(new_articles, titles):
        a['title'] = t

    # ② 要約翻訳（次）
    summaries = _gen_all(new_articles, _summary_prompt, _summary_one)
    for a, s in zip(new_articles, summaries):
        a['summary'] = s

    # ③ 社会への影響（最後・テクノロジーのみ／余力があれば）
    top_new = [a for a in new_articles if a['category'] == TOP_CATEGORY]
    if top_new:
        for a, imp in zip(top_new, _gen_all(top_new, _impact_prompt, _impact_one)):
            a['impact'] = imp

    # 内部用フィールド除去
    for a in new_articles:
        a.pop('en_title', None)
        a.pop('en_text', None)

    day = load_day(today)
    day['articles'].extend(new_articles)
    day['updated_at'] = datetime.now(JST).isoformat()
    save_day(day)

    cleanup_old_days()
    rebuild_index()
    print(f'追記完了（新着{len(new_articles)}件 / テック{len(top_new)}件 / {today} 累計{len(day["articles"])}件）')

    if gemini_errors:
        if 'quota' in gemini_errors:
            line_alert(f'⚠️ Gemini APIが上限に達した可能性があります（{len(gemini_errors)}件失敗）。時間をおくと回復します。')
        else:
            line_alert(f'⚠️ ニュース生成でエラー（{len(gemini_errors)}件）。例: {gemini_errors[0]}')


if __name__ == '__main__':
    main()
