"""
1999.co.jp 商品偵測 + Email 通知 + Amazon Pay 自動下單
偵測到有庫存商品時，依 1 小時冷卻邏輯寄送 Email 給所有 GMAIL_RECIPIENTS。
AUTO_CHECKOUT=true 時：加入購物車 → Amazon Pay → /orderamazon 確認下單。
Amazon Pay 需預先執行 generate_1999_session.py 儲存 browser session。
"""

import asyncio
import json
import logging
import os
import random
import smtplib
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

SEARCH_URL = os.environ.get("SEARCH_URL", "").strip()
if not SEARCH_URL:
    raise ValueError("SEARCH_URL 環境變數未設定（請設定 GitHub Variable SEARCH_URL_1999）")

BASE_URL = "https://www.1999.co.jp"
CHECKOUT_URL = "https://www.1999.co.jp/order"

CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
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


def _extract_keyword(url: str) -> str:
    try:
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query
        )
        return params.get(
            "searchkey",
            ["商品"]
        )[0].replace("+", " ")
    except Exception:
        return "商品"


NOTIFY_KEYWORD = os.environ.get(
    "PRODUCT_NAME",
    _extract_keyword(SEARCH_URL)
)


# ─────────────────────────────────────────────
# 完全不通知的商品
# ─────────────────────────────────────────────
#
# 商品名稱含以下任一關鍵字：
#
#   ❌ 不寄 Email
#   ❌ 不自動下單
#   ❌ 不加入通知冷卻紀錄
#   ❌ 不影響其他商品通知
#
# 例如 BX-43 即使一直在架上、有庫存，
# 也會被完全忽略。
#
NOT_NOTIFY_KEYWORDS: list[str] = [
    "BX-43",
]


# ─────────────────────────────────────────────
# 略過自動下單的商品關鍵字
# ─────────────────────────────────────────────
#
# 商品名稱含以下任一關鍵字：
#
#   ✅ 仍然寄 Email
#   ❌ 不自動下單
#
SKIP_KEYWORDS: list[str] = [

    # BX 系列
    "BX-11",
    "BX-25",
    "BX-33",
    "BX-26",

    # BXG 系列
    "BXG-29",
    "BXG-33",

    # Marvel 聯名
    "蜘蛛人",
    "鋼鐵人",
    "薩諾斯",
    "綠惡魔",

    # Star Wars 聯名
    "路克天行者",
    "達斯維達",

    # 其他
    "暴風天馬",
    "銀牙烈虎",
    "烈焰飛鳳",
]


# ─────────────────────────────────────────────
# 自動下單目標商品關鍵字（白名單）
# ─────────────────────────────────────────────
#
# 商品名稱含以下任一關鍵字：
#
#   ✅ 寄 Email
#   ✅ AUTO_CHECKOUT=true 時自動下單
#
# 不在 BUY_KEYWORDS
# 且不在 SKIP_KEYWORDS
# 且不在 NOT_NOTIFY_KEYWORDS
#
# → 只寄 Email，不下單。
#
BUY_KEYWORDS: list[str] = [

    "BX-09",

    "UX-15",
    "UX-21",
    "UX-17",
    "UX-03",

    "CX-11",

    "UX-11",
    "UX-16",
    "UX-01",

    "BX-35",
    "BX-48",
    "BX-23",

    "UX-20",
    "UX-10",

    "CX-19",
    "CX-08",

    "UX-08",
    "CX-18",

    # BX-00
    "ドラグーンストーム",
    "ドラシエルシールド",

    "BX-42",
    "BX-29",
    "BX-30",
]


# ─────────────────────────────────────────────
# 自動下單設定
# ─────────────────────────────────────────────

AUTO_CHECKOUT = (
    os.environ.get(
        "AUTO_CHECKOUT",
        "false"
    ).lower()
    == "true"
)

ACCOUNT_1999 = os.environ.get(
    "ACCOUNT_1999",
    ""
).strip()

PASSWORD_1999 = os.environ.get(
    "PASSWORD_1999",
    ""
).strip()

# Amazon Japan 登入 Email
AMAZON_ACCOUNT = os.environ.get(
    "AMAZON_ACCOUNT",
    ""
).strip()

# Amazon Japan 登入密碼
AMAZON_PASSWORD = os.environ.get(
    "AMAZON_PASSWORD",
    ""
).strip()

STORAGE_STATE_FILE = Path(
    os.environ.get(
        "STORAGE_STATE_FILE",
        "1999_storage_state.json"
    )
)

ORDER_STATE_FILE = Path(
    os.environ.get(
        "ORDER_STATE_FILE",
        "1999_order_state.json"
    )
)


# ─────────────────────────────────────────────
# 隨機 UA / Viewport
# ─────────────────────────────────────────────

_UA_OS = [
    "Windows NT 10.0; Win64; x64",
    "Windows NT 11.0; Win64; x64",
    "Macintosh; Intel Mac OS X 10_15_7",
    "Macintosh; Intel Mac OS X 13_4",
    "Macintosh; Intel Mac OS X 14_0",
    "X11; Linux x86_64",
]

