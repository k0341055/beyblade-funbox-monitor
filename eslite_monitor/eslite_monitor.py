"""
誠品網路書店 Beyblade X 戰鬥陀螺監控

MONITOR_MODE=exhibition（預設）→ 監控書展 API（book_exhibits），不含個別追蹤商品
MONITOR_MODE=product           → 監控 EXTRA_PRODUCT_GUIDS 個別追蹤商品

兩種模式共用：登入、購物車、自動下單、Email 通知。
通知邏輯：每輪偵測到有庫存即通知（無冷卻）；下單去重由 ORDER_STATE_FILE 負責。
"""

import json
import logging
import os
import random
import smtplib
import time
import urllib.parse
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(dotenv_path=".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 基底類別（共用邏輯）
# ─────────────────────────────────────────────

class EsliteMonitorBase(ABC):
    """
    誠品監控基底類別。
    子類別只需實作 fetch_in_stock_products()，其餘流程（通知、下單、session）
    全部繼承自此類別。
    """

    ESLITE_BASE = "https://www.eslite.com"
    ATHENA_BASE = "https://athena.eslite.com"
    _UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.TW_TZ = timezone(timedelta(hours=8))

        _extra_env = os.environ.get("ESLITE_EXTRA_PRODUCTS", "").strip()
        self.EXTRA_PRODUCT_GUIDS = [
            g.strip()
            for g in _extra_env.split(",")
            if g.strip()
        ]
        self.CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))

        self.GMAIL_SENDER     = os.environ["GMAIL_SENDER"]
        self.GMAIL_PASSWORD   = os.environ["GMAIL_PASSWORD"]
        self.GMAIL_RECIPIENTS = [
            a.strip() for a in os.environ["GMAIL_RECIPIENTS"].split(",") if a.strip()
        ]

        self.AUTO_CHECKOUT    = os.environ.get("AUTO_CHECKOUT", "true").lower() != "false"
        self.ESLITE_ACCOUNT   = os.environ.get("ESLITE_ACCOUNT", "")
        self.ESLITE_PASSWORD  = os.environ.get("ESLITE_PASSWORD", "")
        self.CHECKOUT_CITY    = os.environ.get("CHECKOUT_CITY", "新竹市")
        self.CHECKOUT_STORE_CODE = os.environ.get("CHECKOUT_STORE_CODE", "B060")
        self.CHECKOUT_MAX     = int(os.environ.get("CHECKOUT_MAX", "3"))
        self.HEADLESS         = os.environ.get("HEADLESS", "true").lower() != "false"

        self.STORAGE_STATE_FILE = Path(os.environ.get("STORAGE_STATE_FILE", "eslite_storage_state.json"))
        self.ORDER_STATE_FILE   = Path(os.environ.get("ORDER_STATE_FILE", "eslite_order_state.json"))

        _recip_raw = [r.strip() for r in os.environ.get("ORDER_RECIPIENT", "").split(",") if r.strip()]
        self.ORDER_RECIPIENTS = _recip_raw or self.GMAIL_RECIPIENTS[:1]
        self.EVENT_PAGE_URL = os.environ.get("ESLITE_EVENT_URL", "").strip()

        # 記憶體內追蹤（僅本次執行有效），防止同一 run 內重複清空購物車
        self._cart_added_guids: set = set()

        # 登入失敗計數：累計達上限後放棄自動下單，僅繼續通知
        self._login_fail_count: int = 0
        self.LOGIN_FAIL_MAX: int = 2

        # 已購買商品關鍵字（ESLITE_PURCHASED_NAMES，逗號分隔商品型號）
        # 符合的商品：發通知但跳過自動下單
        _purchased_env = os.environ.get("ESLITE_PURCHASED_NAMES", "").strip()
        self.PURCHASED_NAMES: list = [
            n.strip().upper() for n in _purchased_env.split(",") if n.strip()
        ]

    def _is_purchased(self, product: dict) -> bool:
        """判斷商品名稱是否符合已購買關鍵字（大小寫不分）。"""
        name = product.get("name", "").upper()
        return any(kw in name for kw in self.PURCHASED_NAMES)

    # ── 抽象介面 ──────────────────────────────

    @abstractmethod
    def fetch_in_stock_products(self, page) -> list:
        """回傳當輪所有有庫存商品的 dict 清單。子類別實作。"""

    # ── 工具 ──────────────────────────────────

    def _mask(self, email: str) -> str:
        if "@" not in email:
            return "***"
        local, domain = email.split("@", 1)
        return f"{local[0]}***@{domain}"

    def _send_email(self, recipients: list, subject: str, body: str):
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"]    = self.GMAIL_SENDER
            msg["To"]      = ", ".join(recipients)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
                s.login(self.GMAIL_SENDER, self.GMAIL_PASSWORD)
                s.sendmail(self.GMAIL_SENDER, recipients, msg.as_string())
            log.info(f"Email 發送成功 → {[self._mask(r) for r in recipients]}")
        except Exception as e:
            log.error(f"Email 發送失敗：{e}")

    # ── 下單狀態 ──────────────────────────────

    def _load_order_state(self) -> dict:
        if self.ORDER_STATE_FILE.exists():
            try:
                return json.loads(
                    self.ORDER_STATE_FILE.read_text(encoding="utf-8")
                ).get("ordered", {})
            except Exception:
                return {}
        return {}

    def _save_order_state(self, ordered: dict):
        self.ORDER_STATE_FILE.write_text(
            json.dumps(
                {"ordered": ordered, "updated": datetime.now(self.TW_TZ).isoformat()},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def _calc_order_qty(self, product: dict) -> int:
        """依帳號上限、每單上限、庫存三者取最小值，至少 1。"""
        qty = product.get("stock") or 1
        if product.get("order_qty_limit"):
            qty = min(qty, int(product["order_qty_limit"]))
        if product.get("account_qty_limit"):
            qty = min(qty, int(product["account_qty_limit"]))
        return max(1, qty)

    # ── 個別商品 API（兩種模式都可能用到）────

    def _fetch_single_product(self, page, guid: str):
        """呼叫單品 API，有庫存回傳 dict，否則回傳 None。"""
        api_url = f"{self.ATHENA_BASE}/api/v1/products/{guid}"
        try:
            page.goto(api_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1000)
            try:
                raw = page.inner_text("pre")
            except Exception:
                raw = page.inner_text("body")

            data = json.loads(raw)
            name         = data.get("name", f"商品 {guid}")
            product_guid = str(data.get("product_guid", guid))
            stock        = int(data.get("stock") or 0)
            btn_status   = data.get("product_button_status", "")
            in_stock     = (stock > 0) or (btn_status == "add_to_shopping_cart")

            if not in_stock:
                log.info(f"個別商品無庫存：{name}（{btn_status}，stock={stock}）")
                return None

            acct_lim = data.get("account_qty_limit")
            ord_lim  = data.get("order_qty_limit")
            log.info(
                f"有庫存 → {name} | 庫存:{stock} 件 | "
                f"{'帳號上限:'+str(acct_lim)+'件' if acct_lim else '無限購'} | {btn_status}"
            )
            return {
                "guid": product_guid, "name": name,
                "stock": stock, "status": btn_status,
                "account_qty_limit": acct_lim, "order_qty_limit": ord_lim,
                "url": f"{self.ESLITE_BASE}/product/{product_guid}",
            }
        except Exception as e:
            log.error(f"個別商品查詢失敗：{guid} | {e}")
            return None

    # ── 登入管理 ──────────────────────────────

    def _is_logged_in(self, page) -> bool:
        try:
            page.goto(f"{self.ESLITE_BASE}/member", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
            if "/login" in page.url:
                return False
            if page.query_selector(
                "a[href*='logout'], button:has-text('登出'), a:has-text('登出'), "
                "[class*='logout'], [data-testid*='logout']"
            ):
                return True
            if page.query_selector("input[type='password']"):
                return False
            if page.query_selector("button:has-text('登入'), a:has-text('會員登入')"):
                return False
            return False
        except Exception:
            return False

    def _do_login(self, page, ctx) -> bool:
        if not self.ESLITE_ACCOUNT or not self.ESLITE_PASSWORD:
            log.warning("未設定 ESLITE_ACCOUNT / ESLITE_PASSWORD，跳過自動下單")
            return False
        try:
            log.info(f"登入誠品帳號：{self.ESLITE_ACCOUNT}")
            page.goto(f"{self.ESLITE_BASE}/login", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            page.get_by_role("textbox", name="台灣手機/會員卡號/自訂帳號").click()
            page.get_by_role("textbox", name="台灣手機/會員卡號/自訂帳號").fill(self.ESLITE_ACCOUNT)
            page.get_by_role("textbox", name="請輸入密碼").click()
            page.get_by_role("textbox", name="請輸入密碼").fill(self.ESLITE_PASSWORD)
            page.get_by_role("button", name="登入").click()
            page.wait_for_timeout(1500)

            if not self.HEADLESS:
                log.info("若出現圖形驗證碼，請完成後腳本自動繼續（等待最多 120 秒）")
                try:
                    page.wait_for_function(
                        "() => !window.location.pathname.startsWith('/login')",
                        timeout=120_000,
                    )
                except Exception:
                    pass
            else:
                page.wait_for_timeout(4000)

            sms_indicators = ["verify", "otp", "sms", "驗證碼"]
            sms_detected = any(kw in page.url.lower() for kw in sms_indicators)
            if not sms_detected:
                try:
                    if page.query_selector("input[placeholder*='驗證碼'], input[name*='otp'], input[name*='sms']"):
                        sms_detected = True
                except Exception:
                    pass

            if sms_detected:
                if not self.HEADLESS:
                    log.info("偵測到簡訊驗證，請在瀏覽器中輸入驗證碼（等待 90 秒）...")
                    page.wait_for_timeout(90_000)
                    if "/login" not in page.url:
                        ctx.storage_state(path=str(self.STORAGE_STATE_FILE))
                        log.info(f"簡訊驗證完成，session 已儲存至 {self.STORAGE_STATE_FILE}")
                        return True
                log.warning("需要簡訊驗證碼，無法在 GitHub Actions 中自動完成")
                self._login_fail_count += 1
                self._notify_login_required()
                return False

            if "/login" in page.url:
                try:
                    page.screenshot(path="debug_login.png")
                except Exception:
                    pass
                self._login_fail_count += 1
                log.error(f"登入失敗（仍停在登入頁）：{page.url}")
                return False

            ctx.storage_state(path=str(self.STORAGE_STATE_FILE))
            log.info(f"登入成功，session 已儲存至 {self.STORAGE_STATE_FILE}")
            return True
        except Exception as e:
            self._login_fail_count += 1
            log.error(f"登入時發生例外：{e}")
            return False

    def _ensure_logged_in(self, page, ctx) -> bool:
        if self._is_logged_in(page):
            log.info("Session 有效，已登入誠品")
            return True
        log.info("Session 已失效，嘗試重新登入...")
        return self._do_login(page, ctx)

    # ── 購物車與結帳 ──────────────────────────

    def _clear_cart(self, page):
        try:
            page.goto(f"{self.ESLITE_BASE}/cart", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)
            for _ in range(30):
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

    def _add_to_cart(self, page, guid: str, qty: int = 1) -> bool:
        url = f"{self.ESLITE_BASE}/product/{guid}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_selector(
                    "button:has-text('加入購物車'), button:has-text('立即購買')",
                    timeout=10000,
                )
            except Exception:
                pass

            if qty > 1:
                try:
                    qty_input = page.query_selector(
                        "input[type='number'][class*='qty'], "
                        "input[type='number'][class*='quantity'], "
                        "input[type='number'][name*='qty'], "
                        "input[type='number']"
                    )
                    if qty_input:
                        qty_input.triple_click()
                        qty_input.fill(str(qty))
                        log.info(f"已設定購買數量：{qty}")
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
                except Exception:
                    pass
                return False
            if not btn.is_enabled():
                log.warning(f"加入購物車按鈕為禁用狀態：{guid}")
                return False

            btn.click()
            page.wait_for_timeout(2000)
            try:
                confirm = page.query_selector("button:has-text('確認')")
                if confirm and confirm.is_visible():
                    confirm.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

            log.info(f"已加入購物車：{guid}（數量 {qty}）")
            return True
        except Exception as e:
            log.error(f"加入購物車失敗：{guid} | {e}")
            return False

    def _checkout(self, page):
        """誠品門市取貨 + ATM 轉帳結帳，成功回傳訂單編號，失敗回傳 None。"""
        try:
            page.goto(f"{self.ESLITE_BASE}/cart", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

            checkout_btn = (
                page.query_selector("button:has-text('結帳')") or
                page.query_selector("a:has-text('結帳')") or
                page.query_selector("[class*='checkout'] button") or
                page.query_selector("[class*='cart-checkout']")
            )
            if not checkout_btn:
                log.error("找不到結帳按鈕")
                try:
                    page.screenshot(path="debug_cart.png")
                except Exception:
                    pass
                return None
            checkout_btn.click()
            page.wait_for_timeout(2000)

            page.get_by_title("誠品門市取貨").click()
            page.wait_for_timeout(1500)
            page.evaluate("window.scrollBy(0, 400)")
            page.wait_for_timeout(1000)

            try:
                page.wait_for_selector(
                    "select[name='recipientCity'], select[name*='city']", timeout=8000
                )
                city_sel = (
                    page.locator("select[name='recipientCity']").first or
                    page.locator("select[name*='city']").first
                )
                city_sel.select_option(self.CHECKOUT_CITY)
            except Exception:
                page.get_by_role("combobox").nth(2).select_option(self.CHECKOUT_CITY)
            page.wait_for_timeout(1000)

            page.wait_for_selector("select[name='recipientEsliteStore']", timeout=8000)
            page.locator("select[name='recipientEsliteStore']").select_option(self.CHECKOUT_STORE_CODE)
            page.wait_for_timeout(1000)

            page.get_by_title("ATM轉帳").click()
            page.wait_for_timeout(1000)
            page.get_by_role("button", name="確認結帳").click()
            page.wait_for_url("**/cart/step3**", timeout=20000)

            params   = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
            order_id = params.get("orderid", [None])[0]
            if order_id:
                log.info(f"結帳成功！訂單編號：{order_id}")
            else:
                log.warning(f"結帳完成但未擷取訂單編號，URL：{page.url}")
            return order_id

        except Exception as e:
            log.error(f"結帳失敗：{e}", exc_info=True)
            try:
                page.goto("about:blank")
            except Exception:
                pass
            return None

    # ── Email 通知 ────────────────────────────

    def _notify_products(self, products: list):
        regular   = [p for p in products if not self._is_purchased(p)]
        purchased = [p for p in products if self._is_purchased(p)]

        if regular:
            count   = len(regular)
            subject = f"【誠品有貨了！】偵測到 {count} 件商品"
            lines   = [
                f"誠品戰鬥陀螺專區偵測到共 {count} 件有庫存商品",
                f"偵測時間：{datetime.now(self.TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
                "",
                f">>> 立即結帳：{self.ESLITE_BASE}/cart/step2",
                f">>> 查看購物車：{self.ESLITE_BASE}/cart",
                "", "=" * 50,
            ]
            for i, p in enumerate(regular, 1):
                acct_lim = p.get("account_qty_limit")
                ord_lim  = p.get("order_qty_limit")
                lines += [
                    f"\n【商品 {i}】",
                    f"商品名：{p['name']}",
                    f"庫存：{p['stock']} 件",
                    f"帳號上限：{'無限制' if acct_lim is None else f'{acct_lim} 件'}",
                    f"每單上限：{'無限制' if ord_lim is None else f'{ord_lim} 件'}",
                    f"商品連結：{p['url']}",
                    "-" * 40,
                ]
            if self.EVENT_PAGE_URL:
                lines += ["", f"完整專區頁：{self.EVENT_PAGE_URL}"]
            self._send_email(self.GMAIL_RECIPIENTS, subject, "\n".join(lines))

        if purchased:
            count   = len(purchased)
            subject = f"【誠品有貨！已購買商品】偵測到 {count} 件"
            lines   = [
                "您已購買的商品有庫存，僅通知，不自動下單。",
                f"偵測時間：{datetime.now(self.TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
                "", "=" * 50,
            ]
            for i, p in enumerate(purchased, 1):
                acct_lim = p.get("account_qty_limit")
                ord_lim  = p.get("order_qty_limit")
                lines += [
                    f"\n【商品 {i}】【已購買，僅通知不下單】",
                    f"商品名：{p['name']}",
                    f"庫存：{p['stock']} 件",
                    f"帳號上限：{'無限制' if acct_lim is None else f'{acct_lim} 件'}",
                    f"每單上限：{'無限制' if ord_lim is None else f'{ord_lim} 件'}",
                    f"商品連結：{p['url']}",
                    "-" * 40,
                ]
            if self.EVENT_PAGE_URL:
                lines += ["", f"完整專區頁：{self.EVENT_PAGE_URL}"]
            self._send_email(self.ORDER_RECIPIENTS, subject, "\n".join(lines))

    def _notify_cart_added(self, products: list):
        count   = len(products)
        subject = f"【誠品購物車已更新！】{count} 件商品已入購物車，請前往結帳"
        now_str = datetime.now(self.TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
        lines   = [
            f"已將 {count} 件商品加入誠品購物車！",
            f"時間：{now_str}（台灣時間）",
            "",
            f">>> 立即結帳（一鍵進入）：{self.ESLITE_BASE}/cart/step2",
            f">>> 查看購物車：{self.ESLITE_BASE}/cart",
            "", "=" * 50,
        ]
        for i, p in enumerate(products, 1):
            acct_lim = p.get("account_qty_limit")
            ord_lim  = p.get("order_qty_limit")
            qty = self._calc_order_qty(p)
            lines += [
                f"\n【商品 {i}】",
                f"商品名：{p['name']}",
                f"加入數量：{qty} 件",
                f"庫存（加入時）：{p.get('stock', '?')} 件",
                f"帳號上限：{'無限制' if acct_lim is None else f'{acct_lim} 件'}",
                f"每單上限：{'無限制' if ord_lim is None else f'{ord_lim} 件'}",
                f"商品連結：{p['url']}",
                "-" * 40,
            ]
        self._send_email(self.ORDER_RECIPIENTS, subject, "\n".join(lines))

    def _notify_order(self, products: list, order_id: str):
        count   = len(products)
        subject = f"【誠品自動下單成功！】已下單 {count} 件商品｜訂單 {order_id}"
        now_str = datetime.now(self.TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
        lines   = [
            "誠品自動下單成功！",
            f"訂單編號：{order_id}",
            f"下單時間：{now_str}（台灣時間）",
            "付款方式：ATM 轉帳（請記得在期限內完成匯款）",
            f"取貨門市：{self.CHECKOUT_CITY}－{self.CHECKOUT_STORE_CODE}",
            "", "=" * 50,
        ]
        for i, p in enumerate(products, 1):
            lines += [
                f"\n【商品 {i}】",
                f"商品名：{p['name']}",
                f"庫存（下單時）：{p.get('stock', '?')} 件",
                f"商品連結：{p['url']}",
                "-" * 40,
            ]
        lines += ["", f"查看訂單：{self.ESLITE_BASE}/member/orders"]
        self._send_email(self.ORDER_RECIPIENTS, subject, "\n".join(lines))

    def _notify_login_required(self):
        subject = "【誠品監控警告】需要手動重新登入"
        body = "\n".join([
            "誠品自動下單失敗：登入時偵測到簡訊驗證要求。",
            "",
            "請在本機執行以下步驟恢復自動下單：",
            "  1. cd eslite_monitor && python generate_session.py",
            "  2. 在彈出的瀏覽器中完成登入/驗證",
            "  3. gh secret set ESLITE_STORAGE_STATE_B64 \\",
            '       --body "$(base64 -i eslite_storage_state.json | tr -d \'\\n\')" \\',
            "       --repo k0341055/beyblade-funbox-monitor",
            "",
            f"本次偵測時間：{datetime.now(self.TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
        ])
        self._send_email(self.ORDER_RECIPIENTS, subject, body)

    # ── 自動下單流程 ──────────────────────────

    def _attempt_checkout(self, page, in_stock_products: list):
        """
        加入購物車 → 立即發購物車通知（含結帳連結）→ 嘗試自動結帳。
        結帳失敗時商品仍留購物車，使用者可手動結帳。

        誠品限購一次（不論哪次上架）：所有商品均已下單時直接返回，不建立 session context。
        結帳 context 與監控 context 完全隔離，避免帳號被異地登出。
        """
        if self._login_fail_count >= self.LOGIN_FAIL_MAX:
            log.warning(
                f"本次執行已累計 {self._login_fail_count} 次登入失敗，"
                f"放棄自動下單（僅繼續通知）"
            )
            return

        ordered  = self._load_order_state()
        to_order = [
            p for p in in_stock_products
            if p["guid"] not in ordered
            and p["guid"] not in self._cart_added_guids
            and not self._is_purchased(p)
        ][:self.CHECKOUT_MAX]
        if not to_order:
            log.info("所有有庫存商品均已下單或已購買（限購一次），跳過自動下單")
            return

        log.info(f"準備加入購物車 {len(to_order)} 件（CHECKOUT_MAX={self.CHECKOUT_MAX}）")

        # 建立獨立的結帳 context（載入 session），與匿名監控 context 完全隔離
        browser = page.context.browser
        ckout_kwargs = {"user_agent": self._UA}
        if self.STORAGE_STATE_FILE.exists():
            ckout_kwargs["storage_state"] = str(self.STORAGE_STATE_FILE)
        ckout_ctx = browser.new_context(**ckout_kwargs)
        ckout_ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        ckout_page = ckout_ctx.new_page()

        try:
            if not self._ensure_logged_in(ckout_page, ckout_ctx):
                log.warning("無法登入誠品，跳過自動下單")
                return

            self._clear_cart(ckout_page)

            added = []
            for p in to_order:
                if self._add_to_cart(ckout_page, p["guid"], self._calc_order_qty(p)):
                    added.append(p)

            if not added:
                log.warning("所有商品加入購物車均失敗，跳過結帳")
                return

            for p in added:
                self._cart_added_guids.add(p["guid"])

            self._notify_cart_added(added)
            log.info(f"已加入購物車 {len(added)} 件，開始自動結帳...")

            order_id = self._checkout(ckout_page)
            if order_id:
                self._notify_order(added, order_id)
                now_str = datetime.now(self.TW_TZ).isoformat()
                for p in added:
                    ordered[p["guid"]] = {"order_id": order_id, "ordered_at": now_str}
                self._save_order_state(ordered)
            else:
                log.warning("自動結帳失敗，商品已留在購物車，請點擊通知信中連結手動結帳")
        finally:
            try:
                ckout_ctx.storage_state(path=str(self.STORAGE_STATE_FILE))
                log.info(f"Session 狀態已更新：{self.STORAGE_STATE_FILE}")
            except Exception as e:
                log.warning(f"更新 session 狀態失敗：{e}")
            ckout_ctx.close()

    # ── 核心偵測（每輪） ──────────────────────

    def check_once(self, page) -> bool:
        try:
            products = self.fetch_in_stock_products(page)

            if not products:
                log.info("目前無庫存商品，繼續監控")
                return True

            log.info(f"發送庫存通知：{len(products)} 件")
            self._notify_products(products)

            if self.AUTO_CHECKOUT and self.ESLITE_ACCOUNT:
                self._attempt_checkout(page, products)

            return True

        except Exception as e:
            log.error(f"執行例外：{e}", exc_info=True)
            try:
                page.goto("about:blank")
            except Exception:
                pass
            return False

    # ── 主迴圈 ────────────────────────────────

    def run(self):
        log.info(
            f"{self.__class__.__name__} | 輪數：{self.CHECK_ROUNDS}"
            f" | 自動下單：{'啟用' if self.AUTO_CHECKOUT else '停用'}"
        )

        with sync_playwright() as pw:
            br = pw.chromium.launch(
                headless=self.HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )
            # 監控 context 永遠匿名，不載入 session cookies
            # session 只在 _attempt_checkout 內的獨立 context 短暫使用
            ctx = br.new_context(user_agent=self._UA)
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()

            for round_num in range(1, self.CHECK_ROUNDS + 1):
                if self.CHECK_ROUNDS > 1:
                    log.info(f"── 第 {round_num}/{self.CHECK_ROUNDS} 輪 ──")
                self.check_once(page)
                if round_num < self.CHECK_ROUNDS:
                    wait = random.randint(3, 5)
                    log.info(f"等待 {wait} 秒後進行下一輪...")
                    time.sleep(wait)

            br.close()

        log.info("所有輪次完成")


# ─────────────────────────────────────────────
# 展覽監控（書展 API）
# ─────────────────────────────────────────────

class ExhibitionMonitor(EsliteMonitorBase):
    """
    監控誠品書展 API（book_exhibits）。
    不含個別追蹤商品，每輪只呼叫一次 API → 每輪約 6 秒，可跑 580 輪/小時。
    """

    def __init__(self):
        super().__init__()
        self.API_URL = os.environ.get("ESLITE_API_URL", "").strip()
        if not self.API_URL:
            raise ValueError("ESLITE_API_URL 環境變數未設定（請設定 GitHub Variable ESLITE_API_URL）")

    def _extract_products(self, data: dict) -> dict:
        """遞迴解析書展 JSON，找出所有含 product_guid + name 的節點。"""
        products = {}

        def add(p):
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
                "status": p.get("status") or p.get("product_button_status") or "unknown",
                "stock": stock,
                "account_qty_limit": p.get("account_qty_limit"),
                "order_qty_limit":   p.get("order_qty_limit"),
                "image": p.get("image", ""),
                "url": f"{self.ESLITE_BASE}/product/{guid}",
            }

        def walk(value):
            if isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if value.get("product_guid") and value.get("name"):
                    add(value)
                for child in value.values():
                    walk(child)

        walk(data)
        return products

    def fetch_in_stock_products(self, page) -> list:
        page.goto(self.API_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        try:
            raw = page.inner_text("pre")
        except Exception:
            raw = page.inner_text("body")

        all_products = self._extract_products(json.loads(raw))
        result = []
        for guid, p in all_products.items():
            name  = p["name"]
            stock = p["stock"]
            if not stock or stock <= 0:
                continue
            lim = f"帳號上限:{p['account_qty_limit']}件" if p.get("account_qty_limit") else "無限購"
            log.info(f"有庫存 → {name} | 庫存:{stock} 件 | {lim} | {p['status']}")
            result.append({"guid": guid, **p})

        log.info(f"展覽 API 抽取 {len(all_products)} 件，有庫存 {len(result)} 件")
        return result


# ─────────────────────────────────────────────
# 個別商品監控
# ─────────────────────────────────────────────

class ProductMonitor(EsliteMonitorBase):
    """
    監控 EXTRA_PRODUCT_GUIDS 列出的個別商品。
    不呼叫書展 API，專注於特定商品的即時追蹤。
    """

    def fetch_in_stock_products(self, page) -> list:
        if not self.EXTRA_PRODUCT_GUIDS:
            log.warning("ESLITE_EXTRA_PRODUCTS 未設定，ProductMonitor 無商品可追蹤")
            return []

        result = []
        for guid in self.EXTRA_PRODUCT_GUIDS:
            product = self._fetch_single_product(page, guid)
            if product:
                result.append(product)

        log.info(f"個別追蹤 {len(self.EXTRA_PRODUCT_GUIDS)} 件，有庫存 {len(result)} 件")
        return result


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    mode = os.environ.get("MONITOR_MODE", "exhibition").lower()
    if mode == "product":
        ProductMonitor().run()
    else:
        ExhibitionMonitor().run()
