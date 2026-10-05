"""PDF 浮水印工具網頁版：以標準庫 http.server 提供上傳、加浮水印與下載。

設計重點：
- 只綁 127.0.0.1，僅供本機瀏覽器使用，不對外開放。
- 每個上傳工作使用獨立的暫存目錄（tempfile.mkdtemp），完成後以 uuid4 hex 當下載 id；
  伺服器結束時一併清除所有暫存目錄。
- 回應給前端的錯誤訊息不含暫存目錄的絕對路徑，避免洩漏本機目錄結構。
- access log 只記 method、路徑（不含 query）與狀態碼，因為 query 內有使用者輸入的檔名與文字。

用法：
    uv run web_server.py
    uv run add_watermark.py        （不帶任何引數時同樣啟動網頁版）
"""
import json
import logging
import os
import re
import shutil
import string
import sys
import tempfile
import threading
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from add_watermark import WatermarkError, WatermarkResult, add_watermark

HOST = "127.0.0.1"
PORT = 5050
FRONT_END_DIR = Path(__file__).resolve().parent / "front_end"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_TEXT_LENGTH = 100
MAX_OUTPUT_NAME_LENGTH = 150
DEFAULT_OUTPUT_NAME = "output_wm.pdf"
DEFAULT_INPUT_NAME = "input.pdf"
# 使用者把輸出檔名取成 input.pdf 時改用此名稱存上傳檔，避免 add_watermark 拒絕「輸出與輸入相同」
FALLBACK_INPUT_NAME = "_upload_input.pdf"
JOB_DIR_PREFIX = "wm_"
PDF_MAGIC = b"%PDF-"
WATERMARK_API_PATH = "/api/watermark"
DOWNLOAD_PREFIX = "/download/"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
# Windows 檔名禁用字元與控制字元（含 DEL）
INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# Windows 保留裝置名稱：即使加了副檔名（例如 CON.pdf）也無法建立檔案
RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL",
                  *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
UNEXPECTED_ERROR_MESSAGE = "伺服器處理時發生未預期錯誤"

# job id → (輸出檔路徑, 下載檔名)；ThreadingHTTPServer 每個請求一條執行緒，需加鎖
JOBS: dict[str, tuple[Path, str]] = {}
# 所有仍存在的暫存目錄（含處理中尚未登記到 JOBS 的），供伺服器結束時清除
JOB_DIRS: set[Path] = set()
JOBS_LOCK = threading.Lock()


class RequestError(Exception):
    """請求不合法時拋出，攜帶要回給前端的 HTTP 狀態碼與繁中訊息。"""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def sanitize_output_name(raw: str, default: str = DEFAULT_OUTPUT_NAME) -> str:
    """清洗使用者輸入的輸出檔名，確保只是一個安全的 .pdf 檔名（不含任何目錄）。"""
    base_name = re.split(r"[\\/]", raw)[-1]
    cleaned = INVALID_NAME_CHARS.sub("", base_name).strip(string.whitespace + ".")
    if not cleaned:
        return default
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    if len(cleaned) > MAX_OUTPUT_NAME_LENGTH:
        stem = cleaned[:MAX_OUTPUT_NAME_LENGTH - len(".pdf")].rstrip(string.whitespace + ".")
        cleaned = f"{stem}.pdf" if stem else default
    if Path(cleaned).stem.upper() in RESERVED_STEMS:
        cleaned = f"_{cleaned}"
    return cleaned


def validate_text(raw: str) -> str:
    """檢查浮水印文字；不合格時拋 RequestError(400)。"""
    text = raw.strip()
    if not text:
        raise RequestError(HTTPStatus.BAD_REQUEST, "浮水印文字不可為空")
    try:
        text.encode("latin-1")
    except UnicodeEncodeError as exc:
        # Helvetica-Bold 是 reportlab 內建標準字型，只涵蓋 Latin-1，中文會變成方塊或亂碼
        raise RequestError(HTTPStatus.BAD_REQUEST,
                           "浮水印文字目前只支援英文、數字與符號（字型不含中文）") from exc
    if len(text) > MAX_TEXT_LENGTH:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"浮水印文字不可超過 {MAX_TEXT_LENGTH} 個字元")
    return text


def parse_content_length(raw_length: str | None) -> int:
    """解析 Content-Length 並檢查上限；不合格時拋 RequestError（尚未讀取 body）。"""
    if raw_length is None:
        raise RequestError(HTTPStatus.LENGTH_REQUIRED, "缺少 Content-Length 標頭")
    if not raw_length.strip().isdigit():
        raise RequestError(HTTPStatus.BAD_REQUEST, "Content-Length 標頭格式錯誤")
    length = int(raw_length.strip())
    if length > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"檔案超過 {limit_mb}MB 上限")
    if length == 0:
        raise RequestError(HTTPStatus.BAD_REQUEST, "請先選取 PDF 檔")
    return length


def get_query_value(query: dict[str, list[str]], key: str) -> str:
    """取出 query 參數的第一個值；不存在時回傳空字串。"""
    values = query.get(key)
    return values[0] if values else ""


