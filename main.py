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
from google.genai import types
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

BATCH_SIZE = 4   # 翻訳・影響のバッチ。小さめにして記事の混線・件数ズレを防ぐ（枠に余裕ができたため）
ALERT_THRESHOLD = 4  # この件数以上が英語のまま残ったらLINE通知（軽微な失敗は鳴らさない）


# ---------- Gemini（gemini-3.1-flash-lite: RPM15 / RPD500）----------
# 2.5-flash-lite(RPD20)から乗り換え。RPD枠はモデルごとの別バケツで、3.1-flash-liteは桁違いに広い。
# ・CALL_GAP：RPM15（1分15回）→ 4秒が下限。余裕を見て5秒（最大12回/分）
# ・CALL_BUDGET：1収集の呼び出し上限（暴走防止の安全弁）。新着の翻訳＋保留分の再挑戦に
#   十分な15回。3回/日 × 15 = 最大45回/日でもRPD500に対して大幅に余裕。
CALL_GAP = 5.0
CALL_BUDGET = 15
_last_call = [0.0]
_calls_used = [0]   # この収集で使った呼び出し回数（予算管理用）

# 構造化出力のスキーマ（JSONモード）。出力の形をモデルに強制し、
# マークダウンや説明文の混入によるJSON崩れ＝バッチ失敗を大幅に減らす。
TRANSLATE_SCHEMA = types.Schema(
    type=types.Type.ARRAY,
    items=types.Schema(
        type=types.Type.OBJECT,
        properties={
            'title': types.Schema(type=types.Type.STRING),
            'summary': types.Schema(type=types.Type.STRING),
        },
        required=['title', 'summary'],
    ),
)
IMPACT_SCHEMA = types.Schema(
    type=types.Type.ARRAY,
    items=types.Schema(type=types.Type.STRING),
)


def gemini_call(prompt, schema):
    # ① 予算（1回の収集の呼び出し上限）を超えたら呼ばない＝雪だるま防止・RPD保護
    if _calls_used[0] >= CALL_BUDGET:
        raise RuntimeError('call budget reached')
    # ② RPM対策：前回呼び出しから CALL_GAP 秒空ける
    wait = CALL_GAP - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    _calls_used[0] += 1
    client = genai.Client(api_key=GEMINI_API_KEY)
    # JSONモード（構造化出力）：返答の形を強制してJSON崩れを防ぐ
    res = client.models.generate_content(
        model='gemini-3.1-flash-lite',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type='application/json',
            response_schema=schema,
        ),
    )
    return res.text.strip()


def is_quota_error(e):
    m = str(e)
    return '429' in m or 'RESOURCE_EXHAUSTED' in m or 'quota' in m.lower()


def note_error(e):
    gemini_errors.append('quota' if is_quota_error(e) else str(e)[:120])


# 日本語が含まれていれば翻訳成功とみなす（英語のまま＝失敗の判定に使う）
_JP_RE = re.compile(r'[ぁ-んァ-ヶ一-龠]')


def looks_translated(s):
    return bool(_JP_RE.search(s or ''))


def parse_json(text):
    """Geminiの返答からJSONを取り出す。モデルがコードフェンスや前後の説明文を
    付けてくることがあるので、最初の [..] または {..} だけを抜き出して読む。"""
    t = text.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', t, re.S)
    if m:
        t = m.group(1).strip()
    m = re.search(r'(\[.*\]|\{.*\})', t, re.S)
    if m:
        t = m.group(1)
    return json.loads(t)


def _try_batch(group, build_prompt, schema, valid):
    """1バッチをGeminiに投げ、件数一致＋各要素が妥当なら結果リストを返す。
    失敗（JSON崩れ・件数ズレ・予算切れ・エラー）なら None。
    ※1件ずつ処理は行わない（失敗の巻き添えでコストを食い潰さないため）。"""
    try:
        arr = parse_json(gemini_call(build_prompt(group), schema))
        if isinstance(arr, list) and len(arr) == len(group) and all(valid(x) for x in arr):
            return arr
    except RuntimeError:
        pass  # CALL_BUDGET到達（自前の打ち切り）＝正常系なのでエラー扱いしない
    except Exception as e:
        note_error(e)
    return None


def _gen_all(articles, build_prompt, schema, valid, fallback):
    """バッチ処理。失敗したバッチの記事は「保留プール」に入れ、全バッチを回し終えた後に
    保留分だけもう一度まとめてバッチで再挑戦する。それでも埋まらない分だけ fallback
    （Geminiは呼ばない）。→ 1件ずつ呼ぶ無駄をなくす。"""
    results = [None] * len(articles)

    def run_pass(index_list):
        failed = []
        for i in range(0, len(index_list), BATCH_SIZE):
            idxs = index_list[i:i + BATCH_SIZE]
            out = _try_batch([articles[j] for j in idxs], build_prompt, schema, valid)
            if out is not None:
                for j, r in zip(idxs, out):
                    results[j] = r
            else:
                failed.extend(idxs)
        return failed

    pending = run_pass(list(range(len(articles))))   # パス1
    if pending:
        run_pass(pending)                            # パス2：失敗分だけ再バッチ
    for j in range(len(articles)):
        if results[j] is None:
            results[j] = fallback(articles[j])
    return results


# --- タイトル＋要約を1回のリクエストでまとめて翻訳（呼び出しを半減・A案） ---
def _translate_prompt(group):
    items = "\n\n".join(
        f"[{i+1}]\nTitle: {a['en_title']}\nText: {a['en_text'][:300]}"
        for i, a in enumerate(group)
    )
    return (f"次の{len(group)}件の英語ニュースを日本語にしてください。\n"
            "各記事について、自然な日本語タイトルと、本文の要約（2文以内・具体的に）を作ってください。\n"
            f"必ず{len(group)}件を順番どおりにJSON配列で返してください。\n"
            '形式: [{"title": "日本語タイトル", "summary": "日本語要約"}]\n\n' + items)


