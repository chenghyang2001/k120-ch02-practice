"""產生浮水印測試用 PDF。

輸出（與本腳本同資料夾）：
- sample.pdf：5 頁、尺寸各不相同（A4 直、Letter 直、A3 橫、A5 直、自訂窄長頁），
  附 metadata（Title/Author）、每頁一個 outline 書籤、第 1 頁一個 Link 註解，
  用來目視檢查混合尺寸頁面的置中與縮放，以及文件層級資料是否保留。
- sample_rotated.pdf：邊界情況集合——/Rotate 90、180、270、mediabox 原點非 0、
  cropbox 明顯小於 mediabox、cropbox + 非 0 原點 + /Rotate 90 組合、100x1000 極窄頁。
  每頁都在 cropbox 中心畫十字線，並在中心上方放一個 Link 註解，方便比對浮水印中心
  與註解位置是否跑掉。

用法：
    uv run make_sample_pdf.py
"""
import sys
from io import BytesIO
from pathlib import Path
from typing import NamedTuple

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from pypdf.generic import RectangleObject
from reportlab.lib.pagesizes import A3, A4, A5, landscape, letter
from reportlab.pdfgen import canvas

OUTPUT_DIR = Path(__file__).parent
SAMPLE_PATH = OUTPUT_DIR / "sample.pdf"
ROTATED_PATH = OUTPUT_DIR / "sample_rotated.pdf"
LINK_URL = "https://example.com/"
CROSSHAIR_HALF = 10

Box = tuple[float, float, float, float]  # (left, bottom, right, top)

PAGE_SPECS: list[tuple[str, tuple[float, float]]] = [
    ("A4 portrait", A4),
    ("Letter portrait", letter),
    ("A3 landscape", landscape(A3)),
    ("A5 portrait", A5),
    ("Custom narrow 300x800", (300.0, 800.0)),
]

BODY_LINES = [
    "This is a sample page for watermark testing.",
    "The watermark should be centered on every page,",
    "rotated along the diagonal, and scaled to the page.",
]


class EdgePageSpec(NamedTuple):
    """sample_rotated.pdf 單頁規格；cropbox 為 None 表示與 mediabox 相同。"""
    label: str
    mediabox: Box
    cropbox: Box | None
    rotation: int


EDGE_PAGE_SPECS: list[EdgePageSpec] = [
    EdgePageSpec("Letter /Rotate 90", (0, 0, letter[0], letter[1]), None, 90),
    EdgePageSpec("A4 landscape /Rotate 180", (0, 0, A4[1], A4[0]), None, 180),
    EdgePageSpec("A5 /Rotate 270", (0, 0, A5[0], A5[1]), None, 270),
    EdgePageSpec("Mediabox origin (100,100)", (100, 100, 900, 900), None, 0),
    EdgePageSpec("Cropbox << mediabox", (0, 0, A4[0], A4[1]), (100, 150, 450, 650), 0),
    EdgePageSpec("Crop+offset+Rotate 90", (50, 50, 650, 850), (120, 200, 520, 700), 90),
    EdgePageSpec("Narrow 100x1000", (0, 0, 100, 1000), None, 0),
]


def draw_crosshair(pdf_canvas: canvas.Canvas, center_x: float, center_y: float) -> None:
    """畫中心十字線：浮水印的視覺中心應與此對齊。"""
    pdf_canvas.line(center_x - CROSSHAIR_HALF, center_y, center_x + CROSSHAIR_HALF, center_y)
    pdf_canvas.line(center_x, center_y - CROSSHAIR_HALF, center_x, center_y + CROSSHAIR_HALF)


def draw_link_box(pdf_canvas: canvas.Canvas, rect: Box) -> None:
    """畫出可見外框並加一個 Link 註解；外框讓人能目視確認註解 /Rect 是否跑位。"""
    left, bottom, right, top = rect
    pdf_canvas.setStrokeColorRGB(0, 0, 1)
    pdf_canvas.rect(left, bottom, right - left, top - bottom)
    pdf_canvas.linkURL(LINK_URL, rect, relative=0, thickness=0)
    pdf_canvas.setStrokeGray(0.6)


def draw_sample_page(pdf_canvas: canvas.Canvas, page_number: int,
                     size_name: str, page_size: tuple[float, float]) -> None:
    """在目前頁面畫上頁碼、尺寸名稱、內文、邊框與中心十字線。"""
    width, height = page_size
    margin = min(width, height) * 0.08
    pdf_canvas.setStrokeGray(0.6)
    pdf_canvas.rect(margin, margin, width - 2 * margin, height - 2 * margin)
    pdf_canvas.setFont("Helvetica-Bold", 16)
    pdf_canvas.drawString(margin + 10, height - margin - 26, f"Page {page_number}")
    pdf_canvas.setFont("Helvetica", 11)
    pdf_canvas.drawString(margin + 10, height - margin - 46,
                          f"{size_name} ({width:.0f} x {height:.0f} pt)")
    for line_index, body_line in enumerate(BODY_LINES):
        pdf_canvas.drawString(margin + 10, height - margin - 76 - line_index * 16, body_line)
    draw_crosshair(pdf_canvas, width / 2, height / 2)