_UA_CHROME_VERSIONS = list(
    range(118, 126)
)

_UA_WEBKIT_BUILD = list(
    range(530, 538)
)


def _random_ua() -> str:

    os_str = random.choice(
        _UA_OS
    )

    major = random.choice(
        _UA_CHROME_VERSIONS
    )

    webkit = (
        f"537."
        f"{random.choice(_UA_WEBKIT_BUILD)}"
    )

    return (
        f"Mozilla/5.0 ({os_str}) "
        f"AppleWebKit/{webkit} "
        f"(KHTML, like Gecko) "
        f"Chrome/"
        f"{major}.0."
        f"{random.randint(5000, 7000)}."
        f"{random.randint(0, 9)} "
        f"Safari/{webkit}"
    )


def _random_viewport() -> dict:

    return random.choice([
        {
            "width": 1920,
            "height": 1080,
        },
        {
            "width": 1440,
            "height": 900,
        },
        {
            "width": 1366,
            "height": 768,
        },
        {
            "width": 1536,
            "height": 864,
        },
        {
            "width": 1600,
            "height": 900,
        },
    ])


def _jitter(
    base_ms: int,
    pct: float = 0.3
) -> int:

    delta = int(
        base_ms * pct
    )

    return (
        base_ms
        + random.randint(
            -delta,
            delta
        )
    )


async def _wait_cf(
    page,
    max_ms: int = 15_000
):
    """
    等待 Cloudflare JS 挑戰自動通過。
    """

    try:

        await page.wait_for_function(
            """
            () => {

                const t = document.title;

                return (
                    !t.includes('Just a moment') &&
                    !t.includes('Checking your browser') &&
                    !t.includes('Attention Required') &&
                    !t.includes('ちょっと待ってください') &&
                    !document.querySelector(
                        '#challenge-form, '
                        '.cf-browser-verification'
                    )
                );
            }
            """,
            timeout=max_ms,
        )

    except Exception:
        pass


def _mask_email(
    email: str
) -> str:

    if "@" not in email:
        return "***"

    local, domain = email.split(
        "@",
        1
    )

    return (
        f"{local[0]}***@{domain}"
    )


# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

logging.basicConfig(

    level=logging.INFO,

    format=(
        "%(asctime)s "
        "[%(levelname)s] "
        "%(message)s"
    ),

    handlers=[
        logging.StreamHandler()
    ],
)

log = logging.getLogger(
    __name__
)


# ─────────────────────────────────────────────
# 狀態管理
# ─────────────────────────────────────────────

def load_notified() -> dict:
    """
    回傳：
    {
        href: 上次廣播通知時間
    }
    """

    if STATE_FILE.exists():

        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        ).get(
            "notified",
            {}
        )

    return {}


def save_notified(
    notified: dict
):

    STATE_FILE.write_text(

        json.dumps(

            {
                "notified": notified,
                "updated": (
                    datetime
                    .now(TW_TZ)
                    .isoformat()
                ),
            },

            ensure_ascii=False,

            indent=2,
        ),

        encoding="utf-8",
    )


def _parse_ts(
    ts_str: str
) -> datetime:

    dt = datetime.fromisoformat(
        ts_str
    )

    if dt.tzinfo is None:

        dt = dt.replace(
            tzinfo=TW_TZ
        )

    return dt


def load_order_state() -> dict:
    """
    回傳：
    {
        href: ISO時間戳
    }

    記錄已成功下單商品，
    避免重複下單。
    """

    if ORDER_STATE_FILE.exists():

        return json.loads(
            ORDER_STATE_FILE.read_text(
                encoding="utf-8"
            )
        ).get(
            "purchased",
            {}
        )

    return {}


def save_order_state(
    purchased: dict
):

    ORDER_STATE_FILE.write_text(

        json.dumps(

            {
                "purchased": purchased,
                "updated": (
                    datetime
                    .now(TW_TZ)
                    .isoformat()
                ),
            },

            ensure_ascii=False,

            indent=2,
        ),

        encoding="utf-8",
    )


# ─────────────────────────────────────────────
# 擷取商品清單
# ─────────────────────────────────────────────

