(() => {
  const params = new URLSearchParams(location.search);
  let personaId = params.get("persona") || "";
  const title = document.getElementById("studio-title");
  const status = document.getElementById("studio-status");
  const list = document.getElementById("file-list");
  const editor = document.getElementById("editor");
  const label = document.getElementById("file-label");
  const saveBtn = document.getElementById("save-btn");
  const resumeBtn = document.getElementById("resume-btn");
  const exportBtn = document.getElementById("export-btn");
  let currentPath = "";
  let savedText = "";
  let dirty = false;

  function setDirty(next) {
    dirty = next;
    saveBtn.disabled = !dirty || !currentPath;
  }

  async function api(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  }

  function confirmDiscard() {
    if (!dirty) return true;
    return window.confirm("这个文件还有没保存的修改。离开会丢掉它们。");
  }

  async function hold(on) {
    if (!personaId) return;
    const data = await api("/api/studio/hold", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persona: personaId, hold: on }),
    });
    status.textContent = data.hold
      ? "回复和巩固已暂停。保存不会自动恢复。"
      : "已恢复运行。之后的对话会重新进入巩固。";
  }

  async function loadList() {
    if (!personaId) {
      title.textContent = "设定编辑";
      status.textContent = "新建或从对话页进入一位人格。";
      list.replaceChildren();
      return;
    }
    const data = await api(`/api/studio/files?persona_id=${encodeURIComponent(personaId)}`);
    title.textContent = data.name || personaId;
    await hold(true);
    const groups = new Map();
    for (const file of data.files || []) {
      if (!groups.has(file.group)) groups.set(file.group, []);
      groups.get(file.group).push(file);
    }
    list.replaceChildren();
    for (const [group, files] of groups) {
      const heading = document.createElement("div");
      heading.className = "studio-group";
      heading.textContent = group;
      list.append(heading);
      for (const file of files) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "studio-file" + (file.path === currentPath ? " active" : "");
        button.textContent = file.path;
        button.addEventListener("click", () => openFile(file.path));
        list.append(button);
      }
    }
  }

  async function openFile(path) {
    if (path === currentPath) return;
    if (!confirmDiscard()) return;
    const data = await api(
      `/api/studio/file?persona_id=${encodeURIComponent(personaId)}&path=${encodeURIComponent(path)}`
    );
    currentPath = path;
    savedText = data.content || "";
    editor.value = savedText;
    label.textContent = path;
    setDirty(false);
    await loadList();
  }

  async function save() {
    if (!currentPath || !dirty) return;
    await api("/api/studio/file", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persona: personaId, path: currentPath, content: editor.value }),
    });
    savedText = editor.value;
    setDirty(false);
    status.textContent = "已保存。运行仍暂停，直到你点恢复。";
  }

  document.getElementById("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = document.getElementById("new-id").value.trim();
    if (!id) return;
    if (!confirmDiscard()) return;
    try {
      const data = await api("/api/studio/personas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      location.href = `/studio?persona=${encodeURIComponent(data.persona)}`;
    } catch (err) {
      status.textContent = err.message;
    }
  });

  document.getElementById("new-file-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const path = document.getElementById("new-path").value.trim();
    if (!personaId || !path) return;
    try {
      await api("/api/studio/file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona: personaId, path }),
      });
      document.getElementById("new-path").value = "";
      await openFile(path);
    } catch (err) {
      status.textContent = err.message;
    }
  });

  saveBtn.addEventListener("click", () => save().catch((err) => (status.textContent = err.message)));
  resumeBtn.addEventListener("click", () => hold(false).catch((err) => (status.textContent = err.message)));
  exportBtn.addEventListener("click", () => {
    if (!personaId) return;
    location.href = `/api/studio/export?persona_id=${encodeURIComponent(personaId)}`;
  });
  document.getElementById("import-input").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const id = window.prompt("导入到哪个人格 id？已存在时会询问是否覆盖。", personaId || "");
    if (!id) return;
    const params = new URLSearchParams({ persona_id: id.trim(), replace: "false" });
    let res = await fetch(`/api/studio/import?${params}`, {
      method: "POST",
      headers: { "Content-Type": "application/zip" },
      body: file,
    });
    if (res.status === 400) {
      const err = await res.json().catch(() => ({}));
      if (String(err.detail || "").includes("覆盖") && window.confirm(`${err.detail}。用压缩包替换这位吗？`)) {
        params.set("replace", "true");
        res = await fetch(`/api/studio/import?${params}`, {
          method: "POST",
          headers: { "Content-Type": "application/zip" },
          body: file,
        });
      } else {
        status.textContent = err.detail || "导入取消";
        return;
      }
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      status.textContent = data.detail || "导入失败";
      return;
    }
    location.href = `/studio?persona=${encodeURIComponent(data.persona)}`;
  });

  editor.addEventListener("input", () => setDirty(editor.value !== savedText));
  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  loadList().catch((err) => (status.textContent = err.message));
})();
