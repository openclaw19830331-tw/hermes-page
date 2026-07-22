"""
每日智慧長照文章產生器（GitHub Actions 排程執行）。

流程：Google News RSS 找近期文章 → Gemini 挑一篇並寫約 500 字專欄
→ 抓來源頁 og:image 當配圖（抓不到就用漸層佔位）
→ 產出 posts/YYYY-MM-DD.html、更新 posts/posts.json 與 index.html。

只用 Python 標準庫，不需 pip install。
Gemini key 讀 GEMINI_API_KEY，失敗自動換 GEMINI_API_KEY_2、_3…
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windows 主控台 cp950 印不出 emoji
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = ROOT / "posts"
POSTS_JSON = POSTS_DIR / "posts.json"

TAIPEI = timezone(timedelta(hours=8))
TODAY = datetime.now(TAIPEI).strftime("%Y-%m-%d")

MODEL = "gemini-3.5-flash-lite"

KEYWORDS = [
    "智慧長照",
    "長照科技",
    "AI 長照",
    "高齡科技",
    "長照 2.0",
    "智慧居家照護",
    "失智症 科技",
    "長照 機器人",
]
FRESHNESS_DAYS = 3
PER_KEYWORD = 5

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


# ---------------------------------------------------------------- 新聞候選
def fetch_candidates() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESHNESS_DAYS)
    seen: set[str] = set()
    items: list[dict] = []
    for kw in KEYWORDS:
        params = urllib.parse.urlencode(
            {"q": kw, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh"}
        )
        url = f"https://news.google.com/rss/search?{params}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=15
            ) as resp:
                xml_bytes = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"[warn] RSS 失敗 {kw}: {e}", file=sys.stderr)
            continue

        try:
            from defusedxml import ElementTree as ET  # 防 XXE / billion-laughs
        except ImportError:
            from xml.etree import ElementTree as ET

        root = ET.fromstring(xml_bytes)
        for item in list(root.iterfind(".//item"))[:PER_KEYWORD]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source = (item.findtext(".//{*}source") or "").strip()
            if not title or not link or link in seen:
                continue
            try:
                dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").replace(
                    tzinfo=timezone.utc
                )
                if dt < cutoff:
                    continue
            except ValueError:
                pass
            seen.add(link)
            items.append(
                {"keyword": kw, "title": title, "url": link, "source": source, "published": pub}
            )
    return items


# ---------------------------------------------------------------- Gemini
def gemini_json(prompt: str) -> dict:
    keys = [k for k in [os.environ.get("GEMINI_API_KEY")] if k]
    i = 2
    while os.environ.get(f"GEMINI_API_KEY_{i}"):
        keys.append(os.environ[f"GEMINI_API_KEY_{i}"])
        i += 1
    if not keys:
        raise RuntimeError("GEMINI_API_KEY 未設定")

    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
    ).encode()

    last: Exception | None = None
    for idx, key in enumerate(keys, 1):
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except Exception as e:  # noqa: BLE001 — 換下一把 key
            print(f"[warn] Gemini key #{idx} 失敗: {e}", file=sys.stderr)
            last = e
    raise last  # type: ignore[misc]


def write_article(candidates: list[dict]) -> tuple[dict, dict, list[dict]]:
    listing = "\n".join(
        f"[{i}] {c['title']}（來源：{c['source']}，關鍵字：{c['keyword']}）"
        for i, c in enumerate(candidates)
    )
    prompt = f"""\
你是「馬偕智慧長照」每日專欄的編輯，主題是智慧科技 × 長期照顧，讀者是關心長照的一般大眾與想投入長照產業的人。

以下是近期候選新聞：

{listing}

請完成：
1. 挑出最值得寫成今日專欄的【一篇】（不要挑純廣告、股價、與長照無關的）。
2. 以那篇為主軸寫一篇約 500 字（450-550 字）的繁體中文專欄：
   • 先講新聞重點，再加入你的觀察：這對台灣長照現場、家庭照顧者或想入行的人有什麼意義
   • 平實、專業、有溫度，不誇大療效
   • 分 3-4 段，段落間用兩個換行分隔
3. 給一個吸引人但不聳動的專欄標題（20 字內）。

只輸出 JSON：{{"pick": <編號>, "title": "<標題>", "body": "<內文>"}}
"""
    result = gemini_json(prompt)
    pick = int(result["pick"])
    chosen = candidates[pick]
    related = [c for i, c in enumerate(candidates) if i != pick][:3]
    return result, chosen, related


# ---------------------------------------------------------------- og:image
def find_og_image(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            page = resp.read(400_000).decode("utf-8", errors="replace")
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', page
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', page
        )
        if m and m.group(1).startswith("http"):
            return html.unescape(m.group(1))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] og:image 失敗: {e}", file=sys.stderr)
    return None


# ---------------------------------------------------------------- 每日封面
def render_cover_svg(date: str, keyword: str) -> str:
    """以日期為種子畫一張幾何封面：每天配色/構圖不同，整體風格統一。"""
    seed = int(date.replace("-", ""))
    # 科技又溫馨：色相只在暖青綠～珊瑚橘家族輪替，不出現冷紫藍
    palette = [168, 152, 96, 38, 16]  # teal / sage / 黃綠 / 琥珀 / 珊瑚
    hue = palette[seed % len(palette)]
    circles = []
    x = seed
    for i in range(6):
        x = (x * 1103515245 + 12345) % (2**31)  # 簡單 LCG，確保可重現
        cx, cy = 100 + x % 1000, 60 + (x >> 8) % 380
        r = 40 + (x >> 16) % 110
        op = 0.10 + (i % 3) * 0.07
        circles.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" '
            f'fill="hsl({(hue + i * 25) % 360} 45% 62%)" opacity="{op:.2f}"/>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 500" role="img" aria-label="每日封面">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
  <stop offset="0" stop-color="hsl({hue} 40% 32%)"/>
  <stop offset="1" stop-color="hsl({max(hue - 60, 16)} 55% 48%)"/>
</linearGradient>
<pattern id="dots" width="34" height="34" patternUnits="userSpaceOnUse">
  <circle cx="2" cy="2" r="1.6" fill="#ffffff22"/>
</pattern></defs>
<rect width="1200" height="500" fill="url(#g)"/>
<rect width="1200" height="500" fill="url(#dots)"/>
{"".join(circles)}
<text x="70" y="392" font-family="'Noto Sans TC',sans-serif" font-size="54" fill="#fff" font-weight="700">{html.escape(keyword)}</text>
<text x="70" y="446" font-family="'Noto Sans TC',sans-serif" font-size="28" fill="#ffffffbb">Hermes 智慧長照日報 · {date}</text>
</svg>
"""