async def fetch_products(
    page
) -> list:

    log.info(
        f"正在載入頁面："
        f"{SEARCH_URL}"
    )

    try:

        await page.goto(

            SEARCH_URL,

            wait_until="networkidle",

            timeout=30_000,
        )

    except PlaywrightTimeoutError:

        log.warning(
            "networkidle 逾時，"
            "嘗試繼續解析頁面..."
        )


    await page.wait_for_timeout(
        _jitter(800)
    )


    try:

        await page.wait_for_selector(

            "div.c-card__info",

            timeout=10_000,
        )

    except PlaywrightTimeoutError:

        # 先判斷：
        #
        # Cloudflare
        # 或
        # 搜尋結果真的沒有商品

        is_cf = await page.evaluate(
            """
            () =>
                document.title.includes(
                    'Just a moment'
                )
                ||
                document.title.includes(
                    'Checking'
                )
                ||
                !!document.querySelector(
                    '#challenge-form, '
                    '.cf-browser-verification'
                )
            """
        )

        if is_cf:

            log.warning(
                "偵測到 Cloudflare 挑戰，"
                "本輪跳過"
            )

        else:

            log.info(
                "目前查無有庫存商品"
                "（搜尋結果為空）"
            )

        return []


    cards = (
        await page
        .query_selector_all(
            "div.c-card__info"
        )
    )

    log.info(
        f"找到 {len(cards)} 個商品卡"
    )

    products = []


    for card in cards:

        link_el = await card.query_selector(
            "a.c-card__info-links"
        )

        if not link_el:
            continue


        href = (
            await link_el
            .get_attribute(
                "href"
            )
        ) or ""


        title_el = (
            await card
            .query_selector(
                "div.c-card__title"
            )
        )

        if title_el:

            title = (
                await title_el
                .inner_text()
            ).strip()

        else:

            title = (
                "(タイトル不明)"
            )


        maker_el = (
            await card
            .query_selector(
                "div.c-card__maker"
            )
        )

        if maker_el:

            release = (
                await maker_el
                .inner_text()
            ).strip()

        else:

            release = ""


        price_el = (
            await card
            .query_selector(
                "div.c-card__price-element"
            )
        )


        if price_el:

            span = (
                await price_el
                .query_selector(
                    "span"
                )
            )

            if span:

                price_num = (
                    await span
                    .inner_text()
                ).strip()

            else:

                price_num = ""


            if price_num:

                price = (
                    f"¥{price_num}"
                )

            else:

                price = (
                    await price_el
                    .inner_text()
                ).strip()

        else:

            price = "価格未定"


        discount_el = (
            await card
            .query_selector(
                "div.c-card__price-tags-discount span"
            )
        )


        if discount_el:

            discount = (
                f"{(
                    await discount_el
                    .inner_text()
                ).strip()}%OFF"
            )

        else:

            discount = ""


        products.append({

            "href": href,

            "url": (
                f"{BASE_URL}{href}"
                if href.startswith("/")
                else href
            ),

            "title": title,

            "release": release,

            "price": price,

            "discount": discount,
        })


    return products


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────

def _send_email(
    to: list,
    subject: str,
    body: str
):

    try:

        msg = MIMEText(
            body,
            "plain",
            "utf-8"
        )

        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = ", ".join(to)


        with smtplib.SMTP_SSL(

            "smtp.gmail.com",

            465,

            timeout=15,

        ) as server:

            server.login(
                GMAIL_SENDER,
                GMAIL_PASSWORD
            )

            server.sendmail(
                GMAIL_SENDER,
                to,
                msg.as_string()
            )


        log.info(
            "Email 發送成功 → "
            f"{[
                _mask_email(r)
                for r in to
            ]}"
        )


    except Exception as e:

        log.error(
            f"Email 發送失敗："
            f"{e}"
        )


def send_notify_email(
    products: list
):

    """
    廣播通知：

    寄給全體 GMAIL_RECIPIENTS。

    只包含：
    - 商品資訊
    - 結帳連結

    不包含自動下單結果。
    """

    count = len(products)


    subject = (
        f"【1999 "
        f"{NOTIFY_KEYWORD} "
        f"補貨！】"
        f"偵測到 {count} 件商品"
    )


    now_str = (
        datetime
        .now(TW_TZ)
        .strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )


    lines = [

        (
            f"1999.co.jp "
            f"偵測到 "
            f"{NOTIFY_KEYWORD} "
            f"共 {count} 件有庫存商品"
        ),

        (
            f"偵測時間："
            f"{now_str}"
            f"（台灣時間）"
        ),

        "",

        "=" * 50,
    ]


    for i, p in enumerate(
        products,
        1
    ):

        lines.append(
            f"\n【商品 {i}】"
        )

        lines.append(
            f"商品名："
            f"{p['title']}"
        )


        if p["release"]:

            lines.append(
                f"發售日："
                f"{p['release']}"
            )


        price_str = p["price"]


        if p["discount"]:

            price_str += (
                f"（"
                f"{p['discount']}"
                f"）"
            )


        lines.append(
            f"價格："
            f"{price_str}"
        )


        lines.append(
            f"商品連結："
            f"{p['url']}"
        )


        lines.append(
            "-" * 40
        )


    lines += [

        "",

        (
            "─ 點擊下方連結"
            "前往購物車結帳 ─"
        ),

        f"  {CHECKOUT_URL}",

        "",

        f"完整搜尋頁：{SEARCH_URL}",
    ]


    _send_email(

        GMAIL_RECIPIENTS,

        subject,

        "\n".join(lines),
    )


