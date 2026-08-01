"""一次性工具：登入誠品並儲存 session 狀態到 eslite_storage_state.json"""
import os
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(dotenv_path=".env")

ACCOUNT  = os.environ["ESLITE_ACCOUNT"]
PASSWORD = os.environ["ESLITE_PASSWORD"]
OUT_FILE = os.environ.get("STORAGE_STATE_FILE", "eslite_storage_state.json")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

with sync_playwright() as pw:
    br  = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    ctx = br.new_context(user_agent=UA)
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = ctx.new_page()

    page.goto("https://www.eslite.com/login", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)
    page.get_by_role("textbox", name="台灣手機/會員卡號/自訂帳號").click()
    page.get_by_role("textbox", name="台灣手機/會員卡號/自訂帳號").fill(ACCOUNT)
    page.get_by_role("textbox", name="請輸入密碼").click()
    page.get_by_role("textbox", name="請輸入密碼").fill(PASSWORD)
    page.get_by_role("button", name="登入").click()
    page.wait_for_timeout(1500)

    print(">>> 請在瀏覽器中完成圖形驗證碼（若出現），完成後腳本自動繼續（等待最多 120 秒）")
    try:
        page.wait_for_function(
            "() => !window.location.pathname.startsWith('/login')",
            timeout=120_000,
        )
        ctx.storage_state(path=OUT_FILE)
        print(f">>> Session 已儲存至 {OUT_FILE}")
    except Exception as e:
        print(f">>> 登入失敗：{e}")

    br.close()
