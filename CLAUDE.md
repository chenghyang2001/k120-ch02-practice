# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概述

PDF 浮水印練習專案：`add_watermark.py` 在 PDF 每一頁疊上 "Confidential" 對角線半透明浮水印，輸出為 `<原檔名>_wm.pdf`（同資料夾）。`make_sample_pdf.py` 產生測試用 PDF。

## 使用 Python 環境管理

使用 uv 管理 Python 執行環境（Python 3.14，依賴：pypdf 6.19、reportlab 5.0.1）：

- 必要時使用 `uv init` 初始化資料夾
- 使用 `uv add` 加入套件，不要使用 pip
- 使用 `uv run` 執行 Python 腳本檔
- 臨時驗證用的套件（如 pymupdf 轉 PNG）用 `uv run --with <pkg>`，不要寫進 pyproject

## 常用指令

```bash
PYTHONUTF8=1 uv run make_sample_pdf.py              # 產生 sample.pdf（5 種尺寸）與 sample_rotated.pdf（7 頁邊界情況）
PYTHONUTF8=1 uv run add_watermark.py sample.pdf     # → sample_wm.pdf；可一次傳多個檔
PYTHONUTF8=1 uv run add_watermark.py                # 無引數 → 啟動網頁版 http://127.0.0.1:5050/
uvx ruff check                                      # lint
```

專案內沒有正式測試套件。驗證方式是重跑 `make_sample_pdf.py` 後加浮水印，再檢查下列不變量。

## 架構與必須維持的不變量

`add_watermark()` 的處理流程：`open_reader`（含空密碼解密）→ `PdfWriter(clone_from=reader)` → `split_shared_contents` → 逐頁 `stamp_page` → `compact_output` → `write_pdf_atomically`。

以下每一條都是踩過坑後的設計，修改時不可破壞：

- **用 `clone_from`，不要用 `PdfWriter()` + `add_page`**：後者會遺失書籤、metadata、頁碼標籤、named destinations、AcroForm。
- **不要呼叫 `transfer_rotation_to_content()`**：它會改寫頁面座標與 box，但不會改 `/Annots` 的 `/Rect`，導致連結錯位。原頁一律不動，旋轉改在 overlay 內處理：`rotate(+rotation)` 再轉對角線角度（推導見 `render_overlay_pdf` 註解）。
- **依 cropbox 置中，不是 mediabox**；box 要先經 `normalize_box` 正規化（PDF 允許反向座標如 `[612 792 0 0]`）。
- **overlay 以 (0,0) 為原點繪製**，再用 `merge_transformed_page` + `translate(cropbox.left, cropbox.bottom)` 合併。pypdf merge 會用 overlay 的 cropbox 裁切內容，直接在 overlay 上平移會被裁掉。
- **先拆共用 `/Contents`，再蓋章，兩個階段不可交錯**：merge 會就地改寫共用的 content 物件，若邊拆邊蓋，後面的頁會複製到已帶浮水印的內容而疊兩層。
- **`compact_output` 必須在所有 merge 之後**，且 `compress_content_streams` 與 `compress_identical_objects(remove_unreferenced=True)` 兩步都要做（只壓縮不清孤兒物件，檔案反而變大）。
- overlay 依 `(寬, 高, rotation, text)` 快取。
- `ensure_unique_contents` 使用 pypdf 私有 API `writer._add_object`；`compact_output` 依賴 `compress_identical_objects` 單次掃描的行為。兩者都只在 pypdf 6.19 驗證過，升級 pypdf 後要重新驗證「每頁剛好一個浮水印」。

## 網頁版（需求見 `web_prd.md`）

- `add_watermark.py` 的 `main()` 在沒有命令行引數時延遲 import `web_server.run_server()`；有引數時走原 CLI。
- `web_server.py`：標準庫 `ThreadingHTTPServer`，只綁 `127.0.0.1:5050`，零額外依賴。
  - `POST /api/watermark?output=&text=`：body 是 PDF 原始 bytes（不用 multipart，因 Python 3.13 起已移除 `cgi`）；上限 50MB。
  - 處理結果放在 `tempfile.mkdtemp()` 的 job 目錄，`GET /download/<job id>` 下載；伺服器結束時清除。
  - 浮水印字型是 Helvetica-Bold，不含中文字形，非 Latin-1 文字一律回 400。
- 前端在 `front_end/`（index.html / index.css / index.js / bg.svg 自製底圖，檔案規範見 `front_end/CLAUDE.md`），使用者資料一律用 `textContent` 插入。

## 錯誤處理慣例

- pypdf 的例外（`PyPdfError`、`NotImplementedError`）統一包成 `WatermarkError`；`DependencyError`（缺 cryptography 解 AES）一律經 `cryptography_error()` 包裝，因為它可能在開檔、解密或 clone 三個階段才拋出。
- CLI 逐檔處理，最外層有兜底的 `except Exception`，用來隔離批次中的單一檔案失敗；任何檔案失敗時 exit code 為 1。
- 輸入檔名的 stem 已以 `_wm` 結尾時會略過（不算失敗）。
- 「原檔加密限制不會保留」的警告在寫檔成功後才印（依 `WatermarkResult.was_encrypted`）。
- 錯誤訊息一律繁體中文；pypdf logger 在 `main()` 被調到 ERROR 等級。

## 驗證不變量

修改 `add_watermark.py` 後至少確認：

- 每頁「Confidential」數量剛好比原檔多 1。
- mediabox、cropbox、/Rotate、Link `/Rect`、metadata、書籤都不變。
- 用 pymupdf 轉 PNG 目視：浮水印置中、沿畫面對角線、文字正向、未被裁切。

`sample_rotated.pdf` 涵蓋的邊界情況：/Rotate 90/180/270、非 0 原點、cropbox 小於 mediabox、cropbox + 原點偏移 + 旋轉的組合、100x1000 極窄頁。
