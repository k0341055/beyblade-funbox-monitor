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
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

COLLECTION_URL = os.environ.get("SEARCH_URL", "").strip()
if not COLLECTION_URL:
    raise ValueError("SEARCH_URL 環境變數未設定（請設定 GitHub Variable FUNBOX_SEARCH_URL）")
API_URL = f"{COLLECTION_URL}/products.json"
BASE_URL = "https://shop.funbox.com.tw"

CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_products.json"))
NOTIFY_COOLDOWN = timedelta(hours=1)
CART_QTY = 3          # 每件商品目標加入數量
MAX_BUY_PRODUCTS = int(os.environ.get("MAX_BUY_PRODUCTS", "0"))  # 0 = 不限制
TEST_MODE = os.environ.get("TEST_MODE", "").strip() not in ("", "0", "false")

TW_TZ = timezone(timedelta(hours=8))  # 台灣時區 UTC+8

GMAIL_SENDER = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENTS = [
    addr.strip()
    for addr in os.environ["GMAIL_RECIPIENTS"].split(",")
    if addr.strip()
]

# 支援多組帳號：FUNBOX_EMAIL / FUNBOX_EMAIL_2 / FUNBOX_EMAIL_3
FUNBOX_ACCOUNTS = [
    (e, p)
    for e, p in [
        (os.environ.get("FUNBOX_EMAIL", ""), os.environ.get("FUNBOX_PASSWORD", "")),
        (os.environ.get("FUNBOX_EMAIL_2", ""), os.environ.get("FUNBOX_PASSWORD_2", "")),
        (os.environ.get("FUNBOX_EMAIL_3", ""), os.environ.get("FUNBOX_PASSWORD_3", "")),
    ]
    if e and p
]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 全域 Session — 讓 750 輪之間重用 TCP/TLS connection
FUNBOX_SESSION = requests.Session()
FUNBOX_SESSION.headers.update({
    "User-Agent": _UA,
    "Accept": "application/json",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": COLLECTION_URL,
})
FUNBOX_SESSION.mount(
    "https://",
    HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0),
)

# 隨機強化組 / 抽抽包關鍵字（這類商品通常限購 3，其他一般款限購 1）
_RANDOM_KEYWORDS = ["隨機強化組", "抽抽包", "RANDOM BOOSTER", "RANDOM"]

# Playwright 結帳頁若出現以下文字，代表 CYBERBIZ 在結帳層級攔截限購
_CHECKOUT_LIMIT_TEXTS = ["最多只能購買", "達購買上限", "超過購買限制", "超出限購", "purchase limit"]

# 結帳時商品已售完（庫存在加入購物車後被搶走，URL 不會變但頁面有提示文字）
_CHECKOUT_STOCK_OUT_TEXTS = ["庫存不足", "數量不足", "已無庫存", "商品已售完", "無法結帳", "庫存已不足", "商品已下架", "out of stock"]

# ── 完全靜默的商品關鍵字（不通知、不下單）──────────────
SKIP_KEYWORDS: list[str] = [
    "app",
    "APP",
]

# ── 通知但不自動購買的商品關鍵字 ──────────────────────
SKIP_BUY_KEYWORDS: list[str] = [
    "BX-33",
    "BX-26",
    "暴風天馬",
    "銀牙烈虎",
    "烈焰飛鳳",
]


def is_random_product(p: dict) -> bool:
    """隨機強化組 / 抽抽包類商品，每輪都嘗試購買（限購較寬，通常 3 個）。"""
    title = p["title"].upper()
    return any(k.upper() in title for k in _RANDOM_KEYWORDS)

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
# 狀態管理（1 小時冷卻 + 已購紀錄）
# ─────────────────────────────────────────────


def load_state() -> tuple:
    """
    回傳 (notified, purchased, attempts)。
    notified:  {href: ISO時間戳}              ← 1 小時通知冷卻
    purchased: {href: [帳號email清單]}        ← 已成功結帳
    attempts:  {href: {帳號email: 次數}}      ← 失敗嘗試次數（≥2 則不再試）
    """
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return (
            data.get("notified", {}),
            data.get("purchased", {}),
            data.get("attempts", {}),
        )
    return {}, {}, {}