def build_sample_pdf(output_path: Path) -> None:
    """產生多尺寸 sample PDF，含 metadata、每頁書籤與第 1 頁的 Link 註解。"""
    pdf_canvas = canvas.Canvas(str(output_path))
    pdf_canvas.setTitle("Watermark Sample")
    pdf_canvas.setAuthor("k120-ch02-practice")
    for page_number, (size_name, page_size) in enumerate(PAGE_SPECS, start=1):
        pdf_canvas.setPageSize(page_size)
        draw_sample_page(pdf_canvas, page_number, size_name, page_size)
        bookmark_key = f"page{page_number}"
        pdf_canvas.bookmarkPage(bookmark_key)
        pdf_canvas.addOutlineEntry(f"Page {page_number}: {size_name}", bookmark_key, level=0)
        if page_number == 1:
            draw_link_box(pdf_canvas, (60, 60, 220, 90))
            pdf_canvas.setFont("Helvetica", 10)
            pdf_canvas.drawString(68, 71, "Link annotation")
        pdf_canvas.showPage()
    pdf_canvas.save()


def visible_box(spec: EdgePageSpec) -> Box:
    """回傳該頁可見範圍（cropbox，未設定時為 mediabox）。"""
    return spec.cropbox if spec.cropbox is not None else spec.mediabox


def draw_edge_page(pdf_canvas: canvas.Canvas, spec: EdgePageSpec) -> None:
    """在頁面座標畫 mediabox 淺框、cropbox 深框、標籤、cropbox 中心十字線與 Link 註解。"""
    m_left, m_bottom, m_right, m_top = spec.mediabox
    c_left, c_bottom, c_right, c_top = visible_box(spec)
    pdf_canvas.setStrokeGray(0.8)
    pdf_canvas.rect(m_left + 2, m_bottom + 2, m_right - m_left - 4, m_top - m_bottom - 4)
    pdf_canvas.setStrokeGray(0.3)
    pdf_canvas.rect(c_left + 4, c_bottom + 4, c_right - c_left - 8, c_top - c_bottom - 8)
    # 極窄頁寬度只有 100pt，字級隨可見寬度縮小，避免標籤整段被裁掉
    font_size = max(4.0, min(11.0, (c_right - c_left) / 30))
    pdf_canvas.setFont("Helvetica", font_size)
    pdf_canvas.drawString(c_left + 8, c_top - 8 - font_size, spec.label)
    pdf_canvas.drawString(c_left + 8, c_top - 10 - 2 * font_size,
                          f"/Rotate {spec.rotation}  crop={visible_box(spec)}")
    center_x, center_y = (c_left + c_right) / 2, (c_bottom + c_top) / 2
    pdf_canvas.setStrokeGray(0)
    draw_crosshair(pdf_canvas, center_x, center_y)
    draw_link_box(pdf_canvas, (center_x - 30, center_y + 20, center_x + 30, center_y + 40))


def render_edge_pages() -> bytes:
    """以 reportlab 畫出所有邊界頁；頁面大小取 mediabox 右上角，讓非 0 原點的內容也畫得到。"""
    buffer = BytesIO()
    pdf_canvas = canvas.Canvas(buffer)
    pdf_canvas.setTitle("Watermark Edge Cases")
    pdf_canvas.setAuthor("k120-ch02-practice")
    for spec in EDGE_PAGE_SPECS:
        pdf_canvas.setPageSize((spec.mediabox[2], spec.mediabox[3]))
        draw_edge_page(pdf_canvas, spec)
        pdf_canvas.showPage()
    pdf_canvas.save()
    return buffer.getvalue()


def build_rotated_pdf(output_path: Path) -> None:
    """產生邊界情況 PDF：reportlab 畫內容後，再以 pypdf 設定 mediabox / cropbox / /Rotate。"""
    reader = PdfReader(BytesIO(render_edge_pages()))
    writer = PdfWriter(clone_from=reader)
    for page, spec in zip(writer.pages, EDGE_PAGE_SPECS, strict=True):
        page.mediabox = RectangleObject(spec.mediabox)
        # 一律明確寫入 cropbox，避免 reportlab 預設值殘留成 (0,0,w,h) 而與非 0 原點不符
        page.cropbox = RectangleObject(visible_box(spec))
        if spec.rotation:
            page.rotate(spec.rotation)
    with output_path.open("wb") as output_file:
        writer.write(output_file)


def main() -> int:
    """產生兩份測試 PDF；失敗時印出繁中錯誤訊息並回傳非 0。"""
    try:
        build_sample_pdf(SAMPLE_PATH)
        print(f"已產生：{SAMPLE_PATH}（{len(PAGE_SPECS)} 頁）")
        build_rotated_pdf(ROTATED_PATH)
        print(f"已產生：{ROTATED_PATH}（{len(EDGE_PAGE_SPECS)} 頁邊界情況）")
    except OSError as exc:
        print(f"錯誤：寫入或讀取 PDF 失敗（{exc}）", file=sys.stderr)
        return 1
    except PyPdfError as exc:
        print(f"錯誤：中間產生的 PDF 無法解析（{exc}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
