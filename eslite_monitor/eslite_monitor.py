"""
誠品網路書店 Beyblade X 戰鬥陀螺監控
- 偵測：athena.eslite.com book_exhibits API（Cloudflare 保護，需 Playwright 瀏覽器取得）
- 通知：Gmail SMTP（所有 GMAIL_RECIPIENTS）
- 自動下單：偵測到有庫存商品時，自動登入誠品並完成結帳
  - 誠品會認裝置，首次在 GitHub Actions 執行前需先於本機完成一次簡訊驗證
  - STORAGE_STATE_FILE 保存 session cookies（含信任裝置 cookie），跨 VM 重用
  - 下單通知只發送給 ORDER_RECIPIENT
- 冷卻：同款商品 1 小時內最多通知一次；庫存歸零即清除，重新上架立即通知
- 效能：單次執行僅啟動一次 Chromium，所有輪次共用同一 page
"""

import json
import logging
import os
import random
import smtplib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(dotenv_path=".env")

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

API_URL = os.environ.get(
    "ESLITE_API_URL",
    "https://athena.eslite.com/api/v1/book_exhibits/CU202503-00091",
)
ESLITE_BASE = "https://www.eslite.com"
ATHENA_BASE = "https://athena.eslite.com"

# 個別追蹤商品（不在展覽 API 內的單一商品），逗號分隔
EXTRA_PRODUCT_GUIDS = [
    g.strip()
    for g in os.environ.get("ESLITE_EXTRA_PRODUCTS", "10022136782683190211005").split(",")
    if g.strip()
]

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

SKIP_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get("ESLITE_SKIP_KEYWORDS", "UX-14").split(",")
    if kw.strip()
]

# ── 自動下單設定 ──
AUTO_CHECKOUT = os.environ.get("AUTO_CHECKOUT", "true").lower() != "false"
ESLITE_ACCOUNT = os.environ.get("ESLITE_ACCOUNT", "")
ESLITE_PASSWORD = os.environ.get("ESLITE_PASSWORD", "")
CHECKOUT_CITY = os.environ.get("CHECKOUT_CITY", "新竹市")
CHECKOUT_STORE_CODE = os.environ.get("CHECKOUT_STORE_CODE", "B060")
STORAGE_STATE_FILE = Path(os.environ.get("STORAGE_STATE_FILE", "eslite_storage_state.json"))
ORDER_STATE_FILE = Path(os.environ.get("ORDER_STATE_FILE", "eslite_order_state.json"))
CHECKOUT_MAX = int(os.environ.get("CHECKOUT_MAX", "3"))  # 每次最多加入購物車的商品數

_order_recip_raw = [r.strip() for r in os.environ.get("ORDER_RECIPIENT", "").split(",") if r.strip()]
ORDER_RECIPIENTS = _order_recip_raw or GMAIL_RECIPIENTS[:1]

HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

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


def _send_email(recipients: list, subject: str, body: str):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = ", ".join(recipients)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, recipients, msg.as_string())
        log.info(f"Email 發送成功 → {[_mask_email(r) for r in recipients]}")
    except Exception as e:
        log.error(f"Email 發送失敗：{e}")


# ─────────────────────────────────────────────
# 通知冷卻狀態（1 小時）
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
# 下單狀態（避免重複下單）
# ─────────────────────────────────────────────


def _load_order_state() -> dict:
    if ORDER_STATE_FILE.exists():
        try:
            return json.loads(ORDER_STATE_FILE.read_text(encoding="utf-8")).get("ordered", {})
        except Exception:
            return {}
    return {}


