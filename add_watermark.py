"""PDF 浮水印工具：為 PDF 每一頁疊上沿「畫面對角線」排列的半透明 "Confidential" 浮水印。

設計重點：
- 以 PdfWriter(clone_from=reader) 複製整份文件，書籤、metadata、頁碼標籤、
  named destinations、AcroForm 等文件層級資料都會保留。
- 完全不改動原頁面的座標系統（不呼叫 transfer_rotation_to_content），
  因此 /Annots 的 /Rect、表單欄位位置都不會跑掉。
- 頁面 /Rotate 在 overlay 內處理：依使用者「看到的畫面」置中並沿畫面對角線排列。
- 位置、尺寸、字級一律以 cropbox（可見範圍）計算；cropbox 未設定時 pypdf 會
  fallback 到 mediabox。
- 相同 (cropbox 寬, 高, rotation, text) 的 overlay 只產生一次並快取。
- 多頁共用同一個 /Contents stream 時，在任何一頁蓋章之前先替後出現的頁複製獨立 stream，
  避免浮水印互相洩漏（merge 會就地改寫共用物件，故拆分與蓋章分成兩個階段）。
- cropbox 以「任意兩個對角點」表示（例如 [612 792 0 0]）時先正規化成正的寬高。

用法：
    uv run add_watermark.py <input.pdf> [更多 pdf...]
"""
import argparse
import contextlib
import logging
import math
import os
import sys
from io import BytesIO
from pathlib import Path
from typing import NamedTuple

from pypdf import PageObject, PasswordType, PdfReader, PdfWriter, Transformation
from pypdf.errors import DependencyError, PyPdfError
from pypdf.generic import ArrayObject, IndirectObject, NameObject, PdfObject
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

WATERMARK_TEXT = "Confidential"
FONT_NAME = "Helvetica-Bold"
# 文字寬度佔畫面對角線的比例；0.6 讓一般頁面的字不會碰到邊緣
TEXT_TO_DIAGONAL_RATIO = 0.6
# 字級上限 = 短邊 × 此比例。100x1000 這類極窄頁的對角線幾乎垂直，
# 字的「高度」會橫跨短邊；實算 0.9 時字的兩端仍會超出 100pt 寬而被切，
# 0.5 時水平半寬約 35pt < 50pt，才能完整落在可見範圍內
MAX_FONT_TO_SHORT_SIDE = 0.5
FILL_ALPHA = 0.3
FILL_GRAY = 0.5
OUTPUT_SUFFIX = "_wm"
TEMP_SUFFIX = ".tmp"

# 快取鍵：(cropbox 寬, cropbox 高, rotation, text)，寬高四捨五入到 2 位避免浮點誤差
OverlayKey = tuple[float, float, int, str]
# 正規化後的 box：(left, bottom, width, height)，width/height 恆為非負且已 round
BoxGeometry = tuple[float, float, float, float]
# 寬高 round 的小數位數；尺寸判斷與快取 key 必須用同一個值，否則 0.004 這類寬度
# 會通過「> 0」判斷、快取 key 卻變成 0，導致產生零尺寸 overlay
SIZE_DECIMALS = 2
CRYPTOGRAPHY_HINT = "需要 cryptography 套件才能解密 AES 加密的 PDF，請執行 uv add cryptography"


class WatermarkError(Exception):
    """PDF 無法加浮水印時拋出（加密、損毀等），訊息已整理成人看得懂的內容。"""


class OutputWriteError(WatermarkError):
    """輸出檔寫入失敗（常見原因：輸出檔正被 PDF 閱讀器開啟而被鎖住）。"""


class WatermarkResult(NamedTuple):
    """add_watermark 的結果：輸出路徑，以及原檔是否加密（供呼叫端在寫檔成功後提醒權限不保留）。"""

    output_path: Path
    was_encrypted: bool


def cryptography_error(exc: DependencyError, input_path: Path) -> WatermarkError:
    """把缺少 cryptography 的 DependencyError 包成帶安裝提示的 WatermarkError。

    DependencyError 直接繼承 Exception 而非 PyPdfError，各處必須單獨捕捉；
    AES 解密可能發生在 decrypt、解析頁面樹或 clone 複製物件時，三處共用此訊息。
    """
    return WatermarkError(f"{CRYPTOGRAPHY_HINT}：{input_path}（{exc}）")


def build_output_path(input_path: Path) -> Path:
    """依輸入檔名產生輸出路徑：同資料夾、檔名加 "_wm"，例如 report.pdf → report_wm.pdf。"""
    return input_path.with_name(f"{input_path.stem}{OUTPUT_SUFFIX}{input_path.suffix}")


