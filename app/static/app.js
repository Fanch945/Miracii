const thread = document.getElementById("thread");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const clearBtn = document.getElementById("clear-btn");
const refreshBtn = document.getElementById("refresh-btn");
const nameEl = document.getElementById("persona-name");
const statusEl = document.getElementById("status-line");
const personaSelect = document.getElementById("persona-select");
const imageInput = document.getElementById("image-input");
const attachBtn = document.getElementById("attach-btn");
const previewsEl = document.getElementById("previews");

let busy = false;
let sessionId = "holmes-test";
let canReset = false;
let unconsolidatedCount = 0;
let lastSeenId = 0;
const pendingImages = [];
const MAX_IMAGES = 3;

function hideEmpty() {
  document.getElementById("empty-hint")?.remove();
}

function resetThread(hintText) {
  thread.replaceChildren();
  const hint = document.createElement("p");
  hint.className = "empty";
  hint.id = "empty-hint";
  hint.textContent = hintText;
  thread.append(hint);
}

function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function addRow(role, content, createdAt, pending = false, images = []) {
  hideEmpty();
  const row = document.createElement("article");
  row.className = `row ${role}`;
  const bubble = document.createElement("div");
  bubble.className = pending ? "bubble pending" : "bubble";
  if (images && images.length) {
    const gallery = document.createElement("div");
    gallery.className = "bubble-images";
    for (const src of images) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = "图";
      gallery.append(img);
    }
    bubble.append(gallery);
  }
  const body = document.createElement("span");
  body.className = "bubble-text";
  body.textContent = content || "";
  bubble.append(body);
  const time = document.createElement("div");
  time.className = "time";
  time.textContent = formatTime(createdAt);
  row.append(bubble, time);
  thread.append(row);
  thread.scrollTop = thread.scrollHeight;
  return { row, bubble, body, time };
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function applyMeta(data) {
  sessionId = data.session_id || data.persona_id;
  nameEl.textContent = data.name;
  const tag = data.persona_id === data.main_persona ? "主人格" : "测试人格";
  const ver = data.version ? ` · v${data.version}` : "";
  statusEl.textContent = `${tag} · ${data.model}${ver}`;
  document.title = data.name;
  personaSelect.replaceChildren();
  for (const item of data.personas || []) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.main ? `${item.name}（存档）` : item.name;
    if (item.active) option.selected = true;
    personaSelect.append(option);
  }
}

async function loadMeta() {
  const res = await fetch("/api/meta");
  applyMeta(await res.json());
}

function applyHistoryMeta(data) {
  canReset = Boolean(data.can_reset);
  unconsolidatedCount = data.unconsolidated_count || 0;
  clearBtn.disabled = !canReset;
  if (canReset) {
    clearBtn.title = `撤回尚未写入记忆的 ${unconsolidatedCount} 句，网页会与记录对齐`;
  } else {
    clearBtn.title = "已经写入短期记忆的对话不能退回";
  }
}

async function loadHistory() {
  resetThread("还没有话。从下面那一行开始。");
  lastSeenId = 0;
  const res = await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`);
  const data = await res.json();
  applyHistoryMeta(data);
  for (const message of data.messages || []) {
    addRow(message.role, message.content, message.created_at, false, message.images || []);
    if (message.id) lastSeenId = Math.max(lastSeenId, message.id);
  }
  thread.scrollTop = thread.scrollHeight;
}

let refreshTimer = 0;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => {
    if (busy) return;
    refreshChat();
  }, 250);
}

async function refreshChat() {
  if (busy) return;
  try {
    await loadHistory();
    window.MiraciiDock?.refresh?.();
  } catch (err) {
    statusEl.textContent = err.message || "刷新失败";
  }
}

async function readSse(response, handlers) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const eventLine = part.split("\n").find((line) => line.startsWith("event:"));
      const dataLine = part.split("\n").find((line) => line.startsWith("data:"));
      if (!eventLine || !dataLine) continue;
      const event = eventLine.slice(6).trim();
      const payload = JSON.parse(dataLine.slice(5));
      if (handlers[event]) handlers[event](payload);
    }
  }
}

async function sendMessage(text, images) {
  if (busy) return;
  busy = true;
  sendBtn.disabled = true;
  const previewUrls = images.map((item) => item.previewUrl).filter(Boolean);
  addRow("user", text || "（看）", new Date().toISOString(), false, previewUrls);
  const pending = addRow("assistant", "", "", true);
  let rolledBack = false;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        session_id: sessionId,
        images: images.map(({ mime, data }) => ({ mime, data })),
      }),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    let assembled = "";
    await readSse(res, {
      user(payload) {
        if (payload.id) lastSeenId = Math.max(lastSeenId, payload.id);
      },
      token(payload) {
        assembled += payload.text;
        pending.bubble.classList.remove("pending");
        pending.body.textContent = assembled;
        thread.scrollTop = thread.scrollHeight;
      },
      done(payload) {
        pending.bubble.classList.remove("pending");
        pending.body.textContent = payload.content;
        pending.time.textContent = formatTime(payload.created_at);
        if (payload.id) lastSeenId = Math.max(lastSeenId, payload.id);
      },
      error(payload) {
        rolledBack = Boolean(payload.rolled_back);
        pending.bubble.classList.remove("pending");
        pending.bubble.classList.add("error");
        pending.body.textContent = payload.message || "回复失败";
      },
    });
      if (rolledBack) {
        input.value = text;
        resizeInput();
        await loadHistory();
      } else if (!assembled && !pending.body.textContent) {
        pending.bubble.classList.add("error");
        pending.body.textContent = "没有收到回复";
      } else {
        const meta = await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`);
        applyHistoryMeta(await meta.json());
      }
      window.MiraciiDock?.refresh?.();
  } catch (err) {
    input.value = text;
    resizeInput();
    await loadHistory();
    statusEl.textContent = err.message || "网络错误，已撤回这句，可再发";
  } finally {
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text && !pendingImages.length) return;
  const images = pendingImages.splice(0, pendingImages.length);
  renderPreviews();
  input.value = "";
  resizeInput();
  await sendMessage(text, images);
  images.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener("input", resizeInput);

