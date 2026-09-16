/* 排演执行台 — 前端逻辑（原生 JS）
 *
 * 服务端每秒轮询一次取权威状态；场钟与按钮可用性在两次轮询之间于本地实时推导，
 * 保证「就绪」一到点即可点击。刷新页面后：进行中的场次继续走，已结束的最近一场
 * 仍会展示其全部已记录状态。
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const state = {
    run: null,                 // 最近一次服务端视图（进行中或已结束）
    active: false,             // 当前展示场次是否进行中
    clockOrigin: 0,            // 本地 performance.now()/1000 与场钟零点的换算基准
    clockBase: 0,              // 收到响应那一刻的 elapsed
    lastServerEpoch: 0,
    pollTimer: null,
    tickTimer: null,
    busy: false,
  };

  // ----------------------------------------------------------- 工具

  function fmtTime(sec) {
    if (sec === null || sec === undefined || Number.isNaN(sec)) return "—";
    sec = Math.max(0, sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const ss = String(s).padStart(2, "0");
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}`
                 : `${m}:${ss}`;
  }

  // 场钟格式：m:ss.t（小时以上 h:mm:ss.t），十分之一秒
  function fmtClock(sec) {
    if (sec === null || sec === undefined || Number.isNaN(sec)) return "--:--.-";
    sec = Math.max(0, sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const t = Math.floor((sec * 10) % 10);
    const ss = String(s).padStart(2, "0");
    return h > 0
      ? `${h}:${String(m).padStart(2, "0")}:${ss}.${t}`
      : `${String(m).padStart(2, "0")}:${ss}.${t}`;
  }

  function fmtSigned(sec) {
    if (sec === null || sec === undefined || Number.isNaN(sec)) return "—";
    const v = Math.round(sec * 10) / 10;
    if (Math.abs(v) < 0.05) return '<span class="zero">±0.0</span>';
    const sign = v > 0 ? "+" : "−";
    const cls = v > 0 ? "late" : "early";
    const word = v > 0 ? "晚" : "早";
    return `<span class="dev ${cls}" title="相对计划${word}">${sign}${Math.abs(v).toFixed(1)}</span>`;
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
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

  // ----------------------------------------------------------- 本地场钟 / 状态推导

  function syncClock(run) {
    // 用 server_epoch 校准浏览器时钟偏差：elapsed = now_local - offset - origin
    const localNow = Date.now() / 1000;
    state.clockBase = localNow - (run.server_epoch - run.origin_epoch);
    state.lastServerEpoch = run.server_epoch;
  }

  function localElapsed() {
    const run = state.run;
    if (!run) return 0;
    if (run.status !== "running") return run.elapsed;
    return Math.max(0, Date.now() / 1000 - state.clockBase);
  }

  /**
   * 依据最近一次服务端视图与本地场钟，实时推导每条提示状态。
   * 与后端 rehearsal._derive_states 同口径。
   */
  function deriveLocal() {
    const run = state.run;
    if (!run) return new Map();
    const elapsed = localElapsed();
    const byId = new Map(run.cues.map((c) => [c.id, c]));
    const out = new Map();
    for (const c of run.cues) {
      if (c.status === "completed" || c.status === "running") {
        out.set(c.id, { status: c.status, readyIn: null, elapsed });
        continue;
      }
      const preds = c.predecessors || [];
      let depThreshold = null;
      let allDone = true;
      if (preds.length) {
        depThreshold = 0;
        for (const p of preds) {
          const pre = byId.get(p.id);
          if (!pre || pre.status !== "completed") { allDone = false; continue; }
          if (pre.actual_end !== null)
            depThreshold = Math.max(depThreshold, pre.actual_end + p.delay);
        }
      }
      const cands = [];
      if (depThreshold !== null) cands.push(depThreshold);
      if (c.locked_start !== null && c.locked_start !== undefined)
        cands.push(c.locked_start);
      const threshold = cands.length ? Math.max(...cands) : 0;
      const noGate = !preds.length &&
        (c.locked_start === null || c.locked_start === undefined);
      let status = "waiting";
      let readyIn = null;
      if (noGate || (allDone && elapsed >= threshold - 1e-6)) {
        status = "ready";
      } else if (allDone) {
        readyIn = Math.max(0, threshold - elapsed);
      }
      out.set(c.id, { status, readyIn, elapsed, threshold });
    }
    return out;
  }

  // ----------------------------------------------------------- 渲染

  function showIdle() {
    state.active = false;
    $("#idlePanel").classList.remove("hidden");
    $("#runPanel").classList.add("hidden");
    document.body.classList.remove("ended");
    stopTimers();
  }

  function showRun(run, active) {
    state.run = run;
    state.active = active;
    syncClock(run);
    $("#idlePanel").classList.add("hidden");
    $("#runPanel").classList.remove("hidden");
    renderShell();
    renderTable();
    if (active) startTimers();
    else {
      stopTimers();
      document.body.classList.add("ended");
    }
  }

  function renderShell() {
    const run = state.run;
    const active = run.status === "running";
    document.body.classList.toggle("ended", !active);

    const badge = $("#runBadge");
    badge.textContent = active ? "进行中" : "已结束";
    badge.className = `run-badge ${active ? "running" : "ended"}`;

    $("#runTitle").textContent = `第 ${run.id} 场排演`;
    $("#runNoteText").textContent = run.note ? `· ${run.note}` : "";
    $("#wallOpen").textContent = run.wall_clock_open;
    $("#progressText").textContent =
      `${run.counts.completed} / ${run.counts.total}`;

    const endBtn = $("#endRunBtn");
    endBtn.disabled = false;
    if (active) {
      endBtn.classList.remove("hidden");
      endBtn.textContent = "■ 结束本场";
      endBtn.classList.add("danger-ghost");
      endBtn.classList.remove("primary");
    } else {
      endBtn.classList.remove("hidden");
      endBtn.textContent = "↺ 开始新一场";
      endBtn.classList.remove("danger-ghost");
      endBtn.classList.add("primary");
    }
    $("#frozenHint").classList.toggle("hidden", !active);
  }

  function statusChip(st, readyIn) {
    if (st === "ready")
      return '<span class="state-chip ready"><i class="st st-ready"></i>就绪</span>';
    if (st === "running")
      return '<span class="state-chip running"><i class="st st-running"></i>执行中</span>';
    if (st === "completed")
      return '<span class="state-chip completed"><i class="st st-done"></i>已完成</span>';
    const cd = readyIn !== null
      ? `<span class="countdown">${readyIn < 60
          ? Math.ceil(readyIn) + "s" : fmtTime(readyIn)}</span>`
      : "";
    return `<span class="state-chip waiting"><i class="st st-waiting"></i>等待${cd}</span>`;
  }

  const MEDIA_ICON = { image: "🖼", audio: "🎵", video: "🎬" };

  function mediaCell(c) {
    const media = c.media || [];
    if (!media.length) return '<span class="muted">—</span>';
    return `<span class="media-mini">${media.map((m) => {
      const cls = m.ready ? "media-chip ready" : "media-chip notready";
      const title = `${m.name}（${m.status === "ok" ? "就绪" : "未就绪：" + m.status}）`;
      if (!m.ready)
        return `<span class="${cls}" title="${escapeHtml(title)}">${MEDIA_ICON[m.kind] || "📄"}⚠</span>`;
      return `<a class="${cls}" href="${m.url}" data-media="${m.kind}"
        data-name="${escapeAttr(m.name)}" title="${escapeAttr(title)}">${MEDIA_ICON[m.kind] || "📄"}</a>`;
    }).join("")}</span>`;
  }

  function openMediaPreview(kind, url, name) {
    let inner;
    if (kind === "image") inner = `<img src="${url}" alt="">`;
    else if (kind === "video") inner = `<video src="${url}" controls autoplay></video>`;
    else inner = `<audio src="${url}" controls autoplay></audio>`;
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.style.cssText = "z-index:200;padding:5vh 16px";
    ov.innerHTML = `<div class="modal preview-modal" style="width:min(720px,94vw)">
      <h3>${escapeHtml(name)}</h3>
      <div class="preview-body" style="margin:10px 0;display:flex;justify-content:center;background:#000;border-radius:8px;overflow:hidden">${inner}</div>
      <div class="modal-actions"><button type="button" class="primary">关闭</button></div>
    </div>`;
    document.body.appendChild(ov);
    const close = () => { ov.remove(); };
    ov.addEventListener("click", (e) => { if (e.target === ov || e.target.closest("button")) close(); });
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
    });
  }

  function renderTable() {
    const run = state.run;
    const active = run.status === "running";
    const derived = active ? deriveLocal() : null;
    const elapsed = active ? localElapsed() : run.elapsed;

    const rows = run.cues.map((c) => {
      const st = active ? derived.get(c.id).status : c.status;
      const readyIn = active ? derived.get(c.id).readyIn : null;
      const lock = c.locked_start !== null && c.locked_start !== undefined
        ? '<span class="lock-mini" title="锁定开场">🔒</span>' : "";
      const conflict = c.conflict
        ? ` <span title="锁定早于依赖允许 ${fmtTime(c.allowed)}">⚠</span>` : "";

      // 开始偏差取服务端记录；执行中的结束偏差用本地场钟实时估算
      let endDev = c.end_deviation;
      let liveTag = "";
      if (active && st === "running") {
        endDev = elapsed - c.end;
        liveTag = '<span class="live-tag">实时</span>';
      }

      let op;
      if (!active) {
        op = st === "completed" ? '<span class="muted">✓</span>' : '<span class="muted">—</span>';
      } else if (st === "ready") {
        op = `<button class="primary" type="button" data-start="${c.id}">▶ 开始</button>`;
      } else if (st === "running") {
        op = `<button type="button" data-complete="${c.id}">■ 完成</button>`;
      } else if (st === "completed") {
        op = '<button type="button" disabled>✓ 已完成</button>';
      } else {
        op = '<button type="button" disabled>等待</button>';
      }

      const actualStart = c.actual_start !== null && c.actual_start !== undefined
        ? fmtTime(c.actual_start)
        : (st === "running"
            ? '<span class="actual-cell"><span class="now-dot"></span>' + fmtClock(elapsed) + "</span>"
            : "—");
      const actualEnd = c.actual_end !== null && c.actual_end !== undefined
        ? fmtTime(c.actual_end) : "—";

      return `<tr class="row-${st}" data-id="${c.id}">
        <td class="num">${c.id}</td>
        <td><span class="tag">${escapeHtml(c.department)}</span></td>
        <td>${escapeHtml(c.name)}${lock}${conflict}</td>
        <td class="num">${fmtTime(c.start)}</td>
        <td class="num">${actualStart}</td>
        <td>${fmtSigned(c.start_deviation)}</td>
        <td class="num">${fmtTime(c.end)}</td>
        <td class="num">${actualEnd}</td>
        <td>${fmtSigned(endDev)}${liveTag}</td>
        <td>${mediaCell(c)}</td>
        <td>${statusChip(st, readyIn)}</td>
        <td class="op-col"><span class="row-ops">${op}</span></td>
      </tr>`;
    }).join("");
    $("#runTbody").innerHTML = rows;
  }

  function tick() {
    if (!state.active || !state.run) return;
    $("#fieldClock").textContent = fmtClock(localElapsed());
    renderTable();
  }

  function startTimers() {
    stopTimers();
    state.tickTimer = setInterval(tick, 200);
    state.pollTimer = setInterval(poll, 1000);
  }
  function stopTimers() {
    clearInterval(state.tickTimer);
    clearInterval(state.pollTimer);
    state.tickTimer = state.pollTimer = null;
  }

  // ----------------------------------------------------------- 轮询

  async function poll() {
    if (!state.run || state.busy) return;
    try {
      const r = await api(`/api/runs/${state.run.id}`);
      state.run = r;
      if (r.status !== "running") {
        showRun(r, false);
        return;
      }
      syncClock(r);
      $("#progressText").textContent =
        `${r.counts.completed} / ${r.counts.total}`;
      renderTable();
    } catch (err) {
      // 轮询失败不打断执行台，下一拍重试
      console.warn("poll failed", err);
    }
  }

  async function initialLoad() {
    try {
      const active = await api("/api/runs/active");
      if (active.run) {
        showRun(active.run, true);
        return;
      }
      const latest = await api("/api/runs/latest");
      if (latest.run) {
        // 最近一场已结束：继续展示它与全部已记录状态
        showRun(latest.run, false);
      } else {
        showIdle();
      }
    } catch (err) {
      toast(err.message);
      showIdle();
    }
  }

  // ----------------------------------------------------------- 操作

  async function startRun() {
    const note = $("#runNote").value.trim();
    $("#idleError").classList.add("hidden");
    try {
      const r = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ note }),
      });
      toast(`第 ${r.id} 场排演已开始，快照已冻结`);
      showRun(r, true);
    } catch (err) {
      const e = $("#idleError");
      e.textContent = err.message;
      e.classList.remove("hidden");
    }
  }

  async function cueAction(kind, id) {
    if (!state.run || state.busy) return;
    state.busy = true;
    try {
      const r = await api(
        `/api/runs/${state.run.id}/cues/${id}/${kind}`,
        { method: "POST" });
      state.run = r;
      syncClock(r);
      renderTable();
      $("#progressText").textContent =
        `${r.counts.completed} / ${r.counts.total}`;
    } catch (err) {
      toast(err.message);
    } finally {
      state.busy = false;
    }
  }

  async function endOrRestart() {
    const run = state.run;
    if (!run) return;
    if (run.status !== "running") {
      // 已结束场次页面上的按钮 -> 进入空态，准备开新场
      showIdle();
      return;
    }
    const pending = run.counts.total - run.counts.completed;
    if (!confirm(`确认结束第 ${run.id} 场排演？\n` +
        (pending ? `尚有 ${pending} 条提示未完成，结束后不能再记录操作。`
                 : "结束后不能再记录操作。"))) return;
    try {
      const r = await api(`/api/runs/${run.id}/end`, { method: "POST" });
      toast(`第 ${r.id} 场排演已结束`);
      showRun(r, false);
    } catch (err) {
      toast(err.message);
    }
  }

  // ----------------------------------------------------------- 绑定 / 启动

  function bind() {
    $("#startRunBtn").addEventListener("click", startRun);
    $("#endRunBtn").addEventListener("click", endOrRestart);
    document.addEventListener("click", (e) => {
      const media = e.target.closest("[data-media]");
      if (media) {
        e.preventDefault();
        openMediaPreview(media.dataset.media, media.getAttribute("href"),
                         media.dataset.name);
        return;
      }
      const s = e.target.closest("[data-start]");
      const c = e.target.closest("[data-complete]");
      if (s) cueAction("start", Number(s.dataset.start));
      else if (c) cueAction("complete", Number(c.dataset.complete));
    });
  }

  bind();
  initialLoad();
})();
