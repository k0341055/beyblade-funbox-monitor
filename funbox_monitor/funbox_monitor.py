"""
shop.funbox.com.tw 商品偵測器
Cyberbiz /products.json API 直接回傳商品+庫存，無需 Playwright（偵測階段）。
偵測到非 APP 限定商品時，自動登入並完成結帳（Playwright 跑結帳流程）。
"""

import json
import logging
import os
import random
import re
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

FUNBOX_EMAIL = os.environ.get("FUNBOX_EMAIL", "")
FUNBOX_PASSWORD_SITE = os.environ.get("FUNBOX_PASSWORD", "")

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


def _extract_csrf(html: str) -> str:
    for pattern in [
        r'name=["\']authenticity_token["\'][^>]*value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']authenticity_token["\']',
        r'<meta[^>]+name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
    ]:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return ""


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
            continue

        href = item.get("url", "")
        products.append({
            "href": href,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
            "title": item.get("title", "(未知商品)").strip(),
            "price": f"NT${int(variant.get('price', 0))}",
            "inventory": inventory,
            "variant_id": variant.get("id"),
        })

    log.info(f"API 回傳 {len(raw)} 件，有庫存 {len(products)} 件")
    return products


# ─────────────────────────────────────────────
# 自動購買（requests 登入+加購 → Playwright 結帳）
# ─────────────────────────────────────────────


def _requests_login_and_add(variant_id: int) -> tuple:
    """
    用 requests 完成登入 + 加入購物車。
    回傳 (session, cart_url, cookies_list) 或 (None, None, None)。
    """
    sess = requests.Session()
    sess.headers["User-Agent"] = _UA
    try:
        r = sess.get(f"{BASE_URL}/account/login", timeout=10)
        token = _extract_csrf(r.text)
        if not token:
            log.error("登入頁面找不到 CSRF token")
            return None, None, None

        r = sess.post(
            f"{BASE_URL}/account/login",
            data={
                "customer[login]": FUNBOX_EMAIL,
                "customer[password]": FUNBOX_PASSWORD_SITE,
                "authenticity_token": token,
            },
            allow_redirects=True,
            timeout=10,
        )
        if "login" in r.url:
            log.error(f"Funbox 登入失敗，仍停在 {r.url}")
            return None, None, None
        log.info(f"Funbox 登入成功 → {r.url}")

        r = sess.post(
            f"{BASE_URL}/cart/add",
            data={"id": variant_id, "quantity": 1},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code not in (200, 409):
            log.error(f"加入購物車失敗，HTTP {r.status_code}")
            return None, None, None
        log.info(f"已加入購物車（variant_id={variant_id}，HTTP {r.status_code}）")

        r = sess.get(f"{BASE_URL}/cart", allow_redirects=True, timeout=10)
        cart_url = r.url
        cookies_list = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain or "shop.funbox.com.tw",
                "path": c.path or "/",
            }
            for c in sess.cookies
        ]
        return sess, cart_url, cookies_list

    except Exception as e:
        log.error(f"登入/加購例外：{e}")
        return None, None, None


def _playwright_checkout(cart_url: str, cookies_list: list) -> str:
    """
    用 Playwright 完成結帳。
    回傳值："success" | "3ds_pending" | "cart" | "failed"
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright 未安裝，無法執行結帳")
        return "cart"

    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=_UA)
            ctx.add_cookies(cookies_list)
            page = ctx.new_page()

            page.goto(cart_url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            log.info(f"結帳頁 URL：{page.url}")

            # 選擇信用卡付款
            try:
                page.locator("text=信用卡").first.click()
                page.wait_for_timeout(800)
            except Exception:
                pass

            # 選擇已存卡（下拉選第一個選項）
            try:
                sel = page.locator("select").all()
                for s in sel:
                    opts = s.evaluate("el => Array.from(el.options).map(o => o.text)")
                    if opts:
                        s.select_option(index=0)
                        break
            except Exception:
                pass

            # 勾選同意條款 checkbox
            for cb in page.locator("input[type='checkbox']").all():
                try:
                    if cb.is_visible() and not cb.is_checked():
                        cb.check()
                        page.wait_for_timeout(200)
                except Exception:
                    pass

            # 點擊立即結帳
            page.locator("text=立即結帳").last.click()
            log.info("已點擊「立即結帳」，等待跳轉...")

            # 等待跳轉至銀行 3DS 或訂單確認
            for _ in range(20):
                page.wait_for_timeout(1500)
                url = page.url
                if any(k in url for k in ("order", "thank", "complete", "success")):
                    log.info(f"結帳完成！URL={url}")
                    br.close()
                    return "success"
                if any(k in url for k in ("acs.", "challenge", "3ds", "sinopac", "esunbank", "authentication")):
                    log.info(f"3DS 驗證頁面：{url}")
                    br.close()
                    return "3ds_pending"

            log.warning(f"結帳後最終 URL={page.url}，狀態未確認")
            br.close()
            return "cart"

    except Exception as e:
        log.error(f"Playwright 結帳例外：{e}")
        return "cart"


def auto_buy(product: dict) -> str:
    """登入 → 加入購物車 → 結帳。回傳狀態字串。"""
    if not FUNBOX_EMAIL or not FUNBOX_PASSWORD_SITE:
        log.warning("未設定 FUNBOX_EMAIL / FUNBOX_PASSWORD，跳過自動購買")
        return "skipped"

    log.info(f"自動購買啟動：{product['title']}")
    variant_id = product.get("variant_id")
    if not variant_id:
        log.error("找不到 variant_id")
        return "failed"

    _, cart_url, cookies_list = _requests_login_and_add(variant_id)
    if not cart_url:
        return "failed"

    return _playwright_checkout(cart_url, cookies_list)


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────

_BUY_STATUS_LABEL = {
    "success":     "[已自動結帳完成]",
    "3ds_pending": "[訂單已建立，請立即完成銀行 3DS 驗證（OTP 簡訊或 Wallet App）才能完成付款]",
    "cart":        "[已加入購物車，請手動完成結帳]",
    "failed":      "[自動購買失敗，請手動下單]",
    "skipped":     "[未設定自動購買帳密]",
    "app_skip":    "[APP 限定，已略過]",
}


def notify_products(products: list, buy_results: dict = None) -> bool:
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
        if buy_results and p["href"] in buy_results:
            label = _BUY_STATUS_LABEL.get(buy_results[p["href"]], buy_results[p["href"]])
            lines.append(f"購買狀態：{label}")
        lines.append("-" * 40)

    if buy_results and "3ds_pending" in buy_results.values():
        lines += [
            "",
            "⚠ 注意：有訂單需要 3DS 驗證",
            f"請前往訂單頁確認：{BASE_URL}/account/orders",
            "並完成銀行 OTP 簡訊或 Wallet App 驗證以完成付款。",
        ]

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
            buy_results = {}
            for p in to_notify:
                if "APP" in p["title"].upper():
                    log.info(f"APP 限定商品，略過自動購買：{p['title']}")
                    buy_results[p["href"]] = "app_skip"
                else:
                    buy_results[p["href"]] = auto_buy(p)

            log.info(
                f"發送通知：{len(to_notify)} 件"
                f"（共 {len(products)} 件，跳過 {len(products) - len(to_notify)} 件冷卻中）"
            )
            notify_products(to_notify, buy_results)
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
