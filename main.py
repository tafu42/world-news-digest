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

# Guardian Open Platform 規約：コンテンツを24時間を超えて保持してはならない。
# そのためGuardian由来の記事は取得から削除する（HN等はRETENTION_DAYSに従う）。
# 収集は固定時刻（6:30/13:30/21:30）で走るため、しきい値を厳密に24hにすると
# 「収集時刻と実行時刻が重なった記事」がちょうど24h判定をすり抜け、次サイクル（最大+9h）まで
# 残ってしまう。これを防ぐため0.5hの安全マージンを取り、23.5hで失効させる（24hを超える前に確実に削除）。
GUARDIAN_HOST = 'theguardian.com'
GUARDIAN_TTL_HOURS = 23.5

GUARDIAN_ENDPOINT = 'https://content.guardianapis.com/search'
HN_TOP = 'https://hacker-news.firebaseio.com/v0/topstories.json'
HN_ITEM = 'https://hacker-news.firebaseio.com/v0/item/{}.json'

# IT特化。Guardianはテクノロジーのみ＋Hacker Newsで補強
TOP_CATEGORY = 'テクノロジー'           # Guardian technology（社会への影響つき）
TOP_SECTION = 'technology'
TOP_COUNT = 6
HN_CATEGORY = '海外IT'                  # Hacker News
HN_COUNT = 6

BATCH_SIZE = 8   # 要約バッチ（呼び出し回数を抑える）
TITLE_BATCH = 16  # タイトルは短いので一括翻訳（最優先・低コスト）
ALERT_THRESHOLD = 4  # 再挑戦後もこの件数以上翻訳できなければLINE通知（軽微な失敗は鳴らさない）


# ---------- Gemini ----------
# RPM(1分あたり制限)対策：全呼び出しの間隔を最低 CALL_GAP 秒空けてバーストを防ぐ。
# 例) 4秒間隔なら最大15回/分に収まり、連射による瞬間的な上限超過が起きない。
CALL_GAP = 4.0
_last_call = [0.0]


def gemini_call(prompt, retries=3):
    client = genai.Client(api_key=GEMINI_API_KEY)
    for attempt in range(retries):
        # 前回呼び出しから CALL_GAP 秒経つまで待つ（呼び出しを時間的にバラけさせる）
        wait = CALL_GAP - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
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


# 日本語が含まれていれば翻訳成功とみなす（英語のまま＝失敗の判定に使う）
_JP_RE = re.compile(r'[ぁ-んァ-ヶ一-龠]')


def looks_translated(s):
    return bool(_JP_RE.search(s or ''))


def retry_failed(articles, field, build_one):
    """翻訳に失敗（英語のまま）した記事を待機列に入れ、間隔を空けて1件ずつ再挑戦する。
    gemini_call が CALL_GAP 秒の間隔を確保するので、再挑戦時にはRPMの窓が空いている。"""
    failed = [a for a in articles if not looks_translated(a.get(field, ''))]
    for a in failed:
        try:
            val = build_one(a)
            if looks_translated(val):
                a[field] = val
        except Exception:
            pass  # 最終的な失敗判定は main の集計で行う


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


def fetch_og(url):
    """リンク先記事から og:image（画像）と og:description（書き出し）を取得"""
    try:
        html = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'}).text
    except Exception:
        return '', ''
    mi = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
    md = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)', html)
    return (mi.group(1) if mi else ''), (md.group(1) if md else '')


def hn_fetch(count):
    """Hacker Newsのトップ記事を取得（外部リンクありのstoryのみ・画像はog:image）"""
    try:
        ids = requests.get(HN_TOP, timeout=15).json()
    except Exception:
        return []
    items = []
    for i in ids[:count * 4]:
        if len(items) >= count:
            break
        try:
            it = requests.get(HN_ITEM.format(i), timeout=10).json()
        except Exception:
            continue
        if not it or it.get('type') != 'story' or not it.get('url'):
            continue
        url = it['url']
        if url in [x['url'] for x in items]:
            continue
        image, desc = fetch_og(url)
        items.append({
            'en_title': it.get('title', ''),
            'en_text': desc or it.get('title', ''),
            'image': image,
            'url': url,
        })
    return items