def validate_input_path(input_path: Path) -> None:
    """檢查輸入檔存在且為 .pdf；不合格時拋出 FileNotFoundError 或 ValueError。"""
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到檔案：{input_path}")
    if input_path.suffix.lower() != ".pdf":
        raise ValueError(f"不是 PDF 檔（副檔名須為 .pdf）：{input_path}")


def resolve_target_path(input_path: Path, output_path: Path | None) -> Path:
    """決定輸出路徑，並拒絕與輸入相同的路徑以免覆蓋原檔。"""
    target_path = Path(output_path) if output_path is not None else build_output_path(input_path)
    if target_path.resolve() == input_path.resolve():
        raise ValueError(f"輸出路徑不可與輸入相同，避免覆蓋原檔：{target_path}")
    return target_path


def normalize_rotation(raw_rotation: int) -> int:
    """把 /Rotate 正規化成 0/90/180/270。

    規格要求 /Rotate 是 90 的倍數；遇到不合規的值（例如 45）各閱讀器行為不一，
    這裡取最接近的 90 倍數，讓浮水印至少與多數閱讀器的顯示一致，而不是整份失敗。
    注意：採用 Python 內建 round 的 banker's rounding（四捨六入五成雙），剛好落在
    兩個 90 倍數正中間時取偶數倍，例如 45 → 0、135 → 180、225 → 180。
    """
    return (round(raw_rotation / 90) * 90) % 360


def get_visual_size(width: float, height: float, rotation: int) -> tuple[float, float]:
    """回傳使用者在畫面上看到的寬高：rotation 為 90/270 時寬高對調。"""
    if rotation in (90, 270):
        return height, width
    return width, height


def compute_font_size(visual_w: float, visual_h: float, text: str) -> float:
    """依畫面對角線決定字級，並以短邊設上限，避免極端窄長頁的字被切掉。"""
    unit_width = stringWidth(text, FONT_NAME, 1)
    if unit_width <= 0:
        raise ValueError("浮水印文字不可為空")
    by_diagonal = math.hypot(visual_w, visual_h) * TEXT_TO_DIAGONAL_RATIO / unit_width
    by_short_side = min(visual_w, visual_h) * MAX_FONT_TO_SHORT_SIDE
    return min(by_diagonal, by_short_side)


def render_overlay_pdf(width: float, height: float, rotation: int, text: str) -> bytes:
    """以 reportlab 產生單頁浮水印 PDF（原點 (0,0)、pagesize = cropbox 寬高），回傳 bytes。

    pypdf 合併時會用 overlay 自己的 cropbox (0,0,w,h) 裁切 overlay 內容，所以 overlay
    必須以 (0,0) 為原點繪製，再於合併時平移到原頁 cropbox 的左下角；若直接把座標畫在
    原頁的非 0 原點上，超出 (0,0,w,h) 的部分會被裁掉。
    """
    visual_w, visual_h = get_visual_size(width, height, rotation)
    font_size = compute_font_size(visual_w, visual_h, text)
    diagonal_deg = math.degrees(math.atan2(visual_h, visual_w))
    buffer = BytesIO()
    pdf_canvas = canvas.Canvas(buffer, pagesize=(width, height))
    pdf_canvas.setFillGray(FILL_GRAY)
    pdf_canvas.setFillAlpha(FILL_ALPHA)
    pdf_canvas.setFont(FONT_NAME, font_size)
    # 頁面旋轉時中心點不變，所以畫面中心 = cropbox 中心
    pdf_canvas.translate(width / 2, height / 2)
    # 推導：PDF /Rotate N 是顯示時把頁面「順時針」轉 N 度；reportlab rotate(θ) 是
    # 座標系「逆時針」轉 θ 度。頁面座標中角度 α（逆時針）的線，顯示後角度為 α - N。
    # 要讓畫面角度等於對角線角度 d，需 α = N + d，故先 rotate(+N) 抵消頁面旋轉，
    # 再 rotate(d)。驗算：/Rotate 90、d=0 → α=90，字朝 +y，順時針轉 90 後朝右，水平。
    pdf_canvas.rotate(rotation)
    pdf_canvas.rotate(diagonal_deg)
    # 往下移約 1/3 字高，讓字的視覺中心（而非基線）落在畫面中心
    pdf_canvas.drawCentredString(0, -font_size * 0.35, text)
    pdf_canvas.showPage()
    pdf_canvas.save()
    return buffer.getvalue()


