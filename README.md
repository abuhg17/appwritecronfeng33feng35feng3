# Appwrite 定時備份（GitHub Actions Cron）

每小時透過 GitHub Actions 呼叫 Appwrite REST API，把指定 Database 的 **collections + documents** 匯出成 JSON，並寫回本 repo 做版本化備份。

---

## 運作原理

```text
┌─────────────────┐     cron / dispatch      ┌──────────────────────┐
│  GitHub Actions │ ───────────────────────► │  ubuntu-latest job   │
│  schedule 33/37 │   或手動 / 外部 HTTP     │  checkout + Python   │
└─────────────────┘                          └──────────┬───────────┘
                                                        │
                         secrets: ENDPOINT / PROJECT / DB / API_KEY
                                                        │
                                                        ▼
                                             ┌──────────────────────┐
                                             │ fetch_appwrite_      │
                                             │ backup.py            │
                                             └──────────┬───────────┘
                                                        │
          ┌─────────────────────────────────────────────┼────────────────────────┐
          │                                             ▼                        │
          │                              Appwrite REST (server key)              │
          │                     GET /databases/{id}/collections  (分頁)          │
          │                     GET .../collections/{cid}/documents (分頁)       │
          │                                             │                        │
          │                                             ▼                        │
          │                              sanitize（遮罩 token/password 等）       │
          │                              content fingerprint (SHA-256)           │
          │                                             │                        │
          │                    ┌────────────────────────┴────────────────┐       │
          │                    │ 與 latest.json 內容相同？               │       │
          │                    └───────────┬──────────────┬──────────────┘       │
          │                          是 ▼            否 ▼                        │
          │                     跳過 latest/history    寫 latest + history       │
          │                                          prune 舊 history            │
          │                     └──────────┬───────────┘                         │
          │                                ▼                                     │
          │              landtophistory：UTC 單數時寫入 / 偶數時移除               │
          └──────────────────────────────────────────────┬───────────────────────┘
                                                         │
                                                         ▼
                                              git commit + push（有 diff 才做）
```

### 1. 觸發（Trigger）

| 來源 | 說明 |
|------|------|
| `schedule` | UTC 每小時的 **:33** 與 **:37**（雙排程提高可靠度） |
| `workflow_dispatch` | Actions 頁面手動跑 |
| `repository_dispatch` | 外部 cron `POST` GitHub API（`event_type: external-hourly-sync`） |

`concurrency.group` 設為 `appwrite-sync-${{ github.ref }}` 且 **不** `cancel-in-progress`，避免同分支重疊寫入；若兩次觸發太近會排隊。

### 2. 匯出（Export）

腳本 `scripts/fetch_appwrite_backup.py` 使用 **純標準庫**（`urllib` + `json`），不需 `pip install`。

1. 讀環境變數：`APPWRITE_ENDPOINT`、`PROJECT_ID`、`DATABASE_ID`、`API_KEY`
2. 用 Server API Key 對 Appwrite 發 `GET`（headers: `X-Appwrite-Project` / `X-Appwrite-Key`）
3. **分頁**：Appwrite Query 的 `limit` + `offset`，預設每頁 100 筆，直到回傳少於一頁
4. 對每個 collection 拉齊全部 documents，組成 snapshot：

```json
{
  "exportedAt": "ISO-8601 UTC",
  "projectId": "...",
  "databaseId": "...",
  "collectionCount": N,
  "collections": [
    {
      "collection": { /* schema / metadata */ },
      "documentsCount": M,
      "documents": [ /* ... */ ]
    }
  ]
}
```

### 3. 安全遮罩（Sanitize）

寫入 repo 前會遞迴處理 JSON：

- **依 key 名**：`password`、`token`、`api_key`、`secret`… 等（含常見後綴）→ 改成 `[REDACTED_SECRET]`
- **依值的 pattern**：`sk-...`、`Bearer ...` 等
- **刻意不遮罩** 單純叫 `key` 的欄位（Appwrite attribute schema 的 `{"key": "fieldName"}`），避免把欄位名稱整批蓋掉

### 4. 變更偵測 + 歷史保留（優化）

| 機制 | 行為 |
|------|------|
| **Content fingerprint** | 對 `projectId` / `databaseId` / `collections` 做穩定 JSON + SHA-256（**忽略** `exportedAt`） |
| **Skip if unchanged** | 與現有 `latest.json` 指紋相同 → **不寫檔** → workflow 不會 commit（減少無意義歷史與 repo 膨脹） |
| **History retention** | 變更時寫入 `history/snapshot-YYYYMMDDTHHMMSSZ.json`，再刪除超過 `APPWRITE_HISTORY_RETENTION` 的舊檔（預設 **168** ≈ 每小時備份保留約 7 天） |
| **landtophistory 奇偶時** | 依 **UTC 小時** 奇偶切換（與內容是否變更無關）：**單數小時寫入**、**偶數小時整目錄移除** |

