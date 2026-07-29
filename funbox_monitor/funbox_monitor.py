"""
shop.funbox.com.tw 商品偵測器
- 偵測：Cyberbiz /products.json API，每輪 < 1 秒
- 下單：多帳號平行執行，每件商品加 3 個入購物車，全部加完後一次結帳
- 付款：7-11 貨到付款（避免信用卡 3DS 卡單）
"""

import json
import logging
import os
import random
import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
CART_QTY = 3  # 每件商品目標加入數量

GMAIL_SENDER = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENTS = [
    addr.strip()
    for addr in os.environ["GMAIL_RECIPIENTS"].split(",")
    if addr.strip()
]

# 支援多組帳號：FUNBOX_EMAIL / FUNBOX_EMAIL_2
FUNBOX_ACCOUNTS = [
    (e, p)
    for e, p in [
        (os.environ.get("FUNBOX_EMAIL", ""), os.environ.get("FUNBOX_PASSWORD", "")),
        (os.environ.get("FUNBOX_EMAIL_2", ""), os.environ.get("FUNBOX_PASSWORD_2", "")),
    ]
    if e and p
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
        title = item.get("title", "(未知商品)").strip()
        price = f"NT${int(variant.get('price', 0))}"
        products.append({
            "href": href,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
            "title": title,
            "price": price,
            "inventory": inventory,
            "variant_id": variant.get("id"),
        })
        log.info(f"有庫存 → {title} | 庫存:{inventory} 件 | {price}")

    log.info(f"API 回傳 {len(raw)} 件，有庫存 {len(products)} 件")
    return products


# ─────────────────────────────────────────────
# 登入 + 加入購物車（requests）
# ─────────────────────────────────────────────


def _login_and_fill_cart(email: str, password: str, products: list) -> tuple:
    """
    登入並將所有商品加入購物車（每件先試 CART_QTY，失敗則試 1）。
    回傳 (added_hrefs, cart_url, cookies_list) 或 ([], None, None)。
    """
    sess = requests.Session()
    sess.headers["User-Agent"] = _UA
    try:
        r = sess.get(f"{BASE_URL}/account/login", timeout=10)
        token = _extract_csrf(r.text)
        if not token:
            log.error(f"[{email}] 登入頁找不到 CSRF token")
            return [], None, None

        r = sess.post(
            f"{BASE_URL}/account/login",
            data={
                "customer[login]": email,
                "customer[password]": password,
                "authenticity_token": token,
            },
            allow_redirects=True,
            timeout=10,
        )
        if "login" in r.url:
            log.error(f"[{email}] 登入失敗，停在 {r.url}")
            return [], None, None
        log.info(f"[{email}] 登入成功")

        added = []
        for p in products:
            vid = p["variant_id"]
            target_qty = min(CART_QTY, p["inventory"])

            r2 = sess.post(
                f"{BASE_URL}/cart/add",
                data={"id": vid, "quantity": target_qty},
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                timeout=10,
            )
            if r2.status_code in (200, 409):
                log.info(f"[{email}] 加入購物車：{p['title']} x{target_qty}")
                added.append(p["href"])
            else:
                # 限購或其他原因，退而求其次加 1 個
                r3 = sess.post(
                    f"{BASE_URL}/cart/add",
                    data={"id": vid, "quantity": 1},
                    headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                    timeout=10,
                )
                if r3.status_code in (200, 409):
                    log.info(f"[{email}] 加入購物車：{p['title']} x1（受限）")
                    added.append(p["href"])
                else:
                    log.error(f"[{email}] 無法加入購物車：{p['title']} (HTTP {r3.status_code})")

        if not added:
            log.error(f"[{email}] 所有商品均加入失敗")
            return [], None, None

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
        log.info(f"[{email}] 購物車 URL：{cart_url}，共加入 {len(added)} 件商品")
        return added, cart_url, cookies_list

    except Exception as e:
        log.error(f"[{email}] 登入/加購例外：{e}")
        return [], None, None


# ─────────────────────────────────────────────
# Playwright 結帳（7-11 貨到付款）
# ─────────────────────────────────────────────


def _playwright_checkout(email: str, cart_url: str, cookies_list: list) -> str:
    """
    用 Playwright 完成結帳，選 7-11 貨到付款。
    回傳："success" | "cart" | "3ds_pending" | "failed"
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(f"[{email}] Playwright 未安裝")
        return "cart"

    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=_UA)
            ctx.add_cookies(cookies_list)
            page = ctx.new_page()

            page.goto(cart_url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            log.info(f"[{email}] 結帳頁：{page.url}")

            # 選擇 7-11 貨到付款
            clicked_711 = False
            for sel in [
                "button[id^='cvs-shipping-button']",
                "button:has-text('7-11 貨到付款')",
                "[data-translate-keys='cod_names.seven']",
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1000)
                        log.info(f"[{email}] 已選擇 7-11 貨到付款")
                        clicked_711 = True
                        break
                except Exception:
                    continue
            if not clicked_711:
                log.warning(f"[{email}] 找不到 7-11 按鈕，繼續嘗試結帳")

            # 套用優惠券（若有可用）
            try:
                coupon_open = page.locator("button.button-block:has-text('選擇優惠券或輸入優惠碼')").first
                if coupon_open.count() > 0 and coupon_open.is_visible():
                    coupon_open.click()
                    page.wait_for_timeout(1500)
                    # 選第一張可用的優惠券
                    first_coupon = page.locator("[data-testid='checkable-radio']").first
                    if first_coupon.count() > 0 and first_coupon.is_visible():
                        first_coupon.click()
                        page.wait_for_timeout(500)
                        confirm_btn = page.locator("button.confirm-btn").first
                        if confirm_btn.count() > 0 and confirm_btn.is_visible():
                            confirm_btn.click()
                            page.wait_for_timeout(1000)
                            log.info(f"[{email}] 已套用優惠券")
                        else:
                            log.warning(f"[{email}] 找不到優惠券確認按鈕")
                    else:
                        log.info(f"[{email}] 無可用優惠券，關閉選單")
                        # 按 Escape 或再按一次關閉
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
            except Exception as e:
                log.warning(f"[{email}] 優惠券套用例外：{e}")

            # 勾選所有同意條款 checkbox
            for cb in page.locator("input[type='checkbox']").all():
                try:
                    if cb.is_visible() and not cb.is_checked():
                        cb.check()
                        page.wait_for_timeout(200)
                except Exception:
                    pass

            # 點擊立即結帳
            page.locator("text=立即結帳").last.click()
            log.info(f"[{email}] 已點擊立即結帳，等待跳轉...")

            # 輪詢結果（7-11 COD 不經過 3DS，通常直接到訂單確認頁）
            for _ in range(20):
                page.wait_for_timeout(1500)
                url = page.url
                if any(k in url for k in ("order", "thank", "complete", "success")):
                    log.info(f"[{email}] 結帳完成：{url}")
                    br.close()
                    return "success"
                # 萬一仍觸發 3DS（備用偵測）
                if any(k in url for k in ("acs.", "challenge", "3ds", "sinopac", "esunbank", "authentication")):
                    log.warning(f"[{email}] 觸發非預期 3DS：{url}")
                    br.close()
                    return "3ds_pending"

            log.warning(f"[{email}] 結帳後停在：{page.url}")
            br.close()
            return "cart"

    except Exception as e:
        log.error(f"[{email}] Playwright 例外：{e}")
        return "cart"


# ─────────────────────────────────────────────
# 單一帳號完整流程
# ─────────────────────────────────────────────


def _checkout_for_account(email: str, password: str, products: list) -> dict:
    """登入 → 全部加入購物車 → 一次結帳。"""
    added, cart_url, cookies_list = _login_and_fill_cart(email, password, products)
    if not cart_url:
        return {"added": [], "checkout": "failed"}
    checkout = _playwright_checkout(email, cart_url, cookies_list)
    return {"added": added, "checkout": checkout}


# ─────────────────────────────────────────────
# 多帳號平行執行
# ─────────────────────────────────────────────


def auto_buy_all(products: list) -> dict:
    """
    對所有非 APP 商品，以每個帳號平行下單。
    回傳 {email: {"added": [...hrefs...], "checkout": status}}
    """
    if not FUNBOX_ACCOUNTS:
        log.warning("未設定任何 FUNBOX 帳號，跳過自動購買")
        return {}

    non_app = [p for p in products if "APP" not in p["title"].upper()]
    if not non_app:
        log.info("所有商品均為 APP 限定，略過自動購買")
        return {}

    log.info(f"自動購買啟動：{len(non_app)} 件商品 × {len(FUNBOX_ACCOUNTS)} 組帳號")

    results = {}
    if len(FUNBOX_ACCOUNTS) == 1:
        email, pwd = FUNBOX_ACCOUNTS[0]
        results[email] = _checkout_for_account(email, pwd, non_app)
    else:
        with ThreadPoolExecutor(max_workers=len(FUNBOX_ACCOUNTS)) as executor:
            futures = {
                executor.submit(_checkout_for_account, email, pwd, non_app): email
                for email, pwd in FUNBOX_ACCOUNTS
            }
            for future in as_completed(futures):
                email = futures[future]
                try:
                    results[email] = future.result()
                except Exception as e:
                    log.error(f"[{email}] 執行緒例外：{e}")
                    results[email] = {"added": [], "checkout": "failed"}
    return results


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────

_CHECKOUT_LABEL = {
    "success":     "✅ 已自動結帳完成",
    "3ds_pending": "⚠ 訂單已建立（需完成銀行 3DS 驗證才能付款）",
    "cart":        "🛒 商品已在購物車，請手動完成結帳",
    "failed":      "❌ 自動購買失敗，請手動下單",
}


def notify_products(products: list, account_results: dict = None) -> bool:
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

        if "APP" in p["title"].upper():
            lines.append("購買狀態：[APP 限定，已略過]")
        elif account_results:
            for email, result in account_results.items():
                status = "已加入購物車" if p["href"] in result.get("added", []) else "加入失敗"
                lines.append(f"  ▸ {email}：{status}")

        lines.append("-" * 40)

    if account_results:
        lines += ["", "=" * 50, "各帳號結帳結果", "=" * 50]
        for email, result in account_results.items():
            label = _CHECKOUT_LABEL.get(result.get("checkout", ""), result.get("checkout", ""))
            lines.append(f"▸ {email}：{label}")

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
            account_results = auto_buy_all(to_notify)
            log.info(
                f"發送通知：{len(to_notify)} 件"
                f"（共 {len(products)} 件，跳過 {len(products) - len(to_notify)} 件冷卻中）"
            )
            notify_products(to_notify, account_results)
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
    log.info(f"Funbox 商品偵測器 | 輪數：{CHECK_ROUNDS} | 帳號數：{len(FUNBOX_ACCOUNTS)}")
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
