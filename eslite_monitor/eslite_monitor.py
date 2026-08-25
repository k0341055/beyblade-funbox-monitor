"""
誠品網路書店 Beyblade X 戰鬥陀螺監控

MONITOR_MODE=exhibition（預設）→ 監控書展 API（book_exhibits），不含個別追蹤商品
MONITOR_MODE=product           → 監控 EXTRA_PRODUCT_GUIDS 個別追蹤商品
MONITOR_MODE=keyword           → 監控 Holmes 搜尋 API（ESLITE_SEARCH_URL 關鍵字搜尋）
MONITOR_MODE=combined          → 展覽 API + 關鍵字搜尋合併，展覽下架時自動降級

兩種模式共用：登入、購物車、自動下單、Email 通知。
通知邏輯：同商品 Email 通知冷卻 1 小時；下單去重由 ORDER_STATE_FILE 負責。
自動下單：僅 BUY_KEYWORDS 白名單商品；單帳號 × 多商品平行。每個 worker 不清空購物車；若購物車已有目標商品就直接結帳，沒有才加入購物車。
ESLITE_PARALLEL_SESSION_MODE=fresh_login（預設，最接近 Funbox）或 storage（沿用預登入 session）。
"""

import json
import logging
import os
import random
import smtplib
import time
import threading
import urllib.parse
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
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


SKIP_KEYWORDS: list[str] = [
    "BX-11",
    "BX-25",
    "BX-33",
    "BX-26",
    "BX-43",
    "BXG-29",
    "BXG-33",
    "蜘蛛人",
    "鋼鐵人",
    "薩諾斯",
    "綠惡魔",
    "路克天行者",
    "達斯維達",
    "暴風天馬",
    "銀牙烈虎",
    "烈焰飛鳳",
]