def save_state(notified: dict, purchased: dict, attempts: dict):
    STATE_FILE.write_text(
        json.dumps(
            {
                "notified": notified,
                "purchased": purchased,
                "attempts": attempts,
                "updated": datetime.now(TW_TZ).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _parse_ts(ts_str: str) -> datetime:
    """解析 ISO 時間字串，若無時區資訊則補上台灣時區。"""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW_TZ)
    return dt


# ─────────────────────────────────────────────
# 擷取商品清單（Cyberbiz collections API）
# ─────────────────────────────────────────────


def fetch_products() -> list:
    raw = None
    last_error = None

    for attempt in range(1, 5):
        try:
            resp = FUNBOX_SESSION.get(API_URL, timeout=(8, 15))

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < 4:
                    retry_after = resp.headers.get("Retry-After")
                    wait = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt) + random.uniform(0, 1)
                    log.warning(f"Funbox API HTTP {resp.status_code} ({attempt}/4)，{wait:.1f} 秒後重試")
                    time.sleep(wait)
                    continue

            resp.raise_for_status()
            raw = resp.json()
            break

        except (requests.ConnectTimeout, requests.ReadTimeout, requests.ConnectionError) as e:
            last_error = e
            if attempt < 4:
                wait = (2 ** (attempt - 1)) + random.uniform(0, 1)
                log.warning(f"Funbox API 連線失敗 ({attempt}/4)：{type(e).__name__}，{wait:.1f} 秒後重試")
                time.sleep(wait)
            else:
                log.warning(f"Funbox API 連續 {attempt} 次連線失敗，本輪跳過：{e}")

    if raw is None:
        raise last_error or requests.ConnectionError("Funbox API 未取得資料")

    if isinstance(raw, dict):
        raw = raw.get("products", [raw])

    if not isinstance(raw, list):
        raise ValueError(f"Funbox API 格式異常：{type(raw).__name__}")

    products = []
    for item in raw:
        variant = (item.get("variants") or [{}])[0]
        inventory = int(variant.get("inventory_quantity", 0) or 0)
        if inventory <= 0:
            continue

        href = item.get("url", "")
        title = item.get("title", "(未知商品)").strip()
        price = f"NT${int(variant.get('price', 0) or 0)}"
        raw_qc = variant.get("qc")
        qty_cap = int(raw_qc) if raw_qc is not None else None

        products.append({
            "href": href,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
            "title": title,
            "price": price,
            "inventory": inventory,
            "variant_id": variant.get("id"),
            "qty_cap": qty_cap,
        })
        cap_info = f"限購:{qty_cap}件/單" if qty_cap is not None else "無限購"
        log.info(f"有庫存 → {title} | 庫存:{inventory} 件 | {cap_info} | {price}")

    log.info(f"API 回傳 {len(raw)} 件，有庫存 {len(products)} 件")
    return products


# ─────────────────────────────────────────────
# 智慧加購數量決策
# ─────────────────────────────────────────────


def get_target_qty(p: dict) -> int:
    """
    根據商品資訊決定加入購物車的目標數量。

    優先順序：
    1. API 回傳 qc（每筆訂單上限）→ 最優先，直接 min(CART_QTY, 庫存, qc)
    2. 商品名含隨機強化組 / 抽抽包關鍵字 → 限購通常 3，允許買 3
    3. 其他一般戰鬥陀螺 → 麗嬰/Funbox 實際限購規則多為每款 1，保守預設 1

    CYBERBIZ 的「會員累計限購」可能在結帳才檢查，qc=null 不代表真的不限購，
    因此一般款採保守策略，優先確保訂單成立，而非搶加最多。
    """
    inventory = p["inventory"]
    # qty_cap = p.get("qty_cap")
    title = p["title"].upper()

    # if qty_cap is not None:
        # qty = max(1, min(CART_QTY, inventory, qty_cap))
        # log.info(f"  數量決策：API qc={qty_cap} → {qty} 件")
        # return qty

    # if any(k.upper() in title for k in _RANDOM_KEYWORDS):
    if is_random_product(p):
        qty = min(3, inventory)
        log.info(f"  數量決策：隨機強化組/抽抽包 → {qty} 件")
        return qty

    # 一般陀螺保守預設 1（符合麗嬰 1/1/3 限購政策）
    qty = min(1, inventory)
    log.info(f"  數量決策：一般陀螺保守 → {qty} 件")
    return qty


# ─────────────────────────────────────────────
# 登入 + 逐件加購結帳（requests + Playwright）
# ─────────────────────────────────────────────


def _checkout_for_account(email: str, password: str, products: list) -> dict:
    """
    登入一次 → 逐件商品：清空購物車 → 加一件 → 立即結帳。
    回傳 {"added": [...hrefs], "attempted": [...hrefs], "checkout": {href: status}}。
    """
    import time as _time
    attempted = [p["href"] for p in products]

    # ── 登入 ──
    t0 = _time.perf_counter()
    sess = requests.Session()
    sess.headers["User-Agent"] = _UA
    try:
        r = sess.get(f"{BASE_URL}/account/login", timeout=10)
        token = _extract_csrf(r.text)
        if not token:
            log.error(f"[{email}] 登入頁找不到 CSRF token")
            return {"added": [], "attempted": attempted, "checkout": {}}
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
            return {"added": [], "attempted": attempted, "checkout": {}}
        log.info(f"[{email}] ⏱ 登入成功（{_time.perf_counter()-t0:.2f}s）")
    except Exception as e:
        log.error(f"[{email}] 登入例外：{e}")
        return {"added": [], "attempted": attempted, "checkout": {}}

    added = []
    checkout_statuses = {}

    for p in products:
        t_product = _time.perf_counter()
        vid = p["variant_id"]
        target_qty = get_target_qty(p)
        log.info(f"[{email}] ── 開始處理：{p['title']} ──")

        # ── 清空購物車 ──
        t1 = _time.perf_counter()
        try:
            cart_js = sess.get(f"{BASE_URL}/cart.js", timeout=10).json()
            updates = {str(item["variant_id"]): 0 for item in cart_js.get("items", [])}
            if updates:
                sess.post(f"{BASE_URL}/cart/update.js", json={"updates": updates}, timeout=10)
                log.info(f"[{email}] ⏱ 購物車已清空（{len(updates)} 項，{_time.perf_counter()-t1:.2f}s）")
            else:
                log.info(f"[{email}] ⏱ 購物車原本為空（{_time.perf_counter()-t1:.2f}s）")
        except Exception as e:
            log.warning(f"[{email}] 購物車清空失敗：{e}")

        # ── 加入購物車 ──
        t2 = _time.perf_counter()
        r2 = sess.post(
            f"{BASE_URL}/cart/add",
            data={"id": vid, "quantity": target_qty},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            timeout=30,
        )
        if r2.status_code == 200:
            log.info(f"[{email}] ⏱ 加入購物車：{p['title']} x{target_qty}（{_time.perf_counter()-t2:.2f}s）")
        elif target_qty > 1:
            log.warning(f"[{email}] 加購 x{target_qty} 失敗：{p['title']} | HTTP {r2.status_code}")
            r3 = sess.post(
                f"{BASE_URL}/cart/add",
                data={"id": vid, "quantity": 1},
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                timeout=30,
            )
            if r3.status_code == 200:
                log.info(f"[{email}] ⏱ 加入購物車：{p['title']} x1（降量重試，{_time.perf_counter()-t2:.2f}s）")
            else:
                log.error(f"[{email}] 無法加入購物車：{p['title']} (HTTP {r3.status_code})")
                checkout_statuses[p["href"]] = "failed"
                continue
        else:
            log.error(f"[{email}] 無法加入購物車：{p['title']} (HTTP {r2.status_code})")
            checkout_statuses[p["href"]] = "failed"
            continue

        added.append(p["href"])

        # ── 取得購物車 URL ──
        t3 = _time.perf_counter()
        try:
            r_cart = sess.get(f"{BASE_URL}/cart", allow_redirects=True, timeout=30)
            cart_url = r_cart.url
            if "/carts/" not in cart_url:
                log.error(f"[{email}] 購物車頁面異常（{cart_url}）")
                checkout_statuses[p["href"]] = "failed"
                continue
            log.info(f"[{email}] ⏱ 購物車 URL 取得（{_time.perf_counter()-t3:.2f}s）：{cart_url}")
        except Exception as e:
            log.error(f"[{email}] 取得購物車 URL 例外：{e}")
            checkout_statuses[p["href"]] = "failed"
            continue

        cookies_list = [
            {"name": c.name, "value": c.value,
             "domain": c.domain or "shop.funbox.com.tw", "path": c.path or "/"}
            for c in sess.cookies
        ]

        # ── Playwright 結帳 ──
        t4 = _time.perf_counter()
        status = _playwright_checkout(email, cart_url, cookies_list)
        checkout_statuses[p["href"]] = status
        log.info(
            f"[{email}] ⏱ 結帳完成：{p['title']} → {status}"
            f"（Playwright {_time.perf_counter()-t4:.2f}s｜本件合計 {_time.perf_counter()-t_product:.2f}s）"
        )

    return {"added": added, "attempted": attempted, "checkout": checkout_statuses}


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

    import time as _time
    t_pw_start = _time.perf_counter()

    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=_UA)
            ctx.add_cookies(cookies_list)
            page = ctx.new_page()

            t1 = _time.perf_counter()
            page.goto(cart_url, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(1500)
            log.info(f"[{email}] ⏱ 結帳頁載入（{_time.perf_counter()-t1:.2f}s）：{page.url}")
            if "/carts/" not in page.url:
                log.error(f"[{email}] 結帳頁面異常（{page.url}），中止結帳")
                br.close()
                return "cart"

            # 選擇 7-11 取貨（貨到付款或先付款均可）
            t2 = _time.perf_counter()
            clicked_711 = False
            for sel in [
                "button[id^='cvs-shipping-button']",
                "button:has-text('7-11 貨到付款')",
                "button:has-text('7-11取貨')",
                "button:has-text('7-11')",
                "[data-translate-keys='cod_names.seven']",
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1000)
                        log.info(f"[{email}] ⏱ 7-11 按鈕點擊（{_time.perf_counter()-t2:.2f}s）：{btn.inner_text()[:30].strip()}")
                        clicked_711 = True
                        break
                except Exception:
                    continue
            if not clicked_711:
                log.warning(f"[{email}] ⏱ 找不到 7-11 按鈕（{_time.perf_counter()-t2:.2f}s），繼續嘗試結帳")

            # 勾選所有同意條款 checkbox
            for cb in page.locator("input[type='checkbox']").all():
                try:
                    if cb.is_visible() and not cb.is_checked():
                        cb.check()
                        page.wait_for_timeout(200)
                except Exception:
                    pass

            # 點擊立即結帳
            t3 = _time.perf_counter()
            clicked_checkout = False
            for sel in [
                "a.btn-lg:has-text('立即結帳')",
                "button.btn-lg:has-text('立即結帳')",
                "a.btn-default:has-text('立即結帳')",
                "button.btn-default:has-text('立即結帳')",
                ".btn-bloc:has-text('立即結帳')",
            ]:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.scroll_into_view_if_needed()
                        loc.click()
                        log.info(f"[{email}] ⏱ 立即結帳點擊（{_time.perf_counter()-t3:.2f}s），等待跳轉...")
                        clicked_checkout = True
                        break
                except Exception:
                    continue
            if not clicked_checkout:
                log.error(f"[{email}] 找不到結帳按鈕，中止")
                br.close()
                return "cart"

            # 輪詢結果
            t4 = _time.perf_counter()
            for poll in range(1, 21):
                page.wait_for_timeout(1500)
                url = page.url
                if any(k in url for k in ("order", "thank", "complete", "success")):
                    if "show_failed" in url:
                        log.warning(f"[{email}] ⏱ 付款失敗 show_failed（輪詢第{poll}次，{_time.perf_counter()-t4:.2f}s）：{url}")
                        br.close()
                        return "payment_failed"
                    log.info(f"[{email}] ⏱ 結帳確認頁（輪詢第{poll}次，{_time.perf_counter()-t4:.2f}s）：{url}")
                    br.close()
                    return "success"
                if any(k in url for k in ("acs.", "challenge", "3ds", "sinopac", "esunbank", "authentication")):
                    log.warning(f"[{email}] ⏱ 3DS 觸發（輪詢第{poll}次，{_time.perf_counter()-t4:.2f}s）：{url}")
                    br.close()
                    return "3ds_pending"
                try:
                    body_text = page.locator("body").inner_text(timeout=500)
                    if any(kw in body_text for kw in _CHECKOUT_LIMIT_TEXTS):
                        log.warning(f"[{email}] ⏱ 限購攔截（輪詢第{poll}次，{_time.perf_counter()-t4:.2f}s）：{body_text[:200]}")
                        br.close()
                        return "checkout_limited"
                    if any(kw in body_text for kw in _CHECKOUT_STOCK_OUT_TEXTS):
                        log.warning(f"[{email}] ⏱ 商品售完（輪詢第{poll}次，{_time.perf_counter()-t4:.2f}s）：{body_text[:200]}")
                        br.close()
                        return "stock_out"
                except Exception:
                    pass

            log.warning(f"[{email}] ⏱ 輪詢逾時（{_time.perf_counter()-t4:.2f}s），停在：{page.url}")
            br.close()
            return "cart"

    except Exception as e:
        log.error(f"[{email}] Playwright 例外：{e}")
        return "cart"


# ─────────────────────────────────────────────
# 單一帳號完整流程
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# 多帳號平行執行
# ─────────────────────────────────────────────


def auto_buy_all(products: list, purchased: dict, attempts: dict) -> dict:
    """
    對所有非 APP 商品，以每個帳號平行下單。

    purchased: {href: [已成功購買的帳號 email 清單]}
    attempts:  {href: {帳號email: 失敗次數}}
    - 已成功購買過 → 跳過
    - 失敗嘗試 ≥ 2 次 → 跳過（第三次看到同一商品不再浪費時間）
    - 其餘 → 嘗試下單

    回傳 {email: {"added": [...hrefs...], "attempted": [...], "checkout": status}}
    """
    if not FUNBOX_ACCOUNTS:
        log.warning("未設定任何 FUNBOX 帳號，跳過自動購買")
        return {}

    non_app = [
        p for p in products
        if "APP" not in p["title"].upper()
        and not any(kw.upper() in p["title"].upper() for kw in SKIP_BUY_KEYWORDS)
    ]
    if not non_app:
        log.info("所有商品均為 APP 限定或在 SKIP_BUY_KEYWORDS 中，略過自動購買")
        return {}

    if MAX_BUY_PRODUCTS > 0 and len(non_app) > MAX_BUY_PRODUCTS:
        log.info(f"MAX_BUY_PRODUCTS={MAX_BUY_PRODUCTS}，僅購買前 {MAX_BUY_PRODUCTS} 件")
        non_app = non_app[:MAX_BUY_PRODUCTS]

    def _products_for_account(email: str) -> list:
        """
        已成功購買 → 跳過；失敗嘗試 ≥ 2 → 跳過；其餘都試。
        一般款與隨機包邏輯相同（第一次成功後就不再重複，失敗兩次後放棄）。
        """
        result = []
        for p in non_app:
            href = p["href"]
            if email in purchased.get(href, []):
                log.info(f"[{email}] 已成功購買，跳過：{p['title']}")
                continue
            if attempts.get(href, {}).get(email, 0) >= 2:
                log.info(f"[{email}] 已嘗試 2 次失敗，跳過：{p['title']}")
                continue
            result.append(p)
        return result

    log.info(f"自動購買啟動：{len(non_app)} 件商品 × {len(FUNBOX_ACCOUNTS)} 組帳號")

    results = {}
    if len(FUNBOX_ACCOUNTS) == 1:
        email, pwd = FUNBOX_ACCOUNTS[0]
        acct_products = _products_for_account(email)
        if acct_products:
            results[email] = _checkout_for_account(email, pwd, acct_products)
        else:
            log.info(f"[{email}] 本輪無需購買的商品，跳過")
    else:
        with ThreadPoolExecutor(max_workers=len(FUNBOX_ACCOUNTS)) as executor:
            futures = {}
            for email, pwd in FUNBOX_ACCOUNTS:
                acct_products = _products_for_account(email)
                if acct_products:
                    futures[executor.submit(_checkout_for_account, email, pwd, acct_products)] = email
                else:
                    log.info(f"[{email}] 本輪無需購買的商品，跳過")
            for future in as_completed(futures):
                email = futures[future]
                try:
                    results[email] = future.result()
                except Exception as e:
                    log.error(f"[{email}] 執行緒例外：{e}")
                    results[email] = {"added": [], "attempted": [], "checkout": "failed"}
    return results


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────

_CHECKOUT_LABEL = {
    "success":          "✅ 已自動結帳完成",
    "payment_failed":   "❌ 訂單已建立但付款失敗（show_failed），請手動完成付款",
    "3ds_pending":      "⚠ 訂單已建立（需完成銀行 3DS 驗證才能付款）",
    "checkout_limited": "🚫 結帳時被 CYBERBIZ 限購攔截，請手動調整數量後結帳",
    "stock_out":        "⚡ 結帳時商品已售完（已被其他人搶走）",
    "cart":             "🛒 商品已在購物車，請手動完成結帳",
    "failed":           "❌ 自動購買失敗，請手動下單",
}


def _send_email(to: list, subject: str, body: str) -> bool:
    """底層 SMTP 發送，to 為收件人清單。"""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = ", ".join(to)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, to, msg.as_string())
        log.info(f"Email 發送成功 → {[_mask_email(r) for r in to]}")
        return True
    except Exception as e:
        log.error(f"Email 發送失敗：{e}")
        return False