def pick_input_name(output_name: str) -> str:
    """決定上傳檔在 job 目錄內的檔名，避開與輸出檔同名（Windows 檔名不分大小寫）。"""
    if output_name.lower() == DEFAULT_INPUT_NAME:
        return FALLBACK_INPUT_NAME
    return DEFAULT_INPUT_NAME


def hide_job_path(message: str, job_dir: Path) -> str:
    """把錯誤訊息中的暫存目錄絕對路徑移除，只留下檔名。"""
    job_dir_text = str(job_dir)
    return message.replace(job_dir_text + os.sep, "").replace(job_dir_text, "")


def register_job_dir(job_dir: Path) -> None:
    """登記暫存目錄，供伺服器結束時清除。"""
    with JOBS_LOCK:
        JOB_DIRS.add(job_dir)


def discard_job_dir(job_dir: Path) -> None:
    """刪除單一工作的暫存目錄並取消登記；刪不掉（例如被防毒鎖住）不影響回應。"""
    shutil.rmtree(job_dir, ignore_errors=True)
    with JOBS_LOCK:
        JOB_DIRS.discard(job_dir)


def cleanup_all_jobs() -> None:
    """伺服器結束時清除所有暫存目錄與下載紀錄。"""
    with JOBS_LOCK:
        job_dirs = list(JOB_DIRS)
        JOB_DIRS.clear()
        JOBS.clear()
    for job_dir in job_dirs:
        shutil.rmtree(job_dir, ignore_errors=True)


def run_watermark_job(pdf_bytes: bytes, output_name: str, text: str) -> tuple[str, WatermarkResult]:
    """在獨立暫存目錄內加浮水印，成功回傳 (job id, 結果)；已知錯誤轉成 RequestError(400)。"""
    job_dir = Path(tempfile.mkdtemp(prefix=JOB_DIR_PREFIX))
    register_job_dir(job_dir)
    try:
        input_path = job_dir / pick_input_name(output_name)
        input_path.write_bytes(pdf_bytes)
        result = add_watermark(input_path, job_dir / output_name, text)
    except (FileNotFoundError, ValueError, WatermarkError) as exc:
        discard_job_dir(job_dir)
        raise RequestError(HTTPStatus.BAD_REQUEST, hide_job_path(str(exc), job_dir)) from exc
    except BaseException:
        discard_job_dir(job_dir)
        raise
    # 上傳的原檔已不再需要，刪掉以節省暫存空間；刪不掉也不影響下載
    try:
        input_path.unlink(missing_ok=True)
    except OSError:
        pass
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = (result.output_path, output_name)
    return job_id, result


def build_content_disposition(filename: str) -> str:
    """產生同時相容舊瀏覽器（ASCII fallback）與 RFC 5987（UTF-8 檔名）的 Content-Disposition。"""
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", filename).replace('"', "_").replace("\\", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def resolve_static_path(url_path: str) -> Path | None:
    """把 URL 路徑對應到 front_end 內的檔案；越界、不存在或非檔案時回傳 None。"""
    relative = unquote(url_path, encoding="utf-8").lstrip("/") or "index.html"
    try:
        candidate = (FRONT_END_DIR / relative).resolve()
    except (OSError, ValueError):
        # 含 NUL 字元或 Windows 不合法路徑時 resolve 會失敗，一律視為不存在
        return None
    if not candidate.is_relative_to(FRONT_END_DIR) or not candidate.is_file():
        return None
    return candidate


def guess_content_type(file_path: Path) -> str:
    """依副檔名決定 Content-Type；常用前端檔案明確指定 charset。"""
    suffix = file_path.suffix.lower()
    if suffix in CONTENT_TYPES:
        return CONTENT_TYPES[suffix]
    guessed, _ = guess_type(file_path.name)
    return guessed or "application/octet-stream"


