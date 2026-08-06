"""
1999.co.jp 商品偵測 + Email 通知
偵測到有庫存商品時，依 1 小時冷卻邏輯寄送 Email 給所有 GMAIL_RECIPIENTS。
不自動下單（1999.co.jp 結帳需通過 reCAPTCHA，需人工完成）。
"""

import asyncio
import json
import logging
import os
import random
import smtplib
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

SEARCH_URL = os.environ.get("SEARCH_URL", "").strip()
if not SEARCH_URL:
    raise ValueError("SEARCH_URL 環境變數未設定（請設定 GitHub Variable SEARCH_URL_1999）")
BASE_URL = "https://www.1999.co.jp"
CHECKOUT_URL = "https://www.1999.co.jp/order"

CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_products.json"))
NOTIFY_COOLDOWN = timedelta(hours=1)

TW_TZ = timezone(timedelta(hours=8))

GMAIL_SENDER = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENTS = [
    addr.strip()
    for addr in os.environ["GMAIL_RECIPIENTS"].split(",")
    if addr.strip()
]


def _extract_keyword(url: str) -> str:
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return params.get("searchkey", ["商品"])[0].replace("+", " ")
    except Exception:
        return "商品"


NOTIFY_KEYWORD = os.environ.get("PRODUCT_NAME", _extract_keyword(SEARCH_URL))

# ─────────────────────────────────────────────
# 隨機 UA / Viewport（反 Cloudflare 指紋）
# ─────────────────────────────────────────────

_UA_OS = [
    "Windows NT 10.0; Win64; x64",
    "Windows NT 11.0; Win64; x64",
    "Macintosh; Intel Mac OS X 10_15_7",
    "Macintosh; Intel Mac OS X 13_4",
    "Macintosh; Intel Mac OS X 14_0",
    "X11; Linux x86_64",
]
_UA_CHROME_VERSIONS = list(range(118, 126))
_UA_WEBKIT_BUILD    = list(range(530, 538))


def _random_ua() -> str:
    os_str = random.choice(_UA_OS)
    major  = random.choice(_UA_CHROME_VERSIONS)
    webkit = f"537.{random.choice(_UA_WEBKIT_BUILD)}"
    return (
        f"Mozilla/5.0 ({os_str}) "
        f"AppleWebKit/{webkit} (KHTML, like Gecko) "
        f"Chrome/{major}.0.{random.randint(5000, 7000)}.{random.randint(0, 9)} "
        f"Safari/{webkit}"
    )


def _random_viewport() -> dict:
    return random.choice([
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1366, "height": 768},
        {"width": 1536, "height": 864},
        {"width": 1600, "height": 900},
    ])


def _jitter(base_ms: int, pct: float = 0.3) -> int:
    delta = int(base_ms * pct)
    return base_ms + random.randint(-delta, delta)


async def _wait_cf(page, max_ms: int = 15_000):
    """等待 Cloudflare JS 挑戰自動通過。"""
    try:
        await page.wait_for_function(
            """() => {
                const t = document.title;
                return !t.includes('Just a moment') &&
                       !t.includes('Checking your browser') &&
                       !t.includes('Attention Required') &&
                       !t.includes('ちょっと待ってください') &&
                       !document.querySelector('#challenge-form, .cf-browser-verification');
            }""",
            timeout=max_ms,
        )
    except Exception:
        pass


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[0]}***@{domain}"


# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 狀態管理
# ─────────────────────────────────────────────