def normalize_box(box: ArrayObject) -> BoxGeometry:
    """把 PDF 矩形正規化成 (left, bottom, width, height)，寬高恆為非負。

    PDF 規格允許用任意兩個對角點表示矩形（例如 [612 792 0 0]），此時 pypdf 的
    width/height 會是負數；用 sorted() 取出真正的左右、上下邊界才能算出正確尺寸。
    寬高 round 到 SIZE_DECIMALS 位，讓零尺寸判斷與快取 key 一致。
    """
    left, right = sorted((float(box[0]), float(box[2])))
    bottom, top = sorted((float(box[1]), float(box[3])))
    return (left, bottom, round(right - left, SIZE_DECIMALS),
            round(top - bottom, SIZE_DECIMALS))


def get_cached_overlay(width: float, height: float, rotation: int, text: str,
                       cache: dict[OverlayKey, PageObject]) -> PageObject:
    """取得符合指定（已正規化）寬高與旋轉角度的 overlay 頁面；相同條件只產生一次。"""
    key: OverlayKey = (width, height, rotation, text)
    if key not in cache:
        overlay_bytes = render_overlay_pdf(*key)
        cache[key] = PdfReader(BytesIO(overlay_bytes)).pages[0]
    return cache[key]


def stamp_page(page: PageObject, page_number: int, text: str,
               cache: dict[OverlayKey, PageObject]) -> None:
    """為單一頁面疊上浮水印；overlay 平移到 cropbox 左下角，不改動原頁座標系統。"""
    left, bottom, width, height = normalize_box(page.cropbox)
    # round 後寬或高為 0 的頁面沒有可見範圍，且字級會是 0 而拋例外；
    # 不靜默略過，讓使用者知道該頁沒有浮水印
    if width <= 0 or height <= 0:
        print(f"警告：第 {page_number} 頁的可見範圍寬或高為 0，無法加浮水印，已略過。",
              file=sys.stderr)
        return
    rotation = normalize_rotation(page.rotation)
    overlay_page = get_cached_overlay(width, height, rotation, text, cache)
    offset = Transformation().translate(left, bottom)
    page.merge_transformed_page(overlay_page, offset, over=True)


def collect_content_ids(raw_contents: PdfObject) -> set[int]:
    """收集 /Contents 本身及其陣列元素的 IndirectObject idnum，用來偵測跨頁共用。"""
    content_ids: set[int] = set()
    resolved = raw_contents
    if isinstance(raw_contents, IndirectObject):
        content_ids.add(raw_contents.idnum)
        resolved = raw_contents.get_object()
    if isinstance(resolved, ArrayObject):
        content_ids.update(item.idnum for item in resolved if isinstance(item, IndirectObject))
    return content_ids


def ensure_unique_contents(writer: PdfWriter, page: PageObject, seen: set[int]) -> None:
    """若此頁的 /Contents（或其陣列元素）與前面頁面共用，先複製一份獨立的 stream。

    clone_from 會保留物件共用結構：重複頁、模板頁、add_page 複製出的頁可能指向同一個
    content stream。不拆開的話，merge 會改到共用 stream，每頁都疊上兩層浮水印，
    旋轉頁還會多一個方向錯誤的浮水印。只要整份 /Contents 或任一元素已出現過，
    就整份 force_duplicate 複製，避免只拆一部分仍殘留共用。

    注意：必須在「任何一頁蓋章之前」對所有頁呼叫完畢（見 split_shared_contents）。
    若與 stamp_page 在同一迴圈交錯執行，前一頁的 merge 已就地改寫共用 stream，
    這裡複製到的就會是已帶浮水印的內容。
    """
    if "/Contents" not in page:
        return
    content_ids = collect_content_ids(page.raw_get("/Contents"))
    if content_ids & seen:
        duplicated = page["/Contents"].clone(writer, force_duplicate=True)
        # _add_object 是 pypdf 私有 API：已在 6.19 驗證「已登記的物件會直接回傳原參照」，
        # 新物件則登記並回傳新參照。升級 pypdf 時須重新驗證此行為。
        page[NameObject("/Contents")] = writer._add_object(duplicated)
        content_ids = collect_content_ids(page.raw_get("/Contents"))
    seen.update(content_ids)


