"""
shop.funbox.com.tw 商品偵測器
商品為 JS 動態渲染，使用 Playwright。無 Cloudflare，無需反偵測。
"""

import asyncio
import json
import logging
import os
import random
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

SEARCH_URL = "https://shop.funbox.com.tw/collections/%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BA"
BASE_URL = "https://shop.funbox.com.tw"

CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))
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
# 工具函式
# ─────────────────────────────────────────────


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
# 擷取商品清單（Playwright，等待 JS 渲染）
# ─────────────────────────────────────────────


async def fetch_products(page) -> list:
    log.info(f"正在載入頁面：{SEARCH_URL}")
    try:
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        log.warning("networkidle 逾時，嘗試繼續解析頁面...")

    # SPA 渲染需要額外時間，等商品卡實際出現
    try:
        await page.wait_for_selector(".collection_products .product", timeout=20_000)
    except PlaywrightTimeoutError:
        log.warning("找不到商品卡，頁面可能尚未渲染或結構已變更")
        return []

    # 商品資料都在 a.productClick 的 data-* 屬性中
    links = await page.query_selector_all(".collection_products .product a.productClick")
    log.info(f"找到 {len(links)} 個商品")

    products = []
    for link_el in links:
        href = (await link_el.get_attribute("href")) or ""
        name = (await link_el.get_attribute("data-name")) or "(未知商品)"
        raw_price = (await link_el.get_attribute("data-price")) or ""

        # data-price 為浮點數字串（如 "4100.0"），轉為整數後加 NT$
        try:
            price = f"NT${int(float(raw_price))}"
        except (ValueError, TypeError):
            price = raw_price

        products.append({
            "href": href,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
            "title": name,
            "price": price,
        })

    return products


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────


def notify_products(products: list) -> bool:
    count = len(products)
    subject = f"【Funbox 商品通知】偵測到 {count} 件商品"

    lines = [
        f"Funbox 官網偵測到共 {count} 件商品",
        f"偵測時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "=" * 50,
    ]
    for i, p in enumerate(products, 1):
        lines.append(f"\n【商品 {i}】")
        lines.append(f"商品名：{p['title']}")
        lines.append(f"價格：{p['price']}")
        lines.append(f"商品連結：{p['url']}")
        lines.append("-" * 40)

    lines += [
        "",
        f"完整商品頁：{SEARCH_URL}",
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
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            products = await fetch_products(page)

            if not products:
                log.warning("未擷取到任何商品，跳過本輪")
                return False

            now = datetime.now()
            cutoff = now - NOTIFY_COOLDOWN
            notified = load_notified()

            to_notify = [
                p for p in products
                if p["href"] not in notified
                or datetime.fromisoformat(notified[p["href"]]) < cutoff
            ]

            if to_notify:
                log.info(f"發送通知：{len(to_notify)} 件（共 {len(products)} 件，跳過 {len(products)-len(to_notify)} 件冷卻中）")
                notify_products(to_notify)
                for p in to_notify:
                    notified[p["href"]] = now.isoformat()
            else:
                log.info(f"所有 {len(products)} 件商品均在 1 小時冷卻期內，不重複通知")

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
    log.info(f"Funbox 商品偵測器 | 輪數：{CHECK_ROUNDS}")
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
