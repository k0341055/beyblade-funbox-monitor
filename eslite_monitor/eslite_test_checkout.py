"""
[TEST ONLY - 測試完畢後請刪除此檔案及對應 GitHub Action]

用途：驗證從 GitHub Actions (US VM) 能否成功登入誠品並下單
監控目標、帳號、收件人均從 GitHub Variable / Secret 讀取，不硬編碼個資。
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

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

    # run() 直接繼承基底類別：匿名監控 context + 僅在找到有庫存商品時才建立結帳 context
    # 移除先前額外的 Step 1 登入驗證，避免多一次 US VM 認證請求導致台灣手機被登出


if __name__ == "__main__":
    TestCheckoutMonitor().run()