def _broadcast_email(products: list, account_results: dict = None) -> None:
    """
    廣播通知：寄給全體 GMAIL_RECIPIENTS。
    包含商品資訊與各帳號結帳摘要（不含個別帳號的詳細加購狀態）。
    """
    count = len(products)
    test_tag = "【測試】" if TEST_MODE else ""
    subject = f"{test_tag}【Funbox 有貨了！】偵測到 {count} 件商品"

    lines = [
        f"Funbox 官網偵測到共 {count} 件有庫存商品",
        f"偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
    ]
    if TEST_MODE:
        lines.append("⚠ 本封為測試信件，請勿當作正式上架通知")
    lines += ["", "=" * 50]

    for i, p in enumerate(products, 1):
        lines.append(f"\n【商品 {i}】")
        lines.append(f"商品名：{p['title']}")
        lines.append(f"價格：{p['price']}")
        lines.append(f"庫存：{p['inventory']} 件")
        cap = p.get("qty_cap")
        lines.append(f"API 限購：{'無限制' if cap is None else f'{cap} 件/筆'}")
        lines.append(f"實際加購：{get_target_qty(p)} 件/帳號")
        lines.append(f"商品連結：{p['url']}")
        if any(kw.upper() in p["title"].upper() for kw in SKIP_BUY_KEYWORDS):
            lines.append("購買狀態：[略過自動購買]")
        lines.append("-" * 40)

    if account_results:
        lines += ["", "=" * 50, "自動下單摘要", "=" * 50]
        for acct_email, result in account_results.items():
            checkout = result.get("checkout", {})
            n_added = len(result.get("added", []))
            n_tried = len(result.get("attempted", []))
            if isinstance(checkout, dict) and checkout:
                for href, status in checkout.items():
                    label = _CHECKOUT_LABEL.get(status, status)
                    p_obj = next((p for p in products if p["href"] == href), None)
                    title_short = p_obj["title"][:25] if p_obj else href[:25]
                    lines.append(f"▸ {acct_email} | {title_short}：{label}")
            else:
                lines.append(f"▸ {acct_email}：（加入 {n_added}/{n_tried} 件，無結帳結果）")
        lines.append("")
        lines.append("📧 各帳號詳細結帳資訊已個別寄送至對應信箱")

    lines += ["", f"完整商品頁：{COLLECTION_URL}"]
    _send_email(GMAIL_RECIPIENTS, subject, "\n".join(lines))


