"""
[TEST ONLY - 測試完畢後請刪除此檔案及對應 GitHub Action]

用途：驗證從 GitHub Actions (US VM) 能否成功登入誠品並下單
監控目標、帳號、收件人均從 GitHub Variable / Secret 讀取，不硬編碼個資。
"""

import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

from playwright.sync_api import sync_playwright
from eslite_monitor import EsliteMonitorBase

log = logging.getLogger(__name__)

EXHIBITION_URL = os.environ.get("TEST_EXHIBITION_URL", "").strip()
TEST_ACCOUNT   = os.environ.get("TEST_ESLITE_ACCOUNT", "").strip()
TEST_RECIPIENT = os.environ.get("TEST_RECIPIENT_EMAIL", "").strip()

if not EXHIBITION_URL:
    raise ValueError("TEST_EXHIBITION_URL 未設定（請設定 GitHub Variable）")
if not TEST_ACCOUNT:
    raise ValueError("TEST_ESLITE_ACCOUNT 未設定（請設定 GitHub Secret）")
if not TEST_RECIPIENT:
    raise ValueError("TEST_RECIPIENT_EMAIL 未設定（請設定 GitHub Secret）")


class TestCheckoutMonitor(EsliteMonitorBase):

    def __init__(self):
        super().__init__()
        # 強制覆寫：不受 env / secret 影響
        self.GMAIL_RECIPIENTS = [TEST_RECIPIENT]
        self.ORDER_RECIPIENTS  = [TEST_RECIPIENT]
        self.ESLITE_ACCOUNT    = TEST_ACCOUNT
        self.AUTO_CHECKOUT     = True
        self.EVENT_PAGE_URL    = EXHIBITION_URL
        self.CHECK_ROUNDS      = int(os.environ.get("CHECK_ROUNDS", "3"))

    # ── 抓書展頁商品 ──────────────────────────

    def fetch_in_stock_products(self, page) -> list:
        page.goto(EXHIBITION_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)

        links = page.eval_on_selector_all(
            "a[href*='/product/']",
            "els => [...new Set(els.map(el => el.getAttribute('href')))].filter(h => h)",
        )

        guids = []
        seen  = set()
        for link in links:
            parts = link.split("/product/")
            if len(parts) > 1:
                guid = parts[1].split("?")[0].split("/")[0].strip()
                if guid and guid not in seen:
                    seen.add(guid)
                    guids.append(guid)
        guids = guids[:10]
        log.info(f"書展頁抽取到 {len(guids)} 個商品 GUID")

        result = []
        for guid in guids:
            product = self._fetch_single_product(page, guid)
            if product:
                result.append(product)

        log.info(f"有庫存 {len(result)} 件")
        return result

    # ── 覆寫 run()：先驗證登入，再開始監控 ────

    def run(self):
        TW_TZ = timezone(timedelta(hours=8))
        log.info("=== 誠品結帳功能測試開始 ===")

        with sync_playwright() as pw:
            br = pw.chromium.launch(
                headless=self.HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )

            # Step 1：驗證 session 登入
            log.info("Step 1 — 驗證 Session 登入狀態")
            ckout_kwargs = {"user_agent": self._UA}
            if self.STORAGE_STATE_FILE.exists():
                ckout_kwargs["storage_state"] = str(self.STORAGE_STATE_FILE)
                log.info(f"Session 檔案已找到：{self.STORAGE_STATE_FILE}")
            else:
                log.warning("未找到 session 檔案，將嘗試帳密登入")

            login_ctx = br.new_context(**ckout_kwargs)
            login_ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            login_page = login_ctx.new_page()
            login_ok   = self._ensure_logged_in(login_page, login_ctx)
            login_ctx.close()

            now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if login_ok:
                log.info("✓ 登入驗證成功，繼續監控")
                self._send_email(
                    self.GMAIL_RECIPIENTS,
                    "【誠品測試】✓ US VM 登入成功",
                    "\n".join([
                        f"帳號 {TEST_ACCOUNT} 從 GitHub Actions (US VM) 登入誠品成功！",
                        f"Session 有效，自動下單功能正常。",
                        f"時間：{now_str}（台灣時間）",
                        "",
                        "接下來開始監控書展，若有庫存將嘗試自動下單...",
                        f"書展連結：{EXHIBITION_URL}",
                    ]),
                )
            else:
                log.error("✗ 登入驗證失敗，終止測試")
                self._send_email(
                    self.GMAIL_RECIPIENTS,
                    "【誠品測試】✗ US VM 登入失敗",
                    "\n".join([
                        f"帳號 {TEST_ACCOUNT} 從 GitHub Actions (US VM) 登入誠品失敗！",
                        f"Session 已失效或需要簡訊驗證，請重新執行 generate_session.py。",
                        f"時間：{now_str}（台灣時間）",
                    ]),
                )
                br.close()
                return

            # Step 2：匿名監控 + 嘗試下單
            log.info(f"Step 2 — 監控書展（{self.CHECK_ROUNDS} 輪）")
            mon_ctx = br.new_context(user_agent=self._UA)
            mon_ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            mon_page = mon_ctx.new_page()

            for round_num in range(1, self.CHECK_ROUNDS + 1):
                log.info(f"── 第 {round_num}/{self.CHECK_ROUNDS} 輪 ──")
                self.check_once(mon_page)
                if round_num < self.CHECK_ROUNDS:
                    wait = random.randint(3, 5)
                    log.info(f"等待 {wait} 秒...")
                    time.sleep(wait)

            br.close()

        log.info("=== 誠品結帳功能測試完成 ===")


if __name__ == "__main__":
    TestCheckoutMonitor().run()