def _send_order_result_email(
    products: list,
    checkout_results: dict
):

    """
    下單結果只寄給
    ACCOUNT_1999 本人。
    """

    if (
        not ACCOUNT_1999
        or ACCOUNT_1999
        not in GMAIL_RECIPIENTS
    ):

        log.warning(
            "ACCOUNT_1999 "
            "不在 GMAIL_RECIPIENTS 中，"
            "無法寄送個人下單結果通知"
        )

        return


    statuses = list(
        checkout_results.values()
    )


    if "success" in statuses:

        outcome = (
            "✅ 已自動下單完成"
        )

    elif (
        "amazon_auth_needed"
        in statuses
    ):

        outcome = (
            "⚠ Amazon Pay "
            "需要重新授權"
        )

    else:

        outcome = (
            "❌ 下單失敗"
        )


    subject = (
        f"【1999 下單結果】"
        f"{outcome}"
    )


    now_str = (
        datetime
        .now(TW_TZ)
        .strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )


    lines = [

        (
            f"偵測時間："
            f"{now_str}"
            f"（台灣時間）"
        ),

        "",

        "=" * 50,

        "各商品下單結果",

        "=" * 50,
    ]


    for href, status in (
        checkout_results.items()
    ):

        p_obj = next(

            (
                p
                for p in products
                if p["href"] == href
            ),

            None,
        )


        title = (
            p_obj["title"]
            if p_obj
            else href
        )


        label = (
            _CHECKOUT_LABEL_1999
            .get(
                status,
                status
            )
        )


        lines.append(
            f"▸ {title}：{label}"
        )


    if (
        "amazon_auth_needed"
        in statuses
    ):

        lines += [

            "",

            (
                "⚠ Amazon Pay session "
                "已過期，請重新授權："
            ),

            (
                "  1. 在本機執行："
                "python "
                "generate_1999_session.py"
            ),

            (
                "  2. 完成後更新 "
                "GitHub Secret："
                "AMAZON_1999_STORAGE_STATE_B64"
            ),
        ]


    if "success" in statuses:

        lines += [

            "",

            (
                "請前往 1999 "
                "確認訂單："
                f"{BASE_URL}/mypage"
            ),
        ]


    lines += [

        "",

        (
            f"完整搜尋頁："
            f"{SEARCH_URL}"
        ),
    ]


    _send_email(

        [ACCOUNT_1999],

        subject,

        "\n".join(lines),
    )


# ─────────────────────────────────────────────
# 自動下單
# ─────────────────────────────────────────────

_CART_BTN_SELECTORS = [

    # 原生按鈕
    "label.c-product-detail__info-submit-button-primary",

    "[id*='viwCart'] label",

    "[class*='submit-button-primary']",


    # 備援 selector
    "button:has-text('カートに入れる')",

    "input[value='カートに入れる']",

    "input[value*='カートに入れ']",

    "input[value*='カートへ']",

    "input[type='button'][name*='CartAdd']",

    "input[type='button'][name*='btnCart']",
]


_CHECKOUT_LABEL_1999 = {

    "success":
        "✅ 已自動下單完成",

    "amazon_auth_needed":
        "⚠ Amazon Pay session 過期，需重新授權",

    "cart_not_found":
        "❌ 找不到加入購物車按鈕"
        "（已售完或頁面異常）",

    "no_amazon_pay":
        "❌ 找不到 Amazon Pay 按鈕",

    "no_confirm_button":
        "❌ 找不到確認下單按鈕",

    "login_failed":
        "❌ 1999 登入失敗，"
        "請確認帳號密碼",

    "failed":
        "❌ 下單失敗（未知錯誤）",
}


async def _login_1999(
    page
) -> bool:

    """
    使用帳號密碼登入 1999。
    """

    if (
        not ACCOUNT_1999
        or not PASSWORD_1999
    ):

        log.warning(
            "[1999] "
            "未設定 "
            "ACCOUNT_1999 / "
            "PASSWORD_1999，"
            "無法自動登入"
        )

        return False


    try:

        await page.goto(

            f"{BASE_URL}/login",

            wait_until="domcontentloaded",

            timeout=15_000,
        )


        await page.wait_for_timeout(
            _jitter(800)
        )


        await page.fill(
            "input[name='txtLogin']",
            ACCOUNT_1999
        )


        await page.fill(
            "input[name='Usr_Ps']",
            PASSWORD_1999
        )


        cb = page.locator(
            "input[name='cbAutoLogin']"
        )


        if (
            await cb.count() > 0
            and not await cb.is_checked()
        ):

            await cb.check()


        await page.click(
            "input[name='btnLogin']"
        )


        await page.wait_for_url(

            f"{BASE_URL}/",

            timeout=15_000,
        )


        log.info(
            "[1999] 登入成功："
            f"{_mask_email(ACCOUNT_1999)}"
        )


        return True


    except Exception as e:

        log.error(
            f"[1999] 登入失敗："
            f"{e}"
        )

        return False