clearBtn.addEventListener("click", async () => {
  if (clearBtn.disabled) {
    alert("已经写入短期记忆的对话不能退回。");
    return;
  }
  if (!confirm(`撤回尚未巩固的 ${unconsolidatedCount} 句？网页会与对话记录对齐。已经进记忆的部分不会动。`)) {
    return;
  }
  const res = await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(data.detail || "不能撤回");
    await loadHistory();
    return;
  }
  await loadHistory();
  window.MiraciiDock?.refresh?.();
});

refreshBtn.addEventListener("click", () => refreshChat());

personaSelect.addEventListener("change", async () => {
  const res = await fetch("/api/persona", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: personaSelect.value }),
  });
  if (!res.ok) return;
  applyMeta(await res.json());
  await loadHistory();
  input.focus();
});

loadMeta()
  .then(loadHistory)
  .then(() => input.focus())
  .catch((err) => {
    statusEl.textContent = err.message || "无法连接本地服务";
  });

function renderPreviews() {
  previewsEl.replaceChildren();
  previewsEl.hidden = pendingImages.length === 0;
  pendingImages.forEach((item, index) => {
    const wrap = document.createElement("div");
    wrap.className = "preview";
    const img = document.createElement("img");
    img.src = item.previewUrl;
    img.alt = "待发";
    const drop = document.createElement("button");
    drop.type = "button";
    drop.textContent = "×";
    drop.addEventListener("click", () => {
      const gone = pendingImages.splice(index, 1)[0];
      if (gone?.previewUrl) URL.revokeObjectURL(gone.previewUrl);
      renderPreviews();
    });
    wrap.append(img, drop);
    previewsEl.append(wrap);
  });
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function fileToPayload(file) {
  if (!file || !file.type.startsWith("image/")) {
    throw new Error("只支持图片");
  }
  let blob = file;
  try {
    const bitmap = await createImageBitmap(file);
    const max = 1600;
    const scale = Math.min(1, max / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    blob = await new Promise((resolve) => canvas.toBlob((out) => resolve(out || file), "image/jpeg", 0.85));
  } catch {
    blob = file;
  }
  const data = await blobToBase64(blob);
  return {
    mime: blob.type || "image/jpeg",
    data,
    previewUrl: URL.createObjectURL(blob),
  };
}

async function addFiles(fileList) {
  for (const file of fileList) {
    if (pendingImages.length >= MAX_IMAGES) {
      alert(`一次最多 ${MAX_IMAGES} 张`);
      break;
    }
    try {
      pendingImages.push(await fileToPayload(file));
    } catch (err) {
      statusEl.textContent = err.message || "读图失败";
    }
  }
  renderPreviews();
}

attachBtn?.addEventListener("click", () => imageInput?.click());
imageInput?.addEventListener("change", async () => {
  await addFiles(imageInput.files || []);
  imageInput.value = "";
});

input.addEventListener("paste", async (event) => {
  const files = [...(event.clipboardData?.files || [])].filter((file) => file.type.startsWith("image/"));
  if (!files.length) return;
  event.preventDefault();
  await addFiles(files);
});

form.addEventListener("dragover", (event) => {
  event.preventDefault();
});
form.addEventListener("drop", async (event) => {
  event.preventDefault();
  const files = [...(event.dataTransfer?.files || [])].filter((file) => file.type.startsWith("image/"));
  if (files.length) await addFiles(files);
});

async function pollInbox() {
  if (busy) return;
  try {
    const res = await fetch(
      `/api/inbox?session_id=${encodeURIComponent(sessionId)}&after_id=${lastSeenId}`
    );
    if (!res.ok) return;
    const data = await res.json();
    if (!data.messages?.length) return;
    for (const message of data.messages) {
      if (!message.id || message.id <= lastSeenId) continue;
      lastSeenId = message.id;
      addRow(message.role, message.content, message.created_at, false, message.images || []);
    }
    thread.scrollTop = thread.scrollHeight;
    applyHistoryMeta(
      await (
        await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`)
      ).json()
    );
  } catch {
    /* offline: wait for next poll */
  }
}

setInterval(pollInbox, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) scheduleRefresh();
});
window.addEventListener("focus", scheduleRefresh);
