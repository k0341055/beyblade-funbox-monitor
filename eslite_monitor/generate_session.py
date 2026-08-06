"""一次性工具：開啟誠品登入頁，手動登入後儲存 session 狀態"""
import os
from playwright.sync_api import sync_playwright

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
    print(">>> 瀏覽器已開啟，請手動輸入帳號密碼登入（含簡訊驗證碼，等待最多 5 分鐘）")
    print(f">>> 目前 URL：{page.url}")

    try:
        # 等待 pathname 離開 /login（最多 5 分鐘）
        page.wait_for_function(
            "() => !window.location.pathname.startsWith('/login')",
            timeout=300_000,
        )
        print(f">>> 已離開登入頁，目前 URL：{page.url}")

        # 若跳到 SMS / OTP 驗證頁，繼續等完成
        sms_keywords = ["verify", "otp", "sms", "驗證"]
        if any(kw in page.url.lower() for kw in sms_keywords):
            print(">>> 偵測到驗證頁，請完成驗證（再等最多 3 分鐘）")
            page.wait_for_function(
                "() => !['verify','otp','sms','驗證'].some(k => window.location.href.includes(k))",
                timeout=180_000,
            )
            print(f">>> 驗證完成，目前 URL：{page.url}")

        ctx.storage_state(path=OUT_FILE)
        print(f">>> ✓ Session 已儲存至 {OUT_FILE}")
        print()
        print(">>> 接下來執行以下指令更新 GitHub Secret：")
        print(f"    base64 -i eslite_monitor/{OUT_FILE} | tr -d '\\n' | gh secret set ESLITE_STORAGE_STATE_B64")

    except Exception as e:
        print(f">>> 等待超時或發生錯誤：{e}")
        print(f">>> 目前 URL：{page.url}")

    br.close()