async def _handle_amazon_auth(
    page
) -> str:

    """
    處理 Amazon Pay 授權頁。

    1.
    如果有 Email 欄位，
    代表 Amazon 尚未登入。

    2.
    登入後或原本已登入，
    尋找「続行 / 同意」按鈕。

    回傳：

    ok
    amazon_auth_needed
    """

    await page.wait_for_load_state(

        "domcontentloaded",

        timeout=15_000,
    )


    await page.wait_for_timeout(
        _jitter(1500)
    )


    log.info(
        "[Amazon Pay] 授權頁："
        f"{page.url}"
    )


    # ─────────────────────────────
    # Amazon 尚未登入
    # ─────────────────────────────

    email_loc = page.locator(

        "input[id='ap_email'], "
        "input[type='email']"

    ).first


    needs_login = (

        await email_loc.count() > 0

        and await email_loc.is_visible(
            timeout=3_000
        )
    )


    if needs_login:

        if (
            not AMAZON_ACCOUNT
            or not AMAZON_PASSWORD
        ):

            log.warning(
                "[Amazon Pay] "
                "需要登入但未設定 "
                "AMAZON_ACCOUNT / "
                "AMAZON_PASSWORD"
            )

            return (
                "amazon_auth_needed"
            )


        log.info(
            "[Amazon Pay] "
            "填入 Email："
            f"{_mask_email(AMAZON_ACCOUNT)}"
        )


        await email_loc.fill(
            AMAZON_ACCOUNT
        )


        await page.wait_for_timeout(
            _jitter(500)
        )


        # 點「次へ進む」

        next_btn = page.locator(
            "input[id='continue']"
        ).first


        if (

            await next_btn.count() > 0

            and await next_btn.is_visible(
                timeout=3_000
            )
        ):

            await next_btn.click()

            await page.wait_for_timeout(
                _jitter(1500)
            )


        # 密碼欄位

        pwd_loc = page.locator(

            "input[id='ap_password'], "
            "input[type='password']"

        ).first


        if (

            await pwd_loc.count() == 0

            or not await pwd_loc.is_visible(
                timeout=5_000
            )
        ):

            log.warning(
                "[Amazon Pay] "
                "找不到密碼輸入框"
            )

            return (
                "amazon_auth_needed"
            )


        await pwd_loc.fill(
            AMAZON_PASSWORD
        )


        await page.wait_for_timeout(
            _jitter(500)
        )


        # 點登入

        signin_btn = page.locator(
            "input[id='signInSubmit']"
        ).first


        if (

            await signin_btn.count() == 0

            or not await signin_btn.is_visible(
                timeout=3_000
            )
        ):

            log.warning(
                "[Amazon Pay] "
                "找不到登入按鈕"
            )

            return (
                "amazon_auth_needed"
            )


        await signin_btn.click()


        # 預留 CAPTCHA /
        # 新裝置驗證時間

        try:

            await page.wait_for_function(

                "() => "
                "!window.location.href"
                ".includes('/ap/signin')",

                timeout=45_000,
            )

        except PlaywrightTimeoutError:

            log.warning(
                "[Amazon Pay] "
                "登入後仍停在 signin 頁，"
                f"URL：{page.url}"
            )

            return (
                "amazon_auth_needed"
            )


        await page.wait_for_load_state(

            "networkidle",

            timeout=20_000,
        )


        log.info(
            "[Amazon Pay] "
            "帳密登入成功，"
            "目前頁面："
            f"{page.url}"
        )


    # ─────────────────────────────
    # 已登入 → 點授權 / 続行
    # ─────────────────────────────

    consent_sel = (

        "input[name='consentApply'], "

        "input[value*='続行']:not(#continue), "

        "input[value*='同意して'], "

        "button:has-text('続行'), "

        "button:has-text('同意して'), "

        "[role='button']:has-text('続行'), "

        "[role='button']:has-text('同意して')"
    )


    try:

        cont = page.locator(
            consent_sel
        ).first


        await cont.wait_for(

            state="visible",

            timeout=12_000,
        )


        log.info(
            "[Amazon Pay] "
            "點擊同意/続行，"
            "目前頁面："
            f"{page.url}"
        )


        await cont.click()


        await page.wait_for_timeout(
            _jitter(3000)
        )


    except Exception:
        pass


    return "ok"


async def _add_to_cart(
    page,
    product: dict
) -> bool:

    """
    將商品加入購物車。

    True：
    成功加入。

    False：
    找不到購物車按鈕
    或發生例外。
    """

    import time as _t


    t0 = _t.perf_counter()

    title = product["title"]


    async def _block_zenlink(
        route,
        request
    ):

        await route.abort()


    await page.route(
        "**/*zenlink*",
        _block_zenlink
    )


    try:

        await page.goto(

            product["url"],

            wait_until="load",

            timeout=30_000,
        )


        await _wait_cf(
            page
        )


        await page.wait_for_timeout(
            _jitter(400)
        )


        # session 過期

        if "login" in page.url.lower():

            log.info(
                "[1999加購] "
                "session 已過期，"
                "嘗試重新登入..."
            )


            if not await _login_1999(
                page
            ):

                return False


            await page.goto(

                product["url"],

                wait_until="load",

                timeout=30_000,
            )


        # 尋找加入購物車按鈕

        for sel in _CART_BTN_SELECTORS:

            try:

                loc = page.locator(
                    sel
                ).first


                if (

                    await loc.count() > 0

                    and await loc.is_visible()
                ):

                    await loc.click()


                    log.info(

                        "[1999加購] ✅ "
                        f"({_t.perf_counter() - t0:.1f}s) "
                        f"{title}"
                    )


                    await page.wait_for_timeout(
                        _jitter(600)
                    )


                    return True


            except Exception:
                continue


        log.warning(
            "[1999加購] "
            "找不到購物車按鈕："
            f"{title}"
        )


        return False


    except Exception as e:

        log.error(
            "[1999加購] 例外："
            f"{e}"
        )


        return False


    finally:

        await page.unroute(
            "**/*zenlink*"
        )


