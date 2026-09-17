/* 提示素材库 — 前端逻辑（原生 JS）
 *
 * 上传走 XHR 以显示真实进度；列表轮询 /api/assets，按名称检索；支持预览、
 * 删除（被引用条目服务器返回 409）、绑定到提示、完整性检查与孤儿文件清理。
 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);

  const state = { items: [], cues: [], binding: null, selected: new Set() };

  // ----------------------------------------------------------- 工具
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }
  const escapeAttr = escapeHtml;

  function fmtSize(n) {
    if (n == null) return "—";
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }
  const kindIcon = { image: "🖼️", audio: "🎵", video: "🎬" };
  const kindText = { image: "图片", audio: "音频", video: "视频" };

  let toastTimer = null;
  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.style.borderColor = isError ? "var(--danger)" : "var(--accent)";
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: options.body ? { "Content-Type": "application/json" } : {},
      ...options,
    });
    if (!res.ok) {
      let msg = `请求失败 (${res.status})`;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.status === 204 ? null : res.json();
  }

  // ----------------------------------------------------------- 加载
  async function loadCues() {
    const sched = await api("/api/schedule");
    state.cues = sched.cues.map((c) => ({
      id: c.id, department: c.department, name: c.name,
      assets_ready: c.assets_ready,
    }));
  }

  async function loadAssets() {
    const q = $("#searchInput").value.trim();
    const data = await api("/api/assets" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    state.items = data.items;
    render();
  }

  // ----------------------------------------------------------- 渲染
  function render() {
    const rows = state.items.map((a) => {
      const refs = (a.cue_ids || []).map((id) => {
        const cue = state.cues.find((c) => c.id === id);
        const label = cue ? `#${id} ${cue.name}` : `#${id}`;
        return `<a class="ref-cue" href="/#cue-${id}">${escapeHtml(label)}</a>`;
      }).join("");
      const stateCell = a.state === "ready"
        ? '<span class="state-ready">✓ 就绪</span>'
        : '<span class="state-bad" title="实体缺失或 SHA-256 校验不符">⚠ 未就绪</span>';
      let thumb;
      if (a.kind === "image") {
        thumb = `<div class="thumb" data-preview="${a.id}"><img alt="" loading="lazy"
                  src="${a.content_url}" /></div>`;
      } else {
        thumb = `<div class="thumb" data-preview="${a.id}">${kindIcon[a.kind] || "📄"}</div>`;
      }
      return `<tr data-id="${a.id}" class="${a.state === "ready" ? "" : "row-bad"}">
        <td>${thumb}</td>
        <td><strong>${escapeHtml(a.name)}</strong>
          <div class="muted">${escapeHtml(a.original_name || "")}</div></td>
        <td><span class="kind-badge kind-${a.kind}">${kindText[a.kind] || a.kind} · ${escapeHtml(a.ext)}</span></td>
        <td class="num">${fmtSize(a.size)}</td>
        <td class="sha-cell" title="${escapeAttr(a.sha256)}">${escapeHtml(a.sha256.slice(0, 16))}…</td>
        <td>${refs || '<span class="muted">未绑定</span>'}</td>
        <td>${stateCell}</td>
        <td class="op-col"><span class="row-ops">
          <button type="button" data-bind="${a.id}">绑定提示</button>
          <button type="button" data-del="${a.id}" class="danger-ghost">删除</button>
        </span></td>
      </tr>`;
    }).join("");
    $("#assetTbody").innerHTML = rows;
    $("#assetEmpty").classList.toggle("hidden", state.items.length > 0);
    const bad = state.items.filter((a) => a.state !== "ready").length;
    $("#assetCount").textContent =
      `共 ${state.items.length} 条素材` + (bad ? `，其中 ${bad} 条未就绪` : "，全部就绪");
  }

  // ----------------------------------------------------------- 上传
  function uploadFile(file) {
    const errEl = $("#uploadError");
    errEl.classList.add("hidden");
    if (!file) return;
    const name = $("#assetName").value.trim();
    const fd = new FormData();
    fd.append("file", file, file.name);
    if (name) fd.append("name", name);

    const xhr = new XMLHttpRequest();
    const prog = $("#uploadProgress");
    prog.classList.remove("hidden");
    $("#progressFill").style.width = "0%";
    $("#progressText").textContent = `0% · ${fmtSize(0)}/${fmtSize(file.size)}`;

    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      $("#progressFill").style.width = pct + "%";
      $("#progressText").textContent =
        `${pct}% · ${fmtSize(e.loaded)}/${fmtSize(file.size)}`;
    });
    xhr.addEventListener("load", async () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        prog.classList.add("hidden");
        $("#assetName").value = "";
        $("#fileInput").value = "";
        toast("上传成功");
        await loadAssets();
      } else {
        prog.classList.add("hidden");
        let msg = `上传失败 (${xhr.status})`;
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
        errEl.textContent = msg;
        errEl.classList.remove("hidden");
        toast(msg, true);
      }
    });
    xhr.addEventListener("error", () => {
      prog.classList.add("hidden");
      errEl.textContent = "网络错误，上传中断";
      errEl.classList.remove("hidden");
    });
    xhr.open("POST", "/api/assets");
    xhr.send(fd);
  }

  // ----------------------------------------------------------- 预览
  function openPreview(id) {
    const a = state.items.find((x) => x.id === id);
    if (!a) return;
    $("#previewTitle").textContent = a.name;
    const url = a.content_url;
    let body;
    if (a.kind === "image") body = `<img src="${url}" alt="${escapeAttr(a.name)}" />`;
    else if (a.kind === "video")
      body = `<video src="${url}" controls preload="metadata"></video>`;
    else if (a.kind === "audio")
      body = `<audio src="${url}" controls preload="metadata"></audio>`;
    else body = "<p>不支持预览</p>";
    $("#previewBody").innerHTML = body;
    $("#previewDownload").href = url;
    $("#previewOverlay").classList.remove("hidden");
  }

  // ----------------------------------------------------------- 绑定
  async function openBind(id) {
    const a = state.items.find((x) => x.id === id);
    if (!a) return;
    state.binding = id;
    await loadCues();
    $("#bindTitle").textContent = `把「${a.name}」绑定到提示`;
    const current = new Set(a.cue_ids || []);
    state.selected = new Set(current);
    $("#bindList").innerHTML = state.cues.map((c) => `
      <label class="bind-item">
        <input type="checkbox" data-cue="${c.id}" ${current.has(c.id) ? "checked" : ""} />
        <span>#${c.id}</span>
        <span>${escapeHtml(c.name)}</span>
        <span class="bi-dept">${escapeHtml(c.department)}</span>
      </label>`).join("") || '<p class="muted">还没有提示，请先在提示单中创建。</p>';
    $("#bindError").classList.add("hidden");
    $("#bindOverlay").classList.remove("hidden");
  }

  async function saveBind() {
    const id = state.binding;
    const after = new Set(state.selected);
    try {
      // 取一次当前排程，得到每个提示的完整素材集合，只对发生变化的提示做
      // 全量更新（加入或移除本素材），避免覆盖各提示的其它素材。
      const sched = await api("/api/schedule");
      for (const cue of state.cues) {
        const c = sched.cues.find((x) => x.id === cue.id);
        const set = new Set((c.assets || []).map((x) => x.id));
        let changed = false;
        if (after.has(cue.id) && !set.has(id)) { set.add(id); changed = true; }
        if (!after.has(cue.id) && set.has(id)) { set.delete(id); changed = true; }
        if (changed) {
          await api(`/api/cues/${cue.id}/assets`, {
            method: "PUT", body: JSON.stringify([...set]),
          });
        }
      }
      $("#bindOverlay").classList.add("hidden");
      toast("绑定已更新");
      await loadAssets();
    } catch (err) {
      const e = $("#bindError");
      e.textContent = err.message; e.classList.remove("hidden");
    }
  }

  // ----------------------------------------------------------- 删除
  async function del(id) {
    const a = state.items.find((x) => x.id === id);
    if (!a) return;
    if (!confirm(`确认删除素材「${a.name}」？\n` +
        (a.cue_ids?.length
          ? "该素材正被提示引用，需先解除绑定才能删除。"
          : "若这是相同哈希的最后一个条目，其实体文件将被回收。"))) return;
    try {
      await api(`/api/assets/${id}`, { method: "DELETE" });
      toast("已删除");
      await loadAssets();
    } catch (err) { toast(err.message, true); }
  }

  // ----------------------------------------------------------- 完整性检查
  async function verify() {
    const btn = $("#verifyBtn");
    btn.disabled = true; btn.textContent = "检查中…";
    try {
      const r = await api("/api/assets/verify", { method: "POST" });
      showVerify(r);
      await loadAssets();
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; btn.textContent = "🔍 完整性检查"; }
  }

  function showVerify(r) {
    const b = $("#verifyBanner");
    if (r.healthy && !r.orphan_files.length) {
      b.className = "verify-banner ok";
      b.innerHTML = `<h4>✓ 完整性检查通过</h4>
        共 ${r.total_blobs} 个实体，SHA-256 全部一致、无缺失、无孤儿文件。`;
      return;
    }
    b.className = "verify-banner bad";
    const miss = (r.missing || []).map((m) =>
      `<li>实体缺失 <code>${escapeHtml(m.sha256.slice(0, 16))}…</code></li>`).join("");
    const bad = (r.mismatch || []).map((m) =>
      `<li>${escapeHtml(m.reason)} <code>${escapeHtml(m.sha256.slice(0, 16))}…</code></li>`).join("");
    const cues = (r.affected_cue_ids || []).map((id) => `#${id}`).join("、");
    const orph = (r.orphan_files || []).slice(0, 10)
      .map((f) => `<code>${escapeHtml(f)}</code>`).join("、");
    b.innerHTML = `<h4>⚠ 发现完整性问题</h4><ul>${miss}${bad}</ul>
      <div>受影响素材 ${(r.affected_assets || []).length} 条；
      引用它们的提示（将显示「未就绪」）：${cues || "无"}</div>
      ${r.orphan_files.length ? `<div class="muted">孤儿文件：${orph}
        （可点击「清理孤儿文件」回收）</div>` : ""}`;
  }

  async function cleanupOrphans() {
    if (!confirm("清理磁盘上无数据库记录的孤儿实体文件？")) return;
    try {
      const r = await api("/api/assets/cleanup-orphans", { method: "POST" });
      toast(`已清理 ${r.count} 个孤儿文件`);
    } catch (err) { toast(err.message, true); }
  }

  // ----------------------------------------------------------- 事件
  function bind() {
    const dz = $("#dropZone"), input = $("#fileInput");
    input.addEventListener("change", () => uploadFile(input.files[0]));
    ["dragover", "dragenter"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragover"); }));
    dz.addEventListener("drop", (e) => {
      const f = e.dataTransfer.files?.[0];
      if (f) uploadFile(f);
    });

    let t = null;
    $("#searchInput").addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(loadAssets, 250);
    });

    document.addEventListener("click", (e) => {
      const pv = e.target.closest("[data-preview]");
      const bd = e.target.closest("[data-bind]");
      const dl = e.target.closest("[data-del]");
      if (pv) openPreview(Number(pv.dataset.preview));
      else if (bd) openBind(Number(bd.dataset.bind));
      else if (dl) del(Number(dl.dataset.del));
    });

    $("#bindCancel").addEventListener("click", () =>
      $("#bindOverlay").classList.add("hidden"));
    $("#bindSave").addEventListener("click", saveBind);
    $("#bindList").addEventListener("change", (e) => {
      const cb = e.target.closest("input[data-cue]");
      if (!cb) return;
      const id = Number(cb.dataset.cue);
      cb.checked ? state.selected.add(id) : state.selected.delete(id);
    });

    $("#previewClose").addEventListener("click", () => {
      $("#previewBody").innerHTML = "";
      $("#previewOverlay").classList.add("hidden");
    });
    $("#previewOverlay").addEventListener("click", (e) => {
      if (e.target.id === "previewOverlay") {
        $("#previewBody").innerHTML = "";
        $("#previewOverlay").classList.add("hidden");
      }
    });

    $("#verifyBtn").addEventListener("click", verify);
    $("#cleanupBtn").addEventListener("click", cleanupOrphans);
  }

  bind();
  (async () => {
    try {
      await loadCues();
      await loadAssets();
    } catch (err) {
      $("#assetTbody").innerHTML =
        `<tr><td colspan="8" class="empty-hint">加载失败：${escapeHtml(err.message)}</td></tr>`;
    }
  })();
})();
