"""
誠品網路書店 Beyblade X 戰鬥陀螺監控
- 偵測：athena.eslite.com book_exhibits API（Cloudflare 保護，需 Playwright 瀏覽器取得）
- 通知：Gmail SMTP
- 冷卻：同款商品 1 小時內最多通知一次；庫存歸零即清除，重新上架立即通知
"""

import json
import logging
import os
import random
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

API_URL = os.environ.get(
    "ESLITE_API_URL",
    "https://athena.eslite.com/api/v1/book_exhibits/CU202503-00091",
)
ESLITE_BASE = "https://www.eslite.com"

CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))
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

# 略過的商品關鍵字（名稱含此字串則跳過通知），逗號分隔
SKIP_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get("ESLITE_SKIP_KEYWORDS", "UX-14").split(",")
    if kw.strip()
]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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
# 商品擷取（遞迴解析 API）
# ─────────────────────────────────────────────


def _extract_products(data: dict) -> dict:
    """從誠品專區 API 回應中遞迴抽取所有商品，以 product_guid 為 key。"""
    products = {}

    def add_product(p):
        if not isinstance(p, dict):
            return
        guid = p.get("product_guid")
        name = p.get("name")
        if not guid or not name:
            return
        try:
            stock = int(p.get("stock"))
        except (TypeError, ValueError):
            stock = None
        products[str(guid)] = {
            "name": name,
            "status": (
                p.get("status")
                or p.get("product_button_status")
                or "unknown"
            ),
            "stock": stock,
            "account_qty_limit": p.get("account_qty_limit"),  # 帳號購買上限
            "order_qty_limit": p.get("order_qty_limit"),      # 每單購買上限
            "image": p.get("image", ""),
            "url": f"{ESLITE_BASE}/product/{guid}",
        }

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if value.get("product_guid") and value.get("name"):
                add_product(value)
            for child in value.values():
                walk(child)

    walk(data)
    return products


def fetch_products() -> list:
    """
    用 Playwright 瀏覽器取得 API（繞過 Cloudflare），
    遞迴解析後回傳有庫存且不在略過清單的商品列表。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        ctx = br.new_context(user_agent=_UA)
        page = ctx.new_page()
        page.goto(API_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        # API 回應以 <pre> 標籤包住 JSON
        try:
            raw = page.inner_text("pre")
        except Exception:
            raw = page.inner_text("body")
        br.close()

    data = json.loads(raw)
    all_products = _extract_products(data)

    result = []
    for guid, p in all_products.items():
        name = p["name"]
        stock = p["stock"]

        # 略過指定商品
        if any(kw.upper() in name.upper() for kw in SKIP_KEYWORDS):
            log.info(f"略過：{name}")
            continue

        # 庫存為 0 或無法判斷 → 跳過
        if not stock or stock <= 0:
            continue

        limit_info = f"帳號上限:{p['account_qty_limit']}件" if p.get("account_qty_limit") else "無限購"
        log.info(f"有庫存 → {name} | 庫存:{stock} 件 | {limit_info} | {p['status']}")
        result.append({"guid": guid, **p})

    log.info(f"API 抽取 {len(all_products)} 件，有庫存 {len(result)} 件")
    return result


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────


def notify_products(products: list) -> bool:
    count = len(products)
    subject = f"【誠品 Beyblade X 有貨了！】偵測到 {count} 件商品"

    lines = [
        f"誠品戰鬥陀螺專區偵測到共 {count} 件有庫存商品",
        f"偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
        "",
        "=" * 50,
    ]

    for i, p in enumerate(products, 1):
        acct_lim = p.get("account_qty_limit")
        ord_lim = p.get("order_qty_limit")
        lines.append(f"\n【商品 {i}】")
        lines.append(f"商品名：{p['name']}")
        lines.append(f"庫存：{p['stock']} 件")
        lines.append(f"帳號上限：{'無限制' if acct_lim is None else f'{acct_lim} 件'}")
        lines.append(f"每單上限：{'無限制' if ord_lim is None else f'{ord_lim} 件'}")
        lines.append(f"商品連結：{p['url']}")
        lines.append("-" * 40)

    lines += ["", f"完整專區頁：{ESLITE_BASE}/event/CU202503-00091"]
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
        now = datetime.now(TW_TZ)
        cutoff = now - NOTIFY_COOLDOWN

        notified = load_notified()
        current_guids = {p["guid"] for p in products}
        # 庫存歸零的商品立即從 seen_products 移除，下次上架視為新品
        notified = {g: t for g, t in notified.items() if g in current_guids}

        if not products:
            log.info("目前無庫存商品，繼續監控")
            save_notified(notified)
            return True

        to_notify = [
            p for p in products
            if p["guid"] not in notified
            or _parse_ts(notified[p["guid"]]) < cutoff
        ]

        if to_notify:
            log.info(
                f"發送通知：{len(to_notify)} 件"
                f"（共 {len(products)} 件，跳過 {len(products) - len(to_notify)} 件冷卻中）"
            )
            notify_products(to_notify)
            for p in to_notify:
                notified[p["guid"]] = now.isoformat()
        else:
            log.info(f"所有 {len(products)} 件商品均在 1 小時冷卻期內")

        save_notified(notified)
        return True

    except Exception as e:
        log.error(f"執行例外：{e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────


def main():
    log.info(f"誠品 Beyblade X 監控器 | 輪數：{CHECK_ROUNDS} | 略過：{SKIP_KEYWORDS}")
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