BUY_KEYWORDS: list[str] = [
    "BX-09",
    "UX-17",
    "UX-21",
    "UX-15",
    "UX-04",
    "蒼龍勇氣",
    "BX-46",
    "CX-16",
    "CX-04",
    "UX-03",
    "UX-16",
    "CX-11",
    "UX-11",
    "UX-20",
    "UX-10",
    "CX-07",
    "UX-01",
    "BX-35",
    "BX-48",
    "BX-49",
    "CX-19",
    "BX-50",
    "BX-34",
    "CX-13",
    "CX-08",
    "CX-17",
    "CX-05",
    "BX-42",
    "BX-29",
    "BX-30",
    "BXG-22",
    "BXG-11",
]


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
        self.TW_TZ = timezone(
            timedelta(hours=8)
        )

        _extra_env = os.environ.get(
            "ESLITE_EXTRA_PRODUCTS",
            "",
        ).strip()

        self.EXTRA_PRODUCT_GUIDS = [
            g.strip()
            for g in _extra_env.split(",")
            if g.strip()
        ]

        self.CHECK_ROUNDS = int(
            os.environ.get(
                "CHECK_ROUNDS",
                "1",
            )
        )

        self.GMAIL_SENDER = os.environ[
            "GMAIL_SENDER"
        ]

        self.GMAIL_PASSWORD = os.environ[
            "GMAIL_PASSWORD"
        ]

        self.GMAIL_RECIPIENTS = [
            a.strip()
            for a in os.environ[
                "GMAIL_RECIPIENTS"
            ].split(",")
            if a.strip()
        ]

        self.AUTO_CHECKOUT = (
            os.environ.get(
                "AUTO_CHECKOUT",
                "true",
            ).lower()
            != "false"
        )

        self.ESLITE_ACCOUNT = os.environ.get(
            "ESLITE_ACCOUNT",
            "",
        )

        self.ESLITE_PASSWORD = os.environ.get(
            "ESLITE_PASSWORD",
            "",
        )

        self.CHECKOUT_CITY = os.environ.get(
            "CHECKOUT_CITY",
            "新竹市",
        )

        self.CHECKOUT_STORE_CODE = os.environ.get(
            "CHECKOUT_STORE_CODE",
            "B060",
        )

        self.CHECKOUT_MAX = int(
            os.environ.get(
                "CHECKOUT_MAX",
                "3",
            )
        )

        self.PARALLEL_CHECKOUT_LIMIT = int(
            os.environ.get(
                "PARALLEL_CHECKOUT_LIMIT",
                str(self.CHECKOUT_MAX),
            )
        )

        self.PARALLEL_SESSION_MODE = (
            os.environ.get(
                "ESLITE_PARALLEL_SESSION_MODE",
                "fresh_login",
            )
            .strip()
            .lower()
        )

        if self.PARALLEL_SESSION_MODE not in (
            "fresh_login",
            "storage",
        ):
            log.warning(
                "未知 ESLITE_PARALLEL_SESSION_MODE="
                f"{self.PARALLEL_SESSION_MODE}，"
                "改用 fresh_login"
            )

            self.PARALLEL_SESSION_MODE = (
                "fresh_login"
            )

        self.HEADLESS = (
            os.environ.get(
                "HEADLESS",
                "true",
            ).lower()
            != "false"
        )

        self.STORAGE_STATE_FILE = Path(
            os.environ.get(
                "STORAGE_STATE_FILE",
                "eslite_storage_state.json",
            )
        )

        self.ORDER_STATE_FILE = Path(
            os.environ.get(
                "ORDER_STATE_FILE",
                "eslite_order_state.json",
            )
        )

        self.NOTIFY_COOLDOWN = timedelta(
            hours=float(
                os.environ.get(
                    "NOTIFY_COOLDOWN_HOURS",
                    "1",
                )
            )
        )

        self.NOTIFY_STATE_FILE = Path(
            os.environ.get(
                "NOTIFY_STATE_FILE",
                "eslite_notify_state.json",
            )
        )

        _recip_raw = [
            r.strip()
            for r in os.environ.get(
                "ORDER_RECIPIENT",
                "",
            ).split(",")
            if r.strip()
        ]

        self.ORDER_RECIPIENTS = (
            _recip_raw
            or self.GMAIL_RECIPIENTS[:1]
        )

        self.EVENT_PAGE_URL = (
            os.environ.get(
                "ESLITE_EVENT_URL",
                "",
            ).strip()
        )

        self._session_file_lock = (
            threading.Lock()
        )

        self._login_fail_lock = (
            threading.Lock()
        )

        self._login_required_notified = False

        self._login_fail_count: int = 0
        self.LOGIN_FAIL_MAX: int = 2

        _purchased_env = (
            os.environ.get(
                "ESLITE_PURCHASED_NAMES",
                "",
            ).strip()
        )

        self.PURCHASED_NAMES: list = [
            n.strip().upper()
            for n in _purchased_env.split(",")
            if n.strip()
        ]

    def _is_purchased(
        self,
        product: dict,
    ) -> bool:

        name = (
            product.get(
                "name",
                "",
            ).upper()
        )

        return any(
            kw in name
            for kw in self.PURCHASED_NAMES
        )

    def _is_buy_whitelisted(
        self,
        product: dict,
    ) -> bool:

        name = (
            product.get(
                "name",
                "",
            ).upper()
        )

        return any(
            kw.upper() in name
            for kw in BUY_KEYWORDS
        )

    @abstractmethod
    def fetch_in_stock_products(
        self,
        page,
    ) -> list:
        pass

    def _mask(
        self,
        email: str,
    ) -> str:

        if "@" not in email:
            return "***"

        local, domain = email.split(
            "@",
            1,
        )

        return f"{local[0]}***@{domain}"

    def _send_email(
        self,
        recipients: list,
        subject: str,
        body: str,
    ):

        try:
            msg = MIMEText(
                body,
                "plain",
                "utf-8",
            )

            msg["Subject"] = subject
            msg["From"] = self.GMAIL_SENDER
            msg["To"] = ", ".join(
                recipients
            )

            with smtplib.SMTP_SSL(
                "smtp.gmail.com",
                465,
                timeout=15,
            ) as s:

                s.login(
                    self.GMAIL_SENDER,
                    self.GMAIL_PASSWORD,
                )

                s.sendmail(
                    self.GMAIL_SENDER,
                    recipients,
                    msg.as_string(),
                )

            log.info(
                "Email 發送成功 → "
                f"{[self._mask(r) for r in recipients]}"
            )

        except Exception as e:

            log.error(
                f"Email 發送失敗：{e}"
            )

    def _load_order_state(
        self,
    ) -> dict:

        if self.ORDER_STATE_FILE.exists():

            try:
                return json.loads(
                    self.ORDER_STATE_FILE.read_text(
                        encoding="utf-8"
                    )
                ).get(
                    "ordered",
                    {},
                )

            except Exception:
                return {}

        return {}

    def _save_order_state(
        self,
        ordered: dict,
    ):

        self.ORDER_STATE_FILE.write_text(
            json.dumps(
                {
                    "ordered": ordered,
                    "updated": datetime.now(
                        self.TW_TZ
                    ).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_notify_state(
        self,
    ) -> dict:

        if self.NOTIFY_STATE_FILE.exists():

            try:
                return json.loads(
                    self.NOTIFY_STATE_FILE.read_text(
                        encoding="utf-8"
                    )
                ).get(
                    "notified",
                    {},
                )

            except Exception as e:

                log.warning(
                    "讀取通知冷卻狀態失敗，"
                    f"視為無冷卻：{e}"
                )

        return {}

    def _save_notify_state(
        self,
        notified: dict,
    ):

        try:
            self.NOTIFY_STATE_FILE.write_text(
                json.dumps(
                    {
                        "notified": notified,
                        "updated": datetime.now(
                            self.TW_TZ
                        ).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        except Exception as e:

            log.warning(
                f"儲存通知冷卻狀態失敗：{e}"
            )

    def _parse_notify_ts(
        self,
        ts: str,
    ) -> datetime:

        dt = datetime.fromisoformat(
            ts
        )

        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=self.TW_TZ
            )

        return dt

    def _calc_order_qty(
        self,
        product: dict,
    ) -> int:

        qty = (
            product.get("stock")
            or 1
        )

        if product.get(
            "order_qty_limit"
        ):

            qty = min(
                qty,
                int(
                    product[
                        "order_qty_limit"
                    ]
                ),
            )

        if product.get(
            "account_qty_limit"
        ):

            qty = min(
                qty,
                int(
                    product[
                        "account_qty_limit"
                    ]
                ),
            )

        return max(
            1,
            qty,
        )

    def _get_login_fail_count(
        self,
    ) -> int:

        with self._login_fail_lock:
            return self._login_fail_count

    def _record_login_failure(
        self,
    ) -> int:

        with self._login_fail_lock:

            self._login_fail_count += 1

            return self._login_fail_count

    def _load_storage_state_data(
        self,
    ):

        with self._session_file_lock:

            if not self.STORAGE_STATE_FILE.exists():
                return None

            try:
                return json.loads(
                    self.STORAGE_STATE_FILE.read_text(
                        encoding="utf-8"
                    )
                )

            except Exception as e:

                log.warning(
                    "讀取 session state 失敗，"
                    f"將重新登入：{e}"
                )

                return None

    def _persist_storage_state(
        self,
        ctx,
    ):

        try:
            state = ctx.storage_state()

            with self._session_file_lock:

                tmp_path = (
                    self.STORAGE_STATE_FILE.with_name(
                        f"{self.STORAGE_STATE_FILE.name}."
                        f"{threading.get_ident()}.tmp"
                    )
                )

                tmp_path.write_text(
                    json.dumps(
                        state,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                os.replace(
                    tmp_path,
                    self.STORAGE_STATE_FILE,
                )

            log.info(
                "Session 狀態已更新："
                f"{self.STORAGE_STATE_FILE}"
            )

        except Exception as e:

            log.warning(
                f"更新 session 狀態失敗：{e}"
            )

    def _fetch_single_product(
        self,
        page,
        guid: str,
    ):

        api_url = (
            f"{self.ATHENA_BASE}"
            f"/api/v1/products/{guid}"
        )

        try:
            page.goto(
                api_url,
                wait_until="domcontentloaded",
                timeout=20000,
            )

            page.wait_for_timeout(
                1000
            )

            try:
                raw = page.inner_text(
                    "pre"
                )

            except Exception:
                raw = page.inner_text(
                    "body"
                )

            data = json.loads(
                raw
            )

            name = data.get(
                "name",
                f"商品 {guid}",
            )

            product_guid = str(
                data.get(
                    "product_guid",
                    guid,
                )
            )

            stock = int(
                data.get(
                    "stock"
                )
                or 0
            )

            btn_status = data.get(
                "product_button_status",
                "",
            )

            in_stock = (
                stock > 0
                or btn_status
                == "add_to_shopping_cart"
            )

            if not in_stock:

                log.info(
                    f"個別商品無庫存：{name}"
                    f"（{btn_status}，"
                    f"stock={stock}）"
                )

                return None

            acct_lim = data.get(
                "account_qty_limit"
            )

            ord_lim = data.get(
                "order_qty_limit"
            )

            log.info(
                f"有庫存 → {name}"
                f"（{product_guid}）"
                f" | 庫存:{stock} 件"
                " | "
                + (
                    f"帳號上限:{acct_lim}件"
                    if acct_lim
                    else "無限購"
                )
                + f" | {btn_status}"
            )

            return {
                "guid": product_guid,
                "name": name,
                "stock": stock,
                "status": btn_status,
                "account_qty_limit": acct_lim,
                "order_qty_limit": ord_lim,
                "url": (
                    f"{self.ESLITE_BASE}"
                    f"/product/{product_guid}"
                ),
            }

        except Exception as e:

            log.error(
                "個別商品查詢失敗："
                f"{guid} | {e}"
            )

            return None

    def _is_logged_in(
        self,
        page,
    ) -> bool:

        try:
            page.goto(
                f"{self.ESLITE_BASE}/member",
                wait_until="domcontentloaded",
                timeout=15000,
            )

            page.wait_for_timeout(
                2000
            )

            if "/login" in page.url:
                return False

            if page.query_selector(
                "a[href*='logout'], "
                "button:has-text('登出'), "
                "a:has-text('登出'), "
                "[class*='logout'], "
                "[data-testid*='logout']"
            ):
                return True

            if page.query_selector(
                "input[type='password']"
            ):
                return False

            if page.query_selector(
                "button:has-text('登入'), "
                "a:has-text('會員登入')"
            ):
                return False

            return False

        except Exception:
            return False

    def _do_login(
        self,
        page,
        ctx,
    ) -> bool:

        if (
            not self.ESLITE_ACCOUNT
            or not self.ESLITE_PASSWORD
        ):

            log.warning(
                "未設定 ESLITE_ACCOUNT / "
                "ESLITE_PASSWORD，"
                "跳過自動下單"
            )

            return False

        try:
            log.info(
                "登入誠品帳號："
                f"{self.ESLITE_ACCOUNT}"
            )

            page.goto(
                f"{self.ESLITE_BASE}/login",
                wait_until="domcontentloaded",
                timeout=20000,
            )

            page.wait_for_timeout(
                2000
            )

            page.get_by_role(
                "textbox",
                name="台灣手機/會員卡號/自訂帳號",
            ).click()

            page.get_by_role(
                "textbox",
                name="台灣手機/會員卡號/自訂帳號",
            ).fill(
                self.ESLITE_ACCOUNT
            )

            page.get_by_role(
                "textbox",
                name="請輸入密碼",
            ).click()

            page.get_by_role(
                "textbox",
                name="請輸入密碼",
            ).fill(
                self.ESLITE_PASSWORD
            )

            page.get_by_role(
                "button",
                name="登入",
            ).click()

            page.wait_for_timeout(
                1500
            )

            if not self.HEADLESS:

                log.info(
                    "若出現圖形驗證碼，"
                    "請完成後腳本自動繼續"
                    "（等待最多 120 秒）"
                )

                try:
                    page.wait_for_function(
                        "() => "
                        "!window.location.pathname"
                        ".startsWith('/login')",
                        timeout=120_000,
                    )

                except Exception:
                    pass

            else:

                page.wait_for_timeout(
                    4000
                )

            sms_indicators = [
                "verify",
                "otp",
                "sms",
                "驗證碼",
            ]

            sms_detected = any(
                kw in page.url.lower()
                for kw in sms_indicators
            )

            if not sms_detected:

                try:
                    if page.query_selector(
                        "input[placeholder*='驗證碼'], "
                        "input[name*='otp'], "
                        "input[name*='sms']"
                    ):

                        sms_detected = True

                except Exception:
                    pass

            if sms_detected:

                if not self.HEADLESS:

                    log.info(
                        "偵測到簡訊驗證，"
                        "請在瀏覽器中輸入驗證碼"
                        "（等待 90 秒）..."
                    )

                    page.wait_for_timeout(
                        90_000
                    )

                    if "/login" not in page.url:

                        self._persist_storage_state(
                            ctx
                        )

                        log.info(
                            "簡訊驗證完成，"
                            "session 已儲存至 "
                            f"{self.STORAGE_STATE_FILE}"
                        )

                        return True

                log.warning(
                    "需要簡訊驗證碼，"
                    "無法在 GitHub Actions 中"
                    "自動完成"
                )

                self._record_login_failure()

                self._notify_login_required_once()

                return False

            if "/login" in page.url:

                try:
                    page.screenshot(
                        path="debug_login.png"
                    )

                except Exception:
                    pass

                self._record_login_failure()

                log.error(
                    "登入失敗"
                    "（仍停在登入頁）："
                    f"{page.url}"
                )

                return False

            self._persist_storage_state(
                ctx
            )

            log.info(
                "登入成功，"
                "session 已儲存至 "
                f"{self.STORAGE_STATE_FILE}"
            )

            return True

        except Exception as e:

            self._record_login_failure()

            log.error(
                f"登入時發生例外：{e}"
            )

            return False

    def _ensure_logged_in(
        self,
        page,
        ctx,
    ) -> bool:

        if self._is_logged_in(
            page
        ):

            log.info(
                "Session 有效，"
                "已登入誠品"
            )

            return True

        log.info(
            "Session 已失效，"
            "嘗試重新登入..."
        )

        return self._do_login(
            page,
            ctx,
        )

    def _cart_contains_product(
        self,
        page,
        guid: str,
    ) -> bool:
        """
        檢查目前登入 session 的購物車是否已經有指定 GUID。

        不做任何刪除動作。
        若上一輪已成功加車但結帳失敗，
        下一輪可直接利用購物車內既有商品重新結帳。

        回傳 True 時，page 會停在 /cart。
        """

        try:
            page.goto(
                f"{self.ESLITE_BASE}/cart",
                wait_until="domcontentloaded",
                timeout=15000,
            )

            page.wait_for_timeout(
                700
            )

            selectors = [
                f'a[href*="/product/{guid}"]',
                f'a[href*="{guid}"]',
                f'[data-product-guid="{guid}"]',
                f'[data-guid="{guid}"]',
            ]

            for selector in selectors:

                try:
                    node = page.query_selector(
                        selector
                    )

                    if node:

                        log.info(
                            f"[{guid}] "
                            "購物車已存在目標商品，"
                            "略過再次加入購物車"
                        )

                        return True

                except Exception:
                    pass

            log.info(
                f"[{guid}] "
                "購物車未找到目標商品，"
                "需要重新加入購物車"
            )

            return False

        except Exception as e:

            log.warning(
                f"[{guid}] "
                "檢查購物車失敗，"
                "改走重新加車流程："
                f"{e}"
            )

            return False

    def _add_to_cart(
        self,
        page,
        guid: str,
        qty: int = 1,
    ) -> bool:

        url = (
            f"{self.ESLITE_BASE}"
            f"/product/{guid}"
        )

        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000,
            )

            try:
                page.wait_for_selector(
                    "button:has-text('加入購物車'), "
                    "button:has-text('立即購買')",
                    timeout=10000,
                )

            except Exception:
                pass

            if qty > 1:

                try:
                    qty_input = (
                        page.query_selector(
                            "input[type='number'][class*='qty'], "
                            "input[type='number'][class*='quantity'], "
                            "input[type='number'][name*='qty'], "
                            "input[type='number']"
                        )
                    )

                    if qty_input:

                        qty_input.triple_click()

                        qty_input.fill(
                            str(qty)
                        )

                        log.info(
                            "已設定購買數量："
                            f"{qty}"
                        )

                except Exception:
                    pass

            btn = (
                page.query_selector(
                    "button:has-text('加入購物車')"
                )
                or page.query_selector(
                    "button:has-text('立即購買')"
                )
                or page.query_selector(
                    "[data-testid*='add-to-cart']"
                )
                or page.query_selector(
                    "[class*='add-to-cart']"
                )
                or page.query_selector(
                    "[class*='addToCart']"
                )
            )

            if not btn:

                log.warning(
                    "找不到加入購物車按鈕："
                    f"{guid}"
                )

                try:
                    page.screenshot(
                        path=(
                            "debug_product_"
                            f"{guid[:8]}.png"
                        )
                    )

                except Exception:
                    pass

                return False

            if not btn.is_enabled():

                log.warning(
                    "加入購物車按鈕"
                    "為禁用狀態："
                    f"{guid}"
                )

                return False

            btn.click()

            page.wait_for_timeout(
                2000
            )

            try:
                confirm = (
                    page.query_selector(
                        "button:has-text('確認')"
                    )
                )

                if (
                    confirm
                    and confirm.is_visible()
                ):

                    confirm.click()

                    page.wait_for_timeout(
                        500
                    )

            except Exception:
                pass

            log.info(
                "已加入購物車："
                f"{guid}"
                f"（數量 {qty}）"
            )

            return True

        except Exception as e:

            log.error(
                "加入購物車失敗："
                f"{guid} | {e}"
            )

            return False

    def _checkout(
        self,
        page,
        cart_already_open: bool = False,
    ):
        """
        誠品門市取貨 + ATM 轉帳結帳。

        cart_already_open=True：
        代表前一步已經在 /cart 找到目標商品，
        不重新載入購物車頁。
        """

        try:
            if not cart_already_open:

                page.goto(
                    f"{self.ESLITE_BASE}/cart",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )

                page.wait_for_timeout(
                    3000
                )

            else:

                page.wait_for_timeout(
                    300
                )

            checkout_btn = (
                page.query_selector(
                    "button:has-text('結帳')"
                )
                or page.query_selector(
                    "a:has-text('結帳')"
                )
                or page.query_selector(
                    "[class*='checkout'] button"
                )
                or page.query_selector(
                    "[class*='cart-checkout']"
                )
            )

            if not checkout_btn:

                log.error(
                    "找不到結帳按鈕"
                )

                try:
                    page.screenshot(
                        path="debug_cart.png"
                    )

                except Exception:
                    pass

                return None

            checkout_btn.click()

            page.wait_for_timeout(
                2000
            )

            page.get_by_title(
                "誠品門市取貨"
            ).click()

            page.wait_for_timeout(
                1500
            )

            page.evaluate(
                "window.scrollBy(0, 400)"
            )

            page.wait_for_timeout(
                1000
            )

            try:
                page.wait_for_selector(
                    "select[name='recipientCity'], "
                    "select[name*='city']",
                    timeout=8000,
                )

                city_sel = (
                    page.locator(
                        "select[name='recipientCity']"
                    ).first
                    or page.locator(
                        "select[name*='city']"
                    ).first
                )

                city_sel.select_option(
                    self.CHECKOUT_CITY
                )

            except Exception:

                page.get_by_role(
                    "combobox"
                ).nth(
                    2
                ).select_option(
                    self.CHECKOUT_CITY
                )

            page.wait_for_timeout(
                1000
            )

            page.wait_for_selector(
                "select[name='recipientEsliteStore']",
                timeout=8000,
            )

            page.locator(
                "select[name='recipientEsliteStore']"
            ).select_option(
                self.CHECKOUT_STORE_CODE
            )

            page.wait_for_timeout(
                1000
            )

            page.get_by_title(
                "ATM轉帳"
            ).click()

            page.wait_for_timeout(
                1000
            )

            page.get_by_role(
                "button",
                name="確認結帳",
            ).click()

            page.wait_for_url(
                "**/cart/step3**",
                timeout=20000,
            )

            params = (
                urllib.parse.parse_qs(
                    urllib.parse.urlparse(
                        page.url
                    ).query
                )
            )

            order_id = params.get(
                "orderid",
                [None],
            )[0]

            if order_id:

                log.info(
                    "結帳成功！"
                    "訂單編號："
                    f"{order_id}"
                )

            else:

                log.warning(
                    "結帳完成但未擷取訂單編號，"
                    f"URL：{page.url}"
                )

            return order_id

        except Exception as e:

            log.error(
                f"結帳失敗：{e}",
                exc_info=True,
            )

            try:
                page.goto(
                    "about:blank"
                )

            except Exception:
                pass

            return None

    def _notify_products(
        self,
        products: list,
    ):

        regular = [
            p
            for p in products
            if not self._is_purchased(p)
        ]

        purchased = [
            p
            for p in products
            if self._is_purchased(p)
        ]

        if regular:

            count = len(
                regular
            )

            subject = (
                "【誠品有貨了！】"
                f"偵測到 {count} 件商品"
            )

            lines = [
                (
                    "誠品戰鬥陀螺專區"
                    f"偵測到共 {count} 件有庫存商品"
                ),
                (
                    "偵測時間："
                    f"{datetime.now(self.TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                    "（台灣時間）"
                ),
                "",
                (
                    ">>> 立即結帳："
                    f"{self.ESLITE_BASE}/cart/step2"
                ),
                (
                    ">>> 查看購物車："
                    f"{self.ESLITE_BASE}/cart"
                ),
                "",
                "=" * 50,
            ]

            for i, p in enumerate(
                regular,
                1,
            ):

                acct_lim = p.get(
                    "account_qty_limit"
                )

                ord_lim = p.get(
                    "order_qty_limit"
                )

                lines += [
                    f"\n【商品 {i}】",
                    f"商品名：{p['name']}",
                    f"庫存：{p['stock']} 件",
                    (
                        "帳號上限："
                        + (
                            "無限制"
                            if acct_lim is None
                            else f"{acct_lim} 件"
                        )
                    ),
                    (
                        "每單上限："
                        + (
                            "無限制"
                            if ord_lim is None
                            else f"{ord_lim} 件"
                        )
                    ),
                    f"商品連結：{p['url']}",
                    "-" * 40,
                ]

            if self.EVENT_PAGE_URL:

                lines += [
                    "",
                    (
                        "完整專區頁："
                        f"{self.EVENT_PAGE_URL}"
                    ),
                ]

            self._send_email(
                self.GMAIL_RECIPIENTS,
                subject,
                "\n".join(
                    lines
                ),
            )

        if purchased:

            count = len(
                purchased
            )

            subject = (
                "【誠品有貨！已購買商品】"
                f"偵測到 {count} 件"
            )

            lines = [
                (
                    "您已購買的商品有庫存，"
                    "僅通知，不自動下單。"
                ),
                (
                    "偵測時間："
                    f"{datetime.now(self.TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                    "（台灣時間）"
                ),
                "",
                "=" * 50,
            ]

            for i, p in enumerate(
                purchased,
                1,
            ):

                acct_lim = p.get(
                    "account_qty_limit"
                )

                ord_lim = p.get(
                    "order_qty_limit"
                )

                lines += [
                    (
                        f"\n【商品 {i}】"
                        "【已購買，僅通知不下單】"
                    ),
                    f"商品名：{p['name']}",
                    f"庫存：{p['stock']} 件",
                    (
                        "帳號上限："
                        + (
                            "無限制"
                            if acct_lim is None
                            else f"{acct_lim} 件"
                        )
                    ),
                    (
                        "每單上限："
                        + (
                            "無限制"
                            if ord_lim is None
                            else f"{ord_lim} 件"
                        )
                    ),
                    f"商品連結：{p['url']}",
                    "-" * 40,
                ]

            if self.EVENT_PAGE_URL:

                lines += [
                    "",
                    (
                        "完整專區頁："
                        f"{self.EVENT_PAGE_URL}"
                    ),
                ]

            self._send_email(
                self.ORDER_RECIPIENTS,
                subject,
                "\n".join(
                    lines
                ),
            )

    def _notify_cart_added(
        self,
        products: list,
    ):

        count = len(
            products
        )

        subject = (
            "【誠品購物車已更新！】"
            f"{count} 件商品已入購物車，"
            "請前往結帳"
        )

        now_str = (
            datetime.now(
                self.TW_TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        lines = [
            (
                f"已將 {count} 件商品"
                "加入誠品購物車！"
            ),
            f"時間：{now_str}（台灣時間）",
            "",
            (
                ">>> 立即結帳（一鍵進入）："
                f"{self.ESLITE_BASE}/cart/step2"
            ),
            (
                ">>> 查看購物車："
                f"{self.ESLITE_BASE}/cart"
            ),
            "",
            "=" * 50,
        ]

        for i, p in enumerate(
            products,
            1,
        ):

            acct_lim = p.get(
                "account_qty_limit"
            )

            ord_lim = p.get(
                "order_qty_limit"
            )

            qty = self._calc_order_qty(
                p
            )

            lines += [
                f"\n【商品 {i}】",
                f"商品名：{p['name']}",
                f"加入數量：{qty} 件",
                (
                    "庫存（加入時）："
                    f"{p.get('stock', '?')} 件"
                ),
                (
                    "帳號上限："
                    + (
                        "無限制"
                        if acct_lim is None
                        else f"{acct_lim} 件"
                    )
                ),
                (
                    "每單上限："
                    + (
                        "無限制"
                        if ord_lim is None
                        else f"{ord_lim} 件"
                    )
                ),
                f"商品連結：{p['url']}",
                "-" * 40,
            ]

        self._send_email(
            self.ORDER_RECIPIENTS,
            subject,
            "\n".join(
                lines
            ),
        )

    def _notify_order(
        self,
        products: list,
        order_id: str,
    ):

        count = len(
            products
        )

        subject = (
            "【誠品自動下單成功！】"
            f"已下單 {count} 件商品"
            f"｜訂單 {order_id}"
        )

        now_str = (
            datetime.now(
                self.TW_TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        lines = [
            "誠品自動下單成功！",
            f"訂單編號：{order_id}",
            f"下單時間：{now_str}（台灣時間）",
            (
                "付款方式：ATM 轉帳"
                "（請記得在期限內完成匯款）"
            ),
            (
                "取貨門市："
                f"{self.CHECKOUT_CITY}－"
                f"{self.CHECKOUT_STORE_CODE}"
            ),
            "",
            "=" * 50,
        ]

        for i, p in enumerate(
            products,
            1,
        ):

            lines += [
                f"\n【商品 {i}】",
                f"商品名：{p['name']}",
                (
                    "庫存（下單時）："
                    f"{p.get('stock', '?')} 件"
                ),
                f"商品連結：{p['url']}",
                "-" * 40,
            ]

        lines += [
            "",
            (
                "查看訂單："
                f"{self.ESLITE_BASE}/member/orders"
            ),
        ]

        self._send_email(
            self.ORDER_RECIPIENTS,
            subject,
            "\n".join(
                lines
            ),
        )

    def _notify_login_required(
        self,
    ):

        subject = (
            "【誠品監控警告】"
            "需要手動重新登入"
        )

        body = "\n".join(
            [
                (
                    "誠品自動下單失敗："
                    "登入時偵測到簡訊驗證要求。"
                ),
                "",
                (
                    "請在本機執行以下步驟"
                    "恢復自動下單："
                ),
                (
                    "  1. cd eslite_monitor "
                    "&& python generate_session.py"
                ),
                (
                    "  2. 在彈出的瀏覽器中"
                    "完成登入/驗證"
                ),
                (
                    "  3. gh secret set "
                    "ESLITE_STORAGE_STATE_B64 \\"
                ),
                (
                    '       --body "$(base64 -i '
                    "eslite_storage_state.json "
                    "| tr -d '\\n')\" \\"
                ),
                (
                    "       --repo "
                    "k0341055/beyblade-funbox-monitor"
                ),
                "",
                (
                    "本次偵測時間："
                    f"{datetime.now(self.TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                    "（台灣時間）"
                ),
            ]
        )

        self._send_email(
            self.ORDER_RECIPIENTS,
            subject,
            body,
        )

    def _notify_login_required_once(
        self,
    ):

        should_send = False

        with self._login_fail_lock:

            if not self._login_required_notified:

                self._login_required_notified = True

                should_send = True

        if should_send:

            self._notify_login_required()

    def _prepare_checkout_session(
        self,
    ):

        if (
            self._get_login_fail_count()
            >= self.LOGIN_FAIL_MAX
        ):

            log.warning(
                "本次執行已累計 "
                f"{self._get_login_fail_count()} "
                "次登入失敗，"
                "放棄自動下單"
                "（僅繼續通知）"
            )

            return None

        try:
            with sync_playwright() as pw:

                br = pw.chromium.launch(
                    headless=self.HEADLESS,
                    args=[
                        "--disable-blink-features="
                        "AutomationControlled"
                    ],
                )

                try:
                    ctx_kwargs = {
                        "user_agent": self._UA
                    }

                    state = (
                        self._load_storage_state_data()
                    )

                    if state:

                        ctx_kwargs[
                            "storage_state"
                        ] = state

                    ctx = br.new_context(
                        **ctx_kwargs
                    )

                    ctx.add_init_script(
                        "Object.defineProperty("
                        "navigator, 'webdriver', "
                        "{get: () => undefined})"
                    )

                    page = ctx.new_page()

                    if not self._ensure_logged_in(
                        page,
                        ctx,
                    ):

                        log.warning(
                            "無法登入誠品，"
                            "跳過本輪平行自動下單"
                        )

                        ctx.close()

                        return None

                    state = ctx.storage_state()

                    self._persist_storage_state(
                        ctx
                    )

                    ctx.close()

                    return state

                finally:

                    br.close()

        except Exception as e:

            log.error(
                "準備平行下單 session 失敗："
                f"{e}",
                exc_info=True,
            )

            return None

    def _checkout_one_product(
        self,
        product: dict,
        session_state=None,
    ) -> dict:
        """
        一商品 = 一個獨立 thread / Chromium / Context / Page。

        購物車策略：
        1. 先檢查目標 GUID 是否已在購物車
        2. 已存在 → 直接結帳
        3. 不存在 → 加入商品 → 結帳
        4. 不清空購物車
        5. 結帳失敗時下一輪仍可再次重試
        """

        guid = product[
            "guid"
        ]

        result = {
            "product": product,
            "guid": guid,
            "added": False,
            "reused_cart": False,
            "order_id": None,
            "status": "failed",
        }

        def _new_ctx(
            br,
            storage=None,
        ):

            kwargs = {
                "user_agent": self._UA,
            }

            if storage:

                kwargs[
                    "storage_state"
                ] = storage

            ctx = br.new_context(
                **kwargs
            )

            ctx.add_init_script(
                "Object.defineProperty("
                "navigator, 'webdriver', "
                "{get: () => undefined})"
            )

            return ctx

        try:
            with sync_playwright() as pw:

                br = pw.chromium.launch(
                    headless=self.HEADLESS,
                    args=[
                        "--disable-blink-features="
                        "AutomationControlled"
                    ],
                )

                ctx = None

                try:
                    if (
                        self.PARALLEL_SESSION_MODE
                        == "fresh_login"
                    ):

                        ctx = _new_ctx(
                            br
                        )

                        page = (
                            ctx.new_page()
                        )

                        log.info(
                            f"[{guid}] "
                            "平行 worker 啟動"
                            "（fresh_login）："
                            f"{product['name']}"
                        )

                        if not self._ensure_logged_in(
                            page,
                            ctx,
                        ):

                            if session_state:

                                log.warning(
                                    f"[{guid}] "
                                    "fresh_login 失敗，"
                                    "改用預先儲存的 "
                                    "storage_state 重試"
                                )

                                try:
                                    ctx.close()

                                except Exception:
                                    pass

                                ctx = _new_ctx(
                                    br,
                                    session_state,
                                )

                                page = (
                                    ctx.new_page()
                                )

                                if not self._is_logged_in(
                                    page
                                ):

                                    log.warning(
                                        f"[{guid}] "
                                        "storage_state "
                                        "也無法登入，"
                                        "跳過此商品"
                                    )

                                    result[
                                        "status"
                                    ] = "login_failed"

                                    return result

                                log.info(
                                    f"[{guid}] "
                                    "storage_state "
                                    "fallback 登入有效"
                                )

                            else:

                                log.warning(
                                    f"[{guid}] "
                                    "無法登入誠品，"
                                    "跳過此商品"
                                )

                                result[
                                    "status"
                                ] = "login_failed"

                                return result

                    else:

                        if not session_state:

                            log.warning(
                                f"[{guid}] "
                                "storage 模式沒有"
                                "可用 session_state"
                            )

                            result[
                                "status"
                            ] = "login_failed"

                            return result

                        ctx = _new_ctx(
                            br,
                            session_state,
                        )

                        page = (
                            ctx.new_page()
                        )

                        log.info(
                            f"[{guid}] "
                            "平行 worker 啟動"
                            "（storage）："
                            f"{product['name']}"
                        )

                        if not self._ensure_logged_in(
                            page,
                            ctx,
                        ):

                            log.warning(
                                f"[{guid}] "
                                "無法登入誠品，"
                                "跳過此商品"
                            )

                            result[
                                "status"
                            ] = "login_failed"

                            return result

                    cart_has_target = (
                        self._cart_contains_product(
                            page,
                            guid,
                        )
                    )

                    if cart_has_target:

                        result[
                            "reused_cart"
                        ] = True

                        result[
                            "status"
                        ] = "cart_reused"

                        log.info(
                            f"[{guid}] "
                            "購物車已有目標商品，"
                            "直接開始結帳..."
                        )

                        order_id = self._checkout(
                            page,
                            cart_already_open=True,
                        )

                    else:

                        qty = (
                            self._calc_order_qty(
                                product
                            )
                        )

                        if not self._add_to_cart(
                            page,
                            guid,
                            qty,
                        ):

                            log.warning(
                                f"[{guid}] "
                                "加入購物車失敗："
                                f"{product['name']}"
                            )

                            result[
                                "status"
                            ] = "add_failed"

                            return result

                        result[
                            "added"
                        ] = True

                        result[
                            "status"
                        ] = "cart_added"

                        self._notify_cart_added(
                            [product]
                        )

                        log.info(
                            f"[{guid}] "
                            "已加入購物車，"
                            "開始獨立自動結帳..."
                        )

                        order_id = self._checkout(
                            page
                        )

                    if order_id:

                        result[
                            "order_id"
                        ] = order_id

                        result[
                            "status"
                        ] = "success"

                        log.info(
                            f"[{guid}] "
                            "✅ 獨立結帳成功："
                            f"訂單 {order_id}"
                        )

                    else:

                        result[
                            "status"
                        ] = "checkout_failed"

                        log.warning(
                            f"[{guid}] "
                            "自動結帳失敗；"
                            "下一輪仍可再次檢查"
                            "購物車並重試。"
                        )

                    return result

                finally:

                    if ctx is not None:

                        try:
                            ctx.close()

                        except Exception:
                            pass

                    br.close()

        except Exception as e:

            log.error(
                f"[{guid}] "
                "平行下單 worker 例外："
                f"{e}",
                exc_info=True,
            )

            result[
                "status"
            ] = "exception"

            return result

    def _attempt_checkout(
        self,
        in_stock_products: list,
    ):

        if (
            self._get_login_fail_count()
            >= self.LOGIN_FAIL_MAX
        ):

            log.warning(
                "本次執行已累計 "
                f"{self._get_login_fail_count()} "
                "次登入失敗，"
                "放棄自動下單"
                "（僅繼續通知）"
            )

            return {}

        ordered = (
            self._load_order_state()
        )

        whitelist_products = [
            p
            for p in in_stock_products
            if self._is_buy_whitelisted(
                p
            )
        ]

        blocked_by_whitelist = (
            len(in_stock_products)
            - len(whitelist_products)
        )

        if blocked_by_whitelist:

            log.info(
                "BUY_KEYWORDS 白名單外商品"
                "（僅通知，不下單）："
                f"{blocked_by_whitelist} 件"
            )

        to_order = [
            p
            for p in whitelist_products
            if (
                p["guid"]
                not in ordered
            )
            and (
                not self._is_purchased(
                    p
                )
            )
        ][
            :self.CHECKOUT_MAX
        ]

        if not to_order:

            log.info(
                "所有白名單有庫存商品"
                "均已成功下單或已列為已購買，"
                "跳過自動下單"
            )

            return {}

        log.info(
            "平行下單準備："
            f"{len(to_order)} 件商品"
            " | CHECKOUT_MAX="
            f"{self.CHECKOUT_MAX}"
            " | PARALLEL_CHECKOUT_LIMIT="
            f"{self.PARALLEL_CHECKOUT_LIMIT}"
            " | SESSION_MODE="
            f"{self.PARALLEL_SESSION_MODE}"
        )

        if (
            self.PARALLEL_SESSION_MODE
            == "storage"
        ):

            session_state = (
                self._prepare_checkout_session()
            )

            if not session_state:
                return {}

        else:

            session_state = (
                self._load_storage_state_data()
            )

        max_workers = max(
            1,
            min(
                self.PARALLEL_CHECKOUT_LIMIT,
                len(to_order),
            ),
        )

        results = {}

        with ThreadPoolExecutor(
            max_workers=max_workers
        ) as executor:

            futures = {
                executor.submit(
                    self._checkout_one_product,
                    p,
                    session_state,
                ): p
                for p in to_order
            }

            for future in as_completed(
                futures
            ):

                p = futures[
                    future
                ]

                guid = p[
                    "guid"
                ]

                try:

                    result = (
                        future.result()
                    )

                except Exception as e:

                    log.error(
                        f"[{guid}] "
                        "checkout future 例外："
                        f"{e}",
                        exc_info=True,
                    )

                    result = {
                        "product": p,
                        "guid": guid,
                        "added": False,
                        "reused_cart": False,
                        "order_id": None,
                        "status": "exception",
                    }

                results[
                    guid
                ] = result

                order_id = (
                    result.get(
                        "order_id"
                    )
                )

                if order_id:

                    now_str = (
                        datetime.now(
                            self.TW_TZ
                        ).isoformat()
                    )

                    ordered[
                        guid
                    ] = {
                        "order_id": order_id,
                        "ordered_at": now_str,
                    }

                    self._save_order_state(
                        ordered
                    )

                    self._notify_order(
                        [p],
                        order_id,
                    )

                elif (
                    result.get(
                        "added"
                    )
                    or result.get(
                        "reused_cart"
                    )
                ):

                    log.warning(
                        f"[{guid}] "
                        "購物車流程已完成但"
                        "未成功建立訂單；"
                        "不寫入 ORDER_STATE_FILE，"
                        "下一輪仍可再次檢查"
                        "購物車並重試結帳。"
                    )

        success_count = sum(
            1
            for r in results.values()
            if r.get(
                "order_id"
            )
        )

        log.info(
            "平行下單完成："
            f"成功 {success_count}/"
            f"{len(to_order)} 件，"
            "失敗/未完成 "
            f"{len(to_order) - success_count} 件"
        )

        return results

    def check_once(
        self,
        page,
    ) -> bool:

        try:
            products = (
                self.fetch_in_stock_products(
                    page
                )
            )

            now = datetime.now(
                self.TW_TZ
            )

            cutoff = (
                now
                - self.NOTIFY_COOLDOWN
            )

            notified = (
                self._load_notify_state()
            )

            current_guids = {
                p["guid"]
                for p in products
            }

            notified = {
                guid: ts
                for guid, ts
                in notified.items()
                if guid in current_guids
            }

            if not products:

                self._save_notify_state(
                    notified
                )

                log.info(
                    "目前無庫存商品，"
                    "繼續監控"
                )

                return True

            to_notify = []

            for p in products:

                guid = p[
                    "guid"
                ]

                last_ts = (
                    notified.get(
                        guid
                    )
                )

                if not last_ts:

                    to_notify.append(
                        p
                    )

                    continue

                try:

                    if (
                        self._parse_notify_ts(
                            last_ts
                        )
                        < cutoff
                    ):

                        to_notify.append(
                            p
                        )

                except Exception:

                    to_notify.append(
                        p
                    )

            checkout_products = []

            if (
                self.AUTO_CHECKOUT
                and self.ESLITE_ACCOUNT
            ):

                whitelisted_products = [
                    p
                    for p in products
                    if self._is_buy_whitelisted(
                        p
                    )
                ]

                blocked_by_whitelist = (
                    len(products)
                    - len(
                        whitelisted_products
                    )
                )

                if blocked_by_whitelist:

                    log.info(
                        "BUY_KEYWORDS 白名單外商品"
                        "（僅通知，不下單）："
                        f"{blocked_by_whitelist} 件"
                    )

                checkout_products = [
                    p
                    for p
                    in whitelisted_products
                    if not any(
                        kw.upper()
                        in p["name"].upper()
                        for kw in SKIP_KEYWORDS
                    )
                ]

                skipped = (
                    len(whitelisted_products)
                    - len(checkout_products)
                )

                if skipped:

                    log.info(
                        "SKIP_KEYWORDS 商品"
                        "（已在白名單，"
                        "但仍僅通知不下單）："
                        f"{skipped} 件"
                    )

            def _send_due_notifications():

                if not to_notify:

                    log.info(
                        f"所有 {len(products)} "
                        "件有庫存商品均在 "
                        "1 小時通知冷卻期內"
                    )

                    return

                log.info(
                    "發送庫存通知："
                    f"{len(to_notify)} 件"
                    f"（共 {len(products)} 件，"
                    "冷卻中 "
                    f"{len(products) - len(to_notify)} 件）"
                )

                self._notify_products(
                    to_notify
                )

                notified_at = (
                    now.isoformat()
                )

                for p in to_notify:

                    notified[
                        p["guid"]
                    ] = notified_at

            if checkout_products:

                with ThreadPoolExecutor(
                    max_workers=1
                ) as buy_executor:

                    buy_future = (
                        buy_executor.submit(
                            self._attempt_checkout,
                            checkout_products,
                        )
                    )

                    _send_due_notifications()

                    self._save_notify_state(
                        notified
                    )

                    try:

                        buy_future.result()

                    except Exception as e:

                        log.error(
                            "平行自動下單"
                            "主流程例外："
                            f"{e}",
                            exc_info=True,
                        )

            else:

                _send_due_notifications()

                self._save_notify_state(
                    notified
                )

            return True

        except Exception as e:

            log.error(
                f"執行例外：{e}",
                exc_info=True,
            )

            try:
                page.goto(
                    "about:blank"
                )

            except Exception:
                pass

            return False

    def run(
        self,
    ):

        log.info(
            f"{self.__class__.__name__}"
            f" | 輪數：{self.CHECK_ROUNDS}"
            " | 自動下單："
            f"{'啟用' if self.AUTO_CHECKOUT else '停用'}"
            " | 平行上限："
            f"{self.PARALLEL_CHECKOUT_LIMIT}"
            " | Session 模式："
            f"{self.PARALLEL_SESSION_MODE}"
            " | 通知冷卻："
            f"{self.NOTIFY_COOLDOWN.total_seconds() / 3600:g} 小時"
        )

        with sync_playwright() as pw:

            br = pw.chromium.launch(
                headless=self.HEADLESS,
                args=[
                    "--disable-blink-features="
                    "AutomationControlled"
                ],
            )

            ctx = br.new_context(
                user_agent=self._UA
            )

            ctx.add_init_script(
                "Object.defineProperty("
                "navigator, 'webdriver', "
                "{get: () => undefined})"
            )

            page = (
                ctx.new_page()
            )

            for round_num in range(
                1,
                self.CHECK_ROUNDS + 1,
            ):

                if self.CHECK_ROUNDS > 1:

                    log.info(
                        f"── 第 {round_num}/"
                        f"{self.CHECK_ROUNDS} 輪 ──"
                    )

                self.check_once(
                    page
                )

                if (
                    round_num
                    < self.CHECK_ROUNDS
                ):

                    wait = random.randint(
                        3,
                        5,
                    )

                    log.info(
                        f"等待 {wait} 秒後"
                        "進行下一輪..."
                    )

                    time.sleep(
                        wait
                    )

            br.close()

        log.info(
            "所有輪次完成"
        )


class ExhibitionMonitor(
    EsliteMonitorBase
):

    def __init__(
        self,
    ):

        super().__init__()

        self.API_URL = (
            os.environ.get(
                "ESLITE_API_URL",
                "",
            ).strip()
        )

        if not self.API_URL:

            raise ValueError(
                "ESLITE_API_URL 環境變數未設定"
                "（請設定 GitHub Variable ESLITE_API_URL）"
            )

        _kw_env = (
            os.environ.get(
                "MONITOR_KEYWORDS",
                "",
            ).strip()
        )

        self.MONITOR_KEYWORDS: list = [
            k.strip().upper()
            for k in _kw_env.split(",")
            if k.strip()
        ]

    def _extract_products(
        self,
        data: dict,
    ) -> dict:

        products = {}

        def add(p):

            if not isinstance(
                p,
                dict,
            ):
                return

            guid = p.get(
                "product_guid"
            )

            name = p.get(
                "name"
            )

            if not guid or not name:
                return

            try:
                stock = int(
                    p.get(
                        "stock"
                    )
                )

            except (
                TypeError,
                ValueError,
            ):
                stock = None

            products[
                str(guid)
            ] = {
                "name": name,
                "status": (
                    p.get("status")
                    or p.get(
                        "product_button_status"
                    )
                    or "unknown"
                ),
                "stock": stock,
                "account_qty_limit": p.get(
                    "account_qty_limit"
                ),
                "order_qty_limit": p.get(
                    "order_qty_limit"
                ),
                "image": p.get(
                    "image",
                    "",
                ),
                "url": (
                    f"{self.ESLITE_BASE}"
                    f"/product/{guid}"
                ),
            }

        def walk(
            value,
        ):

            if isinstance(
                value,
                list,
            ):

                for item in value:

                    walk(
                        item
                    )

            elif isinstance(
                value,
                dict,
            ):

                if (
                    value.get(
                        "product_guid"
                    )
                    and value.get(
                        "name"
                    )
                ):

                    add(
                        value
                    )

                for child in value.values():

                    walk(
                        child
                    )

        walk(
            data
        )

        return products

    def _fetch_exhibition(
        self,
        page,
    ) -> list:

        page.goto(
            self.API_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        page.wait_for_timeout(
            1500
        )

        try:
            raw = page.inner_text(
                "pre"
            )

        except Exception:

            raw = page.inner_text(
                "body"
            )

        all_products = (
            self._extract_products(
                json.loads(
                    raw
                )
            )
        )

        if self.MONITOR_KEYWORDS:

            all_products = {
                guid: p
                for guid, p
                in all_products.items()
                if any(
                    kw
                    in p["name"].upper()
                    for kw
                    in self.MONITOR_KEYWORDS
                )
            }

        result = []

        for guid, p in all_products.items():

            name = p[
                "name"
            ]

            stock = p[
                "stock"
            ]

            if (
                not stock
                or stock <= 0
            ):
                continue

            lim = (
                "帳號上限:"
                f"{p['account_qty_limit']}件"
                if p.get(
                    "account_qty_limit"
                )
                else "無限購"
            )

            log.info(
                f"有庫存 → {name}"
                f"（{guid}）"
                f" | 庫存:{stock} 件"
                f" | {lim}"
                f" | {p['status']}"
            )

            result.append(
                {
                    "guid": guid,
                    **p,
                }
            )

        if len(
            all_products
        ) == 0:

            if self.MONITOR_KEYWORDS:

                log.warning(
                    "展覽 API 未找到"
                    "含關鍵字 "
                    f"{self.MONITOR_KEYWORDS} "
                    "的商品，繼續監控"
                )

            else:

                log.warning(
                    "展覽 API 回傳 0 件商品"
                    "（書展可能已下架"
                    "或 URL 已更換），"
                    "繼續監控"
                )

        else:

            kw_note = (
                "（關鍵字篩選後）"
                if self.MONITOR_KEYWORDS
                else ""
            )

            log.info(
                "展覽 API 抽取 "
                f"{len(all_products)} 件"
                f"{kw_note}，"
                "有庫存 "
                f"{len(result)} 件"
            )

        return result

    def fetch_in_stock_products(
        self,
        page,
    ) -> list:

        return self._fetch_exhibition(
            page
        )


class ProductMonitor(
    EsliteMonitorBase
):

    def fetch_in_stock_products(
        self,
        page,
    ) -> list:

        if not self.EXTRA_PRODUCT_GUIDS:

            log.warning(
                "ESLITE_EXTRA_PRODUCTS 未設定，"
                "ProductMonitor 無商品可追蹤"
            )

            return []

        result = []

        for guid in self.EXTRA_PRODUCT_GUIDS:

            product = (
                self._fetch_single_product(
                    page,
                    guid,
                )
            )

            if product:

                result.append(
                    product
                )

        log.info(
            "個別追蹤 "
            f"{len(self.EXTRA_PRODUCT_GUIDS)} 件，"
            "有庫存 "
            f"{len(result)} 件"
        )

        return result


def _parse_holmes_response(
    eslite_base: str,
    raw: str,
) -> list:

    data = json.loads(
        raw
    )

    items = None

    if isinstance(
        data,
        list,
    ):

        items = data

    else:

        for key in (
            "results",
            "data",
            "products",
            "items",
            "hits",
            "list",
        ):

            val = data.get(
                key
            )

            if isinstance(
                val,
                list,
            ):

                items = val

                break

    if items is None:

        keys = (
            list(
                data.keys()
            )
            if isinstance(
                data,
                dict,
            )
            else type(
                data
            )
        )

        log.warning(
            "Holmes API 回傳未知格式，"
            "無法解析，keys: "
            f"{keys}"
        )

        return []

    log.info(
        "Holmes 搜尋 API 取得 "
        f"{len(items)} 筆結果"
    )

    result = []

    for item in items:

        if not isinstance(
            item,
            dict,
        ):
            continue

        guid = str(
            item.get("id")
            or item.get(
                "product_guid"
            )
            or item.get(
                "guid"
            )
            or ""
        ).strip()

        name = (
            item.get(
                "name",
                "",
            ).strip()
        )

        if not guid or not name:
            continue

        availability = (
            item.get(
                "availability",
                "",
            )
        )

        try:
            stock = int(
                item.get(
                    "stock"
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):

            stock = 0

        if (
            stock == 0
            and availability
            == "IN_STOCK"
        ):

            stock = 1

        btn_status = (
            item.get(
                "button_status"
            )
            or item.get(
                "product_button_status"
            )
            or item.get(
                "status"
            )
            or ""
        ).strip()

        in_stock = (
            stock > 0
            or availability
            == "IN_STOCK"
            or btn_status
            == "add_to_shopping_cart"
        )

        if not in_stock:
            continue

        acct_lim = item.get(
            "account_qty_limit"
        )

        ord_lim = item.get(
            "order_qty_limit"
        )

        lim = (
            f"帳號上限:{acct_lim}件"
            if acct_lim
            else "無限購"
        )

        log.info(
            "有庫存（搜尋）→ "
            f"{name}（{guid}）"
            " | availability:"
            f"{availability}"
            f" | {lim}"
            f" | {btn_status}"
        )

        result.append(
            {
                "guid": guid,
                "name": name,
                "stock": stock,
                "status": btn_status,
                "account_qty_limit": acct_lim,
                "order_qty_limit": ord_lim,
                "url": (
                    f"{eslite_base}"
                    f"/product/{guid}"
                ),
            }
        )

    return result


class KeywordSearchMonitor(
    EsliteMonitorBase
):

    def __init__(
        self,
    ):

        super().__init__()

        self.SEARCH_URL = (
            os.environ.get(
                "ESLITE_SEARCH_URL",
                "",
            ).strip()
        )

        if not self.SEARCH_URL:

            raise ValueError(
                "ESLITE_SEARCH_URL 環境變數未設定"
                "（請設定 GitHub Variable ESLITE_SEARCH_URL）"
            )

    def _fetch_keyword_search(
        self,
        page,
    ) -> list:

        page.goto(
            self.SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        page.wait_for_timeout(
            1500
        )

        try:
            raw = page.inner_text(
                "pre"
            )

        except Exception:

            raw = page.inner_text(
                "body"
            )

        return _parse_holmes_response(
            self.ESLITE_BASE,
            raw,
        )

    def fetch_in_stock_products(
        self,
        page,
    ) -> list:

        products = (
            self._fetch_keyword_search(
                page
            )
        )

        log.info(
            "關鍵字搜尋，"
            "有庫存 "
            f"{len(products)} 件"
        )

        return products


class CombinedMonitor(
    ExhibitionMonitor
):

    def __init__(
        self,
    ):

        EsliteMonitorBase.__init__(
            self
        )

        self.API_URL = (
            os.environ.get(
                "ESLITE_API_URL",
                "",
            ).strip()
        )

        _kw_env = (
            os.environ.get(
                "MONITOR_KEYWORDS",
                "",
            ).strip()
        )

        self.MONITOR_KEYWORDS = [
            k.strip().upper()
            for k in _kw_env.split(",")
            if k.strip()
        ]

        self.SEARCH_URL = (
            os.environ.get(
                "ESLITE_SEARCH_URL",
                "",
            ).strip()
        )

        if not self.SEARCH_URL:

            raise ValueError(
                "ESLITE_SEARCH_URL 環境變數未設定"
                "（combined 模式需要此變數）"
            )

    def _fetch_keyword_search(
        self,
        page,
    ) -> list:

        page.goto(
            self.SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        page.wait_for_timeout(
            1500
        )

        try:
            raw = page.inner_text(
                "pre"
            )

        except Exception:

            raw = page.inner_text(
                "body"
            )

        return _parse_holmes_response(
            self.ESLITE_BASE,
            raw,
        )

    def fetch_in_stock_products(
        self,
        page,
    ) -> list:

        def _run_exhibition():

            if not self.API_URL:
                return []

            try:
                with sync_playwright() as pw:

                    br = pw.chromium.launch(
                        headless=self.HEADLESS,
                        args=[
                            "--disable-blink-features="
                            "AutomationControlled"
                        ],
                    )

                    try:
                        ctx = br.new_context(
                            user_agent=self._UA
                        )

                        ctx.add_init_script(
                            "Object.defineProperty("
                            "navigator, 'webdriver', "
                            "{get: () => undefined})"
                        )

                        worker_page = (
                            ctx.new_page()
                        )

                        try:
                            return self._fetch_exhibition(
                                worker_page
                            )

                        finally:
                            ctx.close()

                    finally:
                        br.close()

            except Exception as e:

                log.warning(
                    "展覽 API 失敗"
                    f"（{e}），跳過"
                )

                return []

        def _run_search():

            try:
                with sync_playwright() as pw:

                    br = pw.chromium.launch(
                        headless=self.HEADLESS,
                        args=[
                            "--disable-blink-features="
                            "AutomationControlled"
                        ],
                    )

                    try:
                        ctx = br.new_context(
                            user_agent=self._UA
                        )

                        ctx.add_init_script(
                            "Object.defineProperty("
                            "navigator, 'webdriver', "
                            "{get: () => undefined})"
                        )

                        worker_page = (
                            ctx.new_page()
                        )

                        try:
                            return (
                                self._fetch_keyword_search(
                                    worker_page
                                )
                            )

                        finally:
                            ctx.close()

                    finally:
                        br.close()

            except Exception as e:

                log.error(
                    "關鍵字搜尋失敗："
                    f"{e}"
                )

                return []

        with ThreadPoolExecutor(
            max_workers=2
        ) as executor:

            future_exhibition = (
                executor.submit(
                    _run_exhibition
                )
            )

            future_search = (
                executor.submit(
                    _run_search
                )
            )

            exhibition_results = (
                future_exhibition.result()
            )

            search_results = (
                future_search.result()
            )

        products: dict = {}

        for p in exhibition_results:

            products[
                p["guid"]
            ] = p

        if self.API_URL:

            log.info(
                "展覽 API 貢獻 "
                f"{len(exhibition_results)} 件"
            )

        else:

            log.info(
                "ESLITE_API_URL 未設定，"
                "跳過展覽 API"
            )

        before = len(
            products
        )

        for p in search_results:

            if (
                p["guid"]
                not in products
            ):

                products[
                    p["guid"]
                ] = p

        added = (
            len(products)
            - before
        )

        log.info(
            "關鍵字搜尋貢獻 "
            f"{added} 件"
            "（去重後，"
            f"總計 {len(products)} 件）"
        )

        return list(
            products.values()
        )


if __name__ == "__main__":

    mode = (
        os.environ.get(
            "MONITOR_MODE",
            "exhibition",
        ).lower()
    )

    if mode == "product":

        ProductMonitor().run()

    elif mode == "keyword":

        KeywordSearchMonitor().run()

    elif mode == "combined":

        CombinedMonitor().run()

    else:

        ExhibitionMonitor().run()
