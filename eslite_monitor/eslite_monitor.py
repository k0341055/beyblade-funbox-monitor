"""
誠品網路書店 Beyblade X 戰鬥陀螺監控

MONITOR_MODE=exhibition（預設）→ 監控書展 API（book_exhibits），不含個別追蹤商品
MONITOR_MODE=product           → 監控 EXTRA_PRODUCT_GUIDS 個別追蹤商品
MONITOR_MODE=keyword           → 監控 Holmes 搜尋 API；自動排除書籍/雜誌/周邊，只保留純 Beyblade X 陀螺與戰鬥盤
MONITOR_MODE=combined          → 展覽 API + 已過濾的 Holmes 關鍵字搜尋結果合併

兩種模式共用：登入、購物車、自動下單、Email 通知。
通知邏輯：同商品 Email 通知冷卻 1 小時；下單去重由 ORDER_STATE_FILE 負責。
自動下單：BUY_KEYWORDS 為唯一購買白名單；白名單外商品只通知、不下單。單帳號 × 多商品平行。每個 worker 不清空購物車；若購物車已有目標商品就直接結帳，沒有才加入購物車。
ESLITE_PARALLEL_SESSION_MODE=fresh_login（預設，最接近 Funbox）或 storage（沿用預登入 session）。
"""

import json
import logging
import os
import random
import re
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