### 5. 提交回 repo

Workflow 只檢查 `data/appwrite` 是否有 porcelain 變更；有才 `git add` / `commit` / `pull --rebase` / `push`。

### 6. 專案暫停

若 Appwrite 回 `403` 且 `type == project_paused`，腳本印出 `::warning::` 並以 **exit 0** 結束（不讓 Actions 變紅），需到 Console 恢復專案後才會再備份。

---

## 產出路徑

| 路徑 | 用途 |
|------|------|
| `data/appwrite/latest.json` | 最新完整匯出 |
| `data/appwrite/history/snapshot-*.json` | 有資料變更時的時間戳快照（受保留數量限制） |
| `data/appwrite/landtophistory/` | **奇偶小時交替**：見下節 |

### landtophistory 奇偶小時規則（UTC）

以匯出當下的 **UTC `hour`** 判斷（GitHub Actions cron 也是 UTC）：

| UTC 小時 | 行為 |
|----------|------|
| **單數** `1, 3, 5, …, 23` | **寫入** `data/appwrite/landtophistory/latest.json` 與 `snapshot-YYYYMMDDTHHMMSSZ.json` |
| **偶數** `0, 2, 4, …, 22` | **移除** 整個 `data/appwrite/landtophistory/` 目錄 |

此邏輯**每次成功匯出都會執行**，不依賴 DB 內容是否變更。同一小時內的 :33 / :37 兩次排程會做相同動作（同寫或同刪）。

---

## 必要 GitHub Secrets

在 repo → Settings → Secrets and variables → Actions 設定：

| Secret | 內容 |
|--------|------|
| `APPWRITE_ENDPOINT` | 例如 `https://xxx.cloud.appwrite.io/v1` |
| `APPWRITE_PROJECT_ID` | Project ID |
| `APPWRITE_DATABASE_ID` | 要匯出的 Database ID |
| `APPWRITE_API_KEY` | 可讀取該 DB / collections 的 Server API Key |

---

## 可選環境變數（腳本）

| 變數 | 預設 | 說明 |
|------|------|------|
| `APPWRITE_SKIP_IF_UNCHANGED` | `1` | 內容未變則不寫檔 |
| `APPWRITE_HISTORY_RETENTION` | `168` | 保留最近 N 份 history；`0` = 不刪 |
| `APPWRITE_PAGE_SIZE` | `100` | API 分頁大小 |
| `APPWRITE_HTTP_TIMEOUT` | `60` | 單次 HTTP 逾時（秒） |
| `APPWRITE_EXPORT_DEBUG` | `1` | `0` 關閉 `[debug]` 日誌 |

---

## 本地執行（PowerShell）

```powershell
$env:APPWRITE_ENDPOINT="https://tor.cloud.appwrite.io/v1"
$env:APPWRITE_PROJECT_ID="your-project-id"
$env:APPWRITE_DATABASE_ID="your-database-id"
$env:APPWRITE_API_KEY="your-api-key"
# 可選：本機想強制寫出時
# $env:APPWRITE_SKIP_IF_UNCHANGED="0"
python scripts/fetch_appwrite_backup.py
```

---

## 外部 Cron（提高排程可靠度）

GitHub 內建 schedule 偶發延遲或漏跑，可再加外部服務每小時 `POST`：

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/OWNER/REPO/dispatches \
  -d '{"event_type":"external-hourly-sync"}'
```

Token 只放在外部排程服務，不要寫進本 repo。與 GitHub 內建 cron 重疊時，concurrency 會串行、避免互相踩踏。

---

## 重構重點（相對舊版）

1. **`AppwriteClient`**：端點 / 專案 / Key 集中，API 呼叫不再到處傳參
2. **統一 `paginate()`**：collections / documents 共用 offset 分頁
3. **修正敏感欄位誤判**：不再因 key 名含 `"key"` 就把 attribute 欄位名全部 redact
4. **指紋去重**：DB 沒變就不寫 history、不產生空 commit 噪音
5. **History 自動 prune**：避免 `history/` 無限長大（舊 repo 若已有上千檔，下次「有變更」的成功匯出會裁到保留上限）