def _personal_checkout_email(acct_email: str, result: dict, products: list) -> None:
    """
    個人結帳通知：只寄給該 Funbox 帳號本人（前提是該 email 在 GMAIL_RECIPIENTS 中）。
    包含該帳號每件商品的逐件結帳結果。
    """
    checkout = result.get("checkout", {})
    test_tag = "【測試】" if TEST_MODE else ""

    statuses = list(checkout.values()) if isinstance(checkout, dict) else []
    if "success" in statuses:
        overall_label = _CHECKOUT_LABEL["success"]
    elif "3ds_pending" in statuses:
        overall_label = _CHECKOUT_LABEL["3ds_pending"]
    elif "payment_failed" in statuses:
        overall_label = _CHECKOUT_LABEL["payment_failed"]
    elif statuses:
        overall_label = _CHECKOUT_LABEL.get(statuses[0], statuses[0])
    else:
        overall_label = "未完成"

    subject = f"{test_tag}【Funbox 你的結帳結果】{overall_label}"
    has_pending = any(s in ("3ds_pending", "payment_failed") for s in statuses)

    lines = [
        f"帳號：{acct_email}",
        f"偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
        "",
        "=" * 50,
        "各商品結帳結果",
        "=" * 50,
    ]

    for p in products:
        if any(kw.upper() in p["title"].upper() for kw in SKIP_BUY_KEYWORDS):
            status_str = "— 略過自動購買"
        elif isinstance(checkout, dict) and p["href"] in checkout:
            status_str = _CHECKOUT_LABEL.get(checkout[p["href"]], checkout[p["href"]])
        else:
            status_str = "— 未嘗試"
        lines.append(f"▸ {p['title']}：{status_str}")

    lines += ["", f"完整商品頁：{COLLECTION_URL}"]

    if has_pending:
        lines += [
            "",
            "⚠ 訂單已建立但付款未完成，請盡快前往補繳，否則訂單將自動取消：",
            f"  {BASE_URL}/account/orders",
        ]

    _send_email([acct_email], subject, "\n".join(lines))