BUY_KEYWORDS: list[str] = [
    "BX-09",
    "UX-17",
    "UX-21",
    "UX-15",
    "UX-04",
    "BX-46",
    "CX-16",
    "CX-04",
    "UX-03",
    "CX-00 新世紀福音戰士",
    "BX-00 蒼龍神劍",
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

        extra_env = os.environ.get(
            "ESLITE_EXTRA_PRODUCTS",
            "",
        ).strip()

        self.EXTRA_PRODUCT_GUIDS = [
            g.strip()
            for g in extra_env.split(",")
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

        recip_raw = [
            r.strip()
            for r in os.environ.get(
                "ORDER_RECIPIENT",
                "",
            ).split(",")
            if r.strip()
        ]

        self.ORDER_RECIPIENTS = (
            recip_raw
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

        self._login_fail_count = 0
        self.LOGIN_FAIL_MAX = 2

        purchased_env = (
            os.environ.get(
                "ESLITE_PURCHASED_NAMES",
                "",
            ).strip()
        )

        self.PURCHASED_NAMES = [
            n.strip().upper()
            for n in purchased_env.split(",")
            if n.strip()
        ]

    def _is_purchased(
        self,
        product: dict,
    ) -> bool:

        name = product.get(
            "name",
            "",
        ).upper()

        return any(
            kw in name
            for kw in self.PURCHASED_NAMES
        )

    def _is_buy_whitelisted(
        self,
        product: dict,
    ) -> bool:

        name = product.get(
            "name",
            "",
        ).upper()

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
                    f"（{btn_status}，stock={stock}）"
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
                "ESLITE_PASSWORD，跳過自動下單"
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
            ).fill(
                self.ESLITE_ACCOUNT
            )

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

                try:

                    page.wait_for_function(
                        "() => "
                        "!window.location.pathname"
                        ".startsWith('/login')",
                        timeout=120000,
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

                    page.wait_for_timeout(
                        90000
                    )

                    if "/login" not in page.url:

                        self._persist_storage_state(
                            ctx
                        )

                        return True

                self._record_login_failure()

                self._notify_login_required_once()

                return False

            if "/login" in page.url:

                self._record_login_failure()

                return False

            self._persist_storage_state(
                ctx
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
                "Session 有效，已登入誠品"
            )

            return True

        log.info(
            "Session 已失效，嘗試重新登入..."
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
                f"改走重新加車流程：{e}"
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

                    qty_input = page.query_selector(
                        "input[type='number'][class*='qty'], "
                        "input[type='number'][class*='quantity'], "
                        "input[type='number'][name*='qty'], "
                        "input[type='number']"
                    )

                    if qty_input:

                        qty_input.triple_click()

                        qty_input.fill(
                            str(qty)
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

                return False

            if not btn.is_enabled():

                return False

            btn.click()

            page.wait_for_timeout(
                2000
            )

            try:

                confirm = page.query_selector(
                    "button:has-text('確認')"
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
                f"已加入購物車：{guid}"
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
        cart_already_open=False,
    ):

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

                page.locator(
                    "select[name='recipientCity']"
                ).first.select_option(
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

            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(
                    page.url
                ).query
            )

            order_id = params.get(
                "orderid",
                [None],
            )[0]

            if order_id:

                log.info(
                    "結帳成功！"
                    f"訂單編號：{order_id}"
                )

            return order_id

        except Exception as e:

            log.error(
                f"結帳失敗：{e}",
                exc_info=True,
            )

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
                    "誠品戰鬥陀螺專區偵測到共 "
                    f"{count} 件有庫存商品"
                ),
                (
                    "偵測時間："
                    f"{datetime.now(self.TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                    "（台灣時間）"
                ),
                "",
                f">>> 立即結帳：{self.ESLITE_BASE}/cart/step2",
                f">>> 查看購物車：{self.ESLITE_BASE}/cart",
                "",
                "=" * 50,
            ]

            for i, p in enumerate(
                regular,
                1,
            ):

                lines += [
                    f"\n【商品 {i}】",
                    f"商品名：{p['name']}",
                    f"庫存：{p['stock']} 件",
                    f"商品連結：{p['url']}",
                    "-" * 40,
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
                "",
            ]

            for i, p in enumerate(
                purchased,
                1,
            ):

                lines += [
                    f"【商品 {i}】",
                    f"商品名：{p['name']}",
                    f"商品連結：{p['url']}",
                    "",
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

        subject = (
            "【誠品購物車已更新！】"
            f"{len(products)} 件商品已入購物車"
        )

        lines = [
            "商品已加入誠品購物車。",
            f">>> 查看購物車：{self.ESLITE_BASE}/cart",
            "",
        ]

        for p in products:

            lines += [
                p["name"],
                p["url"],
                "",
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

        subject = (
            "【誠品自動下單成功！】"
            f"訂單 {order_id}"
        )

        lines = [
            "誠品自動下單成功！",
            f"訂單編號：{order_id}",
            "",
        ]

        for p in products:

            lines += [
                p["name"],
                p["url"],
                "",
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

        self._send_email(
            self.ORDER_RECIPIENTS,
            "【誠品監控警告】需要手動重新登入",
            "誠品登入失敗或需要 OTP 驗證。",
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

                    kwargs = {
                        "user_agent": self._UA
                    }

                    state = (
                        self._load_storage_state_data()
                    )

                    if state:

                        kwargs[
                            "storage_state"
                        ] = state

                    ctx = br.new_context(
                        **kwargs
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
                f"{e}"
            )

            return None

    def _checkout_one_product(
        self,
        product: dict,
        session_state=None,
    ) -> dict:

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

        def new_ctx(
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

                        ctx = new_ctx(
                            br
                        )

                        page = ctx.new_page()

                        if not self._ensure_logged_in(
                            page,
                            ctx,
                        ):

                            if session_state:

                                try:

                                    ctx.close()

                                except Exception:

                                    pass

                                ctx = new_ctx(
                                    br,
                                    session_state,
                                )

                                page = ctx.new_page()

                                if not self._is_logged_in(
                                    page
                                ):

                                    result[
                                        "status"
                                    ] = "login_failed"

                                    return result

                            else:

                                result[
                                    "status"
                                ] = "login_failed"

                                return result

                    else:

                        if not session_state:

                            result[
                                "status"
                            ] = "login_failed"

                            return result

                        ctx = new_ctx(
                            br,
                            session_state,
                        )

                        page = ctx.new_page()

                        if not self._ensure_logged_in(
                            page,
                            ctx,
                        ):

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

                        order_id = self._checkout(
                            page,
                            cart_already_open=True,
                        )

                    else:

                        qty = self._calc_order_qty(
                            product
                        )

                        if not self._add_to_cart(
                            page,
                            guid,
                            qty,
                        ):

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

                    else:

                        result[
                            "status"
                        ] = "checkout_failed"

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

            return {}

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

                    result = future.result()

                except Exception as e:

                    log.error(
                        f"[{guid}] future 例外：{e}"
                    )

                    result = {
                        "product": p,
                        "guid": guid,
                        "order_id": None,
                        "status": "exception",
                    }

                results[
                    guid
                ] = result

                order_id = result.get(
                    "order_id"
                )

                if order_id:

                    ordered[
                        guid
                    ] = {
                        "order_id": order_id,
                        "ordered_at": datetime.now(
                            self.TW_TZ
                        ).isoformat(),
                    }

                    self._save_order_state(
                        ordered
                    )

                    self._notify_order(
                        [p],
                        order_id,
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
                    "目前無庫存商品，繼續監控"
                )

                return True

            to_notify = []

            for p in products:

                guid = p[
                    "guid"
                ]

                last_ts = notified.get(
                    guid
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

                checkout_products = [
                    p
                    for p in products
                    if self._is_buy_whitelisted(
                        p
                    )
                ]

                blocked = (
                    len(products)
                    - len(checkout_products)
                )

                if blocked:

                    log.info(
                        "BUY_KEYWORDS 白名單外商品"
                        "（僅通知，不下單）："
                        f"{blocked} 件"
                    )

            def send_due_notifications():

                if not to_notify:

                    log.info(
                        "所有有庫存商品"
                        "均在通知冷卻期內"
                    )

                    return

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

                    send_due_notifications()

                    self._save_notify_state(
                        notified
                    )

                    try:

                        buy_future.result()

                    except Exception as e:

                        log.error(
                            "平行自動下單主流程例外："
                            f"{e}"
                        )

            else:

                send_due_notifications()

                self._save_notify_state(
                    notified
                )

            return True

        except Exception as e:

            log.error(
                f"執行例外：{e}",
                exc_info=True,
            )

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

            page = ctx.new_page()

            for round_num in range(
                1,
                self.CHECK_ROUNDS + 1,
            ):

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

                    time.sleep(
                        wait
                    )

            br.close()


class ExhibitionMonitor(
    EsliteMonitorBase
):

    def __init__(
        self,
    ):

        super().__init__()

        api_urls_raw = (
            os.environ.get(
                "ESLITE_API_URL",
                "",
            ).strip()
        )

        self.API_URLS = [
            u.strip()
            for u in api_urls_raw.split(",")
            if u.strip()
        ]

        self.API_URL = (
            self.API_URLS[0]
            if self.API_URLS
            else ""
        )

        if not self.API_URLS:

            raise ValueError(
                "ESLITE_API_URL 未設定"
            )

        kw_env = (
            os.environ.get(
                "MONITOR_KEYWORDS",
                "",
            ).strip()
        )

        self.MONITOR_KEYWORDS = [
            k.strip().upper()
            for k in kw_env.split(",")
            if k.strip()
        ]

    def _extract_products(
        self,
        data,
    ):

        products = {}

        def add(
            p,
        ):

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

            except Exception:

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
        url=None,
    ):

        target_url = (
            url
            if url is not None
            else (
                self.API_URLS[0]
                if self.API_URLS
                else ""
            )
        )

        if not target_url:

            return []

        if "eslite.com/exhibitions/" in target_url:

            all_products = (
                self._fetch_exhibition_page(
                    page,
                    target_url,
                )
            )

        else:

            all_products = (
                self._fetch_exhibition_api(
                    page,
                    target_url,
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

            stock = p.get(
                "stock"
            )

            if (
                stock is None
                or stock <= 0
            ):

                continue

            result.append(
                {
                    "guid": guid,
                    **p,
                }
            )

        return result

    def _fetch_exhibition_api(
        self,
        page,
        url,
    ):

        page.goto(
            url,
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

        return self._extract_products(
            json.loads(raw)
        )

    def _fetch_exhibition_page(
        self,
        page,
        url,
    ):

        captured = []

        lock = threading.Lock()

        def on_response(response):

            try:

                ct = response.headers.get(
                    "content-type",
                    "",
                )

                if "application/json" not in ct:

                    return

                if "eslite.com" not in response.url:

                    return

                data = response.json()

                with lock:

                    captured.append(data)

            except Exception:

                pass

        page.on(
            "response",
            on_response,
        )

        try:

            page.goto(
                url,
                wait_until="networkidle",
                timeout=30000,
            )

            page.wait_for_timeout(
                2000
            )

        finally:

            page.remove_listener(
                "response",
                on_response,
            )

        all_products = {}

        for data in captured:

            all_products.update(
                self._extract_products(data)
            )

        return all_products

    def fetch_in_stock_products(
        self,
        page,
    ):

        all_products = {}

        for url in self.API_URLS:

            for p in self._fetch_exhibition(
                page,
                url,
            ):

                if p["guid"] not in all_products:

                    all_products[
                        p["guid"]
                    ] = p

        return list(
            all_products.values()
        )


class ProductMonitor(
    EsliteMonitorBase
):

    def fetch_in_stock_products(
        self,
        page,
    ):

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

        return result


def _is_holmes_beyblade_target(
    item: dict,
) -> bool:
    """
    Holmes 關鍵字搜尋專用商品過濾。

    只保留：
    1. BEYBLADE X / 戰鬥陀螺系列的實體陀螺商品
    2. BEYBLADE X / 戰鬥陀螺系列的戰鬥盤／陀螺盤

    排除：
    - 書籍、漫畫、電子書、外文書、童書
    - 雜誌、MOOK、Guide、附錄類商品
    - 一般非 BEYBLADE X 的陀螺玩具
    - 發射器、握把、通行證、收納盒／包等配件
    """

    name = str(
        item.get(
            "name",
            "",
        )
        or ""
    ).strip()

    if not name:
        return False

    name_upper = name.upper()

    categories = item.get(
        "categories"
    ) or []

    if not isinstance(
        categories,
        list,
    ):
        categories = [
            str(categories)
        ]

    category_text = " | ".join(
        str(c)
        for c in categories
    ).upper()

    is_book = str(
        item.get(
            "is_book",
            "",
        )
        or ""
    ).strip().lower()

    is_ebook = str(
        item.get(
            "is_ebook",
            "",
        )
        or ""
    ).strip().lower()

    # 第一層：直接使用 Holmes API 的書籍旗標排除。
    if (
        is_book == "yes"
        or is_ebook == "yes"
    ):
        return False

    # 第二層：部分雜誌 / MOOK 的 is_book 可能是 no，
    # 因此再用 category 排除所有出版品。
    book_category_keywords = (
        "中文書",
        "電子書",
        "外文書",
        "童書",
        "雜誌",
        "MOOK",
        "出版",
        "圖文漫畫",
        "動漫畫",
    )

    if any(
        kw in category_text
        for kw in book_category_keywords
    ):
        return False

    # 第三層：category 不完整時，名稱仍可擋掉常見書籍 / 附錄商品。
    book_name_keywords = (
        "電子書",
        "漫畫",
        "雜誌",
        "MOOK",
        "GUIDE",
        "ガイド",
        "別冊",
        "附錄",
    )

    if any(
        kw in name_upper
        for kw in book_name_keywords
    ):
        return False

    # 必須明確是 Beyblade / 戰鬥陀螺系列，
    # 避免「奇幻陀螺」等一般商品混入。
    beyblade_context = (
        "BEYBLADE" in name_upper
        or "戰鬥陀螺" in name
        or "ベイブレード" in name
    )

    if not beyblade_context:
        return False

    # 戰鬥盤 / 陀螺盤直接保留。
    stadium_keywords = (
        "戰鬥盤",
        "陀螺盤",
        "競技盤",
        "STADIUM",
        "スタジアム",
    )

    if any(
        kw in name_upper
        for kw in stadium_keywords
    ):
        return True

    # 純陀螺商品通常會帶 BX / UX / CX / BXG 型號。
    has_beyblade_code = bool(
        re.search(
            r"(?<![A-Z0-9])"
            r"(?:BXG|BX|UX|CX)-\d{2}"
            r"(?!\d)",
            name_upper,
        )
    )

    if not has_beyblade_code:
        return False

    # 型號商品中仍可能是發射器、收納、通行證等配件，
    # 這些不是使用者要的「純陀螺或陀螺盤」。
    accessory_keywords = (
        "發射器",
        "發射握把",
        "握把",
        "握柄",
        "通行證",
        "收納",
        "收納盒",
        "收納包",
        "陀螺盒",
        "保護套",
        "LAUNCHER",
        "GRIP",
        "GEAR CASE",
        "CASE",
        "HOLDER",
    )

    if any(
        kw in name_upper
        for kw in accessory_keywords
    ):
        return False

    return True


def _parse_holmes_response(
    eslite_base,
    raw,
):

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

        return []

    result = []
    filtered_non_target = 0

    for item in items:

        if not isinstance(
            item,
            dict,
        ):

            continue

        # Holmes 搜尋可能回傳漫畫、電子書、雜誌、
        # 一般陀螺與 Beyblade 周邊。
        # 這裡先縮成「純 Beyblade X 陀螺 / 戰鬥盤」。
        if not _is_holmes_beyblade_target(
            item
        ):

            filtered_non_target += 1
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

        name = item.get(
            "name",
            "",
        ).strip()

        if not guid or not name:

            continue

        availability = item.get(
            "availability",
            "",
        )

        try:

            stock = int(
                item.get(
                    "stock"
                )
                or 0
            )

        except Exception:

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
        )

        in_stock = (
            stock > 0
            or availability
            == "IN_STOCK"
            or btn_status
            == "add_to_shopping_cart"
        )

        if not in_stock:

            continue

        result.append(
            {
                "guid": guid,
                "name": name,
                "stock": stock,
                "status": btn_status,
                "account_qty_limit": item.get(
                    "account_qty_limit"
                ),
                "order_qty_limit": item.get(
                    "order_qty_limit"
                ),
                "url": (
                    f"{eslite_base}"
                    f"/product/{guid}"
                ),
            }
        )

    if filtered_non_target:

        log.info(
            "Holmes 搜尋已排除 "
            f"{filtered_non_target} 件"
            "書籍／雜誌／非陀螺／非戰鬥盤商品"
        )

    log.info(
        "Holmes 搜尋保留 "
        f"{len(result)} 件"
        "純 Beyblade X 陀螺／戰鬥盤有貨商品"
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
                "ESLITE_SEARCH_URL 未設定"
            )

    def _fetch_keyword_search(
        self,
        page,
    ):

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
    ):

        return self._fetch_keyword_search(
            page
        )


class CombinedMonitor(
    ExhibitionMonitor
):

    def __init__(
        self,
    ):

        EsliteMonitorBase.__init__(
            self
        )

        api_urls_raw = (
            os.environ.get(
                "ESLITE_API_URL",
                "",
            ).strip()
        )

        self.API_URLS = [
            u.strip()
            for u in api_urls_raw.split(",")
            if u.strip()
        ]

        self.API_URL = (
            self.API_URLS[0]
            if self.API_URLS
            else ""
        )

        kw_env = (
            os.environ.get(
                "MONITOR_KEYWORDS",
                "",
            ).strip()
        )

        self.MONITOR_KEYWORDS = [
            k.strip().upper()
            for k in kw_env.split(",")
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
                "ESLITE_SEARCH_URL 未設定"
            )

    def _fetch_keyword_search(
        self,
        page,
    ):

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
    ):

        def run_exhibition():

            if not self.API_URLS:

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

                            seen = set()

                            results = []

                            for url in self.API_URLS:

                                for p in self._fetch_exhibition(
                                    worker_page,
                                    url,
                                ):

                                    if p["guid"] not in seen:

                                        seen.add(
                                            p["guid"]
                                        )

                                        results.append(p)

                            return results

                        finally:

                            ctx.close()

                    finally:

                        br.close()

            except Exception as e:

                log.warning(
                    f"展覽 API 失敗：{e}"
                )

                return []

        def run_search():

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

                            return self._fetch_keyword_search(
                                worker_page
                            )

                        finally:

                            ctx.close()

                    finally:

                        br.close()

            except Exception as e:

                log.error(
                    f"搜尋 API 失敗：{e}"
                )

                return []

        with ThreadPoolExecutor(
            max_workers=2
        ) as executor:

            future_exhibition = (
                executor.submit(
                    run_exhibition
                )
            )

            future_search = (
                executor.submit(
                    run_search
                )
            )

            exhibition_results = (
                future_exhibition.result()
            )

            search_results = (
                future_search.result()
            )

        products = {}

        for p in exhibition_results:

            products[
                p["guid"]
            ] = p

        for p in search_results:

            if (
                p["guid"]
                not in products
            ):

                products[
                    p["guid"]
                ] = p

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
