"""
1999.co.jp Beyblade X 新商品偵測 + 自動加入購物車
偵測 https://www.1999.co.jp/search?typ1_c=100&cat=&searchkey=beyblade+X&sortid=7&soldout=0
有庫存時：
  - 廣播通知（1 小時冷卻）→ 所有 GMAIL_RECIPIENTS
  - 個人加購通知（每輪）→ 只寄給帳號本人（SITE_1999_EMAIL）
每輪均嘗試加入購物車，直到商品下架為止。
1999 平台限制：每款 Beyblade X 限購 1 件，程式不嘗試更改數量。
"""

import asyncio
import json
import logging
import os
import random
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

SEARCH_URL = os.environ.get(
    "SEARCH_URL",
    "https://www.1999.co.jp/search"
    "?typ1_c=100&cat=&searchkey=beyblade+X&sortid=7&soldout=0",
)
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

# 1999.co.jp 帳號（單一）
SITE_EMAIL    = os.environ.get("SITE_1999_EMAIL", "")
SITE_PASSWORD = os.environ.get("SITE_1999_PASSWORD", "")

# 加入購物車後頁面出現這些文字代表成功
_CART_SUCCESS = ["カートに入れました", "カートに追加しました", "ショッピングカートに入れました", "数量変更"]
# 這些代表無法加入（售完或限購攔截）
_CART_FAIL    = ["売り切れ", "品切れ", "カートに入れません", "購入できません", "在庫なし"]

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
# 登入
# ─────────────────────────────────────────────


async def do_login(page) -> bool:
    """登入 1999.co.jp，成功回傳 True。"""
    try:
        await page.goto("https://www.1999.co.jp/login", wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1_000)
        await page.locator("#txtUserID").fill(SITE_EMAIL)
        await page.locator("#txtUserPW").fill(SITE_PASSWORD)
        await page.get_by_role("button", name="ログイン").click()
        await page.wait_for_load_state("networkidle", timeout=15_000)
        if "login" in page.url.lower():
            log.error("登入失敗：仍在登入頁面")
            return False
        log.info(f"1999 登入成功 → {page.url}")
        return True
    except Exception as e:
        log.error(f"登入例外：{e}")
        return False


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
        log.warning("找不到商品卡，可能遭 Cloudflare 封鎖或頁面結構已變更")
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
# 加入購物車
# ─────────────────────────────────────────────


