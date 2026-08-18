"""
一次性工具：手動登入 1999.co.jp + 完成一次 Amazon Pay 授權，儲存完整 browser session。

使用情境：
  首次設定、或 Amazon Pay session 過期（自動下單收到「需重新授權」通知）時執行。

用法：
  1. python generate_1999_session.py
  2. 在開啟的瀏覽器中登入 1999.co.jp
  3. 瀏覽任一有庫存商品頁，加入購物車
  4. 前往購物車 → 點擊「Amazon Pay」按鈕 → 完成 Amazon Japan 授權
  5. 等待跳回 1999.co.jp /orderamazon 頁（【不要按「我同意並下單。」】）
  6. 程式偵測到 /orderamazon 後自動儲存 session 並關閉瀏覽器

儲存完成後，將 session 上傳至 GitHub Secret：
  base64 -i 1999_storage_state.json | pbcopy
  → GitHub Repo → Settings → Secrets → AMAZON_1999_STORAGE_STATE_B64 → 貼上並儲存
"""

import asyncio
import base64
import subprocess
from pathlib import Path

OUTPUT = Path("1999_storage_state.json")
BASE_URL = "https://www.1999.co.jp"
CHECKOUT_URL = f"{BASE_URL}/order"


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("請先安裝 playwright：pip install playwright && playwright install chromium")
        return

    print("=" * 60)
    print("1999 + Amazon Pay Session 儲存工具")
    print("=" * 60)
    print()
    print("請在開啟的瀏覽器中完成以下步驟：")
    print("  1. 登入 1999.co.jp（若尚未登入）")
    print("  2. 搜尋並開啟任一有庫存的商品頁")
    print("  3. 點擊「カートに入れる」加入購物車")
    print("  4. 前往 /order 結帳頁")
    print("  5. 點擊「Amazon Pay」按鈕，完成 Amazon Japan 登入授權")
    print("  6. 等待頁面跳回 /orderamazon（不要按「我同意並下單。」）")
    print()
    print("程式會自動偵測到 /orderamazon 頁並儲存 session。")
    print()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=50,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 800},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        print("正在開啟 1999.co.jp 登入頁...")
        await page.goto(f"{BASE_URL}/login")

        print("等待您完成操作（最多 10 分鐘）...")
        try:
            await page.wait_for_url(f"{BASE_URL}/orderamazon*", timeout=600_000)
        except Exception:
            print()
            print("⚠ 等待超時或發生錯誤，仍嘗試儲存目前的 session...")

        await context.storage_state(path=str(OUTPUT))
        await browser.close()

    print()
    print(f"✅ Session 已儲存至：{OUTPUT}")
    print()
    print("下一步 — 上傳至 GitHub Secret：")
    print()

    # Try to copy to clipboard automatically
    try:
        encoded = base64.b64encode(OUTPUT.read_bytes()).decode()
        try:
            subprocess.run(["pbcopy"], input=encoded.encode(), check=True)
            print("  已自動複製到剪貼簿！")
            print("  前往 GitHub Repo → Settings → Secrets and variables → Actions")
            print("  → New repository secret → Name: AMAZON_1999_STORAGE_STATE_B64 → 貼上儲存")
        except (FileNotFoundError, subprocess.CalledProcessError):
            print(f"  base64 -i {OUTPUT} | pbcopy")
            print("  （在 macOS Terminal 執行，會複製到剪貼簿）")
    except Exception:
        print(f"  base64 -i {OUTPUT} | pbcopy")

    print()
    print("完成後，GitHub Actions 在下次執行時將自動使用新的 Amazon Pay 授權。")


if __name__ == "__main__":
    asyncio.run(main())
