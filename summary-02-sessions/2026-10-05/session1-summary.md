# Session 1 Summary — PDF 浮水印網頁版

- 日期：2026-10-05（DESKTOP-6LST1BR）
- 專案：`k120-ch02-practice-web`（git worktree，分支 `web`，remote `chenghyang2001/k120-ch02-practice`）

## 完成事項

### 網頁版實作（依書上 web_prd.md）

- 建立 `web_prd.md`（需求：無引數啟動 5050 網頁伺服器、前端放 `front_end/`、高科技底圖、三欄位 + 開始按鈕 + 下載連結）。
- 走 plan 模式：使用者核准計畫後才實作；決策＝中等複雜度、QA 3 case、不派 code-reviewer、後端用標準庫 `http.server`（零新依賴）。
- `add_watermark.py` 只改 `main()`：`argv` 為空時延遲 import `web_server.run_server()`，有引數走原 CLI（+10/-1 行）。
- 新增 `web_server.py`（372 行，code-writer 產出）：`127.0.0.1:5050`、`POST /api/watermark?output=&text=`（body 為 PDF 原始 bytes，上限 50MB）、`GET /download/<32hex>`、靜態檔路徑穿越防護、Windows 關閉 SO_REUSEADDR 才能偵測 port 佔用。
- 前端 `front_end/`：`index.html`、`index.css`、`index.js`（初版為 style.css / app.js，後來更名）、自製 `bg.svg`（電路走線 + 網格 + 光點）。
- QA（code-qa）3 case 全過：Happy（5 頁每頁 Confidential 0→1、`filename*=UTF-8''` 中文檔名）、Edge/Error（非 PDF→400、空文字→400、`/../`、`%2e%2e` 等 4 種路徑穿越全 404）、Integration（CLI 模式 exit 0）。唯一 FAIL 是 3 個多餘 `# noqa`（RUF100），已修。
- 瀏覽器（claude-in-chrome）實測：上傳 `sample_rotated.pdf` → 自動填 `sample_rotated_wm.pdf` → 下載連結；7 頁全 OK（每頁 +1 浮水印、rect/rotation 不變），pymupdf 轉 PNG 目視正確。

### 前端檔案規範兩種寫法（書上練習）

- 第一種：`front_end/CLAUDE.md`；同時把 `style.css→index.css`、`app.js→index.js`（`git mv`）並更新引用。
- 第二種：刪除 `front_end/CLAUDE.md`，改建 `.claude/rules/web.md`（frontmatter `paths`，僅處理符合路徑的檔案時才載入）。原文 paths 重複寫兩次 `**/*.html`，後續 commit `b67d78a` 改為 html/css/js 三種並補 `index.html` 命名。

### 其他設定

- 狀態列：只寫進專案 `.claude/settings.local.json`（已在 .gitignore），顯示 `資料夾 | 分支 | ctx NN%`，依賴 jq-1.8.2，dry-run 輸出 `k120-ch02-practice-web | web | ctx 18%`。
- `/loop 1m` 回報時間 + git status（CronCreate job `15783807`），跑 3 次後依使用者指示 CronDelete 停止。
- `/schedule` 列出雲端 routines：第一頁 20 個全是已執行完的 run_once（已停用），API 有下一頁但工具無法翻頁。

## 關鍵決定

- 先 plan 模式、使用者核准計畫才實作（書上流程）。
- 後端用標準庫 `http.server`：不增加依賴，練習專案夠用。
- 複雜度評中等、QA 3 case、不派 code-reviewer（使用者選擇，換取速度）。
- 前端規範由 `front_end/CLAUDE.md` 改為 `.claude/rules/web.md`：只在處理網頁檔時載入，且不會被網頁伺服器當靜態檔送出。
- 狀態列只寫專案 `.claude/settings.local.json`：不影響全域與其他專案。

## 關鍵技術筆記

- Python 3.13+ 已移除 `cgi`，標準庫做上傳時改用「body 直接送檔案 bytes + query string 帶參數」，免 multipart 解析。
- 浮水印字型 Helvetica-Bold 無中文字形 → 後端對非 Latin-1 文字回 400（前端提示「只支援英文、數字與符號」）。
- `uvx ruff check --fix --select RUF100` 會把「此次未啟用規則」的 noqa 也當多餘刪掉（本次誤刪 BLE001 的 noqa，已手動補回）→ 不要用 `--select` 搭配 RUF100 自動修。
- 瀏覽器自動化時「上傳檔案」與「點按鈕」不可放同一批平行送出，否則點擊時按鈕仍 disabled。
- 背景跑的 `uv run add_watermark.py` 曾無錯誤訊息就以 exit 127 結束（log 最後都是正常 200），原因未查明，已重新啟動。
- `.claude/rules/*.md` 的 `paths` 只在處理符合 glob 的檔案時載入；單獨改 css/js 需把對應 glob 加進去。

## 產出檔案

| 檔案 | 狀態 | 說明 |
| --- | --- | --- |
| `web_prd.md` | 新增 | 網頁版需求 |
| `web_server.py` | 新增 | 標準庫 HTTP 伺服器 |
| `add_watermark.py` | 修改 | `main()` 無引數啟動網頁版 |
| `front_end/index.html` / `index.css` / `index.js` / `bg.svg` | 新增 | 前端 |
| `.claude/rules/web.md` | 新增 | 網頁檔案規範（路徑規則） |
| `CLAUDE.md` | 修改 | 補網頁版架構與指令 |
| `.claude/settings.local.json` | 新增（不入版控） | 狀態列 |

Commits：`f401b39` 新增網頁版、`c2103fd` CSS/JS 更名 + front_end/CLAUDE.md、`014a2c0` 改用 rules、`b67d78a` 修 paths 重複。

## HANDOFF（下次 session 優先處理）

### 立即行動

- [ ] 決定 `web` 分支是否發 PR / merge 回 `main`（目前只在 `origin/web`）。
- [ ] 若要支援中文浮水印：註冊 CJK TTF 字型（reportlab `TTFont`）並移除後端 Latin-1 限制，再跑「每頁剛好多 1 個浮水印」驗證。
- [ ] 視需要讓靜態檔路由只回傳 .html/.css/.js/.svg 白名單。

### 進行中（需接續）

- 網頁伺服器以背景程序在 127.0.0.1:5050 執行中（session 結束後會消失），重新啟動：`PYTHONUTF8=1 uv run add_watermark.py`。

### 注意事項

- 背景執行的網頁伺服器曾無錯誤訊息就以 exit 127 結束，原因未查明；再發生時要保留完整 stderr 追查。

- 本 worktree 的 memory 目錄與主 repo（`k120-ch02-practice`）不同 key，跨 worktree 不共用。
- 修改 `add_watermark.py` 後仍須遵守 CLAUDE.md 的浮水印不變量與 pypdf 6.19 私有 API 驗證。
- `.py/.js` 改動 > 3 行要走 code-writer → code-qa 鐵律；`.html/.css/.svg/.md` 可直接改。