def load_notified() -> dict:
    """回傳 {href: 上次廣播通知時間(ISO字串)}"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("notified", {})
    return {}


def save_notified(notified: dict):
    STATE_FILE.write_text(
        json.dumps(
            {"notified": notified, "updated": datetime.now(TW_TZ).isoformat()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _parse_ts(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW_TZ)
    return dt


# ─────────────────────────────────────────────
# 擷取商品清單（Playwright）
# ─────────────────────────────────────────────


async def fetch_products(page) -> list:
    log.info(f"正在載入頁面：{SEARCH_URL}")
    try:
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        log.warning("networkidle 逾時，嘗試繼續解析頁面...")

    await page.wait_for_timeout(_jitter(800))

    try:
        await page.wait_for_selector("div.c-card__info", timeout=10_000)
    except PlaywrightTimeoutError:
        # 先判斷是 Cloudflare 攔截還是單純無有庫存商品
        is_cf = await page.evaluate("""() =>
            document.title.includes('Just a moment') ||
            document.title.includes('Checking') ||
            !!document.querySelector('#challenge-form, .cf-browser-verification')
        """)
        if is_cf:
            log.warning("偵測到 Cloudflare 挑戰，本輪跳過")
        else:
            log.info("目前查無有庫存商品（搜尋結果為空）")
        return []

    cards = await page.query_selector_all("div.c-card__info")
    log.info(f"找到 {len(cards)} 個商品卡")

    products = []
    for card in cards:
        link_el = await card.query_selector("a.c-card__info-links")
        if not link_el:
            continue

        href = (await link_el.get_attribute("href")) or ""

        title_el = await card.query_selector("div.c-card__title")
        title = (await title_el.inner_text()).strip() if title_el else "(タイトル不明)"

        maker_el = await card.query_selector("div.c-card__maker")
        release = (await maker_el.inner_text()).strip() if maker_el else ""

        price_el = await card.query_selector("div.c-card__price-element")
        if price_el:
            span = await price_el.query_selector("span")
            price_num = (await span.inner_text()).strip() if span else ""
            price = f"¥{price_num}" if price_num else (await price_el.inner_text()).strip()
        else:
            price = "価格未定"

        discount_el = await card.query_selector("div.c-card__price-tags-discount span")
        discount = f"{(await discount_el.inner_text()).strip()}%OFF" if discount_el else ""

        products.append({
            "href": href,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
            "title": title,
            "release": release,
            "price": price,
            "discount": discount,
        })

    return products


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────


def _send_email(to: list, subject: str, body: str):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = ", ".join(to)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, to, msg.as_string())
        log.info(f"Email 發送成功 → {[_mask_email(r) for r in to]}")
    except Exception as e:
        log.error(f"Email 發送失敗：{e}")


def send_notify_email(products: list):
    """寄送商品有庫存通知，含購買連結。"""
    count = len(products)
    subject = f"【1999 {NOTIFY_KEYWORD} 補貨！】偵測到 {count} 件商品"
    now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"1999.co.jp 偵測到 {NOTIFY_KEYWORD} 共 {count} 件有庫存商品",
        f"偵測時間：{now_str}（台灣時間）",
        "",
        "=" * 50,
    ]
    for i, p in enumerate(products, 1):
        lines.append(f"\n【商品 {i}】")
        lines.append(f"商品名：{p['title']}")
        if p["release"]:
            lines.append(f"發售日：{p['release']}")
        price_str = p["price"]
        if p["discount"]:
            price_str += f"（{p['discount']}）"
        lines.append(f"價格：{price_str}")
        lines.append(f"商品連結：{p['url']}")
        lines.append("-" * 40)

    lines += [
        "",
        "─ 點擊下方連結前往購物車結帳 ─",
        f"🛒 {CHECKOUT_URL}",
        "",
        f"完整搜尋頁：{SEARCH_URL}",
    ]
    _send_email(GMAIL_RECIPIENTS, subject, "\n".join(lines))


# ─────────────────────────────────────────────
# 核心偵測邏輯（每輪）
# ─────────────────────────────────────────────


async def check_once(page) -> bool:
    try:
        products = await fetch_products(page)

        if not products:
            return False

        now    = datetime.now(TW_TZ)
        cutoff = now - NOTIFY_COOLDOWN
        notified = load_notified()
        current_hrefs = {p["href"] for p in products}

        # 下架商品清除冷卻（重新上架視為新品）
        notified = {h: t for h, t in notified.items() if h in current_hrefs}

        # 廣播冷卻篩選
        to_notify = [
            p for p in products
            if p["href"] not in notified
            or _parse_ts(notified[p["href"]]) < cutoff
        ]

        if to_notify:
            log.info(
                f"發送通知：{len(to_notify)} 件"
                f"（共 {len(products)} 件，跳過 {len(products)-len(to_notify)} 件冷卻中）"
            )
            send_notify_email(to_notify)
            for p in to_notify:
                notified[p["href"]] = now.isoformat()
        else:
            log.info(f"所有 {len(products)} 件商品均在 1 小時冷卻期內")

        save_notified(notified)
        return True

    except Exception as e:
        log.error(f"執行例外：{e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# 主程式：瀏覽器開一次，跑完所有輪次
# ─────────────────────────────────────────────


async def main():
    log.info(f"1999 監控器 | 關鍵字：{NOTIFY_KEYWORD} | 輪數：{CHECK_ROUNDS}")

    ua       = _random_ua()
    viewport = _random_viewport()
    log.info(f"UA: ...{ua[-50:]} | {viewport['width']}x{viewport['height']}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            slow_mo=_jitter(60),
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=ua,
            viewport=viewport,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,zh-TW;q=0.8,en;q=0.7",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        for round_num in range(1, CHECK_ROUNDS + 1):
            if CHECK_ROUNDS > 1:
                log.info(f"── 第 {round_num}/{CHECK_ROUNDS} 輪 ──")
            await check_once(page)
            if round_num < CHECK_ROUNDS:
                wait = random.randint(5, 8)
                log.info(f"等待 {wait} 秒後進行下一輪...")
                await asyncio.sleep(wait)

        await browser.close()

    log.info("所有輪次完成")


if __name__ == "__main__":
    asyncio.run(main())