# ---------------------------------------------------------------- 頁面渲染
def page(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="../style.css">
</head>
<body>
{body_html}
<footer class="site">此頁由 hermes 機器人每日自動產生 · <a href="../index.html">回首頁</a></footer>
</body>
</html>
"""


def render_post(date: str, art: dict, chosen: dict, related: list[dict], img: str | None) -> str:
    fallback = f'{date}.svg'
    cover = (
        f'<img class="cover" src="{html.escape(img)}" alt="" '
        f"onerror=\"this.src='{fallback}';this.onerror=null\">"
        if img
        else f'<img class="cover" src="{fallback}" alt="每日封面">'
    )
    paras = "".join(
        f"<p>{html.escape(p.strip())}</p>" for p in art["body"].split("\n\n") if p.strip()
    )
    rel_items = "".join(
        f'<li><a href="{html.escape(r["url"])}" target="_blank" rel="noopener">'
        f'{html.escape(r["title"])}</a> <span class="muted">— {html.escape(r["source"])}</span></li>'
        for r in related
    )
    body = f"""<header class="site"><p><a href="../index.html">← Hermes 智慧長照日報</a></p></header>
<article>
  <span class="tag">{html.escape(chosen["keyword"])}</span><time class="muted">{date}</time>
  <h1>{html.escape(art["title"])}</h1>
  {cover}
  {paras}
  <div class="card">
    <strong>📰 本篇主要新聞來源</strong>
    <ul class="links"><li><a href="{html.escape(chosen["url"])}" target="_blank" rel="noopener">{html.escape(chosen["title"])}</a> <span class="muted">— {html.escape(chosen["source"])}</span></li></ul>
    <strong>🔗 相關新聞</strong>
    <ul class="links">{rel_items}</ul>
  </div>
</article>"""
    return page(art["title"], body)


def render_index(posts: list[dict]) -> str:
    latest = posts[0]
    items = "".join(
        f'<li><time>{p["date"]}</time><a href="posts/{p["date"]}.html">{html.escape(p["title"])}</a></li>'
        for p in posts
    )
    body = f"""<header class="site">
  <h1>🛰️ Hermes 智慧長照日報</h1>
  <p>馬偕智慧長照 · 每日一篇智慧科技 × 長期照顧專欄，由 hermes 機器人自動撰寫</p>
</header>
<div class="card">
  <p class="muted">最新一篇 · {latest["date"]}</p>
  <h2 style="margin:.2rem 0 .4rem;font-size:1.2rem"><a href="posts/{latest["date"]}.html">{html.escape(latest["title"])}</a></h2>
</div>
<h2 style="font-size:1.05rem">全部文章</h2>
<ul class="post-list">{items}</ul>"""
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes 智慧長照日報</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
{body}
<footer class="site">狀態：● 運行中（Raspberry Pi + GitHub Actions） · 由 hermes 機器人自動更新</footer>
</body>
</html>
"""


# ---------------------------------------------------------------- 主流程
def main() -> None:
    POSTS_DIR.mkdir(exist_ok=True)
    posts: list[dict] = json.loads(POSTS_JSON.read_text(encoding="utf-8")) if POSTS_JSON.exists() else []

    if any(p["date"] == TODAY for p in posts):
        print(f"今日 {TODAY} 已有文章，跳過")
        return

    used_urls = {p.get("source_url") for p in posts}
    candidates = [c for c in fetch_candidates() if c["url"] not in used_urls]
    if not candidates:
        print("找不到新的候選新聞，今日跳過")
        return
    print(f"候選 {len(candidates)} 篇")

    art, chosen, related = write_article(candidates)
    img = find_og_image(chosen["url"])
    print(f"選定：{chosen['title']}（圖：{'有' if img else '無，用佔位'}）")

    (POSTS_DIR / f"{TODAY}.svg").write_text(
        render_cover_svg(TODAY, chosen["keyword"]), encoding="utf-8"
    )
    (POSTS_DIR / f"{TODAY}.html").write_text(
        render_post(TODAY, art, chosen, related, img), encoding="utf-8"
    )
    posts.insert(0, {"date": TODAY, "title": art["title"], "source_url": chosen["url"]})
    POSTS_JSON.write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "index.html").write_text(render_index(posts), encoding="utf-8")
    print(f"✅ 已產出 posts/{TODAY}.html 並更新 index.html")


if __name__ == "__main__":
    main()
