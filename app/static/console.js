(() => {
  const tree = document.getElementById("file-tree");
  const view = document.getElementById("file-view");
  const meta = document.getElementById("file-meta");
  const cmdForm = document.getElementById("dock-cmd");
  const cmdInput = document.getElementById("cmd-input");
  const refreshBtn = document.getElementById("dock-refresh");
  const toggleBtn = document.getElementById("dock-toggle");
  const headerBtn = document.getElementById("console-btn");
  const modeSelect = document.getElementById("dock-mode");
  const jumpBtn = document.getElementById("dock-jump");
  const hint = document.getElementById("dock-hint");
  const resizeHandle = document.getElementById("dock-resize");
  const dockBody = document.querySelector(".dock-body");

  const RUNTIME = [
    { id: "runtime/heartbeat", kind: "heartbeat", title: "心跳摘要" },
    { id: "runtime/supervisor", kind: "supervisor", title: "监督记录" },
  ];

  let personaId = "";
  let personaName = "";
  let sections = [];
  let mode = "memory";
  let currentId = "";
  let watchTimer = null; // warmth watch 的定时器；换人格或跑别的命令时停掉

  function setOpen(open) {
    document.body.classList.toggle("dock-open", open);
    if (open) refresh();
  }

  function toggle() {
    setOpen(!document.body.classList.contains("dock-open"));
  }

  function writeView(text, tail = false) {
    view.textContent = text;
    const pin = () => {
      view.scrollTop = tail ? view.scrollHeight : 0;
    };
    pin();
    if (tail) requestAnimationFrame(pin);
  }

  function wantsTail(fileId, truncated) {
    if (truncated) return true;
    return fileId.startsWith("transcript/") || fileId.startsWith("runtime/");
  }

  function setMode(next) {
    mode = next === "runtime" ? "runtime" : "memory";
    modeSelect.value = mode;
    jumpBtn.textContent = mode === "memory" ? "看监督" : "看记忆";
    currentId = "";
    refresh();
  }

  async function refresh() {
    hint.textContent = personaName || personaId || "";
    if (!personaId) {
      sections = [];
      renderTree();
      return;
    }
    if (mode === "runtime") {
      sections = [];
      renderTree();
      if (currentId.startsWith("runtime/")) await openRuntime(currentId.slice("runtime/".length), true);
      return;
    }
    const res = await fetch(`/api/files?persona=${encodeURIComponent(personaId)}`);
    const data = await res.json();
    const persona = (data.personas || [])[0];
    personaName = persona?.name || personaName;
    hint.textContent = personaName;
    sections = persona?.sections || [];
    renderTree();
    if (currentId && !currentId.startsWith("runtime/")) await openFile(currentId, true);
  }

  function fileButton(file, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dock-file" + (file.id === currentId ? " active" : "");
    btn.textContent = file.title || file.name;
    btn.addEventListener("click", onClick);
    return btn;
  }

  function folder(label, className, open, children) {
    const details = document.createElement("details");
    details.className = className;
    details.open = open;
    const summary = document.createElement("summary");
    summary.textContent = label;
    details.append(summary);
    for (const child of children) details.append(child);
    return details;
  }

  function renderTree() {
    tree.replaceChildren();
    if (mode === "runtime") {
      const note = document.createElement("div");
      note.className = "dock-section";
      note.textContent = "只显示当前这位。日志文件仍是全局的。";
      tree.append(note);
      for (const item of RUNTIME) {
        tree.append(fileButton(item, () => openRuntime(item.kind)));
      }
      return;
    }
    if (!sections.length) {
      const empty = document.createElement("div");
      empty.className = "dock-section";
      empty.textContent = personaId ? "（空）" : "还没有选定人格";
      tree.append(empty);
      return;
    }
    for (const section of sections) {
      tree.append(
        folder(
          section.label,
          "dock-section",
          section.id === "transcript",
          (section.files || []).map((file) => fileButton(file, () => openFile(file.id)))
        )
      );
    }
  }

  async function openFile(fileId, quiet = false) {
    currentId = fileId;
    renderTree();
    const res = await fetch(`/api/files/content?id=${encodeURIComponent(fileId)}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      meta.textContent = fileId;
      writeView(err.detail || "无法读取");
      return;
    }
    const data = await res.json();
    meta.textContent = `${data.rel}${data.truncated ? "  ·  已截断" : ""}`;
    writeView(data.content || "（空文件）", wantsTail(fileId, data.truncated));
    if (!quiet) cmdInput.blur();
  }

  async function openRuntime(kind, quiet = false) {
    currentId = `runtime/${kind}`;
    renderTree();
    const res = await fetch(
      `/api/runtime?persona=${encodeURIComponent(personaId)}&kind=${encodeURIComponent(kind)}`
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      meta.textContent = kind;
      writeView(err.detail || "无法读取");
      return;
    }
    const data = await res.json();
    meta.textContent = `${data.title} · ${personaName || personaId} · 从共享日志筛出`;
    writeView(data.content || "（空）", true);
    if (!quiet) cmdInput.blur();
  }

  function warmthText(data) {
    const bar = (value, low, high) => {
      const width = 12;
      const filled = Math.max(0, Math.min(width, Math.round(((value - low) / (high - low)) * width)));
      return "█".repeat(filled) + "·".repeat(width - filled);
    };
    const axes = data.axes || {};
    const rows = [
      ["connection", axes.connection, 0, 1, 3],
      ["pride", axes.pride, -1, 1, 2],
      ["valence", axes.valence, -1, 1, 2],
      ["arousal", axes.arousal, -1, 1, 2],
      ["immersion", axes.immersion, 0, 1, 2],
      ["hesitation", axes.hesitation, 0, 1, 2],
    ];
    const lines = [`# 积温 ${data.persona}  ${String(data.at || "").slice(11, 19)}`];
    for (const [name, value, low, high, digits] of rows) {
      const shown = Number(value ?? 0);
      lines.push(`  ${name.padEnd(11)}${shown >= 0 && low < 0 ? "+" : " "}${shown.toFixed(digits)}  ${bar(shown, low, high)}`);
    }
    const fired = (data.gates || []).filter((g) => g.fired).map((g) => g.gate);
    lines.push("");
    lines.push(`  判定    ${fired.length ? fired.join(" / ") : "没触发"}`);
    const nearest = (data.gates || []).filter((g) => g.gap != null).sort((a, b) => a.gap - b.gap)[0];
    if (!fired.length && nearest) lines.push(`  最接近  ${nearest.gate}，还差 ${nearest.gap.toFixed(3)}`);
    lines.push(`  内心    ${data.words || "—"}`);
    lines.push(`  写法    ${data.style || "—"}`);
    const t = data.thresholds || {};
    lines.push(`  门槛    观察 ${Number(t.observation).toFixed(2)}  开口 ${Number(t.contact).toFixed(2)}  强制 ${Number(t.force).toFixed(2)}`);
    const mins = Math.round((data.seconds_until_due || 0) / 60);
    const cd = data.cooldown_left > 0 ? `  冷却剩 ${data.cooldown_left} 分钟` : "";
    lines.push(`  节奏    ${data.user_state}  每 ${data.tick_minutes} 分钟一拍，距下一拍 ${mins} 分钟  积压 ${data.backlog}${cd}`);
    const recent = (data.recent || []).slice(-4);
    if (recent.length) {
      lines.push("");
      lines.push("  最近");
      for (const row of recent) {
        const at = String(row.at || "").slice(5, 16).replace("T", " ");
        if (row.kind === "mark") lines.push(`    ${at}  [标注] ${row.verdict}  ${row.text || ""}`);
        else if (row.kind === "speak") lines.push(`    ${at}  [开口]  ${String(row.text || "").slice(0, 40)}`);
        else lines.push(`    ${at}  [tick]  ${(row.gates || []).join("/") || "无触发"}`);
      }
    }
    lines.push("");
    lines.push(`  标注请用面板 /warmth，或在这里输入：mark <should_speak|should_stay_silent|unsure> <说明>`);
    return lines.join("\n");
  }

  async function showWarmth() {
    try {
      const res = await fetch("/api/warmth", { cache: "no-store" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      meta.textContent = `积温 · ${data.persona}${watchTimer ? " · 每 3 秒刷新中" : ""}`;
      writeView(warmthText(data));
    } catch (exc) {
      meta.textContent = "积温";
      writeView(`读不到积温：${exc.message}`);
    }
  }

  function stopWatch() {
    if (watchTimer) {
      clearInterval(watchTimer);
      watchTimer = null;
    }
  }

  function startWatch() {
    stopWatch();
    watchTimer = setInterval(showWarmth, 3000);
  }

  async function markFromConsole(rest) {
    const verdicts = ["should_speak", "should_stay_silent", "unsure"];
    const first = rest[0] || "";
    const verdict = verdicts.includes(first) ? first : "should_speak";
    const text = (verdicts.includes(first) ? rest.slice(1) : rest).join(" ");
    try {
      const res = await fetch("/api/warmth/mark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, verdict }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      meta.textContent = "标注";
      writeView(`已记下：[${data.verdict}] ${data.text || "（无说明）"}\n想看的话输入 warmth`);
    } catch (exc) {
      writeView(`标注失败：${exc.message}`);
    }
  }

  async function runCommand(raw) {
    const line = raw.trim();
    if (!line) return;
    const [cmd, ...rest] = line.split(/\s+/);
    const arg = rest.join(" ");
    if (cmd === "help") {
      meta.textContent = "命令";
      writeView(
        [
          "ls                 当前人格的记忆文件，日期从早到晚",
          "open <文件id>      读取记忆文件",
          "runtime            切到这位的监督",
          "warmth             看她此刻的积温（六轴、判定、门槛、台账）",
          "warmth watch       同上，每 3 秒自动刷新（再输一次 warmth watch 停止）",
          "mark <结论> <说明>  就地标注，结论取 should_speak / should_stay_silent / unsure",
          "refresh            刷新",
        ].join("\n")
      );
      return;
    }
    if (cmd === "refresh") {
      refresh();
      return;
    }
    if (cmd === "runtime") {
      stopWatch();
      setMode("runtime");
      return;
    }
    if (cmd === "warmth") {
      if (rest[0] === "watch") {
        if (watchTimer) {
          stopWatch();
          meta.textContent = "积温 · 已停止刷新";
          return;
        }
        startWatch();
      } else {
        stopWatch();
      }
      await showWarmth();
      return;
    }
    if (cmd === "mark") {
      await markFromConsole(rest);
      return;
    }
    if (cmd === "ls") {
      stopWatch();
      const lines = [`# ${personaName || personaId}`];
      for (const section of sections) {
        lines.push(`  ${section.label}`);
        for (const file of section.files || []) lines.push(`    ${file.title}    ${file.id}`);
      }
      meta.textContent = "文件列表";
      writeView(lines.join("\n"));
      return;
    }
    if (cmd === "open") {
      if (!arg) {
        writeView("用法：open transcript/miracii/2026-09-02.txt");
        return;
      }
      stopWatch();
      setMode("memory");
      openFile(arg);
      return;
    }
    writeView(`未知命令：${cmd}\n输入 help`);
  }

  function bindResize() {
    let drag = null;
    resizeHandle.addEventListener("pointerdown", (event) => {
      if (!document.body.classList.contains("dock-open")) setOpen(true);
      drag = { y: event.clientY, h: dockBody.getBoundingClientRect().height || 280 };
      document.body.classList.add("dock-dragging");
      resizeHandle.classList.add("dragging");
      resizeHandle.setPointerCapture(event.pointerId);
    });
    resizeHandle.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const max = Math.max(180, window.innerHeight - 200);
      const next = Math.max(160, Math.min(max, drag.h + (drag.y - event.clientY)));
      document.documentElement.style.setProperty("--dock-h", `${next}px`);
    });
    function endDrag() {
      drag = null;
      document.body.classList.remove("dock-dragging");
      resizeHandle.classList.remove("dragging");
    }
    resizeHandle.addEventListener("pointerup", endDrag);
    resizeHandle.addEventListener("pointercancel", endDrag);
  }

  toggleBtn.addEventListener("click", toggle);
  headerBtn.addEventListener("click", toggle);
  refreshBtn.addEventListener("click", refresh);
  jumpBtn.addEventListener("click", () => setMode(mode === "memory" ? "runtime" : "memory"));
  modeSelect.addEventListener("change", () => setMode(modeSelect.value));
  cmdForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = cmdInput.value;
    cmdInput.value = "";
    runCommand(value);
  });
  bindResize();

  window.MiraciiDock = {
    refresh,
    openFile,
    setOpen,
    setPersona(id, name) {
      if (!id || id === personaId) {
        if (name) personaName = name;
        hint.textContent = personaName || personaId;
        return;
      }
      personaId = id;
      if (name) personaName = name;
      currentId = "";
      stopWatch(); // 换人了，别让上一位的积温继续刷
      if (document.body.classList.contains("dock-open")) refresh();
    },
    showRuntime() {
      setOpen(true);
      setMode("runtime");
    },
  };
})();