class WatermarkRequestHandler(BaseHTTPRequestHandler):
    """處理靜態檔、浮水印 API 與下載的 HTTP handler。"""

    server_version = "WatermarkWeb/1.0"

    def do_GET(self) -> None:
        """GET：下載路徑交給 handle_download，其餘當作 front_end 靜態檔。"""
        url_path = urlsplit(self.path).path
        if url_path.startswith(DOWNLOAD_PREFIX):
            self.handle_download(url_path[len(DOWNLOAD_PREFIX):])
            return
        self.serve_static(url_path)

    def do_POST(self) -> None:
        """POST：只接受浮水印 API。"""
        if urlsplit(self.path).path != WATERMARK_API_PATH:
            self.close_connection = True  # 未讀取 body，不能沿用此連線
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "找不到此路徑"})
            return
        self.handle_watermark()

    def reject_method(self) -> None:
        """不支援的 HTTP method 一律回 405。"""
        self.close_connection = True
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "不支援此請求方法"},
                       extra_headers={"Allow": "GET, POST"})

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = reject_method

    def handle_watermark(self) -> None:
        """處理上傳：驗證 → 加浮水印 → 回傳下載資訊；錯誤一律回 JSON。"""
        try:
            payload = self.process_upload()
        except RequestError as exc:
            # body 可能未讀或只讀一部分，關閉連線避免殘留資料被當成下一個請求
            self.close_connection = True
            self.send_json(exc.status, {"error": exc.message})
        except Exception:  # noqa: BLE001 — 兜底回 500，避免未預期例外讓前端拿不到回應
            traceback.print_exc(file=sys.stderr)
            self.close_connection = True
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": UNEXPECTED_ERROR_MESSAGE})
        else:
            self.send_json(HTTPStatus.OK, payload)

    def process_upload(self) -> dict[str, object]:
        """讀取並驗證請求，執行浮水印工作，回傳成功時的 JSON 內容。"""
        # 先處理 body（含大小上限），再驗證 query，順序與規格一致
        pdf_bytes = self.read_pdf_body()
        query = parse_qs(urlsplit(self.path).query, encoding="utf-8")
        text = validate_text(get_query_value(query, "text"))
        output_name = sanitize_output_name(get_query_value(query, "output"))
        job_id, result = run_watermark_job(pdf_bytes, output_name, text)
        return {"download_url": f"{DOWNLOAD_PREFIX}{job_id}", "filename": output_name,
                "was_encrypted": result.was_encrypted}

    def read_pdf_body(self) -> bytes:
        """依 Content-Length 讀取 body 並檢查是否為 PDF；超過上限時不讀取 body。"""
        length = parse_content_length(self.headers.get("Content-Length"))
        pdf_bytes = self.rfile.read(length)
        if len(pdf_bytes) < length:
            raise RequestError(HTTPStatus.BAD_REQUEST, "上傳資料不完整，請重新上傳")
        if not pdf_bytes.startswith(PDF_MAGIC):
            raise RequestError(HTTPStatus.BAD_REQUEST, "上傳的檔案不是 PDF")
        return pdf_bytes

    def handle_download(self, job_id: str) -> None:
        """回傳加好浮水印的 PDF；id 格式錯誤、查不到或檔案已不存在時回 404。"""
        entry = None
        if JOB_ID_PATTERN.fullmatch(job_id):
            with JOBS_LOCK:
                entry = JOBS.get(job_id)
        try:
            if entry is None:
                raise FileNotFoundError(job_id)
            output_path, filename = entry
            pdf_bytes = output_path.read_bytes()
        except OSError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "找不到下載檔案，請重新產生"})
            return
        self.send_bytes(pdf_bytes, "application/pdf",
                        {"Content-Disposition": build_content_disposition(filename)})

    def serve_static(self, url_path: str) -> None:
        """回傳 front_end 內的靜態檔；不存在或路徑越界時回 404。"""
        file_path = resolve_static_path(url_path)
        if file_path is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "找不到此路徑"})
            return
        try:
            content = file_path.read_bytes()
        except OSError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "找不到此路徑"})
            return
        self.send_bytes(content, guess_content_type(file_path), {"Cache-Control": "no-cache"})

    def send_json(self, status: HTTPStatus, payload: dict[str, object],
                  extra_headers: dict[str, str] | None = None) -> None:
        """送出 UTF-8 JSON 回應（ensure_ascii=False 讓中文直接可讀）。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Cache-Control": "no-store", **(extra_headers or {})}
        self.send_bytes(body, JSON_CONTENT_TYPE, headers, status)

    def send_bytes(self, body: bytes, content_type: str, extra_headers: dict[str, str],
                   status: HTTPStatus = HTTPStatus.OK) -> None:
        """送出完整回應：狀態列、Content-Type、Content-Length、額外標頭與 body。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers.items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """簡短 access log：只記 method、路徑與狀態碼，不記 query（內含使用者輸入）。"""
        command = getattr(self, "command", None) or "-"
        url_path = urlsplit(getattr(self, "path", "") or "").path or "-"
        status = code.value if isinstance(code, HTTPStatus) else code
        self.log_message('"%s %s" %s', command, url_path, status)


class WatermarkHTTPServer(ThreadingHTTPServer):
    """關閉 Windows 上的 SO_REUSEADDR：Windows 的語意會讓第二個程式也能綁同一個 port，
    導致「port 已被佔用」偵測失效、請求被隨機分給兩個程式。"""

    allow_reuse_address = os.name != "nt"


def run_server() -> int:
    """啟動網頁伺服器直到 Ctrl+C；port 被佔用時回傳 1，正常結束回傳 0。"""
    # pypdf 的英文 logging 警告會淹沒繁中訊息，只保留 ERROR 以上
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        server = WatermarkHTTPServer((HOST, PORT), WatermarkRequestHandler)
    except OSError as exc:
        print(f"錯誤：無法在 {HOST}:{PORT} 啟動網頁伺服器（連接埠可能已被佔用）：{exc}",
              file=sys.stderr)
        return 1
    print(f"網頁版已啟動：http://{HOST}:{PORT}/（按 Ctrl+C 結束）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("收到 Ctrl+C，正在關閉網頁伺服器…", flush=True)
    finally:
        server.server_close()
        cleanup_all_jobs()
    return 0


if __name__ == "__main__":
    sys.exit(run_server())