def notify_products(products: list, account_results: dict = None) -> bool:
    """
    廣播通知 → 全體 GMAIL_RECIPIENTS（商品資訊 + 下單摘要）
    個人通知 → 各 Funbox 帳號本人（自己的詳細加購 + 結帳結果）
    """
    _broadcast_email(products, account_results)
    if account_results:
        for acct_email, result in account_results.items():
            if acct_email in GMAIL_RECIPIENTS:
                _personal_checkout_email(acct_email, result, products)
    return True


# ─────────────────────────────────────────────
# 核心偵測邏輯
# ─────────────────────────────────────────────


def check_once() -> bool:
    try:
        products = fetch_products()

        # 略過 SKIP_KEYWORDS 商品（不通知、不下單）
        if SKIP_KEYWORDS:
            before = len(products)
            products = [
                p for p in products
                if not any(kw.upper() in p["title"].upper() for kw in SKIP_KEYWORDS)
            ]
            skipped = before - len(products)
            if skipped:
                log.info(f"略過 {skipped} 件符合 SKIP_KEYWORDS 的商品")

        now = datetime.now(TW_TZ)
        cutoff = now - NOTIFY_COOLDOWN

        notified, purchased, attempts = load_state()
        current_hrefs = {p["href"] for p in products}

        # 庫存歸零 → 清除所有紀錄（重新上架視為新品、可重新購買）
        notified  = {h: t       for h, t       in notified.items()  if h in current_hrefs}
        purchased = {h: accts   for h, accts   in purchased.items() if h in current_hrefs}
        attempts  = {h: acct_cx for h, acct_cx in attempts.items()  if h in current_hrefs}

        if not products:
            log.info("目前無庫存商品，繼續監控")
            save_state(notified, purchased, attempts)
            return True

        to_notify = [
            p for p in products
            if p["href"] not in notified
            or _parse_ts(notified[p["href"]]) < cutoff
        ]

        if to_notify:
            account_results = auto_buy_all(to_notify, purchased, attempts)

            for acct_email, result in account_results.items():
                checkout = result.get("checkout", {})
                for href, status in checkout.items():
                    if status == "success":
                        purchased.setdefault(href, [])
                        if acct_email not in purchased[href]:
                            purchased[href].append(acct_email)
                            p_obj = next((x for x in to_notify if x["href"] == href), None)
                            log.info(f"已記錄購買成功：{acct_email} → {p_obj['title'] if p_obj else href}")
                    else:
                        if acct_email not in purchased.get(href, []):
                            attempts.setdefault(href, {})
                            attempts[href][acct_email] = attempts[href].get(acct_email, 0) + 1
                            log.info(f"[{acct_email}] 記錄失敗嘗試 #{attempts[href][acct_email]}：{href}")

            log.info(
                f"發送通知：{len(to_notify)} 件"
                f"（共 {len(products)} 件，跳過 {len(products) - len(to_notify)} 件冷卻中）"
            )
            notify_products(to_notify, account_results)
            for p in to_notify:
                notified[p["href"]] = now.isoformat()
        else:
            log.info(f"所有 {len(products)} 件商品均在 1 小時冷卻期內")

        save_state(notified, purchased, attempts)
        return True

    except requests.Timeout as e:
        log.warning(f"Funbox API 連線逾時：{e}")
        return False
    except requests.ConnectionError as e:
        log.warning(f"Funbox API 連線失敗：{e}")
        return False
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "未知"
        log.warning(f"Funbox API HTTP 錯誤：HTTP {status} | {e}")
        return False
    except Exception:
        log.exception("check_once 發生非預期程式錯誤")
        raise


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────


def main():
    log.info(f"Funbox 商品偵測器 | 輪數：{CHECK_ROUNDS} | 帳號數：{len(FUNBOX_ACCOUNTS)}")
    consecutive_failures = 0
    for round_num in range(1, CHECK_ROUNDS + 1):
        try:
            if CHECK_ROUNDS > 1:
                log.info(f"── 第 {round_num}/{CHECK_ROUNDS} 輪 ──")
            success = check_once()
            if success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                log.warning(f"連續偵測失敗 {consecutive_failures}/3")
                if consecutive_failures >= 3:
                    log.error(
                        "Funbox 連續 3 輪無法連線，判定目前 GitHub runner 網路異常，"
                        "提早結束本次 Job，交由下次排程取得新 runner"
                    )
                    break
        except Exception as error:
            log.exception(f"第 {round_num}/{CHECK_ROUNDS} 輪發生非預期錯誤：{error}")
        if round_num < CHECK_ROUNDS:
            wait = random.randint(3, 5)
            log.info(f"等待 {wait} 秒後進行下一輪...")
            time.sleep(wait)
    log.info("所有輪次完成")


if __name__ == "__main__":
    main()