async def _checkout_amazon_pay(
    page,
    products: list
) -> str:

    """
    購物車商品全部加入完成後，
    一次使用 Amazon Pay 結帳。

    回傳：

    success
    no_amazon_pay
    amazon_auth_needed
    no_confirm_button
    login_failed
    failed
    """

    import time as _t


    t0 = _t.perf_counter()


    titles = "、".join(
        p["title"]
        for p in products
    )


    try:

        # ─────────────────────────
        # Step 1
        # 進入 /order
        # ─────────────────────────

        await page.goto(

            CHECKOUT_URL,

            wait_until="networkidle",

            timeout=30_000,
        )


        # 未登入 1999

        if "login" in page.url.lower():

            if not await _login_1999(
                page
            ):

                return (
                    "login_failed"
                )


            await page.goto(

                CHECKOUT_URL,

                wait_until="networkidle",

                timeout=30_000,
            )


        log.info(

            "[1999結帳] "
            "/order 載入"
            f"（{_t.perf_counter() - t0:.1f}s）"
        )


        # ─────────────────────────
        # Step 2
        # Amazon Pay
        # ─────────────────────────

        apay = page.locator(

            ".amazonpay-button-view1, "
            ".amazonpay-button-logo"

        ).first


        if (

            await apay.count() == 0

            or not await apay.is_visible()
        ):

            log.warning(
                "[1999結帳] "
                "找不到 Amazon Pay 按鈕"
            )


            return (
                "no_amazon_pay"
            )


        await apay.click()


        log.info(
            "[1999結帳] "
            "Amazon Pay 已點擊，"
            "等待 OAuth..."
        )


        # ─────────────────────────
        # Step 3
        # 等待 Amazon
        # ─────────────────────────

        try:

            await page.wait_for_function(

                f"""
                () => {{

                    const u =
                        window.location.href;

                    return (
                        u.includes(
                            '{BASE_URL}/orderamazon'
                        )
                        ||
                        u.includes(
                            'amazon.co.jp'
                        )
                    );
                }}
                """,

                timeout=20_000,
            )


        except PlaywrightTimeoutError:

            log.warning(
                "[1999結帳] "
                "Amazon Pay 無跳轉，"
                "停在："
                f"{page.url}"
            )


            return "failed"


        # Amazon 授權

        if (

            "amazon.co.jp"
            in page.url

            and "/orderamazon"
            not in page.url
        ):

            auth_result = (
                await _handle_amazon_auth(
                    page
                )
            )


            if auth_result != "ok":

                return auth_result


        # 等待跳回 1999

        try:

            await page.wait_for_url(

                f"{BASE_URL}/orderamazon*",

                timeout=45_000,
            )


        except PlaywrightTimeoutError:

            cur = page.url


            if any(

                k in cur

                for k in (
                    "amazon.co.jp",
                    "payments.amazon"
                )
            ):

                log.warning(
                    "[1999結帳] "
                    "授權後未跳回 "
                    "/orderamazon，"
                    "停在："
                    f"{cur}"
                )


                return (
                    "amazon_auth_needed"
                )


            log.warning(
                "[1999結帳] "
                "/orderamazon 超時，"
                "停在："
                f"{cur}"
            )


            return "failed"


        await page.wait_for_load_state(
            "networkidle"
        )


        log.info(

            "[1999結帳] "
            "/orderamazon 載入"
            f"（{_t.perf_counter() - t0:.1f}s）"
        )


        # ─────────────────────────
        # Step 4
        # 最終確認下單
        # ─────────────────────────

        confirm = page.locator(

            "#btnSendRight, "
            "input[name*='btnSendRight']"

        ).first


        if (

            await confirm.count() == 0

            or not await confirm.is_visible()
        ):

            log.warning(
                "[1999結帳] "
                "找不到確認下單按鈕"
            )


            return (
                "no_confirm_button"
            )


        await confirm.click()


        await page.wait_for_timeout(
            3000
        )


        # ─────────────────────────
        # Step 5
        # 判斷成功
        # ─────────────────────────

        body = ""


        try:

            body = await page.locator(
                "body"
            ).inner_text(
                timeout=5000
            )


        except Exception:
            pass


        if (

            "採購訂単（已完成）"
            in body

            or "ご注文ありがとう"
            in body
        ):

            log.info(

                "[1999結帳] ✅ "
                "下單成功"
                f"（{_t.perf_counter() - t0:.1f}s）："
                f"{titles}"
            )


            return "success"


        log.warning(

            "[1999結帳] "
            "結果不明："
            f"{page.url} | "
            f"body[:80]={body[:80]}"
        )


        return "failed"


    except Exception as e:

        log.error(

            "[1999結帳] "
            "例外："
            f"{e}",

            exc_info=True,
        )


        return "failed"