def collect(seen_urls):
    new_articles = []
    # Guardian テクノロジー
    for a in guardian_fetch(TOP_SECTION, TOP_COUNT):
        if not a['url'] or a['url'] in seen_urls:
            continue
        a['category'] = TOP_CATEGORY
        new_articles.append(a)
        seen_urls.add(a['url'])
    # Hacker News 海外IT
    for a in hn_fetch(HN_COUNT):
        if not a['url'] or a['url'] in seen_urls:
            continue
        a['category'] = HN_CATEGORY
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


def _is_guardian(a):
    return GUARDIAN_HOST in (a.get('url') or '')


def _collected_dt(a, date_str):
    """記事を取得したJST時刻を返す。collected_atが無い古いデータは date+time から復元。"""
    ts = a.get('collected_at')
    if ts:
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
    try:
        return datetime.strptime(f"{date_str} {a.get('time', '00:00')}", '%Y-%m-%d %H:%M').replace(tzinfo=JST)
    except ValueError:
        return None


def expire_guardian():
    """Guardian Open Platform 規約：取得から24時間を超えたGuardian由来コンテンツを削除する。
    要約・サムネ画像はGuardianのコンテンツ（翻訳も派生物）なので保持しない。HN等は対象外。"""
    if not os.path.isdir(DATA_DIR):
        return
    now = datetime.now(JST)
    for n in os.listdir(DATA_DIR):
        if not n.endswith('.json') or n == 'index.json':
            continue
        date_str = n[:-5]
        p = os.path.join(DATA_DIR, n)
        with open(p, 'r', encoding='utf-8') as f:
            day = json.load(f)
        articles = day.get('articles', [])
        kept = []
        for a in articles:
            if _is_guardian(a):
                dt = _collected_dt(a, date_str)
                if dt is None or (now - dt) > timedelta(hours=GUARDIAN_TTL_HOURS):
                    continue  # 24時間超のGuardian記事は破棄
            kept.append(a)
        if len(kept) == len(articles):
            continue  # 変化なし
        if kept:
            day['articles'] = kept
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(day, f, ensure_ascii=False, indent=2)
        else:
            os.remove(p)  # Guardianしか無かった日は丸ごと消える


def main():
    today = datetime.now(JST).strftime('%Y-%m-%d')
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime('%Y-%m-%d')

    seen = known_urls([today, yesterday])
    new_articles = collect(seen)

    if not new_articles:
        print('新着記事なし。')
        expire_guardian()
        cleanup_old_days()
        rebuild_index()
        return

    now = datetime.now(JST)
    now_hm = now.strftime('%H:%M')
    for a in new_articles:
        a['time'] = now_hm
        a['collected_at'] = now.isoformat()  # 24時間判定用（Guardian規約）

    # ① タイトル翻訳（最優先）→ 失敗分は待機列に入れて再挑戦
    titles = _gen_sized(new_articles, _title_prompt, _title_one, TITLE_BATCH)
    for a, t in zip(new_articles, titles):
        a['title'] = t
    retry_failed(new_articles, 'title', _title_one)

    # ② 要約翻訳 → 失敗分は待機列に入れて再挑戦
    summaries = _gen_all(new_articles, _summary_prompt, _summary_one)
    for a, s in zip(new_articles, summaries):
        a['summary'] = s
    retry_failed(new_articles, 'summary', _summary_one)

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

    expire_guardian()
    cleanup_old_days()
    rebuild_index()
    print(f'追記完了（新着{len(new_articles)}件 / テック{len(top_new)}件 / {today} 累計{len(day["articles"])}件）')

    # 再挑戦後も翻訳できなかった件数で通知を判断（軽微な失敗では鳴らさない）
    untranslated = sum(
        1 for a in new_articles
        if not looks_translated(a.get('title', '')) or not looks_translated(a.get('summary', ''))
    )
    if untranslated >= ALERT_THRESHOLD:
        if untranslated >= len(new_articles) * 0.5:
            line_alert(f'⚠️ Gemini APIが上限に達した可能性があります'
                       f'（{untranslated}/{len(new_articles)}件が翻訳できず）。時間をおくと回復します。')
        else:
            line_alert(f'⚠️ 翻訳に一部失敗しました（{untranslated}/{len(new_articles)}件）。')


if __name__ == '__main__':
    main()
