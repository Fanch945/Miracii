(() => {
  const dock = document.getElementById("dock");
  const tree = document.getElementById("file-tree");
  const view = document.getElementById("file-view");
  const meta = document.getElementById("file-meta");
  const cmdForm = document.getElementById("dock-cmd");
  const cmdInput = document.getElementById("cmd-input");
  const refreshBtn = document.getElementById("dock-refresh");
  const toggleBtn = document.getElementById("dock-toggle");
  const headerBtn = document.getElementById("console-btn");

  let groups = [];
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

  async function refresh() {
    const res = await fetch("/api/files");
    const data = await res.json();
    groups = data.groups || [];
    renderTree();
    if (currentId) await openFile(currentId, true);
  }

  function renderTree() {
    tree.replaceChildren();
    for (const group of groups) {
      const title = document.createElement("div");
      title.className = "dock-group";
      title.textContent = group.label;
      tree.append(title);
      if (!group.files.length) {
        const empty = document.createElement("div");
        empty.className = "dock-file";
        empty.textContent = "（空）";
        empty.disabled = true;
        tree.append(empty);
        continue;
      }
      for (const file of group.files) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "dock-file" + (file.id === currentId ? " active" : "");
        btn.dataset.id = file.id;
        btn.textContent = file.title || file.name;
        btn.addEventListener("click", () => openFile(file.id));
        tree.append(btn);
      }
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
    const extra = data.truncated ? "  ·  已截断" : "";
    meta.textContent = `${data.rel}${extra}`;
    writeView(data.content || "（空文件）");
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
        [
          "ls                 列出可打开的文件",
          "ls 日志/对话/记忆/设定/人设",
          "open <文件id>      读取文件",
          "refresh            刷新列表",
          "",
          "监督完整输入输出：open log/supervisor.jsonl",
          "心跳摘要：open log/heartbeat.log",
        ].join("\n")
      );
      return;
    }
    if (cmd === "refresh") {
      refresh();
      return;
    }
    if (cmd === "ls") {
      const wanted = arg
        ? groups.filter((group) => group.label.includes(arg) || group.id === arg)
        : groups;
      const lines = [];
      for (const group of wanted) {
        lines.push(`# ${group.label}`);
        if (!group.files.length) lines.push("  （空）");
        for (const file of group.files) lines.push(`  ${file.id}`);
        lines.push("");
      }
      meta.textContent = "文件列表";
      writeView(lines.join("\n").trim() || "没有文件");
      return;
    }
    if (cmd === "open") {
      if (!arg) {
        writeView("用法：open transcript/holmes-test/2026-08-17.txt");
        return;
      }
      openFile(arg);
      return;
    }
    writeView(`未知命令：${cmd}\n输入 help`);
  }

  toggleBtn.addEventListener("click", toggle);
  headerBtn.addEventListener("click", toggle);
  refreshBtn.addEventListener("click", refresh);
  cmdForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = cmdInput.value;
    cmdInput.value = "";
    runCommand(value);
  });

  window.MiraciiDock = { refresh, openFile, setOpen };
})();
