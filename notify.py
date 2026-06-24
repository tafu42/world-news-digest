"""1日2回実行：ニュースを更新したことをLINEに1通だけ知らせる。"""
import os
import json
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_USER_ID = os.getenv('LINE_USER_ID')

JST = timezone(timedelta(hours=9))
DATA_DIR = os.path.join('docs', 'data')
INDEX_FILE = os.path.join(DATA_DIR, 'index.json')
SITE_URL = 'https://tafu42.github.io/world-news-digest/'


def latest_day():
    if not os.path.exists(INDEX_FILE):
        return None
    with open(INDEX_FILE, 'r', encoding='utf-8') as f:
        dates = json.load(f).get('dates', [])
    for d in dates:
        p = os.path.join(DATA_DIR, f'{d}.json')
        if not os.path.exists(p):
            continue
        with open(p, 'r', encoding='utf-8') as f:
            day = json.load(f)
        if day.get('articles'):
            return day
    return None


def line_push(payload):
    res = requests.post('https://api.line.me/v2/bot/message/push',
                        headers={'Content-Type': 'application/json',
                                 'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'},
                        json=payload)
    res.raise_for_status()


def build_flex(day):
    _, m, d = day['date'].split('-')
    label = f'{int(m)}月{int(d)}日'
    count = len(day.get('articles', []))
    top = next((a for a in day['articles'] if a.get('category') == '重大ニュース'), None)
    headline = top['title'] if top else ''
    body = [
        {"type": "text", "text": f"{label}のニュースを更新しました", "size": "sm", "color": "#333333", "wrap": True},
        {"type": "text", "text": f"新着 {count}件", "size": "xs", "color": "#888888", "margin": "sm"},
    ]
    if headline:
        body.append({"type": "text", "text": f"🔥 {headline}", "size": "sm", "color": "#E53935",
                     "weight": "bold", "wrap": True, "margin": "md"})
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A1A2E", "paddingAll": "16px",
                   "contents": [{"type": "text", "text": "🌍 World News Digest", "color": "#FFFFFF",
                                 "weight": "bold", "size": "lg"}]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "12px",
                   "contents": [{"type": "button", "style": "primary", "color": "#1E88E5",
                                 "action": {"type": "uri", "label": "サイトで読む", "uri": SITE_URL}}]},
    }


def main():
    day = latest_day()
    if not day:
        print('送信できるデータがないため通知をスキップ。')
        return
    line_push({'to': LINE_USER_ID,
               'messages': [{'type': 'flex', 'altText': 'ニュースを更新しました', 'contents': build_flex(day)}]})
    print(f'LINE通知を送信しました（{day["date"]}）。')


if __name__ == '__main__':
    main()
