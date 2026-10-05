// PDF 浮水印工具網頁版前端：選檔、送出到 /api/watermark、顯示下載連結
"use strict";

(function initWatermarkApp() {
  const DEFAULT_OUTPUT_NAME = "output_wm.pdf";
  const OUTPUT_SUFFIX = "_wm";
  const START_LABEL = "開始";
  const BUSY_LABEL = "處理中…";
  const NETWORK_ERROR_MESSAGE =
    "無法連線到伺服器，請確認網頁版仍在執行後再試一次";

  const pdfPicker = document.getElementById("pdfPicker");
  const pdfFile = document.getElementById("pdfFile");
  const outputName = document.getElementById("outputName");
  const wmText = document.getElementById("wmText");
  const startBtn = document.getElementById("startBtn");
  const wmForm = document.getElementById("wmForm");
  const result = document.getElementById("result");

  // 一律用 textContent 寫入，檔名與伺服器訊息都可能含使用者資料，禁止 innerHTML
  function showMessage(message, className) {
    result.replaceChildren();
    const line = document.createElement("p");
    line.className = className;
    line.textContent = message;
    result.appendChild(line);
  }

  function clearResult() {
    result.replaceChildren();
  }

  // 沒選到檔或選錯檔時回到初始狀態，避免沿用上一個檔案的輸出檔名
  function resetSelection() {
    pdfPicker.value = "";
    outputName.value = "";
    outputName.disabled = true;
    startBtn.disabled = true;
  }

  function buildOutputName(fileName) {
    const stem = fileName.replace(/\.pdf$/i, "");
    return `${stem}${OUTPUT_SUFFIX}.pdf`;
  }

  function openFilePicker() {
    pdfFile.click();
  }

  function handleFileChange() {
    const file = pdfFile.files && pdfFile.files[0];
    if (!file) {
      resetSelection();
      return;
    }
    if (!/\.pdf$/i.test(file.name)) {
      resetSelection();
      // 清掉選取值，讓使用者重選同一個檔時仍會觸發 change
      pdfFile.value = "";
      showMessage(
        `「${file.name}」不是 PDF 檔，請選取副檔名為 .pdf 的檔案`,
        "error",
      );
      return;
    }
    pdfPicker.value = file.name;
    outputName.disabled = false;
    outputName.value = buildOutputName(file.name);
    startBtn.disabled = false;
    clearResult();
  }

  function setBusy(isBusy) {
    startBtn.disabled = isBusy;
    startBtn.textContent = isBusy ? BUSY_LABEL : START_LABEL;
  }

  function showDownload(payload) {
    result.replaceChildren();
    const linkLine = document.createElement("p");
    const link = document.createElement("a");
    link.href = payload.download_url;
    link.download = payload.filename;
    link.textContent = `下載 ${payload.filename}`;
    linkLine.appendChild(link);
    result.appendChild(linkLine);
    if (payload.was_encrypted === true) {
      const warn = document.createElement("p");
      warn.className = "warn";
      warn.textContent = "注意：原檔的加密與權限限制不會保留在輸出檔中";
      result.appendChild(warn);
    }
  }

  // 伺服器錯誤回應應為 JSON；若不是（例如代理回 HTML），改顯示通用的連線錯誤
  async function readErrorMessage(response) {
    try {
      const payload = await response.json();
      return payload.error ?? `處理失敗（HTTP ${response.status}）`;
    } catch (parseError) {
      return NETWORK_ERROR_MESSAGE;
    }
  }

  async function sendWatermarkRequest(file, output, text) {
    const query = new URLSearchParams({ output, text });
    const response = await fetch(`/api/watermark?${query.toString()}`, {
      method: "POST",
      headers: { "Content-Type": "application/pdf" },
      body: file,
    });
    if (!response.ok) {
      showMessage(await readErrorMessage(response), "error");
      return;
    }
    showDownload(await response.json());
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const file = pdfFile.files && pdfFile.files[0];
    if (!file) {
      showMessage("請先選取 PDF 檔", "error");
      return;
    }
    const text = wmText.value.trim();
    if (text === "") {
      showMessage("浮水印文字不可為空", "error");
      wmText.focus();
      return;
    }
    const output = outputName.value.trim() || DEFAULT_OUTPUT_NAME;
    setBusy(true);
    clearResult();
    try {
      await sendWatermarkRequest(file, output, text);
    } catch (error) {
      // fetch 網路中斷或成功回應的 JSON 解析失敗都會到這裡
      showMessage(NETWORK_ERROR_MESSAGE, "error");
    } finally {
      setBusy(false);
    }
  }

  pdfPicker.addEventListener("click", openFilePicker);
  pdfPicker.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openFilePicker();
    }
  });
  pdfFile.addEventListener("change", handleFileChange);
  wmForm.addEventListener("submit", handleSubmit);
})();
