"""
自動更新 Amazon Pay session（到達 /orderamazon 後存檔，不下單）
存完後自動上傳到 GitHub Secret AMAZON_1999_STORAGE_STATE_B64
"""
import asyncio, base64, os, subprocess
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

PRODUCT_URL   = "https://www.1999.co.jp/10972434"
CHECKOUT_URL  = "https://www.1999.co.jp/order"
BASE_URL      = "https://www.1999.co.jp"
SESSION_FILE  = Path(os.environ.get("STORAGE_STATE_FILE", "1999_storage_state.json"))

ACCOUNT_1999    = os.environ.get("ACCOUNT_1999", "")
PASSWORD_1999   = os.environ.get("PASSWORD_1999", "")
AMAZON_ACCOUNT  = os.environ.get("AMAZON_ACCOUNT", "")
AMAZON_PASSWORD = os.environ.get("AMAZON_PASSWORD", "")

_CART_SELECTORS = [
    "label.c-product-detail__info-submit-button-primary",
    "[id*='viwCart'] label",
    "[class*='submit-button-primary']",
]

async def _handle_amazon_auth(page):
    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    await page.wait_for_timeout(1500)
    print(f"[Amazon Pay] 授權頁：{page.url}")

    email_loc = page.locator("input[id='ap_email'], input[type='email']").first
    needs_login = await email_loc.count() > 0 and await email_loc.is_visible(timeout=3_000)

    if needs_login:
        print(f"[Amazon Pay] 填入 Email：{AMAZON_ACCOUNT}")
        await email_loc.fill(AMAZON_ACCOUNT)
        await page.wait_for_timeout(500)
        next_btn = page.locator("input[id='continue']").first
        if await next_btn.count() > 0 and await next_btn.is_visible(timeout=3_000):
            await next_btn.click()
            await page.wait_for_timeout(1500)
        pwd_loc = page.locator("input[id='ap_password'], input[type='password']").first
        if await pwd_loc.count() == 0 or not await pwd_loc.is_visible(timeout=5_000):
            print("[Amazon Pay] 找不到密碼欄")
            return False
        await pwd_loc.fill(AMAZON_PASSWORD)
        await page.wait_for_timeout(500)
        signin_btn = page.locator("input[id='signInSubmit']").first
        if await signin_btn.count() == 0 or not await signin_btn.is_visible(timeout=3_000):
            print("[Amazon Pay] 找不到登入按鈕")
            return False
        await signin_btn.click()
        try:
            await page.wait_for_function(
                "() => !window.location.href.includes('/ap/signin')",
                timeout=45_000,
            )
        except PlaywrightTimeoutError:
            print(f"[Amazon Pay] 登入後仍在 signin，URL：{page.url}")
            return False
        await page.wait_for_load_state("networkidle", timeout=20_000)
        print(f"[Amazon Pay] 登入成功，目前：{page.url}")

    consent_sel = (
        "input[name='consentApply'], "
        "input[value*='続行']:not(#continue), "
        "input[value*='同意して'], "
        "button:has-text('続行'), button:has-text('同意して'), "
        "[role='button']:has-text('続行'), [role='button']:has-text('同意して')"
    )
    try:
        cont = page.locator(consent_sel).first
        await cont.wait_for(state="visible", timeout=12_000)
        print(f"[Amazon Pay] 點擊続行，URL：{page.url}")
        await cont.click()
        await page.wait_for_timeout(3000)
    except Exception:
        pass
    return True


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=150)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        # ── 1. 加入購物車 ────────────────────────────────────────
        async def _block(route, request): await route.abort()
        await page.route("**/*zenlink*", _block)
        print(f"\n[1] 前往商品頁：{PRODUCT_URL}")
        await page.goto(PRODUCT_URL, wait_until="load", timeout=30_000)
        await page.wait_for_timeout(2000)

        if "login" in page.url.lower():
            print("[1] 需要登入 1999...")
            await page.locator("input[type='email'], input[name*='mail']").first.fill(ACCOUNT_1999)
            await page.locator("input[type='password']").first.fill(PASSWORD_1999)
            await page.locator("input[type='submit'], button[type='submit']").first.click()
            await page.wait_for_timeout(3000)
            await page.goto(PRODUCT_URL, wait_until="load", timeout=30_000)

        cart_ok = False
        for sel in _CART_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    cart_ok = True
                    print(f"[1] ✅ 加入購物車：{sel}")
                    break
            except Exception:
                continue
        await page.unroute("**/*zenlink*")

        if not cart_ok:
            print("[1] ❌ 找不到購物車按鈕（可能缺貨）")
            await browser.close()
            return

        await page.wait_for_timeout(1000)

        # ── 2. /order → Amazon Pay ────────────────────────────────
        print(f"\n[2] 前往：{CHECKOUT_URL}")
        await page.goto(CHECKOUT_URL, wait_until="networkidle", timeout=30_000)
        if "login" in page.url.lower():
            await page.locator("input[type='email'], input[name*='mail']").first.fill(ACCOUNT_1999)
            await page.locator("input[type='password']").first.fill(PASSWORD_1999)
            await page.locator("input[type='submit'], button[type='submit']").first.click()
            await page.wait_for_timeout(3000)
            await page.goto(CHECKOUT_URL, wait_until="networkidle", timeout=30_000)

        apay = page.locator(".amazonpay-button-view1, .amazonpay-button-logo").first
        if await apay.count() == 0 or not await apay.is_visible():
            print("[2] ❌ 找不到 Amazon Pay 按鈕")
            await browser.close()
            return

        print("[2] ✅ 點擊 Amazon Pay...")
        await apay.click()
        await page.wait_for_timeout(2000)

        try:
            await page.wait_for_function(
                f"""() => {{
                    const u = window.location.href;
                    return u.includes('{BASE_URL}/orderamazon') || u.includes('amazon.co.jp');
                }}""",
                timeout=20_000,
            )
        except PlaywrightTimeoutError:
            print(f"[2] ⚠ 無跳轉，URL：{page.url}")

        # ── 3. Amazon 授權 ───────────────────────────────────────
        if "amazon.co.jp" in page.url and "/orderamazon" not in page.url:
            ok = await _handle_amazon_auth(page)
            if not ok:
                print("[3] Amazon 授權失敗")
                await browser.close()
                return
            try:
                await page.wait_for_url(f"{BASE_URL}/orderamazon*", timeout=45_000)
            except PlaywrightTimeoutError:
                print(f"[3] ⚠ 未跳回 /orderamazon，URL：{page.url}")

        # ── 4. 到達 /orderamazon → 存 session（不下單）────────────
        if "/orderamazon" not in page.url:
            print(f"[4] ❌ 未到達 /orderamazon，URL：{page.url}")
            await browser.close()
            return

        print(f"\n[4] ✅ 到達 /orderamazon！儲存 session（不下單）...")
        await ctx.storage_state(path=str(SESSION_FILE))
        print(f"[4] Session 已儲存：{SESSION_FILE}")

        await browser.close()

    # ── 5. 上傳到 GitHub Secret ──────────────────────────────────
    encoded = base64.b64encode(SESSION_FILE.read_bytes()).decode()
    result = subprocess.run(
        ["gh", "secret", "set", "AMAZON_1999_STORAGE_STATE_B64",
         "--repo", "k0341055/beyblade-funbox-monitor",
         "--body", encoded],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("[5] ✅ AMAZON_1999_STORAGE_STATE_B64 已更新到 GitHub Secret")
    else:
        print(f"[5] ❌ GitHub 上傳失敗：{result.stderr}")


asyncio.run(main())
