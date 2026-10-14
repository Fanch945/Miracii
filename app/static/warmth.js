/* 积温面板：每几秒问一次 /api/warmth，并允许就地标注。只读观察 + 一个写入口（标注）。 */
"use strict";

const AXES = [
  { key: "connection", label: "连接需求", low: 0, high: 1 },
  { key: "pride", label: "骄傲", low: -1, high: 1 },
  { key: "valence", label: "愉悦度", low: -1, high: 1 },
  { key: "arousal", label: "唤醒度", low: -1, high: 1 },
  { key: "immersion", label: "沉浸度", low: 0, high: 1 },
  { key: "hesitation", label: "犹豫", low: 0, high: 1 },
];

const BLAMES = [
  "connection", "pride", "hesitation", "valence", "arousal",
  "immersion", "user_state", "cooldown", "threshold", "timing",
];

const picked = new Set();
let timer = null;

function el(id) {
  return document.getElementById(id);
}

/* 一律用 DOM 节点 + textContent：
   台账里有用户自己写的标注文字（还有她的原话），走 innerHTML 就是一个 XSS
   ——复核实测 `<img onerror=...>` 能执行。这一页**不出现 innerHTML**。 */
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function bar(value, low, high) {
  const span = high - low;
  const pct = Math.max(0, Math.min(1, (value - low) / span)) * 100;
  const zero = ((0 - low) / span) * 100;
  const left = Math.min(pct, zero);
  const width = Math.max(Math.abs(pct - zero), 0.8);
  const track = node("div", "track");
  const fill = node("div", "fill " + (value >= 0 ? "pos" : "neg"));
  fill.style.left = left + "%";
  fill.style.width = width + "%";
  track.append(fill);
  if (low < 0) {
    const mark = node("div", "mark-line");
    mark.style.left = zero + "%";
    track.append(mark);
  }
  return track;
}

function renderAxes(axes) {
  const host = el("axes");
  host.replaceChildren();
  for (const a of AXES) {
    const value = Number(axes[a.key] ?? 0);
    const row = node("div", "axis");
    row.append(node("span", "name", a.label));
    row.append(node("span", "val", a.low < 0 ? value.toFixed(2) : value.toFixed(3)));
    row.append(bar(value, a.low, a.high));
    host.append(row);
  }
}

function renderGates(gates) {
  const host = el("gates");
  host.replaceChildren();
  for (const g of gates) {
    const row = node("div", "gate" + (g.fired ? " on" : ""));
    row.append(node("span", "g", `${g.fired ? "●" : "○"} ${g.gate}`));
    const tail = g.gap != null ? `还差 ${g.distance ?? g.gap}` : g.urgency != null ? `强度 ${g.urgency}` : "";
    row.append(node("span", "d", `${g.detail}${tail ? " · " + tail : ""}`));
    host.append(row);
  }
}

function renderLog(rows) {
  const host = el("log");
  host.replaceChildren();
  if (!rows || !rows.length) {
    host.append(node("div", null, "还没有记录。她还没醒过，或者一直没逼近门槛。"));
    return;
  }
  for (const row of rows.slice().reverse()) {
    const at = String(row.at || "").slice(5, 16).replace("T", " ");
    const line = node("div", null);
    if (row.kind === "mark") {
      line.append(node("span", "m", `${at} [标注] ${row.verdict} `));
      const blame = (row.blame || []).map((b) => ` <-${b}`).join("");
      const tags = (row.tags || []).map((t) => ` #${t}`).join("");
      line.append(node("span", null, `${row.text || ""}${blame}${tags}`));
    } else if (row.kind === "speak") {
      line.append(node("span", "k", `${at} [开口] `));
      line.append(node("span", null, String(row.text || "").slice(0, 80)));
    } else {
      const gates = (row.gates || []).join("/") || "无触发";
      line.append(node("span", null, `${at} [tick] c=${Number(row.connection ?? 0).toFixed(3)} ${gates}`));
    }
    host.append(line);
  }
}

function render(data) {
  renderAxes(data.axes || {});
  renderGates(data.gates || []);
  el("words").textContent = data.words || "—";
  el("style").textContent = data.style || "—";
  const t = data.thresholds || {};
  el("thresholds").textContent =
    `观察 ${Number(t.observation).toFixed(2)} · 开口 ${Number(t.contact).toFixed(2)} · 强制 ${Number(t.force).toFixed(2)} · 骄傲挡 ${Number(t.pride_block).toFixed(2)}`;
  const mins = Math.round((data.seconds_until_due || 0) / 60);
  const cd = data.cooldown_left > 0 ? ` · 冷却还剩 ${data.cooldown_left} 分钟` : "";
  const total = data.backlog_total ?? data.backlog;
  el("pacing").textContent =
    `${data.user_state} · 每 ${data.tick_minutes} 分钟一拍，距下一拍 ${mins} 分钟 · 积压 ${data.backlog}/${total}（新鲜/总）${cd}`;
  renderLog(data.recent || []);
  const notes = data.notes || [];
  el("status").textContent = `${data.persona} · ${String(data.at || "").slice(11, 19)}` + (notes.length ? ` · ${notes.join("；")}` : "");
}

async function refresh() {
  try {
    const response = await fetch("/api/warmth", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    render(await response.json());
  } catch (exc) {
    el("status").textContent = `连不上：${exc.message}（应用在跑吗？）`;
    el("status").className = "muted err";
  }
}

async function submitMark() {
  const body = {
    text: el("text").value.trim(),
    verdict: el("verdict").value,
    blame: [...picked],
    expected: el("expected").value || null,
    tags: el("tags").value.split(",").map((s) => s.trim()).filter(Boolean),
  };
  el("mark-result").textContent = "记下中…";
  try {
    const response = await fetch("/api/warmth/mark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    el("mark-result").textContent = `已记下（${data.verdict}）`;
    el("text").value = "";
    el("tags").value = "";
    picked.clear();
    document.querySelectorAll(".chip").forEach((node) => node.classList.remove("on"));
    refresh();
  } catch (exc) {
    el("mark-result").textContent = `失败：${exc.message}`;
    el("mark-result").className = "muted err";
  }
}

function buildChips() {
  const host = el("blames");
  host.replaceChildren();
  for (const name of BLAMES) {
    const chip = node("span", "chip", name);
    chip.dataset.name = name;
    host.append(chip);
  }
  host.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    const name = chip.dataset.name;
    if (picked.has(name)) {
      picked.delete(name);
      chip.classList.remove("on");
    } else {
      picked.add(name);
      chip.classList.add("on");
    }
  });
}

function schedule() {
  if (timer) clearInterval(timer);
  if (el("auto").checked) {
    timer = setInterval(refresh, 3000);
  }
}

buildChips();
el("mark-btn").addEventListener("click", submitMark);
el("auto").addEventListener("change", schedule);
refresh();
schedule();
