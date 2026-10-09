"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {token: null, jobs: [], selected: null, posting: false, pending: null,
    previewKey: null, previewSerial: 0, historyKey: null, pollTimer: null, toastTimer: null};
  const labels = {queued: "等待处理", running: "正在学习", completed: "已完成", failed: "处理失败",
    interrupted: "已中断", outcome_unknown: "调用结果未确认"};
  const stages = {queued: "任务已排队", preparing: "准备视频材料", acquiring: "获取视频与字幕",
    acquisition: "获取视频与字幕", transcribing: "转写语音", sampling: "抽取视频画面",
    overview: "阅读讲解与画面", detail: "补充关键细节", synthesis: "整理学习笔记",
    exporting: "生成阅读文件", complete: "笔记已生成", completed: "笔记已生成"};
  const presets = {economy: [2, 2048, 4096, 6, 24000], standard: [3, 4096, 12288, 16, 80000]};
  const budgetIds = ["max-calls", "output-tokens", "total-output-tokens", "max-images", "max-input-chars"];
  const active = (job) => ["queued", "running"].includes(job.status);
  const show = (id, value) => { $(id).hidden = !value; };
  const text = (id, value) => { $(id).textContent = value == null ? "" : String(value); };
  function notice(id, message) { text(id, message); show(id, Boolean(message)); }
  function toast(message) {
    text("toast", message); show("toast", true); clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => show("toast", false), 5000);
  }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return "10000000-1000-4000-8000-100000000000".replace(/[018]/g,
      (c) => (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
  }
  function formatTime(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "未知";
    const rounded = Math.floor(Math.max(0, value));
    const seconds = String(rounded % 60).padStart(2, "0");
    const minutes = Math.floor(rounded / 60);
    return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
  }
  function formatRange(range) {
    return Array.isArray(range) && range.length === 2 ? `${formatTime(range[0])}–${formatTime(range[1])}` : "尚未确认";
  }
  function formatDate(value, short = false) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "";
    return date.toLocaleString("zh-CN", short ? {month: "numeric", day: "numeric"} :
      {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
  }
  function safeSource(value) {
    try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
    catch (_) { return null; }
  }
  function artifactURL(value, id) {
    try {
      const url = new URL(value, location.origin);
      const prefix = `/api/jobs/${encodeURIComponent(id)}/artifacts/`;
      if (typeof value !== "string" || !value || url.origin !== location.origin || url.username || url.password || url.search || url.hash ||
          !["html", "markdown", "markdown-zip", "markdown-text"].some((kind) => url.pathname === prefix + kind)) return null;
      return url.href;
    } catch (_) { return null; }
  }
  async function request(path, options = {}) {
    const headers = {"X-Video-Notes-Session": state.token, ...(options.body ? {"Content-Type": "application/json"} : {})};
    let response;
    try { response = await fetch(path, {method: options.method || "GET", headers, body: options.body ? JSON.stringify(options.body) : undefined,
      credentials: "omit", cache: "no-store", redirect: "error"}); }
    catch (_) { throw new Error(options.method === "POST" ? "没有收到提交结果。请先检查左侧任务历史；再次提交时不会自动重试模型调用。" : "无法连接本机服务。请确认服务仍在运行，然后刷新页面。"); }
    if (!response.ok) {
      let message = "请求未完成，请检查输入后再试。";
      try { const body = await response.json(); if (typeof body.error?.message === "string") message = body.error.message; } catch (_) { /* No provider body is displayed. */ }
      if (response.status === 401 || response.status === 403) message = "页面会话已失效，请刷新页面后再操作。";
      throw new Error(message);
    }
    return options.blob ? response.blob() : response.json();
  }
  async function loadSession() {
    const response = await fetch("/api/session", {credentials: "omit", cache: "no-store", redirect: "error"});
    if (!response.ok) throw new Error("无法建立本机页面会话，请刷新页面。");
    const session = await response.json();
    if (typeof session.csrf_token !== "string" || !session.csrf_token) throw new Error("本机服务没有返回有效会话，请刷新页面。");
    state.token = session.csrf_token;
    renderReadiness(session.readiness);
    notice("connection-notice", ""); updateSubmitButton();
  }
  function renderReadiness(readiness) {
    const items = Array.isArray(readiness?.items) ? readiness.items : [];
    const missing = items.filter((item) => !item.available);
    show("readiness", missing.length > 0);
    text("readiness-title", readiness?.ready === false ? "运行环境还需准备" : "部分视频处理功能需要准备");
    const list = $("readiness-items"); list.replaceChildren();
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = `${item.name}: ${item.available ? "可用" : item.message || "尚未安装"}${item.required && !item.available ? "（必需）" : ""}`;
      if (item.available) li.classList.add("available"); list.append(li);
    }
  }
  function updateSubmitButton() {
    const busy = state.jobs.some(active);
    $("start-analysis").disabled = !state.token || state.posting || busy;
    text("start-analysis", state.posting ? "正在提交…" : busy ? "已有任务正在处理" : "生成学习笔记");
    $("resume-analysis").disabled = state.posting || busy;
  }
  function renderHistory() {
    const historyKey = JSON.stringify([state.selected, state.jobs.map((job) => [job.id, job.title, job.status, job.created_at])]);
    if (historyKey === state.historyKey) { updateSubmitButton(); return; }
    state.historyKey = historyKey;
    const list = $("job-list"); list.replaceChildren(); show("history-empty", !state.jobs.length);
    for (const job of state.jobs) {
      const li = document.createElement("li"), button = document.createElement("button"); button.type = "button";
      button.dataset.jobId = job.id; if (state.selected === job.id) button.setAttribute("aria-current", "true");
      const title = document.createElement("span"); title.className = "history-title"; title.textContent = job.title || "视频学习任务";
      const meta = document.createElement("span"); meta.className = "history-meta";
      const status = document.createElement("span"); status.className = "history-status"; status.dataset.status = job.status; status.textContent = labels[job.status] || "处理中";
      const date = document.createElement("span"); date.textContent = formatDate(job.created_at, true);
      meta.append(status, date); button.append(title, meta); button.addEventListener("click", () => selectJob(job.id));
      li.append(button); list.append(li);
    }
    updateSubmitButton();
  }
  function clearPreview() {
    state.previewSerial += 1;
    state.previewKey = null; $("note-preview").removeAttribute("src"); $("note-preview").removeAttribute("srcdoc"); show("note-preview", false);
    notice("preview-message", "");
  }
  function previewDocument(documentText) {
    const parsed = new DOMParser().parseFromString(documentText, "text/html");
    for (const anchor of parsed.querySelectorAll("a[href]")) {
      const href = anchor.getAttribute("href");
      if (href.startsWith("#")) anchor.setAttribute("href", "about:srcdoc" + href);
    }
    return "<!doctype html>\n" + parsed.documentElement.outerHTML;
  }
  async function renderPreview(job) {
    const url = artifactURL(job.artifacts?.html, job.id);
    const key = `${job.id}:${url}`;
    if (!url) { if (state.previewKey) clearPreview(); return; }
    if (state.previewKey === key) return;
    clearPreview(); state.previewKey = key;
    const serial = state.previewSerial;
    notice("preview-message", "正在打开学习笔记…");
    try {
      const blob = await request(url, {blob: true});
      const documentText = await blob.text();
      if (serial !== state.previewSerial || state.selected !== job.id) return;
      $("note-preview").srcdoc = previewDocument(documentText); show("note-preview", true); notice("preview-message", "");
    } catch (error) {
      if (serial === state.previewSerial) { state.previewKey = null; notice("preview-message", `${error.message} 可以重新选择这份笔记，或下载文件阅读。`); }
    }
  }
  function renderJob(job) {
    show("reading-empty", false); show("job-detail", true);
    text("job-title", job.title || "视频学习任务"); text("job-status", labels[job.status] || job.status); $("job-status").dataset.status = job.status;
    text("job-created", formatDate(job.created_at));
    const source = safeSource(job.source_url); show("job-source", Boolean(source));
    if (source) $("job-source").href = source; else $("job-source").removeAttribute("href");
    text("job-stage", stages[job.stage] || (typeof job.stage === "string" && job.stage.trim() ? job.stage.slice(0, 300) :
      active(job) ? "正在处理视频" : labels[job.status] || "任务状态"));
    text("job-message", job.message || (job.status === "completed" ? "笔记已保存，可在下方阅读或下载。" : "任务状态会在这里更新。"));
    $("progress-marker").classList.toggle("is-running", active(job));
    const scope = job.scope || {}, usage = job.usage || {};
    text("job-scope", formatRange(scope.processed_range));
    text("usage-calls", usage.api_calls == null ? "未知" : `${usage.api_calls} 次`);
    text("usage-tokens", usage.total_tokens == null ? "未知" : Number(usage.total_tokens).toLocaleString("zh-CN"));
    text("usage-cost", usage.cost_usd == null ? "未知" : `$${Number(usage.cost_usd).toFixed(4)}`);
    const remaining = Array.isArray(scope.remaining_range) ? scope.remaining_range : null;
    const scopeNotice = remaining ? `已处理 ${formatRange(scope.processed_range)}；还有 ${formatRange(remaining)} 未处理。本笔记覆盖已处理部分。` :
      Array.isArray(scope.processed_range) && Number.isFinite(scope.video_duration_seconds) &&
      (scope.processed_range[0] > 0 || scope.processed_range[1] < scope.video_duration_seconds) ? `本笔记只覆盖 ${formatRange(scope.processed_range)}；视频全长 ${formatTime(scope.video_duration_seconds)}。` : "";
    notice("coverage-notice", scopeNotice);
    notice("usage-notice", usage.unknown_attempts > 0 ? `有 ${usage.unknown_attempts} 次调用的结果未确认，显示的用量可能不完整；不会自动重复这些调用。` :
      usage.cost_usd == null && usage.total_tokens != null ? "服务未提供可确认的费用，费用保持未知。Tokens 来自服务实际返回的用量。" : "");
    const subtotals = [];
    if (usage.total_tokens == null && usage.known_total_tokens != null)
      subtotals.push(`已知 Tokens 小计：${Number(usage.known_total_tokens).toLocaleString("zh-CN")}；${usage.unknown_token_calls || 0} 次调用缺少完整用量`);
    if (usage.cost_usd == null && usage.known_cost_subtotal_usd != null)
      subtotals.push(`已知费用小计：$${Number(usage.known_cost_subtotal_usd).toFixed(4)}`);
    if (usage.unknown_cost_calls > 0) subtotals.push(`${usage.unknown_cost_calls} 次调用费用未知`);
    if (subtotals.length) subtotals.push("已知小计不代表完整用量或账单");
    notice("usage-subtotal", subtotals.join("。"));
    show("resume-form", Boolean(job.can_resume) && !active(job));
    for (const button of document.querySelectorAll("[data-artifact]")) button.hidden = !artifactURL(job.artifacts?.[button.dataset.artifact], job.id);
    show("artifact-controls", Array.from(document.querySelectorAll("[data-artifact]")).some((button) => !button.hidden));
    renderPreview(job);
  }
  async function selectJob(id) {
    if (state.selected !== id) { clearPreview(); $("resume-key").value = ""; notice("resume-error", ""); }
    state.selected = id; renderHistory();
    const known = state.jobs.find((job) => job.id === id); if (known) renderJob(known);
    try { const job = await request(`/api/jobs/${encodeURIComponent(id)}`); if (state.selected === id) renderJob(job); }
    catch (error) { notice("connection-notice", error.message); }
  }
  async function refreshHistory() {
    if (!state.token) return;
    try {
      const result = await request("/api/jobs"); state.jobs = Array.isArray(result.jobs) ? result.jobs : [];
      renderHistory(); const job = state.jobs.find((item) => item.id === state.selected);
      if (job) renderJob(job); notice("connection-notice", "");
    } catch (error) { notice("connection-notice", error.message); }
  }
  async function poll() {
    await refreshHistory(); state.pollTimer = setTimeout(poll, 2000);
  }
  function buildSubmission() {
    const start = Number($("range-start").value), end = $("range-end").value.trim() ? Number($("range-end").value) : null;
    if (end !== null && end <= start) throw new Error("结束时间需要晚于开始时间。");
    const output = Number($("output-tokens").value), reserved = Number($("total-output-tokens").value);
    if (Number($("max-calls").value) < 2 || reserved < output * 2)
      throw new Error("至少需要 2 次调用，并预留概览阅读和笔记整理两次输出的上限。");
    return {url: $("video-url").value.trim(), start, end, preset: document.querySelector('input[name="preset"]:checked').value,
      focus: $("study-focus").value.trim(), language: "zh", allow_asr: $("allow-asr").checked, strategy: $("sampling-strategy").value,
      provider: {base_url: $("base-url").value.trim(), model: $("model-name").value.trim(), token_parameter: $("token-parameter").value,
        json_mode: $("json-mode").checked, timeout_seconds: Number($("request-timeout").value)},
      budget: {max_calls: Number($("max-calls").value), max_input_chars: Number($("max-input-chars").value), max_images: Number($("max-images").value),
        output_tokens_per_call: output, max_reserved_output_tokens: reserved}};
  }
  $("analysis-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.posting || state.jobs.some(active)) return;
    notice("form-error", ""); let payload;
    try { payload = buildSubmission(); } catch (error) { notice("form-error", error.message); return; }
    const fingerprint = JSON.stringify(payload);
    if (!state.pending || state.pending.fingerprint !== fingerprint) state.pending = {id: uuid(), fingerprint};
    payload.submission_id = state.pending.id; payload.api_key = $("api-key").value;
    $("api-key").value = ""; state.posting = true; updateSubmitButton();
    try {
      const job = await request("/api/jobs", {method: "POST", body: payload});
      state.pending = null; state.selected = job.id; await refreshHistory();
      renderJob(state.jobs.find((item) => item.id === job.id) || job);
      if (matchMedia("(max-width: 850px)").matches) $("reading-panel").scrollIntoView({behavior: "auto", block: "start"});
    } catch (error) { notice("form-error", error.message); await refreshHistory(); }
    finally { payload.api_key = ""; state.posting = false; updateSubmitButton(); }
  });
  $("resume-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.posting || state.jobs.some(active) || !state.selected) return;
    notice("resume-error", ""); const payload = {api_key: $("resume-key").value};
    $("resume-key").value = ""; state.posting = true; updateSubmitButton();
    try { const job = await request(`/api/jobs/${encodeURIComponent(state.selected)}/resume`, {method: "POST", body: payload}); renderJob(job); await refreshHistory(); }
    catch (error) { notice("resume-error", error.message); }
    finally { payload.api_key = ""; state.posting = false; updateSubmitButton(); }
  });
  for (const radio of document.querySelectorAll('input[name="preset"]')) radio.addEventListener("change", () => {
    presets[radio.value].forEach((value, index) => { $(budgetIds[index]).value = value; });
  });
  for (const button of document.querySelectorAll("[data-artifact]")) button.addEventListener("click", async () => {
    const job = state.jobs.find((item) => item.id === state.selected);
    const url = job && artifactURL(job.artifacts?.[button.dataset.artifact], job.id); if (!url) return;
    button.disabled = true;
    try {
      const blob = await request(url, {blob: true}), blobURL = URL.createObjectURL(blob), anchor = document.createElement("a");
      const extension = button.dataset.artifact === "html" ? "html" : button.dataset.artifact === "markdown_zip" ? "zip" : "md";
      anchor.href = blobURL; anchor.download = `学习笔记-${String(job.id).slice(0, 8)}.${extension}`;
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(blobURL), 10000);
      toast("笔记文件已准备好，浏览器将开始下载。");
    } catch (error) { toast(error.message); } finally { button.disabled = false; }
  });
  $("new-note").addEventListener("click", () => {
    state.selected = null; clearPreview(); renderHistory(); show("job-detail", false); show("reading-empty", true);
    $("resume-key").value = ""; $("video-url").focus();
  });
  $("refresh-history").addEventListener("click", refreshHistory);
  $("refresh-readiness").addEventListener("click", async () => {
    try { await loadSession(); toast("已重新检查运行环境。"); } catch (error) { notice("connection-notice", error.message); }
  });
  window.addEventListener("pagehide", () => { clearTimeout(state.pollTimer); clearPreview(); $("api-key").value = ""; $("resume-key").value = ""; });
  (async () => {
    try { await loadSession(); await refreshHistory(); if (state.jobs.length) await selectJob(state.jobs[0].id); state.pollTimer = setTimeout(poll, 2000); }
    catch (_) { notice("connection-notice", "无法连接本机服务。请确认服务仍在运行，然后刷新页面。"); }
  })();
})();