def split_shared_contents(writer: PdfWriter) -> None:
    """蓋章前的獨立階段：把所有頁的 /Contents 拆成各自獨立的物件。

    為什麼不能與蓋章放在同一個逐頁迴圈：pypdf 的 merge 會「就地」改寫頁面目前指向的
    content 物件（寫回同一個 idnum）。若第 1 頁先蓋章，共用 stream 就已含浮水印，
    第 2 頁再 force_duplicate 時複製到的是髒內容，再蓋一次即成兩層（旋轉頁還會多一個
    方向錯誤的浮水印）。所以必須先全部拆開，確保每次複製的都是未蓋章的原始內容。
    """
    seen_content_ids: set[int] = set()
    for page in writer.pages:
        ensure_unique_contents(writer, page, seen_content_ids)


def decrypt_with_empty_password(reader: PdfReader, input_path: Path) -> None:
    """加密 PDF 先試空密碼（常見於「只限制列印/編輯」的檔案）；失敗才拋 WatermarkError。

    「加密與權限限制不會保留」的警告不在這裡印：之後仍可能失敗，改由 process_file
    在寫檔成功後才提醒。
    """
    try:
        result = reader.decrypt("")
    except DependencyError as exc:
        raise cryptography_error(exc, input_path) from exc
    except (PyPdfError, NotImplementedError) as exc:
        raise WatermarkError(f"PDF 已加密且無法解密（可能缺少解密支援）：{input_path}（{exc}）") from exc
    if result == PasswordType.NOT_DECRYPTED:
        raise WatermarkError(f"PDF 已加密，請先解除密碼保護：{input_path}")


def open_reader(input_path: Path) -> PdfReader:
    """開啟 PDF、處理加密，並提早解析頁面樹；損毀時統一包成 WatermarkError。"""
    try:
        reader = PdfReader(input_path)
        if reader.is_encrypted:
            decrypt_with_empty_password(reader, input_path)
        _ = len(reader.pages)  # 提早觸發頁面樹解析，損毀檔才會在這裡就報錯
    except DependencyError as exc:
        # 部分加密檔要到解析頁面樹時才真正需要 AES 解密
        raise cryptography_error(exc, input_path) from exc
    except PyPdfError as exc:
        raise WatermarkError(f"PDF 損毀或無法解析：{input_path}（{exc}）") from exc
    return reader


def compact_output(writer: PdfWriter) -> None:
    """寫檔前縮小輸出：merge 會把壓縮過的 content 解壓寫回，且留下孤兒物件，檔案約膨脹 7 倍。

    兩步都要做：只壓縮 content stream 而不清掉孤兒物件，檔案反而會變大。
    去重發生在所有 merge 之後，相同內容的頁即使在這裡重新共用同一個 stream，
    也不會再被 merge 就地改寫，因此不會重新產生「共用 stream 被改寫」的問題。
    """
    for page in writer.pages:
        page.compress_content_streams()
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)


def discard_temp_file(temp_path: Path) -> None:
    """刪除寫到一半的暫存檔；刪不掉（例如被防毒鎖住）也不蓋掉原本的錯誤。"""
    with contextlib.suppress(OSError):
        temp_path.unlink(missing_ok=True)


def write_pdf_atomically(writer: PdfWriter, target_path: Path) -> None:
    """先寫同目錄暫存檔再 os.replace，避免寫到一半失敗時留下殘缺的輸出檔。"""
    temp_path = target_path.with_name(target_path.name + TEMP_SUFFIX)
    try:
        with temp_path.open("wb") as output_file:
            writer.write(output_file)
        os.replace(temp_path, target_path)
    except OSError as exc:
        discard_temp_file(temp_path)
        raise OutputWriteError(
            f"無法寫入輸出檔：{target_path}（檔案可能正被其他程式開啟）（{exc}）") from exc
    except PyPdfError as exc:
        discard_temp_file(temp_path)
        raise WatermarkError(f"產生輸出 PDF 時失敗：{target_path}（{exc}）") from exc
    except BaseException:
        discard_temp_file(temp_path)
        raise


