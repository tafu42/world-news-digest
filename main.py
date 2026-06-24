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

# 表示カテゴリ: (Guardianのsection, 取得件数)。重大ニュースはsection指定なしの最新
TOP_CATEGORY = '重大ニュース'
TOP_COUNT = 5
SECTIONS = {
    'テクノロジー': ('technology', 3),
    'ビジネス・経済': ('business', 2),
    '国際': ('world', 3),
    '政治': ('politics', 2),
    '科学': ('science', 1),
}

BATCH_SIZE = 5


# ---------- Gemini ----------
def gemini_call(prompt, retries=3):
    client = genai.Client(api_key=GEMINI_API_KEY)
    for attempt in range(retries):
        try:
            res = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
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


def _translate_prompt(group):
    items = "\n\n".join(
        f"記事{i+1}:\nTitle: {a['en_title']}\nText: {a['en_text'][:300]}"
        for i, a in enumerate(group)
    )
    return (f"次の{len(group)}件の英語ニュースをそれぞれ日本語にしてください。\n"
            "各記事について、自然な日本語のタイトルと、2文以内の日本語要約を作ってください。\n"
            "要約はタイトルの言い換えではなく、本文の具体的な内容を含めてください。\n"
            f"必ず{len(group)}件を記事の順番どおりに、JSON配列で返してください。\n"
            '形式: [{"title": "日本語タイトル", "summary": "日本語要約"}]\n\n' + items)


def _translate_one(a):
    try:
        txt = gemini_call(
            "次の英語ニュースを日本語にしてください。日本語タイトルと2文以内の日本語要約を、"
            'JSONで {"title":"...","summary":"..."} の形だけ返してください。\n'
            f"Title: {a['en_title']}\nText: {a['en_text'][:300]}"
        )
        return parse_json(txt)
    except Exception as e:
        note_error(e)
        return {'title': a['en_title'], 'summary': a.get('en_text', '')[:120]}


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
    plan = [(TOP_CATEGORY, None, TOP_COUNT)] + \
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

    # 翻訳＋要約（全新着）
    translated = _gen_all(new_articles, _translate_prompt, _translate_one)
    now_hm = datetime.now(JST).strftime('%H:%M')
    for a, t in zip(new_articles, translated):
        a['title'] = (t or {}).get('title', a['en_title'])
        a['summary'] = (t or {}).get('summary', '')
        a['time'] = now_hm

    # 社会への影響（重大ニュースのみ）
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
    print(f'追記完了（新着{len(new_articles)}件 / 重大{len(top_new)}件 / {today} 累計{len(day["articles"])}件）')

    if gemini_errors:
        if 'quota' in gemini_errors:
            line_alert(f'⚠️ Gemini APIが上限に達した可能性があります（{len(gemini_errors)}件失敗）。時間をおくと回復します。')
        else:
            line_alert(f'⚠️ ニュース生成でエラー（{len(gemini_errors)}件）。例: {gemini_errors[0]}')


if __name__ == '__main__':
    main()
