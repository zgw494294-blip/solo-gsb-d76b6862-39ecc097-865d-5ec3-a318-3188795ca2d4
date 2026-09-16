/* 舞台排演提示单 — 前端逻辑（原生 JS） */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const state = {
    schedule: { cues: [], edges: [], conflicts: [] },
    pps: Number(localStorage.getItem("cue_pps")) || 6, // 每秒像素
    deptFilter: localStorage.getItem("cue_dept") || "",
    editingId: null,
    // 渲染后缓存：cueId -> { y, barX1, barX2 }
    geo: new Map(),
  };

  const DEPT_COLORS = [
    "#e8a33d", "#7aa5ff", "#4caf7d", "#d36fd6", "#ef5d5d",
    "#46c7d0", "#f4845f", "#b6c64b", "#9a7ef0", "#e06b9a",
  ];
  const deptColor = (name) => {
    let h = 0;
    for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return DEPT_COLORS[h % DEPT_COLORS.length];
  };

  // ----------------------------------------------------------- 工具

  function fmtTime(sec) {
    if (sec === null || sec === undefined || Number.isNaN(sec)) return "—";
    sec = Math.round(sec * 10) / 10;
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const ss = Number.isInteger(s) ? String(s).padStart(2, "0")
                                   : s.toFixed(1).padStart(4, "0");
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}`
                 : `${m}:${ss}`;
  }

  function fmtDur(sec) {
    if (sec === null || sec === undefined) return "—";
    return sec >= 60 ? `${Math.round(sec)} 秒` : `${Math.round(sec * 10) / 10} 秒`;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      let msg = `请求失败 (${res.status})`;
      try {
        const body = await res.json();
        msg = body.detail || msg;
      } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    return res.status === 204 ? null : res.json();
  }

  const cueById = (id) => state.schedule.cues.find((c) => c.id === id);

  // ----------------------------------------------------------- 数据加载

  async function reload() {
    state.schedule = await api("/api/schedule");
    renderAll();
  }

  // ----------------------------------------------------------- 渲染总入口

  function visibleCues() {
    const d = state.deptFilter;
    return d ? state.schedule.cues.filter((c) => c.department === d)
             : state.schedule.cues;
  }

  function renderAll() {
    renderDeptOptions();
    renderBanner();
    renderTimeline();
    renderTable();
  }

  function renderDeptOptions() {
    const depts = [...new Set(state.schedule.cues.map((c) => c.department))].sort();
    const sel = $("#deptFilter");
    const cur = state.deptFilter;
    sel.innerHTML = '<option value="">全部部门</option>' +
      depts.map((d) => `<option value="${escapeAttr(d)}">${escapeHtml(d)}</option>`).join("");
    sel.value = depts.includes(cur) || cur === "" ? cur : "";
    state.deptFilter = sel.value;

    const dl = $("#deptList");
    dl.innerHTML = depts.map((d) => `<option value="${escapeAttr(d)}">`).join("");
  }

  function renderBanner() {
    const banner = $("#conflictBanner");
    const cs = state.schedule.conflicts;
    if (!cs.length) {
      banner.classList.add("hidden");
      banner.innerHTML = "";
      return;
    }
    banner.classList.remove("hidden");
    const items = cs.map((cf) => {
      const chain = cf.path
        .map((id) => cueById(id))
        .filter(Boolean)
        .map((c) => `${escapeHtml(c.department)} / ${escapeHtml(c.name)}`)
        .join('<span class="chain-arrow">→</span>');
      return `<li><strong>#${cf.cue_id} ${escapeHtml(cf.name)}</strong>
        锁定开场 <strong>${fmtTime(cf.locked_start)}</strong>，
        但依赖链最早允许 <strong>${fmtTime(cf.allowed)}</strong>
        （提前 ${fmtTime(cf.overrun)}）。冲突链：${chain}
        <button type="button" data-jump="${cf.cue_id}">查看 / 编辑</button></li>`;
    }).join("");
    banner.innerHTML = `<h4>⚠ 检测到 ${cs.length} 处锁定时间冲突（保留锁定值，时间轴已标红完整冲突链）</h4><ol>${items}</ol>`;
  }

  // ----------------------------------------------------------- 时间轴

  function niceTickStep(pps) {
    const targets = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
    const want = 80 / pps; // 目标刻度间距 ~80px
    return targets.find((t) => t >= want) || 7200;
  }

  function renderTimeline() {
    const cues = visibleCues();
    const pps = state.pps;
    const LABEL_W = parseInt(
      getComputedStyle(document.documentElement).getPropertyValue("--label-w"),
      10) || 300;
    const tracksEl = $("#tracks");
    const rulerEl = $("#ruler");
    const gridEl = $("#grid");
    const svgEl = $("#edgeSvg");
    state.geo = new Map();

    $("#emptyHint").classList.toggle("hidden", cues.length > 0);

    const maxEnd = cues.reduce((m, c) => Math.max(m, c.end || 0), 0);
    const scrollW = $("#timelineScroll").clientWidth - LABEL_W;
    const trackW = Math.max(maxEnd * pps + 96, Math.max(scrollW, 420));
    const innerEl = $("#timelineInner");
    innerEl.style.width = `${LABEL_W + trackW}px`;
    rulerEl.style.width = `${trackW}px`;
    gridEl.style.width = `${trackW}px`;
    svgEl.style.width = `${trackW}px`;
    svgEl.setAttribute("width", trackW);

    // 刻度尺
    const step = niceTickStep(pps);
    let ticks = "";
    for (let t = 0; t <= maxEnd + step; t += step) {
      const x = t * pps;
      ticks += `<div class="tick tick-major" style="left:${x}px"><span>${fmtTime(t)}</span></div>`;
    }
    rulerEl.innerHTML = ticks;

    // 背景网格
    let grid = "";
    for (let t = 0; t <= maxEnd + step; t += step) {
      grid += `<div class="grid-v" style="left:${t * pps}px"></div>`;
    }
    gridEl.innerHTML = grid;

    // 轨道（按部门分组）
    const groups = new Map();
    for (const c of cues) {
      if (!groups.has(c.department)) groups.set(c.department, []);
      groups.get(c.department).push(c);
    }
    let html = "";
    let y = 0;
    const HEAD_H = 30, ROW_H = 38, RULER_H = 44;
    const visibleIds = new Set(cues.map((c) => c.id));

    for (const [dept, list] of [...groups].sort((a, b) =>
      a[0].localeCompare(b[0], "zh"))) {
      html += `<div class="track dept-head" style="width:${LABEL_W + trackW}px">
        <span class="dept-tag" style="color:${deptColor(dept)}">● ${escapeHtml(dept)}</span>
        <span class="dept-count">${list.length} 条</span></div>`;
      y += HEAD_H;

      for (const c of list) {
        const x1 = (c.start || 0) * pps;
        const w = Math.max((c.duration || 0) * pps, 3);
        const cls = ["cue-bar"];
        if (c.locked_start !== null && c.locked_start !== undefined) cls.push("locked");
        else cls.push("normal");
        if (c.in_conflict_chain) cls.push("in-chain");
        if (c.in_cycle) cls.push("in-cycle");

        html += `<div class="track" style="width:${LABEL_W + trackW}px">
          <div class="track-label" data-edit="${c.id}" title="点击编辑">
            <span class="dept-dot" style="background:${deptColor(c.department)}"></span>
            <span class="t-name">${escapeHtml(c.name)}</span>
            <span class="t-dur">${fmtDur(c.duration)}</span>
            ${c.locked_start !== null && c.locked_start !== undefined
              ? '<span class="lock-ico">🔒</span>' : ""}
          </div>
          <div class="track-area">
            <div class="${cls.join(" ")}" data-edit="${c.id}"
                 style="left:${x1}px;width:${w}px"
                 title="${escapeAttr(c.name)}｜${fmtTime(c.start)} → ${fmtTime(c.end)}">
              ${escapeHtml(c.name)}<span class="bar-time">${fmtTime(c.start)}</span>
            </div>
          </div>
        </div>`;

        state.geo.set(c.id, {
          y: RULER_H + y + ROW_H / 2,
          barX1: x1,
          barX2: x1 + w,
        });
        y += ROW_H;
      }
    }
    tracksEl.innerHTML = html;

    const totalH = y;
    gridEl.style.height = `${totalH}px`;
    svgEl.style.height = `${totalH}px`;
    svgEl.setAttribute("height", totalH);

    // 依赖边
    const edgeHtml = state.schedule.edges.map((e) => {
      const a = state.geo.get(e.from);
      const b = state.geo.get(e.to);
      if (!a || !b) {
        return `<path class="hidden-edge" d="M0 0" />`;
      }
      if (!visibleIds.has(e.from) || !visibleIds.has(e.to)) {
        return `<path class="hidden-edge" d="M0 0" />`;
      }
      const x1 = a.barX2, y1 = a.y;
      const x2 = b.barX1, y2 = b.y;
      const dx = Math.max(Math.abs(x2 - x1) * 0.45, 14);
      const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
      return `<path class="${e.in_conflict_chain ? "conflict" : ""}" d="${d}">
        <title>#${e.from} → #${e.to}（延迟 ${fmtDur(e.delay)}）</title></path>`;
    }).join("");
    svgEl.innerHTML = edgeHtml;

    $("#rowCount").textContent =
      `共 ${state.schedule.cues.length} 条` +
      (state.deptFilter ? `，当前显示 ${cues.length} 条` : "");
  }

  // ----------------------------------------------------------- 表格

  function renderTable() {
    const cues = visibleCues();
    const rows = cues.map((c) => {
      const deps = (c.predecessors || []).map((p) => {
        const pre = cueById(p.id);
        const preName = pre ? pre.name : `#${p.id}`;
        const onChain = state.schedule.conflicts.some((cf) =>
          cf.cue_id === c.id &&
          cf.chain_edges.some(([a, b]) => a === p.id && b === c.id));
        return `<span class="dep-chip ${onChain ? "conflict-dep" : ""}"
          title="前置：${escapeAttr(preName)}，延迟 ${p.delay} 秒">
          #${p.id} ${escapeHtml(preName)}${p.delay ? ` +${fmtDur(p.delay)}` : ""}</span>`;
      }).join("");

      let status;
      if (c.in_cycle) status = '<span class="status-conflict">环！</span>';
      else if (c.conflict)
        status = `<span class="status-conflict" title="锁定 ${fmtTime(c.locked_start)} 早于依赖允许 ${fmtTime(c.allowed)}">⚠ 冲突 ${fmtTime(c.locked_start)}/${fmtTime(c.allowed)}</span>`;
      else if (c.locked_start !== null && c.locked_start !== undefined)
        status = '<span class="status-locked">🔒 已锁定</span>';
      else status = '<span class="status-ok">✓ 自动</span>';

      return `<tr data-id="${c.id}" class="${c.in_conflict_chain ? "row-chain" : ""}">
        <td class="num">${c.id}</td>
        <td><span class="tag"><span class="dept-dot-sm" style="background:${deptColor(c.department)}"></span>${escapeHtml(c.department)}</span></td>
        <td>${escapeHtml(c.name)}</td>
        <td class="num">${fmtTime(c.start)}</td>
        <td class="num">${fmtTime(c.end)}</td>
        <td class="num">${fmtDur(c.duration)}</td>
        <td>${deps || '<span class="muted">无（开场）</span>'}</td>
        <td>${status}</td>
        <td class="op-col">
          <span class="row-ops">
            <button type="button" data-edit="${c.id}">编辑</button>
            <button type="button" class="danger-ghost" data-del="${c.id}">删除</button>
          </span>
        </td>
      </tr>`;
    }).join("");
    $("#cueTbody").innerHTML = rows;
  }

  // ----------------------------------------------------------- 编辑弹窗

  function openEditor(id) {
    state.editingId = id;
    $("#formError").classList.add("hidden");
    const all = state.schedule.cues;
    const cue = id ? cueById(id) : null;
    $("#modalTitle").textContent = cue ? `编辑提示 #${cue.id}` : "新增提示";
    $("#fDepartment").value = cue ? cue.department : "";
    $("#fName").value = cue ? cue.name : "";
    $("#fDuration").value = cue ? cue.duration : "";
    $("#fLocked").value = cue && cue.locked_start !== null && cue.locked_start !== undefined
      ? cue.locked_start : "";
    $("#fSort").value = cue ? cue.sort_order : 0;

    const depRows = $("#depRows");
    depRows.innerHTML = "";
    const preds = cue ? cue.predecessors : [];
    if (preds.length) {
      preds.forEach((p) => addDepRow(p.id, p.delay));
    } else {
      addDepRow(null, 0);
    }
    $("#modalOverlay").classList.remove("hidden");
    setTimeout(() => $("#fDepartment").focus(), 30);
  }

  function addDepRow(selectedId, delay) {
    const editingId = state.editingId;
    const opts = state.schedule.cues
      .filter((c) => c.id !== editingId)
      .map((c) => `<option value="${c.id}" ${c.id === selectedId ? "selected" : ""}>
        #${c.id} ${escapeHtml(c.department)} / ${escapeHtml(c.name)}</option>`)
      .join("");
    const row = document.createElement("div");
    row.className = "dep-row";
    row.innerHTML = `
      <select class="f-input"><option value="">选择前置提示…</option>${opts}</select>
      <span class="dep-arrow">结束 +</span>
      <input type="number" min="0" step="0.1" value="${delay ?? 0}" aria-label="延迟秒数" />
      <span class="dep-arrow">秒</span>
      <button type="button" class="danger-ghost" data-rm-dep>移除</button>`;
    $("#depRows").appendChild(row);
  }

  function closeEditor() {
    $("#modalOverlay").classList.add("hidden");
    state.editingId = null;
  }

  function collectForm() {
    const depSel = [...document.querySelectorAll("#depRows .dep-row")];
    const predIds = new Set();
    const predecessors = [];
    for (const row of depSel) {
      const id = Number(row.querySelector("select").value);
      const delay = Number(row.querySelector("input").value);
      if (!id) continue;
      if (predIds.has(id)) throw new Error(`前置提示 #${id} 重复添加`);
      predIds.add(id);
      if (Number.isNaN(delay) || delay < 0) throw new Error("延迟必须是非负数字");
      predecessors.push({ id, delay });
    }
    const duration = Number($("#fDuration").value);
    if (Number.isNaN(duration) || duration < 0) throw new Error("时长必须是非负数字");
    const lockedRaw = $("#fLocked").value.trim();
    let locked_start = null;
    if (lockedRaw !== "") {
      locked_start = Number(lockedRaw);
      if (Number.isNaN(locked_start) || locked_start < 0)
        throw new Error("锁定开场秒数必须是非负数字，或留空");
    }
    const department = $("#fDepartment").value.trim();
    const name = $("#fName").value.trim();
    if (!department || !name) throw new Error("部门和名称不能为空");
    return {
      department, name, duration, locked_start,
      sort_order: Number($("#fSort").value) || 0,
      predecessors,
    };
  }

  async function submitForm(e) {
    e.preventDefault();
    const errEl = $("#formError");
    let payload;
    try {
      payload = collectForm();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
      return;
    }
    try {
      if (state.editingId) {
        await api(`/api/cues/${state.editingId}`, {
          method: "PUT", body: JSON.stringify(payload),
        });
      } else {
        await api("/api/cues", {
          method: "POST", body: JSON.stringify(payload),
        });
      }
      closeEditor();
      await reload();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  }

  async function deleteCue(id) {
    const c = cueById(id);
    if (!confirm(`确认删除提示 #${id}「${c ? c.name : ""}」？\n依赖它的前置关系也会一并移除。`)) return;
    await api(`/api/cues/${id}`, { method: "DELETE" });
    await reload();
  }

  // ----------------------------------------------------------- HTML 转义

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }
  const escapeAttr = escapeHtml;

  // ----------------------------------------------------------- 事件绑定

  function bindEvents() {
    $("#addBtn").addEventListener("click", () => openEditor(null));
    $("#cancelBtn").addEventListener("click", closeEditor);
    $("#cueForm").addEventListener("submit", submitForm);
    $("#addDepBtn").addEventListener("click", () => addDepRow(null, 0));
    $("#modalOverlay").addEventListener("click", (e) => {
      if (e.target === $("#modalOverlay")) closeEditor();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("#modalOverlay").classList.contains("hidden"))
        closeEditor();
    });

    // 动态容器内的点击（编辑 / 删除 / 添加前置行 / banner 跳转）
    document.addEventListener("click", async (e) => {
      const editId = e.target.closest("[data-edit]")?.dataset.edit;
      const delId = e.target.closest("[data-del]")?.dataset.del;
      const rmDep = e.target.closest("[data-rm-dep]");
      const jumpId = e.target.closest("[data-jump]")?.dataset.jump;
      if (editId) openEditor(Number(editId));
      else if (delId) await deleteCue(Number(delId));
      else if (rmDep) rmDep.closest(".dep-row").remove();
      else if (jumpId) openEditor(Number(jumpId));
    });

    $("#deptFilter").addEventListener("change", (e) => {
      state.deptFilter = e.target.value;
      localStorage.setItem("cue_dept", state.deptFilter);
      renderTimeline();
      renderTable();
    });

    const range = $("#zoomRange");
    function setZoom(v) {
      state.pps = Math.min(40, Math.max(1, Number(v)));
      range.value = state.pps;
      $("#zoomVal").textContent = state.pps;
      localStorage.setItem("cue_pps", state.pps);
      renderTimeline();
    }
    range.addEventListener("input", (e) => setZoom(e.target.value));
    $("#zoomIn").addEventListener("click", () => setZoom(state.pps + 1));
    $("#zoomOut").addEventListener("click", () => setZoom(state.pps - 1));
    $("#zoomFit").addEventListener("click", () => {
      const maxEnd = state.schedule.cues.reduce((m, c) => Math.max(m, c.end || 0), 0);
      if (maxEnd <= 0) return;
      const avail = $("#timelineScroll").clientWidth -
        (parseInt(getComputedStyle(document.documentElement)
          .getPropertyValue("--label-w"), 10) || 300) - 96;
      setZoom(Math.min(40, Math.max(1, Math.floor(avail / maxEnd))));
    });
    range.value = state.pps;
    $("#zoomVal").textContent = state.pps;

    window.addEventListener("resize", () => renderTimeline());
  }

  // ----------------------------------------------------------- 启动

  bindEvents();
  reload().catch((err) => {
    $("#tracks").innerHTML =
      `<div class="empty-hint">加载失败：${escapeHtml(err.message)}</div>`;
  });
})();