def add_watermark(input_path: Path, output_path: Path | None = None,
                  text: str = WATERMARK_TEXT) -> WatermarkResult:
    """為 PDF 每一頁疊上浮水印並寫出新檔（保留文件層級資料）。

    Returns:
        WatermarkResult：output_path 為輸出路徑；was_encrypted 表示原檔是否加密
        （輸出檔不會保留原檔的加密與權限限制，由呼叫端決定是否提醒使用者）。

    Raises:
        FileNotFoundError: 輸入檔不存在。
        ValueError: 副檔名不是 .pdf、文字為空，或輸出路徑與輸入相同。
        OutputWriteError: 輸出檔寫入失敗。
        WatermarkError: PDF 加密、損毀，或含 pypdf 不支援的格式。
    """
    input_path = Path(input_path)
    validate_input_path(input_path)
    target_path = resolve_target_path(input_path, output_path)
    reader = open_reader(input_path)
    try:
        writer = PdfWriter(clone_from=reader)
        # 兩階段：先全部拆開共用 /Contents，再逐頁蓋章；順序不可交錯，理由見 split_shared_contents
        split_shared_contents(writer)
        overlay_cache: dict[OverlayKey, PageObject] = {}
        for page_number, page in enumerate(writer.pages, start=1):
            stamp_page(page, page_number, text, overlay_cache)
        compact_output(writer)
    except DependencyError as exc:
        # AES-128 用空密碼 decrypt 會成功，實際解密延到 clone 複製物件時才發生
        raise cryptography_error(exc, input_path) from exc
    except (PyPdfError, NotImplementedError) as exc:
        # NotImplementedError 來自 pypdf 不支援的 filter 等情況，同樣視為「PDF 無法處理」
        raise WatermarkError(
            f"處理 PDF 頁面時失敗（檔案可能損毀或含不支援的格式）：{input_path}（{exc}）") from exc
    write_pdf_atomically(writer, target_path)
    return WatermarkResult(target_path, reader.is_encrypted)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CLI 參數：一個或多個 PDF 路徑。"""
    parser = argparse.ArgumentParser(
        description=f'為 PDF 每一頁加上 "{WATERMARK_TEXT}" 浮水印，輸出為原檔名加 {OUTPUT_SUFFIX}。')
    parser.add_argument("pdf_files", nargs="+", type=Path, metavar="PDF",
                        help="要加浮水印的 PDF 檔案（可多個）")
    return parser.parse_args(argv)


def process_file(pdf_path: Path) -> bool:
    """處理單一檔案並印出結果；已知錯誤在此轉成繁中訊息，回傳是否成功。"""
    try:
        result = add_watermark(pdf_path)
    except FileNotFoundError as exc:
        print(f"錯誤（檔案不存在）：{exc}", file=sys.stderr)
    except ValueError as exc:
        print(f"錯誤（輸入不合法）：{exc}", file=sys.stderr)
    except OutputWriteError as exc:
        print(f"錯誤（寫入輸出檔失敗）：{exc}", file=sys.stderr)
    except WatermarkError as exc:
        print(f"錯誤（PDF 無法處理）：{exc}", file=sys.stderr)
    except OSError as exc:
        print(f"錯誤（讀取檔案失敗）：{pdf_path}（{exc}）", file=sys.stderr)
    else:
        # 寫檔成功後才提醒：輸出檔由 PdfWriter 重新產生且不呼叫 encrypt()，原檔的權限限制會消失
        if result.was_encrypted:
            print(f"警告：{pdf_path} 原檔的加密與權限限制不會保留在輸出檔。", file=sys.stderr)
        print(f"完成：{pdf_path} → {result.output_path}")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點：逐檔處理，單一檔案失敗不中斷其他檔案；有任何失敗回傳 1。

    沒有任何命令列引數時改為啟動網頁版（見 web_server.py）。
    """
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # 延遲 import：CLI 模式不需要載入 http.server，也避免與 web_server 的循環 import
        from web_server import run_server
        return run_server()
    # pypdf 內部會以英文 logging 警告輕微格式問題，會淹沒繁中錯誤訊息，只保留 ERROR 以上
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    args = parse_args(argv)
    failure_count = 0
    for pdf_path in args.pdf_files:
        if pdf_path.stem.endswith(OUTPUT_SUFFIX):
            print(f"警告：{pdf_path} 檔名已以 {OUTPUT_SUFFIX} 結尾，視為已加過浮水印，略過。",
                  file=sys.stderr)
            continue
        # 刻意的批次隔離：pypdf / reportlab 遇到怪檔可能拋出各種非預期例外
        # （KeyError、AttributeError、RecursionError 等），不能讓一個壞檔中斷整批，
        # 所以在最外層兜底，記為失敗後繼續處理下一個檔案。
        try:
            if not process_file(pdf_path):
                failure_count += 1
        except Exception as exc:  # noqa: BLE001 — 批次隔離，見上方說明
            print(f"錯誤（未預期）：{pdf_path}：{type(exc).__name__}: {exc}", file=sys.stderr)
            failure_count += 1
    return 1 if failure_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