async def add_to_cart_single(page, product: dict) -> str:
    """
    導航到商品頁並點擊 カートに入れる。
    回傳：'success' | 'sold_out' | 'not_found' | 'error'
    """
    try:
        await page.goto(product["url"], wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(_jitter(1_200))

        # nth(1) 為商品詳情頁的實體按鈕（nth(0) 通常是 header 的靜態文字）
        clicked = False
        for nth in [1, 0]:
            try:
                btn = page.get_by_text("カートに入れる").nth(nth)
                cnt = await btn.count()
                if cnt > 0 and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(2_000)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            log.warning(f"找不到 カートに入れる 按鈕：{product['title']}")
            return "not_found"

        # 判斷結果
        try:
            body = await page.locator("body").inner_text(timeout=2_000)
        except Exception:
            body = ""

        if any(kw in body for kw in _CART_FAIL):
            log.warning(f"商品無法加入（售完或限購）：{product['title']}")
            return "sold_out"

        log.info(f"加入購物車成功：{product['title']}")
        return "success"

    except Exception as e:
        log.error(f"加入購物車例外：{product['title']} | {e}")
        return "error"


async def add_to_cart_all(page, products: list) -> dict:
    """
    對所有商品嘗試加入購物車。
    回傳 {href: status_str}
    """
    results = {}
    for p in products:
        log.info(f"  → 嘗試加購：{p['title']}")
        status = await add_to_cart_single(page, p)
        results[p["href"]] = status
    return results


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────

_CART_LABEL = {
    "success":  "✅ 已加入購物車",
    "sold_out": "❌ 無法加入（售完或限購）",
    "not_found":"⚠ 找不到加購按鈕",
    "error":    "❌ 加入時發生例外",
}


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


def send_broadcast_email(products: list, cart_results: dict):
    """廣播通知：寄給全體 GMAIL_RECIPIENTS（冷卻後才發）。"""
    count = len(products)
    subject = f"【1999 Beyblade X 補貨！】偵測到 {count} 件商品"
    now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"1999.co.jp 偵測到 Beyblade X 共 {count} 件有庫存商品",
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
        if cart_results:
            label = _CART_LABEL.get(cart_results.get(p["href"], ""), "")
            lines.append(f"加購狀態：{label}")
        lines.append("-" * 40)

    if cart_results:
        n_ok = sum(1 for s in cart_results.values() if s == "success")
        lines += [
            "",
            f"自動加購結果：{n_ok}/{len(cart_results)} 件成功加入購物車",
            f"📧 詳細加購結果已個別寄送至 {_mask_email(SITE_EMAIL)}",
        ]

    lines += [
        "",
        "─ 點擊下方連結手動結帳 ─",
        f"🛒 {CHECKOUT_URL}",
        "",
        f"完整搜尋頁：{SEARCH_URL}",
    ]
    _send_email(GMAIL_RECIPIENTS, subject, "\n".join(lines))


def send_personal_cart_email(products: list, cart_results: dict):
    """個人加購通知：只寄給 SITE_EMAIL（若在收件人名單中）。"""
    if SITE_EMAIL not in GMAIL_RECIPIENTS:
        return

    n_ok   = sum(1 for s in cart_results.values() if s == "success")
    n_fail = len(cart_results) - n_ok
    now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    subject = f"【1999 你的加購結果】{n_ok} 件成功 / {n_fail} 件失敗"

    lines = [
        f"帳號：{SITE_EMAIL}",
        f"偵測時間：{now_str}（台灣時間）",
        "",
        "=" * 50,
        "各商品加購狀態",
        "=" * 50,
    ]
    for p in products:
        status = cart_results.get(p["href"], "—")
        label  = _CART_LABEL.get(status, status)
        lines.append(f"▸ {p['title']}：{label}")
        lines.append(f"  連結：{p['url']}")

    lines += [
        "",
        "─ 點擊下方連結手動結帳 ─",
        f"🛒 {CHECKOUT_URL}",
    ]
    _send_email([SITE_EMAIL], subject, "\n".join(lines))


# ─────────────────────────────────────────────
# 核心偵測邏輯（每輪）
# ─────────────────────────────────────────────


async def check_once(page, logged_in: bool) -> bool:
    try:
        products = await fetch_products(page)

        if not products:
            log.warning("未擷取到任何商品，跳過本輪")
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

        # 每輪對所有在庫商品嘗試加入購物車
        cart_results = {}
        if logged_in:
            log.info(f"開始加入購物車：{len(products)} 件")
            cart_results = await add_to_cart_all(page, products)

        # 廣播通知（冷卻限制）
        if to_notify:
            log.info(
                f"發送廣播通知：{len(to_notify)} 件"
                f"（共 {len(products)} 件，跳過 {len(products)-len(to_notify)} 件冷卻中）"
            )
            send_broadcast_email(to_notify, cart_results)
            for p in to_notify:
                notified[p["href"]] = now.isoformat()
        else:
            log.info(f"所有 {len(products)} 件商品均在 1 小時冷卻期內")

        # 個人加購通知（每輪，只要有嘗試加購就發）
        if cart_results:
            send_personal_cart_email(products, cart_results)

        save_notified(notified)
        return True

    except Exception as e:
        log.error(f"執行例外：{e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# 主程式：瀏覽器開一次，登入一次，跑完所有輪次
# ─────────────────────────────────────────────


async def main():
    log.info(f"1999 Beyblade X 監控器 | 輪數：{CHECK_ROUNDS}")

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

        # 登入一次，之後每輪復用同一 session
        logged_in = False
        if SITE_EMAIL and SITE_PASSWORD:
            logged_in = await do_login(page)
            if not logged_in:
                log.warning("登入失敗，本次執行僅偵測，不自動加購")
        else:
            log.warning("未設定 SITE_1999_EMAIL / SITE_1999_PASSWORD，跳過自動加購")

        for round_num in range(1, CHECK_ROUNDS + 1):
            if CHECK_ROUNDS > 1:
                log.info(f"── 第 {round_num}/{CHECK_ROUNDS} 輪 ──")
            await check_once(page, logged_in)
            if round_num < CHECK_ROUNDS:
                wait = random.randint(5, 8)
                log.info(f"等待 {wait} 秒後進行下一輪...")
                await asyncio.sleep(wait)

        await browser.close()

    log.info("所有輪次完成")


if __name__ == "__main__":
    asyncio.run(main())
