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

  function setOpen(open) {
    document.body.classList.toggle("dock-open", open);
    if (open) refresh();
  }

  function toggle() {
    setOpen(!document.body.classList.contains("dock-open"));
  }

  function writeView(text) {
    view.textContent = text;
    view.scrollTop = 0;
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
    writeView(data.content || "（空文件）");
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
    writeView(data.content || "（空）");
    if (!quiet) cmdInput.blur();
  }

  function runCommand(raw) {
    const line = raw.trim();
    if (!line) return;
    const [cmd, ...rest] = line.split(/\s+/);
    const arg = rest.join(" ");
    if (cmd === "help") {
      meta.textContent = "命令";
      writeView(
        ["ls                 当前人格的记忆文件，日期从早到晚", "open <文件id>      读取记忆文件", "runtime            切到这位的监督", "refresh            刷新"].join(
          "\n"
        )
      );
      return;
    }
    if (cmd === "refresh") {
      refresh();
      return;
    }
    if (cmd === "runtime") {
      setMode("runtime");
      return;
    }
    if (cmd === "ls") {
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
      if (document.body.classList.contains("dock-open")) refresh();
    },
    showRuntime() {
      setOpen(true);
      setMode("runtime");
    },
  };
})();
