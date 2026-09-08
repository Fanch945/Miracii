const thread = document.getElementById("thread");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const clearBtn = document.getElementById("clear-btn");
const nameEl = document.getElementById("persona-name");
const statusEl = document.getElementById("status-line");
const personaSelect = document.getElementById("persona-select");

let busy = false;
let sessionId = "holmes-test";

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

function addRow(role, content, createdAt, pending = false) {
  hideEmpty();
  const row = document.createElement("article");
  row.className = `row ${role}`;
  const bubble = document.createElement("div");
  bubble.className = pending ? "bubble pending" : "bubble";
  bubble.textContent = content;
  const time = document.createElement("div");
  time.className = "time";
  time.textContent = formatTime(createdAt);
  row.append(bubble, time);
  thread.append(row);
  thread.scrollTop = thread.scrollHeight;
  return { row, bubble, time };
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function applyMeta(data) {
  sessionId = data.session_id || data.persona_id;
  nameEl.textContent = data.name;
  const tag = data.persona_id === data.main_persona ? "主人格" : "测试人格";
  statusEl.textContent = `${tag} · ${data.model}`;
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

async function loadHistory() {
  resetThread("还没有话。从下面那一行开始。");
  const res = await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`);
  const data = await res.json();
  for (const message of data.messages || []) {
    addRow(message.role, message.content, message.created_at);
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

async function sendMessage(text) {
  if (busy) return;
  busy = true;
  sendBtn.disabled = true;
  addRow("user", text, new Date().toISOString());
  const pending = addRow("assistant", "", "", true);

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, session_id: sessionId }),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    let assembled = "";
    await readSse(res, {
      token(payload) {
        assembled += payload.text;
        pending.bubble.classList.remove("pending");
        pending.bubble.textContent = assembled;
        thread.scrollTop = thread.scrollHeight;
      },
      done(payload) {
        pending.bubble.classList.remove("pending");
        pending.bubble.textContent = payload.content;
        pending.time.textContent = formatTime(payload.created_at);
      },
      error(payload) {
        pending.bubble.classList.remove("pending");
        pending.bubble.classList.add("error");
        pending.bubble.textContent = payload.message || "回复失败";
      },
    });
      if (!assembled && !pending.bubble.textContent) {
        pending.bubble.classList.add("error");
        pending.bubble.textContent = "没有收到回复";
      }
      window.MiraciiDock?.refresh?.();
  } catch (err) {
    pending.bubble.classList.remove("pending");
    pending.bubble.classList.add("error");
    pending.bubble.textContent = err.message || "网络错误";
  } finally {
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  resizeInput();
  sendMessage(text);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener("input", resizeInput);

clearBtn.addEventListener("click", async () => {
  if (!confirm("清空这页的对话历史？原文文件仍会留在 data/transcripts/")) return;
  await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  resetThread("还没有话。从下面那一行开始。");
});

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
