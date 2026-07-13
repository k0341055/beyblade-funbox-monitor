"""
1999.co.jp Beyblade X 新商品偵測器
偵測 https://www.1999.co.jp/search?typ1_c=100&cat=&searchkey=beyblade+X&sortid=7&soldout=0
當有新商品出現時，發送 Email 通知。

使用 Playwright 以繞過 Cloudflare 瀏覽器指紋偵測。
"""

import asyncio
import json
import logging
import os
import random
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

SEARCH_URL = (
    "https://www.1999.co.jp/search"
    "?typ1_c=100&cat=&searchkey=beyblade+X&sortid=7&soldout=0"
)
BASE_URL = "https://www.1999.co.jp"

CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_products.json"))
NOTIFY_COOLDOWN = timedelta(hours=1)

GMAIL_SENDER = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENTS = [
    addr.strip()
    for addr in os.environ["GMAIL_RECIPIENTS"].split(",")
    if addr.strip()
]

# ─────────────────────────────────────────────
# 隨機 User-Agent / Viewport（反 Cloudflare 指紋）
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
_UA_WEBKIT_BUILD = list(range(530, 538))


def _random_ua() -> str:
    os_str = random.choice(_UA_OS)
    major = random.choice(_UA_CHROME_VERSIONS)
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
# 狀態管理（1 小時冷卻）
# ─────────────────────────────────────────────


def load_notified() -> dict:
    """回傳 {href: 上次通知時間(ISO字串)}"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("notified", {})
    return {}


def save_notified(notified: dict):
    STATE_FILE.write_text(
        json.dumps(
            {"notified": notified, "updated": datetime.now().isoformat()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )



# ─────────────────────────────────────────────
# 擷取商品清單（Playwright）
# ─────────────────────────────────────────────


async def fetch_products(page) -> list[dict]:
    log.info(f"正在載入頁面：{SEARCH_URL}")
    try:
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        # networkidle 逾時時頁面可能仍已載入，嘗試繼續
        log.warning("networkidle 逾時，嘗試繼續解析頁面...")

    await page.wait_for_timeout(_jitter(800))

    # 確認商品卡存在
    try:
        await page.wait_for_selector("div.c-card__info", timeout=10_000)
    except PlaywrightTimeoutError:
        log.warning("找不到商品卡，可能遭 Cloudflare 封鎖或頁面結構已變更")
        screenshot_path = f"/tmp/beyblade_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            log.info(f"Debug 截圖已存：{screenshot_path}")
        except Exception:
            pass
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


def notify_new_products(new_products: list[dict]) -> bool:
    count = len(new_products)
    subject = f"【BEYBLADE X 商品通知】偵測到 {count} 件商品"

    lines = [
        f"1999.co.jp 偵測到 BEYBLADE X 共 {count} 件商品",
        f"偵測時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "=" * 50,
    ]
    for i, p in enumerate(new_products, 1):
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
        f"完整搜尋頁：{SEARCH_URL}",
    ]
    body = "\n".join(lines)

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = ", ".join(GMAIL_RECIPIENTS)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENTS, msg.as_string())
        masked = [_mask_email(r) for r in GMAIL_RECIPIENTS]
        log.info(f"Email 發送成功 → {masked}")
        return True
    except Exception as e:
        log.error(f"Email 發送失敗：{e}")
        return False


# ─────────────────────────────────────────────
# 核心偵測邏輯
# ─────────────────────────────────────────────


async def check_once() -> bool:
    """
    回傳 True = 本輪正常完成；False = 擷取失敗
    首次執行只建立基準，不發通知；後續發現新 href 才通知。
    """
    ua = _random_ua()
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
        # 移除 headless 瀏覽器的 webdriver 特徵，避免被 Cloudflare 偵測
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        try:
            products = await fetch_products(page)

            if not products:
                log.warning("未擷取到任何商品，跳過本輪")
                return False

            now = datetime.now()
            cutoff = now - NOTIFY_COOLDOWN
            notified = load_notified()

            # 只通知「從未通知過」或「上次通知超過 1 小時」的商品
            to_notify = [
                p for p in products
                if p["href"] not in notified
                or datetime.fromisoformat(notified[p["href"]]) < cutoff
            ]

            if to_notify:
                log.info(f"發送通知：{len(to_notify)} 件（共 {len(products)} 件，已跳過 {len(products)-len(to_notify)} 件冷卻中）")
                notify_new_products(to_notify)
                for p in to_notify:
                    notified[p["href"]] = now.isoformat()
            else:
                log.info(f"所有 {len(products)} 件商品均在 1 小時冷卻期內，不重複通知")

            # 清理已下架商品（下次上架時視為新品重新通知）
            current_hrefs = {p["href"] for p in products}
            notified = {h: t for h, t in notified.items() if h in current_hrefs}
            save_notified(notified)
            return True

        except Exception as e:
            log.error(f"執行例外：{e}", exc_info=True)
            return False
        finally:
            await browser.close()


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────


async def main():
    log.info(f"Beyblade X 新商品偵測器 | 輪數：{CHECK_ROUNDS}")
    for round_num in range(1, CHECK_ROUNDS + 1):
        if CHECK_ROUNDS > 1:
            log.info(f"── 第 {round_num}/{CHECK_ROUNDS} 輪 ──")
        await check_once()
        if round_num < CHECK_ROUNDS:
            wait = random.randint(5, 8)
            log.info(f"等待 {wait} 秒後進行下一輪...")
            await asyncio.sleep(wait)
    log.info("所有輪次完成")


if __name__ == "__main__":
    asyncio.run(main())
