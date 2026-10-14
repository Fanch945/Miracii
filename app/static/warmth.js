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

function bar(value, low, high) {
  const span = high - low;
  const pct = Math.max(0, Math.min(1, (value - low) / span)) * 100;
  const zero = ((0 - low) / span) * 100;
  const left = Math.min(pct, zero);
  const width = Math.abs(pct - zero);
  const cls = value >= 0 ? "pos" : "neg";
  return (
    `<div class="track"><div class="fill ${cls}" style="left:${left}%;width:${Math.max(width, 0.8)}%"></div>` +
    (low < 0 ? `<div class="mark-line" style="left:${zero}%"></div>` : "") +
    `</div>`
  );
}

function renderAxes(axes) {
  el("axes").innerHTML = AXES.map((a) => {
    const value = Number(axes[a.key] ?? 0);
    const shown = a.low < 0 ? value.toFixed(2) : value.toFixed(3);
    return (
      `<div class="axis"><span class="name">${a.label}</span>` +
      `<span class="val">${shown}</span>${bar(value, a.low, a.high)}</div>`
    );
  }).join("");
}

function renderGates(gates) {
  el("gates").innerHTML = gates
    .map((g) => {
      const fired = g.fired ? " on" : "";
      const tail = g.gap != null ? `还差 ${g.gap.toFixed(3)}` : g.urgency != null ? `强度 ${g.urgency.toFixed(2)}` : "";
      return `<div class="gate${fired}"><span class="g">${g.fired ? "●" : "○"} ${g.gate}</span>` +
        `<span class="d">${g.detail}${tail ? " · " + tail : ""}</span></div>`;
    })
    .join("");
}

function renderLog(rows) {
  if (!rows || !rows.length) {
    el("log").textContent = "还没有记录。她还没醒过，或者一直没逼近门槛。";
    return;
  }
  el("log").innerHTML = rows
    .slice()
    .reverse()
    .map((row) => {
      const at = String(row.at || "").slice(5, 16).replace("T", " ");
      if (row.kind === "mark") {
        const blame = (row.blame || []).map((b) => ` <-${b}`).join("");
        const tags = (row.tags || []).map((t) => ` #${t}`).join("");
        return `<span class="m">${at} [标注] ${row.verdict}</span> ${row.text || ""}${blame}${tags}`;
      }
      if (row.kind === "speak") {
        return `<span class="k">${at} [开口]</span> ${(row.text || "").slice(0, 80)}`;
      }
      const gates = (row.gates || []).join("/") || "无触发";
      return `${at} [tick] c=${Number(row.connection ?? 0).toFixed(3)} ${gates}`;
    })
    .join("\n");
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
  el("pacing").textContent =
    `${data.user_state} · 每 ${data.tick_minutes} 分钟一拍，距下一拍 ${mins} 分钟 · 积压 ${data.backlog}${cd}`;
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
    el("status").innerHTML = `<span class="err">连不上：${exc.message}（应用在跑吗？）</span>`;
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
    el("mark-result").innerHTML = `<span class="err">失败：${exc.message}</span>`;
  }
}

function buildChips() {
  el("blames").innerHTML = BLAMES.map((name) => `<span class="chip" data-name="${name}">${name}</span>`).join("");
  el("blames").addEventListener("click", (event) => {
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