def _save_order_state(ordered: dict):
    ORDER_STATE_FILE.write_text(
        json.dumps(
            {"ordered": ordered, "updated": datetime.now(TW_TZ).isoformat()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


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
            "account_qty_limit": p.get("account_qty_limit"),
            "order_qty_limit": p.get("order_qty_limit"),
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


def fetch_products(page) -> list:
    """
    1. 從展覽 API 取得商品清單（遞迴解析）
    2. 逐一查詢 EXTRA_PRODUCT_GUIDS 的個別商品
    回傳所有有庫存且不在略過清單的商品列表。
    """
    # ── 1. 展覽 API ──
    page.goto(API_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    try:
        raw = page.inner_text("pre")
    except Exception:
        raw = page.inner_text("body")

    data = json.loads(raw)
    all_products = _extract_products(data)

    result = []
    for guid, p in all_products.items():
        name = p["name"]
        stock = p["stock"]

        if any(kw.upper() in name.upper() for kw in SKIP_KEYWORDS):
            log.info(f"略過：{name}")
            continue

        if not stock or stock <= 0:
            continue

        limit_info = f"帳號上限:{p['account_qty_limit']}件" if p.get("account_qty_limit") else "無限購"
        log.info(f"有庫存 → {name} | 庫存:{stock} 件 | {limit_info} | {p['status']}")
        result.append({"guid": guid, **p})

    log.info(f"展覽 API 抽取 {len(all_products)} 件，有庫存 {len(result)} 件")

    # ── 2. 個別追蹤商品 ──
    exhibit_guids = {p["guid"] for p in result}
    for guid in EXTRA_PRODUCT_GUIDS:
        if guid in exhibit_guids:
            continue  # 已在展覽清單中，不重複
        product = _fetch_single_product(page, guid)
        if product:
            result.append(product)

    return result


def _fetch_single_product(page, guid: str):
    """
    透過 athena 單品 API 查詢指定商品的庫存狀態。
    有庫存回傳商品 dict；無庫存或查詢失敗回傳 None。
    """
    api_url = f"{ATHENA_BASE}/api/v1/products/{guid}"
    try:
        page.goto(api_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1000)
        try:
            raw = page.inner_text("pre")
        except Exception:
            raw = page.inner_text("body")

        data = json.loads(raw)
        name = data.get("name", f"商品 {guid}")
        product_guid = str(data.get("product_guid", guid))

        try:
            stock = int(data.get("stock") or 0)
        except (TypeError, ValueError):
            stock = 0

        btn_status = data.get("product_button_status", "")
        in_stock = (stock > 0) or (btn_status == "add_to_shopping_cart")

        if not in_stock:
            log.info(f"個別商品無庫存：{name}（{btn_status}，stock={stock}）")
            return None

        acct_lim = data.get("account_qty_limit")
        ord_lim = data.get("order_qty_limit")
        limit_info = f"帳號上限:{acct_lim}件" if acct_lim else "無限購"
        log.info(f"有庫存 → {name} | 庫存:{stock} 件 | {limit_info} | {btn_status}")
        return {
            "guid": product_guid,
            "name": name,
            "stock": stock,
            "status": btn_status,
            "account_qty_limit": acct_lim,
            "order_qty_limit": ord_lim,
            "url": f"{ESLITE_BASE}/product/{product_guid}",
        }
    except Exception as e:
        log.error(f"個別商品查詢失敗：{guid} | {e}")
        return None


# ─────────────────────────────────────────────
# 登入管理
# ─────────────────────────────────────────────


def _is_logged_in(page) -> bool:
    """
    導覽至會員中心，判斷是否已登入。
    eslite 為 SPA，未登入時 URL 不一定變為 /login（content 是 client-side render），
    須以頁面內容判斷。
    """
    try:
        page.goto(f"{ESLITE_BASE}/member", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)
        if "/login" in page.url:
            return False
        # 有登出按鈕 → 已登入
        logout = page.query_selector(
            "a[href*='logout'], button:has-text('登出'), a:has-text('登出'), "
            "[class*='logout'], [data-testid*='logout']"
        )
        if logout:
            return True
        # 有密碼輸入框 → 顯示登入表單（未登入）
        pwd_input = page.query_selector("input[type='password']")
        if pwd_input:
            return False
        # 有 '登入' 主要按鈕 → 未登入
        login_btn = page.query_selector("button:has-text('登入'), a:has-text('會員登入')")
        if login_btn:
            return False
        return False
    except Exception:
        return False


def _do_login(page, ctx) -> bool:
    """
    執行完整登入流程。成功後儲存 session 狀態，回傳 True。
    若偵測到簡訊驗證頁面，發送警告信並回傳 False。
    """
    if not ESLITE_ACCOUNT or not ESLITE_PASSWORD:
        log.warning("未設定 ESLITE_ACCOUNT / ESLITE_PASSWORD，跳過自動下單")
        return False

    try:
        log.info(f"登入誠品帳號：{ESLITE_ACCOUNT}")
        page.goto(f"{ESLITE_BASE}/login", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        # 與錄製的流程一致：先 click 再 fill
        page.get_by_role("textbox", name="台灣手機/會員卡號/自訂帳號").click()
        page.get_by_role("textbox", name="台灣手機/會員卡號/自訂帳號").fill(ESLITE_ACCOUNT)
        page.get_by_role("textbox", name="請輸入密碼").click()
        page.get_by_role("textbox", name="請輸入密碼").fill(ESLITE_PASSWORD)
        page.get_by_role("button", name="登入").click()
        page.wait_for_timeout(1500)

        # 有頭模式：等待使用者完成 reCAPTCHA（eslite 對新 session 會觸發圖形驗證）
        if not HEADLESS:
            log.info("若出現圖形驗證碼，請在瀏覽器中完成後腳本自動繼續（等待最多 60 秒）")
            try:
                page.wait_for_function(
                    "() => !window.location.pathname.startsWith('/login')",
                    timeout=120_000,
                )
            except Exception:
                pass
        else:
            page.wait_for_timeout(4000)

        # 偵測簡訊驗證（URL 或頁面元素）
        sms_indicators = ["verify", "otp", "sms", "驗證碼"]
        url_lower = page.url.lower()
        sms_detected = any(kw in url_lower for kw in sms_indicators)
        if not sms_detected:
            try:
                sms_input = page.query_selector("input[placeholder*='驗證碼'], input[name*='otp'], input[name*='sms']")
                if sms_input:
                    sms_detected = True
            except Exception:
                pass

        if sms_detected:
            if not HEADLESS:
                # 有頭模式：等待使用者在瀏覽器中手動完成驗證
                log.info("偵測到簡訊驗證，請在瀏覽器中輸入驗證碼，腳本將等待 90 秒...")
                page.wait_for_timeout(90_000)
                if "/login" not in page.url:
                    ctx.storage_state(path=str(STORAGE_STATE_FILE))
                    log.info(f"簡訊驗證完成，登入成功，session 已儲存至 {STORAGE_STATE_FILE}")
                    return True
                log.warning("等待 90 秒後仍未完成登入")
            log.warning("需要簡訊驗證碼，無法在 GitHub Actions 中自動完成")
            _notify_login_required()
            return False

        # 仍在登入頁表示帳密錯誤或其他驗證問題
        if "/login" in page.url:
            try:
                page.screenshot(path="debug_login.png")
                log.info("已截圖至 debug_login.png")
            except Exception:
                pass
            log.error(f"登入失敗（仍停在登入頁）：{page.url}")
            return False

        # 登入成功，儲存 session 狀態
        ctx.storage_state(path=str(STORAGE_STATE_FILE))
        log.info(f"登入成功，session 已儲存至 {STORAGE_STATE_FILE}")
        return True

    except Exception as e:
        log.error(f"登入時發生例外：{e}")
        return False


def ensure_logged_in(page, ctx) -> bool:
    """確保已登入：先檢查現有 session，失效則重新登入。"""
    if _is_logged_in(page):
        log.info("Session 有效，已登入誠品")
        return True
    log.info("Session 已失效，嘗試重新登入...")
    return _do_login(page, ctx)


# ─────────────────────────────────────────────
# 購物車與結帳
# ─────────────────────────────────────────────


def _clear_cart(page):
    """清空購物車中的所有商品。"""
    try:
        page.goto(f"{ESLITE_BASE}/cart", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)

        for _ in range(30):  # 最多清 30 件
            btn = (
                page.query_selector("button:has-text('刪除')") or
                page.query_selector("[aria-label*='刪除']") or
                page.query_selector("[class*='remove']")
            )
            if not btn:
                break
            btn.click()
            page.wait_for_timeout(1000)

        log.info("購物車已清空")
    except Exception as e:
        log.warning(f"清空購物車發生錯誤（繼續）：{e}")


def add_to_cart(page, guid: str) -> bool:
    """導覽至商品頁並加入購物車。"""
    url = f"{ESLITE_BASE}/product/{guid}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        # 等待 SPA 渲染完成（等按鈕出現，而非固定秒數）
        try:
            page.wait_for_selector(
                "button:has-text('加入購物車'), button:has-text('立即購買')",
                timeout=10000,
            )
        except Exception:
            pass

        btn = (
            page.query_selector("button:has-text('加入購物車')") or
            page.query_selector("button:has-text('立即購買')") or
            page.query_selector("[data-testid*='add-to-cart']") or
            page.query_selector("[class*='add-to-cart']") or
            page.query_selector("[class*='addToCart']")
        )
        if not btn:
            log.warning(f"找不到加入購物車按鈕：{guid}")
            try:
                page.screenshot(path=f"debug_product_{guid[:8]}.png")
                log.info(f"已截圖至 debug_product_{guid[:8]}.png")
            except Exception:
                pass
            return False

        if not btn.is_enabled():
            log.warning(f"加入購物車按鈕為禁用狀態：{guid}")
            return False

        btn.click()
        page.wait_for_timeout(2000)

        # 關閉可能出現的確認對話框
        try:
            confirm = page.query_selector("button:has-text('確認')")
            if confirm and confirm.is_visible():
                confirm.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        log.info(f"已加入購物車：{guid}")
        return True

    except Exception as e:
        log.error(f"加入購物車失敗：{guid} | {e}")
        return False


def checkout(page):
    """
    進行結帳：誠品門市取貨（CHECKOUT_CITY / CHECKOUT_STORE_CODE）、ATM 轉帳。
    成功回傳訂單編號字串，失敗回傳 None。
    """
    try:
        page.goto(f"{ESLITE_BASE}/cart", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)  # 等待 SPA 渲染

        checkout_btn = (
            page.query_selector("button:has-text('結帳')") or
            page.query_selector("a:has-text('結帳')") or
            page.query_selector("[class*='checkout'] button") or
            page.query_selector("[class*='cart-checkout']")
        )
        if not checkout_btn:
            log.error("找不到結帳按鈕，購物車可能為空或 SPA 尚未渲染")
            # 截圖幫助 debug
            try:
                page.screenshot(path="debug_cart.png")
                log.info("已截圖至 debug_cart.png")
            except Exception:
                pass
            return None
        checkout_btn.click()
        page.wait_for_timeout(2000)

        # 選取 誠品門市取貨
        page.get_by_title("誠品門市取貨").click()
        page.wait_for_timeout(1500)

        # 選完取貨方式後往下滑，讓縣市/門市下拉選單進入視窗
        page.evaluate("window.scrollBy(0, 400)")
        page.wait_for_timeout(1000)

        # 等縣市下拉選單出現後再選
        try:
            page.wait_for_selector(
                "select[name='recipientCity'], select[name*='city']",
                timeout=8000,
            )
            city_select = (
                page.locator("select[name='recipientCity']").first or
                page.locator("select[name*='city']").first
            )
            city_select.select_option(CHECKOUT_CITY)
        except Exception:
            page.get_by_role("combobox").nth(2).select_option(CHECKOUT_CITY)
        page.wait_for_timeout(1000)

        # 等門市下拉選單更新後選取
        page.wait_for_selector("select[name='recipientEsliteStore']", timeout=8000)
        page.locator("select[name='recipientEsliteStore']").select_option(CHECKOUT_STORE_CODE)
        page.wait_for_timeout(1000)

        # 選取 ATM 轉帳
        page.get_by_title("ATM轉帳").click()
        page.wait_for_timeout(1000)

        # 確認結帳
        page.get_by_role("button", name="確認結帳").click()

        # 等待跳轉至訂單確認頁
        page.wait_for_url("**/cart/step3**", timeout=20000)

        parsed = urllib.parse.urlparse(page.url)
        params = urllib.parse.parse_qs(parsed.query)
        order_id = params.get("orderid", [None])[0]

        if order_id:
            log.info(f"結帳成功！訂單編號：{order_id}")
        else:
            log.warning(f"結帳完成但未擷取到訂單編號，URL：{page.url}")

        return order_id

    except Exception as e:
        log.error(f"結帳失敗：{e}", exc_info=True)
        try:
            page.goto("about:blank")
        except Exception:
            pass
        return None


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────


def notify_products(products: list) -> bool:
    count = len(products)
    subject = f"【誠品有貨了！】偵測到 {count} 件商品"

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
        _send_email(GMAIL_RECIPIENTS, subject, body)
        return True
    except Exception as e:
        log.error(f"庫存通知 Email 發送失敗：{e}")
        return False


def notify_order(products: list, order_id: str):
    """下單成功後發送確認信給 ORDER_RECIPIENTS。"""
    count = len(products)
    subject = f"【誠品自動下單成功！】已下單 {count} 件商品｜訂單 {order_id}"
    now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "誠品自動下單成功！",
        f"訂單編號：{order_id}",
        f"下單時間：{now_str}（台灣時間）",
        f"付款方式：ATM 轉帳（請記得在期限內完成匯款）",
        f"取貨門市：{CHECKOUT_CITY}－{CHECKOUT_STORE_CODE}",
        "",
        "=" * 50,
    ]
    for i, p in enumerate(products, 1):
        lines.append(f"\n【商品 {i}】")
        lines.append(f"商品名：{p['name']}")
        lines.append(f"庫存（下單時）：{p.get('stock', '?')} 件")
        lines.append(f"商品連結：{p['url']}")
        lines.append("-" * 40)

    lines += [
        "",
        f"查看訂單：{ESLITE_BASE}/member/orders",
    ]
    _send_email(ORDER_RECIPIENTS, subject, "\n".join(lines))


def _notify_login_required():
    """無法完成簡訊驗證時，發送警告信提醒手動重新登入。"""
    subject = "【誠品監控警告】需要手動重新登入"
    body = "\n".join([
        "誠品自動下單失敗：登入時偵測到簡訊驗證要求。",
        "",
        "請在本機執行以下步驟恢復自動下單：",
        "  1. 設定 HEADLESS=false，執行 python eslite_monitor.py",
        "  2. 在彈出的瀏覽器中完成簡訊驗證",
        "  3. 腳本會自動儲存 eslite_storage_state.json",
        "  4. 將此檔案上傳至 GitHub Actions（透過快取或 secret）",
        "",
        f"本次偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
    ])
    _send_email(ORDER_RECIPIENTS, subject, body)


# ─────────────────────────────────────────────
# 自動下單流程
# ─────────────────────────────────────────────


def _attempt_checkout(page, ctx, in_stock_products: list):
    """對尚未下單的有庫存商品執行自動下單。"""
    ordered = _load_order_state()

    to_order = [p for p in in_stock_products if p["guid"] not in ordered][:CHECKOUT_MAX]
    if not to_order:
        log.info("所有有庫存商品均已在訂單狀態中，跳過下單")
        return

    log.info(f"準備自動下單 {len(to_order)} 件商品（上限 CHECKOUT_MAX={CHECKOUT_MAX}）")

    if not ensure_logged_in(page, ctx):
        log.warning("無法登入誠品，跳過自動下單")
        return

    _clear_cart(page)

    added = []
    for p in to_order:
        if add_to_cart(page, p["guid"]):
            added.append(p)

    if not added:
        log.warning("所有商品加入購物車均失敗，跳過結帳")
        return

    log.info(f"已加入購物車 {len(added)} 件，開始結帳...")
    order_id = checkout(page)

    if order_id:
        notify_order(added, order_id)
        now_str = datetime.now(TW_TZ).isoformat()
        for p in added:
            ordered[p["guid"]] = {"order_id": order_id, "ordered_at": now_str}
        _save_order_state(ordered)
    else:
        log.error("結帳失敗，本輪未完成下單（下輪將重試）")


# ─────────────────────────────────────────────
# 核心偵測邏輯
# ─────────────────────────────────────────────


def check_once(page, ctx) -> bool:
    try:
        products = fetch_products(page)
        now = datetime.now(TW_TZ)
        cutoff = now - NOTIFY_COOLDOWN

        notified = load_notified()
        current_guids = {p["guid"] for p in products}
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

        # 自動下單（獨立於通知冷卻，以下單狀態去重）
        if AUTO_CHECKOUT and ESLITE_ACCOUNT:
            _attempt_checkout(page, ctx, products)

        return True

    except Exception as e:
        log.error(f"執行例外：{e}", exc_info=True)
        try:
            page.goto("about:blank")
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────


def main():
    log.info(
        f"誠品監控器 | 輪數：{CHECK_ROUNDS} | 略過：{SKIP_KEYWORDS}"
        f" | 個別追蹤：{EXTRA_PRODUCT_GUIDS}"
        f" | 自動下單：{'啟用' if AUTO_CHECKOUT else '停用'}"
    )

    with sync_playwright() as pw:
        br = pw.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx_kwargs = {"user_agent": _UA}
        if AUTO_CHECKOUT and ESLITE_ACCOUNT and STORAGE_STATE_FILE.exists():
            ctx_kwargs["storage_state"] = str(STORAGE_STATE_FILE)
            log.info(f"已載入 session 狀態：{STORAGE_STATE_FILE}")
        elif AUTO_CHECKOUT and ESLITE_ACCOUNT:
            log.info("尚無 session 狀態，將於首輪登入後儲存")

        ctx = br.new_context(**ctx_kwargs)
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()

        for round_num in range(1, CHECK_ROUNDS + 1):
            if CHECK_ROUNDS > 1:
                log.info(f"── 第 {round_num}/{CHECK_ROUNDS} 輪 ──")
            check_once(page, ctx)
            if round_num < CHECK_ROUNDS:
                wait = random.randint(3, 5)
                log.info(f"等待 {wait} 秒後進行下一輪...")
                time.sleep(wait)

        # 每次執行結束後更新 session 狀態（刷新 cookie 有效期）
        if AUTO_CHECKOUT and ESLITE_ACCOUNT:
            try:
                ctx.storage_state(path=str(STORAGE_STATE_FILE))
                log.info(f"Session 狀態已更新：{STORAGE_STATE_FILE}")
            except Exception as e:
                log.warning(f"更新 session 狀態失敗：{e}")

        br.close()

    log.info("所有輪次完成")


if __name__ == "__main__":
    main()
