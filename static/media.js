/* 提示素材库 — 前端逻辑（原生 JS）
 *
 * 上传使用 XHR 以便显示进度；失败的文件由后端保证不留临时文件 / 空记录。
 * 完整性检查会重算所有实体哈希，缺失 / 不符的素材在本页、提示单与执行台
 * 都显示「未就绪」，重传同内容文件即可自愈。
 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);

  const KIND_ICON = { image: "🖼", audio: "🎵", video: "🎬" };
  const KIND_LABEL = { image: "图片", audio: "音频", video: "视频" };

  let searchTimer = null;
  let items = [];
  let cues = [];

  // ----------------------------------------------------------- 工具

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }
  const escAttr = escapeHtml;

  function fmtSize(n) {
    if (!n) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: options.body instanceof FormData ? {} :
        { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      let msg = `请求失败 (${res.status})`;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.status === 204 ? null : res.json();
  }

  // ----------------------------------------------------------- 列表

  async function loadCues() {
    const s = await api("/api/schedule");
    cues = s.cues;
  }

  async function loadList() {
    const q = $("#searchInput").value.trim();
    const data = await api("/api/media" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    items = data.items;
    render(data);
  }

  function render(data) {
    const shaCount = {};
    for (const it of items) shaCount[it.sha256] = (shaCount[it.sha256] || 0) + 1;

    const rows = items.map((it) => {
      const dup = shaCount[it.sha256] > 1
        ? `<span class="dup-tag" title="相同文件的另一个条目">⎘ ×${shaCount[it.sha256]}</span>` : "";
      const thumb = it.kind === "image" && it.ready
        ? `<div class="thumb"><img src="${it.url}" alt=""></div>`
        : `<div class="thumb" title="${KIND_LABEL[it.kind]}">${KIND_ICON[it.kind] || "📄"}</div>`;
      const status = it.ready
        ? '<span class="st-ok">✓ 就绪</span>'
        : `<span class="st-bad" title="重新上传同内容文件即可自动修复">⚠ ${
            it.status === "missing" ? "实体缺失" : "哈希不符"}（重传修复）</span>`;
      const refs = it.ref_count
        ? `<a href="#" data-bind="${it.id}">${it.ref_count} 条提示</a>`
        : '<span class="muted">未绑定</span>';
      return `<tr data-id="${it.id}">
        <td>${thumb}</td>
        <td>
          <strong>${escapeHtml(it.name)}</strong>${dup}
          <div class="muted">${escapeHtml(it.original_name)}</div>
        </td>
        <td><span class="kind-tag kind-${it.kind}">${KIND_LABEL[it.kind]}</span></td>
        <td class="num">${fmtSize(it.size_bytes)}</td>
        <td class="sha-cell" title="${it.sha256}">${it.sha256.slice(0, 12)}…</td>
        <td>${refs}</td>
        <td>${status}</td>
        <td class="op-col"><span class="row-ops">
          <button type="button" data-preview="${it.id}">预览</button>
          <button type="button" data-bind="${it.id}">绑定提示</button>
          <button type="button" class="danger-ghost" data-del="${it.id}">删除</button>
        </span></td>
      </tr>`;
    }).join("");
    $("#mediaTbody").innerHTML = rows;
    $("#mediaEmpty").classList.toggle("hidden", items.length > 0);
    $("#listCount").textContent =
      `${data.count} 个条目 · ${data.entity_count} 个去重实体`;
  }

  // ----------------------------------------------------------- 上传

  function uploadOne(file) {
    const q = $("#uploadQueue");
    const row = document.createElement("div");
    row.className = "q-item";
    row.innerHTML = `
      <span class="q-name" title="${escAttr(file.name)}">${escapeHtml(file.name)}</span>
      <span class="q-bar"><i></i></span>
      <span class="q-state run">上传中…</span>`;
    q.prepend(row);
    const bar = row.querySelector(".q-bar i");
    const state = row.querySelector(".q-state");

    const fd = new FormData();
    fd.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/media");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) bar.style.width = `${Math.round(e.loaded / e.total * 100)}%`;
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        bar.style.width = "100%";
        state.className = "q-state ok";
        state.textContent = "✓ 完成";
        setTimeout(() => row.remove(), 4000);
        loadList();
      } else {
        let msg = `失败 ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
        state.className = "q-state err";
        state.textContent = "✗ " + msg;
      }
    };
    xhr.onerror = () => {
      state.className = "q-state err";
      state.textContent = "✗ 网络错误";
    };
    xhr.send(fd);
  }

  function pickFiles() { $("#fileInput").click(); }

  // ----------------------------------------------------------- 绑定弹窗

  async function openBind(itemId) {
    const item = items.find((x) => x.id === itemId) || (await api(`/api/media/${itemId}`));
    const sched = await api("/api/schedule");
    cues = sched.cues;
    const bound = new Set((await api(`/api/media/${itemId}`)).cues.map((c) => c.id));
    $("#bindTitle").textContent = `「${item.name}」绑定提示`;
    $("#bindHint").textContent = "点击提示行即可绑定 / 解绑；被任意提示引用的素材条目不可删除。";
    $("#bindError").classList.add("hidden");
    const list = $("#cuePickList");
    list.innerHTML = cues.map((c) => `
      <div class="cue-pick ${bound.has(c.id) ? "bound" : ""}" data-cue="${c.id}">
        <span class="tag"><span class="dept-dot-sm"></span>${escapeHtml(c.department)}</span>
        <span>#${c.id} ${escapeHtml(c.name)}</span>
        <span class="cue-meta">${escAttr(c.predecessors?.length || 0)} 个前置</span>
        <span class="tick">${bound.has(c.id) ? "✓ 已绑定" : "点击绑定"}</span>
      </div>`).join("") || '<p class="muted">还没有提示，先去提示单创建。</p>';
    list.dataset.itemId = itemId;
    list.dataset.bound = JSON.stringify([...bound]);
    $("#bindOverlay").classList.remove("hidden");
  }

  async function toggleCue(cueId) {
    const list = $("#cuePickList");
    const itemId = Number(list.dataset.itemId);
    const bound = new Set(JSON.parse(list.dataset.bound));
    try {
      // 绑定接口是「按提示全量替换」，先取该提示当前绑定的全部条目，
      // 仅增删当前条目，避免误删该提示绑定的其他素材。
      const cue = cues.find((c) => c.id === cueId) ||
        (await api(`/api/cues/${cueId}`));
      const ids = new Set((cue.media || []).map((m) => m.item_id));
      if (ids.has(itemId)) ids.delete(itemId); else ids.add(itemId);
      await api(`/api/cues/${cueId}/media`, {
        method: "PUT",
        body: JSON.stringify({ media_item_ids: [...ids] }),
      });
      if (bound.has(cueId)) bound.delete(cueId); else bound.add(cueId);
      // 用条目当前全部绑定反向同步勾选状态
      const detail = await api(`/api/media/${itemId}`);
      const nowBound = new Set(detail.cues.map((c) => c.id));
      list.dataset.bound = JSON.stringify([...nowBound]);
      list.querySelectorAll(".cue-pick").forEach((el) => {
        const on = nowBound.has(Number(el.dataset.cue));
        el.classList.toggle("bound", on);
        el.querySelector(".tick").textContent = on ? "✓ 已绑定" : "点击绑定";
      });
      await loadList();
      toast("绑定已更新");
    } catch (err) {
      const e = $("#bindError");
      e.textContent = err.message;
      e.classList.remove("hidden");
    }
  }

  // ----------------------------------------------------------- 预览

  async function openPreview(itemId) {
    const it = items.find((x) => x.id === itemId) || (await api(`/api/media/${itemId}`));
    $("#previewTitle").textContent = it.name;
    $("#previewDownload").href = it.url;
    let body;
    if (!it.ready) {
      body = `<div class="preview-body" style="padding:40px;display:block;text-align:center">
        <p class="st-bad">⚠ 素材${it.status === "missing" ? "实体缺失" : "哈希不符"}，无法预览</p>
        <p class="muted">重新上传同内容文件后自动恢复。</p></div>`;
    } else if (it.kind === "image") {
      body = `<div class="preview-body"><img src="${it.url}" alt=""></div>`;
    } else if (it.kind === "video") {
      body = `<div class="preview-body"><video src="${it.url}" controls autoplay></video></div>`;
    } else {
      body = `<div class="preview-body" style="padding:24px"><audio src="${it.url}" controls autoplay></audio></div>`;
    }
    body += `<p class="preview-meta">${escapeHtml(it.original_name)} · ${fmtSize(it.size_bytes)}
      · ${KIND_LABEL[it.kind]}<br>SHA-256: ${it.sha256}</p>`;
    $("#previewBody").innerHTML = body;
    $("#previewOverlay").classList.remove("hidden");
  }

  // ----------------------------------------------------------- 完整性检查

  async function runVerify() {
    const btn = $("#verifyBtn");
    btn.disabled = true; btn.textContent = "检查中…";
    try {
      const r = await api("/api/media/verify");
      const banner = $("#verifyBanner");
      if (r.all_ready) {
        banner.className = "verify-banner good";
        banner.innerHTML = `<h4>✓ 完整性检查通过</h4>
          共核对 ${r.checked} 个去重实体，全部存在且 SHA-256 一致。`;
      } else {
        const c = r.counts;
        const cuesTxt = r.affected_cues.length
          ? `<ul>${r.affected_cues.map((a) =>
              `<li>#${a.cue_id} ${escapeHtml(a.department)} / ${escapeHtml(a.name)}</li>`).join("")}
            </ul><p class="reupload-hint">这些提示在提示单与排演执行台均显示「素材未就绪」；
            重新上传同内容文件即可自动修复。</p>` : "";
        banner.className = "verify-banner bad";
        banner.innerHTML = `<h4>⚠ 发现 ${c.missing + c.corrupt} 个异常实体
          （缺失 ${c.missing} / 哈希不符 ${c.corrupt}）</h4>${cuesTxt}`;
      }
      await loadList();
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false; btn.textContent = "🛡 完整性检查";
    }
  }

  // ----------------------------------------------------------- 删除

  async function del(itemId) {
    const it = items.find((x) => x.id === itemId);
    if (!confirm(`确认删除素材条目「${it ? it.name : itemId}」？\n` +
        (it && it.ref_count ? `它仍被 ${it.ref_count} 条提示引用，需先解绑。\n` :
         "若为同哈希最后一个条目，实体文件将一并回收。"))) return;
    try {
      await api(`/api/media/${itemId}`, { method: "DELETE" });
      toast("已删除");
      await loadList();
    } catch (err) {
      toast(err.message);
    }
  }

  // ----------------------------------------------------------- 事件

  function bind() {
    const dz = $("#dropZone");
    dz.addEventListener("click", pickFiles);
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter") pickFiles(); });
    $("#fileInput").addEventListener("change", (e) => {
      [...e.target.files].forEach(uploadOne);
      e.target.value = "";
    });
    ["dragover", "dragenter"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragover"); }));
    dz.addEventListener("drop", (e) => {
      [...(e.dataTransfer?.files || [])].forEach(uploadOne);
    });

    $("#searchInput").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadList, 250);
    });
    $("#verifyBtn").addEventListener("click", runVerify);

    document.addEventListener("click", (e) => {
      const t = e.target.closest("button, a");
      if (!t) return;
      const p = t.dataset.preview, b = t.dataset.bind, d = t.dataset.del;
      if (p) { e.preventDefault(); openPreview(Number(p)); }
      else if (b) { e.preventDefault(); openBind(Number(b)); }
      else if (d) { e.preventDefault(); del(Number(d)); }
    });

    $("#cuePickList").addEventListener("click", (e) => {
      const row = e.target.closest(".cue-pick");
      if (row) toggleCue(Number(row.dataset.cue));
    });
    $("#bindCancel").addEventListener("click",
      () => $("#bindOverlay").classList.add("hidden"));
    $("#previewClose").addEventListener("click",
      () => { $("#previewOverlay").classList.add("hidden"); });
    [$("#bindOverlay"), $("#previewOverlay")].forEach((ov) =>
      ov.addEventListener("click", (e) => {
        if (e.target === ov) ov.classList.add("hidden");
        const v = $("#previewBody video");
        if (v) v.pause();
      }));
  }

  bind();
  Promise.all([loadCues(), loadList()]).catch((err) => {
    $("#mediaTbody").innerHTML =
      `<tr><td colspan="8" class="empty-hint">加载失败：${escapeHtml(err.message)}</td></tr>`;
  });
})();