def _translate_valid(x):
    """タイトル・要約が両方そろい、かつ日本語になっていること（英語のまま返す失敗を弾く）。"""
    return (isinstance(x, dict)
            and looks_translated(x.get('title', ''))
            and looks_translated(x.get('summary', '')))


def _translate_fallback(a):
    """再バッチでも翻訳できなかった＝英語のまま表示する（Gemini不使用）。
    a['_failed']に印を付けて、呼び出し側で pending 扱い（次回収集で再挑戦）にできるようにする。"""
    a['_failed'] = True
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


def _impact_valid(x):
    return isinstance(x, str) and x.strip()


def _impact_fallback(a):
    """影響が生成できなくても記事自体は読めるので、空のままにする（pendingにはしない）。"""
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


def _apply_translation(a, r, now_iso):
    """翻訳結果を記事へ反映。失敗（_failed）なら pending を立て、材料(en_*)を残して次回に備える。"""
    a['title'] = (r or {}).get('title') or a['en_title']
    a['summary'] = (r or {}).get('summary') or ''
    a['content_at'] = now_iso   # 内容が確定した時刻（改善したことを画面側で判別できる）
    if a.pop('_failed', False):
        a['pending'] = True     # 英語のまま → 次回収集で再挑戦（en_* は材料として保持）
    else:
        a.pop('pending', None)
        a.pop('en_title', None)
        a.pop('en_text', None)


def retry_pending(articles, now_iso):
    """pending（前回翻訳に失敗して英語のまま残った記事）を、残り予算で再バッチする。
    成功したものだけ日本語に差し替えて pending を外す。失敗は据え置き（次回また挑戦）。
    1件ずつは呼ばない。"""
    pend = [a for a in articles if a.get('pending') and a.get('en_title')]
    if not pend:
        return 0
    changed = 0
    for a, r in zip(pend, _gen_all(pend, _translate_prompt, TRANSLATE_SCHEMA,
                                   _translate_valid, lambda x: None)):
        if isinstance(r, dict) and looks_translated(r.get('title', '')):
            a['title'] = r['title']
            a['summary'] = r.get('summary') or a.get('summary', '')
            a.pop('pending', None)
            a.pop('en_title', None)
            a.pop('en_text', None)
            a['content_at'] = now_iso
            changed += 1
    return changed


def main():
    now = datetime.now(JST)
    now_iso = now.isoformat()
    today = now.strftime('%Y-%m-%d')
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')

    seen = known_urls([today, yesterday])
    new_articles = collect(seen)

    _calls_used[0] = 0  # この収集の呼び出し予算をリセット（新着＋保留の再挑戦で共有）

    top_new = []
    if new_articles:
        now_hm = now.strftime('%H:%M')
        for a in new_articles:
            a['time'] = now_hm
            a['collected_at'] = now.isoformat()  # 24時間判定用（Guardian規約）

        # ① タイトル＋要約を1回でまとめて翻訳（失敗分は pending として印を残す）
        for a, r in zip(new_articles, _gen_all(new_articles, _translate_prompt,
                                               TRANSLATE_SCHEMA, _translate_valid,
                                               _translate_fallback)):
            _apply_translation(a, r, now_iso)

        # ② 社会への影響（テクノロジーのみ）
        top_new = [a for a in new_articles if a['category'] == TOP_CATEGORY]
        if top_new:
            for a, imp in zip(top_new, _gen_all(top_new, _impact_prompt, IMPACT_SCHEMA,
                                                _impact_valid, _impact_fallback)):
                if imp:
                    a['impact'] = imp

    day = load_day(today)
    if new_articles:
        day['articles'].extend(new_articles)

    # ③ 保留分（英語のまま残った記事）を、残り予算で再挑戦（新着を処理した後なので新着が優先）
    changed_today = retry_pending(day['articles'], now_iso)

    if new_articles or changed_today:
        day['updated_at'] = now_iso
        save_day(day)

    # 昨日の保留分も救済（日をまたいで残った場合）
    changed_yday = 0
    if os.path.exists(date_path(yesterday)):
        yday = load_day(yesterday)
        changed_yday = retry_pending(yday['articles'], now_iso)
        if changed_yday:
            yday['updated_at'] = now_iso
            save_day(yday)

    expire_guardian()
    cleanup_old_days()
    rebuild_index()

    if not new_articles and not changed_today and not changed_yday:
        print('新着なし・改善なし。')
        return

    pend_left = sum(1 for a in day['articles'] if a.get('pending'))
    print(f'完了（新着{len(new_articles)}件 / テック{len(top_new)}件 / '
          f'改善 今日{changed_today}・昨日{changed_yday}件 / 保留残り{pend_left}件 / '
          f'Gemini呼び出し{_calls_used[0]}回 / {today}）')

    # 英語のまま残った記事が多いときだけLINEで警告（軽微な失敗では鳴らさない）
    if pend_left >= ALERT_THRESHOLD:
        line_alert(f'⚠️ 翻訳できなかった記事が{pend_left}件あります（英語のまま表示中）。\n'
                   f'次回の収集で自動的に再挑戦します。')
    elif 'quota' in gemini_errors:
        line_alert('⚠️ Gemini APIが上限に達した可能性があります。時間をおくと回復します。')


if __name__ == '__main__':
    main()
