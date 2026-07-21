"""
shop.funbox.com.tw 商品偵測器
Cyberbiz /products.json API 直接回傳商品+庫存，無需 Playwright。
"""

import json
import logging
import os
import random
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

COLLECTION_URL = os.environ.get(
    "SEARCH_URL",
    "https://shop.funbox.com.tw/collections/%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BA",
)
API_URL = f"{COLLECTION_URL}/products.json"
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[0]}***@{domain}"


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
# 擷取商品清單（Cyberbiz collections API）
# ─────────────────────────────────────────────


def fetch_products() -> list:
    resp = requests.get(API_URL, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    products = []
    for item in raw:
        variant = (item.get("variants") or [{}])[0]
        inventory = int(variant.get("inventory_quantity", 0))
        if inventory <= 0:
            continue  # 缺貨，跳過

        href = item.get("url", "")
        products.append({
            "href": href,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
            "title": item.get("title", "(未知商品)").strip(),
            "price": f"NT${int(variant.get('price', 0))}",
            "inventory": inventory,
        })

    log.info(f"API 回傳 {len(raw)} 件，有庫存 {len(products)} 件")
    return products


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────


def notify_products(products: list) -> bool:
    count = len(products)
    subject = f"【Funbox 有貨了！】偵測到 {count} 件商品"

    lines = [
        f"Funbox 官網偵測到共 {count} 件有庫存商品",
        f"偵測時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "=" * 50,
    ]
    for i, p in enumerate(products, 1):
        lines.append(f"\n【商品 {i}】")
        lines.append(f"商品名：{p['title']}")
        lines.append(f"價格：{p['price']}")
        lines.append(f"庫存：{p['inventory']} 件")
        lines.append(f"商品連結：{p['url']}")
        lines.append("-" * 40)

    lines += ["", f"完整商品頁：{COLLECTION_URL}"]
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


def check_once() -> bool:
    try:
        products = fetch_products()

        if not products:
            log.info("目前無庫存商品，繼續監控")
            return True

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
            log.info(f"所有 {len(products)} 件商品均在 1 小時冷卻期內")

        current_hrefs = {p["href"] for p in products}
        notified = {h: t for h, t in notified.items() if h in current_hrefs}
        save_notified(notified)
        return True

    except requests.HTTPError as e:
        log.error(f"HTTP 錯誤：{e}")
        return False
    except Exception as e:
        log.error(f"執行例外：{e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────


def main():
    log.info(f"Funbox 商品偵測器 | 輪數：{CHECK_ROUNDS}")
    for round_num in range(1, CHECK_ROUNDS + 1):
        if CHECK_ROUNDS > 1:
            log.info(f"── 第 {round_num}/{CHECK_ROUNDS} 輪 ──")
        check_once()
        if round_num < CHECK_ROUNDS:
            wait = random.randint(3, 5)
            log.info(f"等待 {wait} 秒後進行下一輪...")
            time.sleep(wait)
    log.info("所有輪次完成")


if __name__ == "__main__":
    main()