# ─────────────────────────────────────────────
# 核心偵測邏輯
# ─────────────────────────────────────────────

async def check_once(
    page,
    context=None
) -> bool:

    try:

        products = await fetch_products(
            page
        )


        if not products:

            return False


        # ═════════════════════════════════════
        # NOT_NOTIFY_KEYWORDS
        #
        # BX-43 在這裡直接被排除。
        #
        # 不寄 Email
        # 不下單
        # 不加入冷卻
        # ═════════════════════════════════════

        if NOT_NOTIFY_KEYWORDS:

            ignored_products = [

                p

                for p in products

                if any(

                    kw.upper()
                    in p["title"].upper()

                    for kw
                    in NOT_NOTIFY_KEYWORDS
                )
            ]


            if ignored_products:

                log.info(

                    "NOT_NOTIFY_KEYWORDS "
                    "商品（完全忽略）："

                    + "、".join(
                        p["title"]
                        for p
                        in ignored_products
                    )
                )


                ignored_hrefs = {

                    p["href"]

                    for p
                    in ignored_products
                }


                products = [

                    p

                    for p in products

                    if p["href"]
                    not in ignored_hrefs
                ]


        # 如果搜尋結果只有 BX-43
        # 或其他 NOT_NOTIFY 商品，
        # 直接結束。

        if not products:

            log.info(
                "架上商品皆屬 "
                "NOT_NOTIFY_KEYWORDS，"
                "本輪不通知、不下單"
            )


            return False


        # ═════════════════════════════════════
        # SKIP_KEYWORDS
        #
        # 這些商品：
        #
        # ✅ 通知
        # ❌ 不下單
        # ═════════════════════════════════════

        skip_hrefs: set = set()


        if SKIP_KEYWORDS:

            for p in products:

                if any(

                    kw.upper()
                    in p["title"].upper()

                    for kw
                    in SKIP_KEYWORDS
                ):

                    skip_hrefs.add(
                        p["href"]
                    )


            if skip_hrefs:

                log.info(

                    "SKIP_KEYWORDS "
                    "商品（僅通知，不下單）："
                    f"{len(skip_hrefs)} 件"
                )


        if not products:

            return False


        # ═════════════════════════════════════
        # 通知冷卻
        # ═════════════════════════════════════

        now = datetime.now(
            TW_TZ
        )


        cutoff = (
            now
            - NOTIFY_COOLDOWN
        )


        notified = (
            load_notified()
        )


        current_hrefs = {

            p["href"]

            for p in products
        }


        # 下架商品清除冷卻
        #
        # 商品重新上架時，
        # 就會重新視為可通知。

        notified = {

            h: t

            for h, t
            in notified.items()

            if h
            in current_hrefs
        }


        # ═════════════════════════════════════
        # 找出本輪要通知的商品
        # ═════════════════════════════════════

        to_notify = [

            p

            for p in products

            if (

                p["href"]
                not in notified

                or

                _parse_ts(
                    notified[
                        p["href"]
                    ]
                )
                < cutoff
            )
        ]


        if to_notify:

            checkout_results: dict = {}


            # ═════════════════════════════════
            # 自動下單
            # ═════════════════════════════════

            if AUTO_CHECKOUT:

                purchased = (
                    load_order_state()
                )


                to_checkout = [

                    p

                    for p in to_notify

                    if (

                        p["href"]
                        not in purchased

                        and

                        p["href"]
                        not in skip_hrefs

                        and

                        any(

                            kw.upper()
                            in p["title"].upper()

                            for kw
                            in BUY_KEYWORDS
                        )
                    )
                ]


                if not to_checkout:

                    log.info(
                        "本輪沒有需要"
                        "自動下單的商品"
                    )


                else:

                    log.info(

                        "批次加入購物車："
                        f"{len(to_checkout)} 件"
                    )


                    # ─────────────────────
                    # 一次加入所有購物車
                    # ─────────────────────

                    cart_added = []


                    for p in to_checkout:

                        if await _add_to_cart(
                            page,
                            p
                        ):

                            cart_added.append(
                                p
                            )

                        else:

                            checkout_results[
                                p["href"]
                            ] = (
                                "cart_not_found"
                            )


                    # ─────────────────────
                    # Amazon Pay 一次結帳
                    # ─────────────────────

                    if cart_added:

                        log.info(

                            "一次結帳："
                            f"{len(cart_added)} 件"
                        )


                        status = (
                            await _checkout_amazon_pay(
                                page,
                                cart_added
                            )
                        )


                        for p in cart_added:

                            checkout_results[
                                p["href"]
                            ] = status


                        # 成功才紀錄
                        # 防止下一輪再次購買。

                        if status == "success":

                            for p in cart_added:

                                purchased[
                                    p["href"]
                                ] = (
                                    now.isoformat()
                                )


                        save_order_state(
                            purchased
                        )


                        # 更新 Amazon
                        # storage state

                        if context:

                            try:

                                await context.storage_state(

                                    path=str(
                                        STORAGE_STATE_FILE
                                    )
                                )


                                log.info(

                                    "Amazon session "
                                    "已更新："
                                    f"{STORAGE_STATE_FILE}"
                                )


                            except Exception as e:

                                log.warning(

                                    "儲存 session "
                                    "失敗："
                                    f"{e}"
                                )


            # ═════════════════════════════════
            # Email 通知
            # ═════════════════════════════════

            log.info(

                f"發送通知："
                f"{len(to_notify)} 件"

                f"（共 "
                f"{len(products)} 件，"

                f"跳過 "
                f"{len(products) - len(to_notify)} "
                f"件冷卻中）"
            )


            send_notify_email(
                to_notify
            )


            # 自動下單結果
            # 只寄給購買帳號本人。

            if checkout_results:

                _send_order_result_email(

                    to_notify,

                    checkout_results,
                )


            # 更新通知時間

            for p in to_notify:

                notified[
                    p["href"]
                ] = (
                    now.isoformat()
                )


        else:

            log.info(

                f"所有 "
                f"{len(products)} 件商品"
                f"均在 1 小時冷卻期內"
            )


        save_notified(
            notified
        )


        return True


    except Exception as e:

        log.error(

            "執行例外："
            f"{e}",

            exc_info=True,
        )


        return False


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

