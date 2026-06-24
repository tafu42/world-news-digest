# 🌍 World News Digest

国際ニュースを自動収集し、AIで日本語に翻訳・要約して、スマホで「ショート動画」のように
1記事ずつ読めるニュースアプリ。重大ニュースには「社会への影響」をAIが分析して添えます。

**公開サイト:** https://tafu42.github.io/world-news-digest/

---

## 特長

- **自動運用**：GitHub Actions が1日3回ニュースを収集、1日2回 LINE に更新通知（PC不要）。
- **AI翻訳・要約**：The Guardian の英語ニュースを Gemini で日本語に翻訳・要約。
- **社会への影響**：重大ニュースには Gemini が「起こりうる社会への影響」を生成。
- **ショート風UI**：1記事＝1画面、上下スワイプで次の記事へ（画像つき）。
- **静的サイト**：GitHub Pages 配信。サーバー管理不要・無料運用。

## システム構成

```
[GitHub Actions（cron）]
  ├─ 収集 3回/日 ── main.py
  │     ├─ The Guardian API から取得（タイトル・要約・画像・カテゴリ）
  │     ├─ Gemini：日本語へ翻訳・要約／重大ニュースは社会への影響を生成
  │     ├─ 重複チェック（当日＋前日のURL）
  │     └─ docs/data/YYYY-MM-DD.json に追記
  └─ 通知 2回/日 ── notify.py → LINE（更新＋サイトURL）
                    │
                    ▼
        [GitHub Pages（docs/）] ← 静的サイト（ショートUI）がJSONを読んで表示
```

## 使用技術

| 区分 | 技術 |
|------|------|
| 言語 | Python 3.11 / HTML・CSS・JavaScript |
| 自動実行 | GitHub Actions（cron） |
| ホスティング | GitHub Pages |
| ニュース | The Guardian Open Platform API |
| AI | Gemini API（翻訳・要約・影響分析） |
| 通知 | LINE Messaging API |
| 秘匿情報 | GitHub Secrets / .env |

## ライセンス・出典

ニュースは [The Guardian Open Platform](https://open-platform.theguardian.com/) を
**非商用**で利用し、サイトに出典表示と元記事リンクを掲載しています。
