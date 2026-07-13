# Beyblade X & Funbox 商品偵測器

自動偵測兩個玩具電商的新商品，透過 Gmail 發送通知。每 5 分鐘由 cron-job.org 觸發一次 GitHub Actions。

---

## 架構圖

```
┌─────────────────────────────────────────────────────────────┐
│                        cron-job.org                         │
│              每 5 分鐘 POST → GitHub API                     │
└────────────────────┬────────────────────┬───────────────────┘
                     │                    │
          workflow_dispatch    workflow_dispatch
                     │                    │
         ┌───────────▼──────┐  ┌──────────▼────────┐
         │  GitHub Actions  │  │  GitHub Actions   │
         │ beyblade_monitor │  │  funbox_monitor   │
         │   (ubuntu VM)    │  │   (ubuntu VM)     │
         └───────────┬──────┘  └──────────┬────────┘
                     │                    │
          ┌──────────▼──────┐  ┌──────────▼────────┐
          │  Playwright +   │  │  Playwright +     │
          │  Chromium       │  │  Chromium         │
          │  12 輪 / 次      │  │  18 輪 / 次        │
          └──────────┬──────┘  └──────────┬────────┘
                     │                    │
         ┌───────────▼──────┐  ┌──────────▼────────┐
         │  1999.co.jp      │  │ shop.funbox.com.tw│
         │  Beyblade X 頁   │  │  TOMICA / 陀螺 頁  │
         │  (有 Cloudflare) │  │  (JS SPA 渲染)    │
         └───────────┬──────┘  └──────────┬────────┘
                     │                    │
                     └─────────┬──────────┘
                               │
                  ┌────────────▼───────────┐
                  │   1 小時冷卻去重邏輯    │
                  │  seen_products.json    │
                  │  (GitHub Actions cache)│
                  └────────────┬───────────┘
                               │ 有新商品 / 冷卻到期
                  ┌────────────▼───────────┐
                  │     Gmail SMTP SSL     │
                  │  → kevin850703@gmail   │
                  │  → u0342059@gmail      │
                  └────────────────────────┘
```

---

## 專案結構

```
beyblade-funbox-monitor/
├── .github/
│   └── workflows/
│       ├── beyblade_monitor.yml   # Beyblade X 的 GitHub Actions workflow
│       └── funbox_monitor.yml     # Funbox 的 GitHub Actions workflow
├── beyblade_monitor/
│   ├── beyblade_monitor.py        # 主程式（Playwright，含 Cloudflare 反偵測）
│   └── requirements.txt
└── funbox_monitor/
    ├── funbox_monitor.py          # 主程式（Playwright，等待 JS SPA 渲染）
    └── requirements.txt
```

---

## 兩個監控器比較

| | Beyblade X | Funbox |
|---|---|---|
| 目標網站 | `1999.co.jp` | `shop.funbox.com.tw` |
| 偵測商品 | Beyblade X 系列 | TOMICA 小汽車 / 戰鬥陀螺 |
| 反爬蟲 | Cloudflare（需反偵測） | JS SPA 動態渲染 |
| 瀏覽器技術 | Playwright + 隨機 UA/Viewport | Playwright + wait_for_selector |
| 每次執行輪數 | 12 輪（間隔 5~8 秒） | 18 輪（間隔 5~8 秒） |
| 單次執行時間 | ~4.2 分鐘 | ~4.8 分鐘 |

---

## 通知邏輯

```
每輪執行
  │
  ├─ 擷取頁面上所有商品
  │
  ├─ 比對 seen_products.json
  │     ├─ 從未通知過 → 發通知
  │     ├─ 上次通知超過 1 小時 → 再次發通知
  │     └─ 1 小時內已通知 → 跳過（冷卻中）
  │
  ├─ 更新 seen_products.json 時間戳記
  │
  └─ 清除已下架商品的記錄（下次出現視為新品）
```

---

## 狀態持久化

`seen_products.json` 由 GitHub Actions cache 在跨執行之間傳遞：

```yaml
- uses: actions/cache@v4
  with:
    path: beyblade_monitor/seen_products.json
    key: beyblade-seen-${{ github.run_id }}
    restore-keys: beyblade-seen-   # 永遠取最新的快照
```

---

## 環境設定

### GitHub Secrets（必填）

| Secret | 說明 |
|---|---|
| `GMAIL_SENDER` | 寄件 Gmail 帳號 |
| `GMAIL_PASSWORD` | Gmail App Password（非登入密碼） |
| `GMAIL_RECIPIENTS` | 收件人，逗號分隔 |

### 本機開發（`.env`，不進版控）

```env
GMAIL_SENDER=your@gmail.com
GMAIL_PASSWORD=xxxx xxxx xxxx xxxx
GMAIL_RECIPIENTS=a@gmail.com,b@gmail.com
CHECK_ROUNDS=1
```

```bash
pip install -r beyblade_monitor/requirements.txt
playwright install chromium
python beyblade_monitor/beyblade_monitor.py
```

---

## 觸發方式

由 **cron-job.org** 每 5 分鐘呼叫 GitHub API：

```
POST https://api.github.com/repos/k0341055/beyblade-funbox-monitor/actions/workflows/beyblade_monitor.yml/dispatches
Authorization: Bearer <PAT>
Content-Type: application/json

{"ref": "main"}
```

成功回應：**204 No Content**