async def main():

    log.info(

        f"1999 監控器 | "

        f"關鍵字："
        f"{NOTIFY_KEYWORD} | "

        f"輪數："
        f"{CHECK_ROUNDS}"

        + (
            " | 自動下單：開啟"
            if AUTO_CHECKOUT
            else ""
        )
    )


    ua = _random_ua()

    viewport = (
        _random_viewport()
    )


    log.info(

        f"UA: ..."
        f"{ua[-50:]} | "

        f"{viewport['width']}"
        f"x"
        f"{viewport['height']}"
    )


    # ─────────────────────────────
    # Amazon Pay storage state
    # ─────────────────────────────

    storage_state = None


    if (

        AUTO_CHECKOUT

        and STORAGE_STATE_FILE.exists()
    ):

        storage_state = str(
            STORAGE_STATE_FILE
        )


        log.info(

            "載入 Amazon Pay "
            "session："
            f"{STORAGE_STATE_FILE}"
        )


    elif AUTO_CHECKOUT:

        log.warning(

            f"找不到 "
            f"{STORAGE_STATE_FILE}，"
            "Amazon Pay "
            "可能無法自動完成授權"
        )


    # ─────────────────────────────
    # Playwright
    # ─────────────────────────────

    async with async_playwright() as p:


        browser = await p.chromium.launch(

            headless=HEADLESS,

            slow_mo=_jitter(60),

            args=[

                "--disable-blink-features="
                "AutomationControlled"
            ],
        )


        context = await browser.new_context(

            user_agent=ua,

            viewport=viewport,

            locale="ja-JP",

            timezone_id="Asia/Tokyo",

            storage_state=storage_state,

            extra_http_headers={

                "Accept-Language":
                    "ja-JP,"
                    "ja;q=0.9,"
                    "zh-TW;q=0.8,"
                    "en;q=0.7",

                "Sec-Fetch-Dest":
                    "document",

                "Sec-Fetch-Mode":
                    "navigate",

                "Sec-Fetch-Site":
                    "none",

                "Sec-Fetch-User":
                    "?1",

                "Upgrade-Insecure-Requests":
                    "1",
            },
        )


        await context.add_init_script(

            "Object.defineProperty("
            "navigator, "
            "'webdriver', "
            "{get: () => undefined}"
            ")"
        )


        page = (
            await context.new_page()
        )


        # ═════════════════════════════════════
        # 自動下單模式：
        # 先確認 1999 登入狀態
        # ═════════════════════════════════════

        if AUTO_CHECKOUT:

            try:

                await page.goto(

                    f"{BASE_URL}/mypage",

                    wait_until="domcontentloaded",

                    timeout=15_000,
                )


                if "login" in page.url.lower():

                    log.info(
                        "1999 session "
                        "已過期，"
                        "嘗試使用帳號密碼登入..."
                    )


                    await _login_1999(
                        page
                    )


                else:

                    log.info(
                        "1999 session "
                        "有效，已登入"
                    )


            except Exception as e:

                log.warning(

                    "登入確認失敗："
                    f"{e}"
                )


        # ═════════════════════════════════════
        # 執行偵測輪次
        # ═════════════════════════════════════

        for round_num in range(

            1,

            CHECK_ROUNDS + 1
        ):

            if CHECK_ROUNDS > 1:

                log.info(

                    f"── 第 "
                    f"{round_num}/"
                    f"{CHECK_ROUNDS} "
                    f"輪 ──"
                )


            await check_once(
                page,
                context
            )


            if (
                round_num
                < CHECK_ROUNDS
            ):

                wait = random.randint(
                    5,
                    8
                )


                log.info(

                    f"等待 "
                    f"{wait} 秒後"
                    f"進行下一輪..."
                )


                await asyncio.sleep(
                    wait
                )


        await browser.close()


    log.info(
        "所有輪次完成"
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )
