/* Hermes Operator dashboard — Kanban board, HITL modal, live SSE updates. */
"use strict";

const COLUMNS = [
  { key: "awaiting_product_lock", icon: "🔒", title: "Gate 1: Product Lock" },
  { key: "awaiting_brief_approval", icon: "📝", title: "Gate 2: Creative Brief" },
  { key: "awaiting_scene_plan_approval", icon: "🎬", title: "Gate 3: Scene Plan" },
  { key: "awaiting_storyboard_approval", icon: "🎨", title: "Gate 4: Storyboard & Cost" },
  { key: "in_production", icon: "⚡", title: "In Production" },
  { key: "awaiting_final_approval", icon: "🏆", title: "Gate 5: Final Video" },
];

const state = { detail: null };

/* ---------- helpers ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toast(message, isError) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.className = "fixed bottom-6 right-6 z-50 rounded-lg px-4 py-3 text-sm border " +
    (isError
      ? "bg-red-950 border-red-800 text-red-200"
      : "bg-slate-800 border-slate-700 text-slate-100");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.className = "hidden"; }, 4000);
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? null : response.json();
}

function fmtTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function money(value) {
  if (value === undefined || value === null) return null;
  return `$${Number(value).toFixed(2)}`;
}

/* ---------- board ---------- */

function renderBoard(columns) {
  const board = document.getElementById("board");
  board.textContent = "";

  for (const column of COLUMNS) {
    const cards = (columns && columns[column.key]) || [];

    const section = el("section", "col rounded-xl bg-slate-900/60 border border-slate-800 flex flex-col");
    const head = el("header", "px-4 py-3 border-b border-slate-800 flex items-center gap-2");
    head.appendChild(el("span", "text-base", column.icon));
    head.appendChild(el("h2", "text-sm font-semibold text-slate-200 flex-1", column.title));
    head.appendChild(el("span", "text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-400", String(cards.length)));
    section.appendChild(head);

    const list = el("div", "p-3 space-y-3 overflow-y-auto flex-1 min-h-[120px]");
    if (cards.length === 0) {
      list.appendChild(el("p", "text-xs text-slate-600 text-center py-6", "empty"));
    }
    for (const card of cards) {
      list.appendChild(renderCard(card));
    }
    section.appendChild(list);
    board.appendChild(section);
  }
}

function renderCard(card) {
  const article = el("article", "card-enter rounded-lg bg-slate-800/70 hover:bg-slate-800 border border-slate-700 hover:border-sky-600 cursor-pointer transition overflow-hidden");
  article.tabIndex = 0;

  if (card.thumbnail_url) {
    const img = el("img", "w-full h-24 object-cover");
    img.src = card.thumbnail_url;
    img.alt = card.product_name || card.workflow_id;
    img.loading = "lazy";
    article.appendChild(img);
  }

  const body = el("div", "p-3 space-y-1.5");
  body.appendChild(el("p", "text-sm font-medium truncate", card.product_name || card.workflow_id));
  body.appendChild(el("p", "text-[11px] text-slate-500 truncate", card.workflow_id));

  const meta = el("div", "flex flex-wrap items-center gap-1.5 pt-0.5");
  const cost = money(card.estimated_cost);
  if (cost) meta.appendChild(el("span", "text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300", cost));
  if (card.target_duration) {
    meta.appendChild(el("span", "text-[10px] px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300", `${card.target_duration}s`));
  }
  if (meta.childElementCount) body.appendChild(meta);

  const created = fmtTime(card.created_at);
  if (created) body.appendChild(el("p", "text-[10px] text-slate-500", created));
  article.appendChild(body);

  const open = () => openWorkflow(card.workflow_id);
  article.addEventListener("click", open);
  article.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
  return article;
}

async function loadBoard() {
  const data = await api("/api/operator/kanban");
  renderBoard(data.columns);
}

/* ---------- detail modal ---------- */

async function openWorkflow(workflowId) {
  document.getElementById("modal").classList.remove("hidden");
  document.getElementById("modal").classList.add("modal-open");
  document.getElementById("m-title").textContent = workflowId;
  document.getElementById("m-sub").textContent = "loading…";
  try {
    const detail = await api(`/api/operator/workflow/${encodeURIComponent(workflowId)}`);
    state.detail = detail;
    renderDetail(detail);
  } catch (error) {
    document.getElementById("m-sub").textContent = error.message;
  }
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  state.detail = null;
}

function clear(node) { node.textContent = ""; }

function renderDetail(detail) {
  document.getElementById("m-sub").textContent =
    `${detail.state} · v${detail.version} · ${fmtTime(detail.created_at)}`;

  renderPipeline(detail);
  renderGroundedImages(detail);
  renderDossier(detail);
  renderScenes(detail);
  renderQc(detail);
  renderApprovals(detail);
}

function renderGroundedImages(detail) {
  const container = document.getElementById("m-grounded-images");
  if (!container) return;
  clear(container);

  const ctx = detail.source_context || {};
  const snap = detail.research_snapshot || {};
  const images = (ctx.images && ctx.images.length > 0) ? ctx.images : ((snap.media_assets && snap.media_assets.length > 0) ? snap.media_assets : []);

  if (!Array.isArray(images) || images.length === 0) {
    container.innerHTML = '<p class="text-xs text-slate-600 col-span-full">Chưa có ảnh grounded nào được nạp.</p>';
    return;
  }

  images.forEach((imgObj, idx) => {
    const card = el("div", "rounded-lg overflow-hidden bg-slate-900 border border-slate-700 aspect-square relative group");
    const src = imgObj.original_url || imgObj.asset_url || imgObj.url || (imgObj.local_path ? `/api/artifacts/${encodeURIComponent(imgObj.local_path)}` : "");
    
    if (src) {
      const img = el("img", "w-full h-full object-cover group-hover:scale-105 transition duration-200");
      img.src = src;
      img.alt = `Product Grounded Asset ${idx + 1}`;
      img.loading = "lazy";
      img.onerror = () => { img.src = "https://via.placeholder.com/150?text=No+Preview"; };
      card.appendChild(img);

      if (imgObj.match_score) {
        const score = Math.round(Number(imgObj.match_score) * 100);
        const badge = el("span", "absolute bottom-1 right-1 px-1.5 py-0.5 rounded bg-black/70 text-[9px] font-mono text-emerald-400 font-bold", `${score}% match`);
        card.appendChild(badge);
      }
    } else {
      card.appendChild(el("span", "text-xs text-slate-600 flex items-center justify-center h-full", "No Image"));
    }

    container.appendChild(card);
  });
}

function renderPipeline(detail) {
  const container = document.getElementById("m-pipeline");
  if (!container) return;
  clear(container);

  const state = String(detail.state || "").toLowerCase();
  const stages = [
    { key: "product_lock", label: "1. Product Lock", done: !state.includes("product_lock") && state !== "researching", active: state.includes("product_lock") },
    { key: "creative_brief", label: "2. Creative Brief", done: !state.includes("product_lock") && !state.includes("brief_approval") && state !== "building_resource_pack", active: state.includes("brief_approval") },
    { key: "scene_plan", label: "3. Scene Plan", done: state.includes("storyboard") || state.includes("rendering") || state.includes("export") || state.includes("final") || state === "completed", active: state.includes("scene_plan") || state.includes("scene_approval") },
    { key: "storyboard", label: "4. Storyboard & Cost", done: state.includes("rendering") || state.includes("export") || state.includes("final") || state === "completed", active: state.includes("storyboard") },
    { key: "rendering", label: "5. Scene Renders (4 Scenes)", done: state.includes("export") || state.includes("final") || state === "completed", active: state.includes("rendering") || state.includes("waiting_for_operator") },
    { key: "composite", label: "6. Final Composite Video", done: state === "completed", active: state.includes("final_approval") || state.includes("exporting") },
  ];

  stages.forEach((st, idx) => {
    const node = el("div", "flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-medium transition");
    if (st.done) {
      node.className += " bg-emerald-500/15 border border-emerald-500/30 text-emerald-300";
      node.innerHTML = `<span>🟢</span> <span>${st.label}</span>`;
    } else if (st.active) {
      node.className += " bg-amber-500/20 border border-amber-500/40 text-amber-300 animate-pulse";
      node.innerHTML = `<span>🟡</span> <span>${st.label}</span>`;
    } else {
      node.className += " bg-slate-800/60 border border-slate-700/50 text-slate-500";
      node.innerHTML = `<span>⚪</span> <span>${st.label}</span>`;
    }
    container.appendChild(node);

    if (idx < stages.length - 1) {
      const arrow = el("span", "text-slate-600 font-bold text-sm select-none", "➔");
      container.appendChild(arrow);
    }
  });
}

function renderDossier(detail) {
  const box = document.getElementById("m-dossier");
  clear(box);

  const brief = detail.creative_brief || {};
  const dossier = detail.research_dossier || {};
  const hooks = brief.hooks || brief.hook || dossier.hooks || [];
  const claims = brief.verified_claims || brief.claims || dossier.verified_claims || dossier.claims || [];

  const addList = (label, items) => {
    if (!Array.isArray(items) || items.length === 0) return;
    box.appendChild(el("p", "text-[11px] uppercase tracking-wide text-slate-500", label));
    const ul = el("ul", "list-disc pl-5 space-y-0.5");
    for (const item of items) {
      const value = typeof item === "string" ? item : JSON.stringify(item);
      ul.appendChild(el("li", "text-xs text-slate-300", value.slice(0, 400)));
    }
    box.appendChild(ul);
  };

  if (brief.hook) box.appendChild(el("p", "text-sm text-slate-200", `Hook: ${brief.hook}`));
  if (brief.target_audience) box.appendChild(el("p", "text-xs text-slate-400", `Audience: ${brief.target_audience}`));
  if (brief.target_duration) box.appendChild(el("p", "text-xs text-slate-400", `Target duration: ${brief.target_duration}s`));
  addList("Hooks", hooks);
  addList("Verified claims", claims);

  if (box.childElementCount === 0) {
    box.appendChild(el("p", "text-xs text-slate-600", "No dossier captured yet."));
  }
}

function renderScenes(detail) {
  const grid = document.getElementById("m-scenes");
  clear(grid);

  const plan = detail.scene_plan || {};
  const storyboard = detail.storyboard || detail.latest_artifact || {};
  const scenes = plan.scenes || storyboard.scenes || storyboard.generated_keyframes || [];
  const sceneOps = Array.isArray(detail.scene_operations) ? detail.scene_operations : [];
  const opsByOrder = {};
  sceneOps.forEach(op => { opsByOrder[op.scene_order] = op; });

  if (!Array.isArray(scenes) || scenes.length === 0) {
    grid.appendChild(el("p", "text-xs text-slate-600 col-span-full", "No scenes yet."));
    return;
  }

  scenes.forEach((scene, index) => {
    const sceneOrder = index + 1;
    const op = opsByOrder[sceneOrder];
    const card = el("div", "rounded-xl bg-slate-900 border border-slate-700 p-3 space-y-2 flex flex-col justify-between");
    
    // Top media container (video if output exists or image keyframe)
    const mediaBox = el("div", "w-full rounded-lg overflow-hidden bg-slate-950 aspect-[9/16] relative flex items-center justify-center");
    
    if (op && op.output_path && op.status === "completed") {
      const video = document.createElement("video");
      video.className = "w-full h-full object-cover rounded-lg";
      video.src = `/api/artifacts/${encodeURIComponent(op.output_path)}`;
      video.controls = true;
      video.preload = "metadata";
      mediaBox.appendChild(video);
    } else {
      const image = scene.image_url || scene.keyframe_url || scene.thumbnail_url;
      if (image) {
        const img = el("img", "w-full h-full object-cover");
        img.src = image;
        img.alt = scene.scene_id || `Scene ${sceneOrder}`;
        img.loading = "lazy";
        mediaBox.appendChild(img);
      } else {
        mediaBox.appendChild(el("span", "text-xs text-slate-600 font-mono", `Scene ${sceneOrder}`));
      }
    }

    // Status pill
    if (op) {
      const pill = el("span", "absolute top-2 right-2 px-2 py-0.5 rounded text-[10px] font-bold shadow");
      if (op.status === "completed") pill.className += " bg-emerald-600 text-white";
      else if (op.status === "running" || op.status === "submitting") pill.className += " bg-blue-600 text-white animate-pulse";
      else if (op.status === "failed") pill.className += " bg-red-600 text-white";
      else pill.className += " bg-slate-700 text-slate-300";
      pill.textContent = `${op.status} (att ${op.attempt})`;
      mediaBox.appendChild(pill);
    }

    card.appendChild(mediaBox);

    // Metadata
    const metaBox = el("div", "space-y-1");
    const title = scene.scene_id || scene.frame_id || `Scene #${sceneOrder}`;
    const headerRow = el("div", "flex items-center justify-between");
    headerRow.appendChild(el("p", "text-xs font-bold text-slate-200", title));
    const duration = scene.duration || scene.duration_seconds;
    if (duration) headerRow.appendChild(el("span", "text-[10px] px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300", `${duration}s`));
    metaBox.appendChild(headerRow);

    if (scene.main_action) metaBox.appendChild(el("p", "text-[11px] text-slate-300 line-clamp-2", scene.main_action));
    if (scene.prompt) metaBox.appendChild(el("p", "text-[10px] text-slate-500 line-clamp-2 italic", scene.prompt));
    card.appendChild(metaBox);

    // Scene re-render action button
    const actionBtn = el("button", "w-full py-1.5 rounded-lg bg-slate-800 hover:bg-sky-600 text-slate-200 hover:text-white text-xs font-semibold transition flex items-center justify-center gap-1 border border-slate-700 hover:border-sky-500");
    actionBtn.innerHTML = `<span>🔄</span> <span>Tạo lại Scene #${sceneOrder}</span>`;
    actionBtn.onclick = () => promptSceneReRender(detail.workflow_id, sceneOrder, scene.prompt);
    card.appendChild(actionBtn);

    grid.appendChild(card);
  });
}

async function promptSceneReRender(workflowId, sceneOrder, currentPrompt) {
  const customPrompt = prompt(`Tạo lại Scene #${sceneOrder}\nNhập prompt điều chỉnh (hoặc để trống để dùng prompt gốc):`, currentPrompt || "");
  if (customPrompt === null) return; // User cancelled

  try {
    toast(`Đang yêu cầu re-render Scene #${sceneOrder}...`);
    const res = await api(`/api/operator/workflow/${encodeURIComponent(workflowId)}/scene/re-render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workflow_id: workflowId,
        scene_order: sceneOrder,
        custom_prompt: customPrompt || undefined,
      }),
    });
    toast(`✅ Đã kích hoạt re-render Scene #${sceneOrder}!`);
    await openWorkflow(workflowId);
  } catch (error) {
    toast(`Lỗi re-render: ${error.message}`, true);
  }
}

function verdictClass(verdict) {
  const value = String(verdict || "").toLowerCase();
  if (value === "pass") return "bg-emerald-500/15 text-emerald-300";
  if (value === "fail") return "bg-red-500/15 text-red-300";
  return "bg-amber-500/15 text-amber-300";
}

function renderQc(detail) {
  const grid = document.getElementById("m-qc");
  clear(grid);

  const reports = Array.isArray(detail.qc_reports) ? detail.qc_reports : [];
  const latest = {};
  for (const report of reports) latest[String(report.stage).toLowerCase()] = report;

  const wanted = ["image", "keyframe", "video"];
  const present = wanted.filter((stage) => latest[stage]);

  if (present.length === 0) {
    grid.appendChild(el("p", "text-xs text-slate-600 col-span-full", "No QC reports yet."));
    return;
  }

  for (const stage of present) {
    const report = latest[stage];
    const card = el("div", "rounded-lg bg-slate-800/60 border border-slate-700 p-3 space-y-1.5");
    card.appendChild(el("p", "text-xs uppercase tracking-wide text-slate-500", `${stage} QC`));
    card.appendChild(el("p", "text-2xl font-semibold", String(report.score ?? "—")));
    const badge = el("span", `text-[10px] px-2 py-0.5 rounded ${verdictClass(report.verdict)}`, String(report.verdict || "unknown"));
    card.appendChild(badge);

    const findings = report.findings || {};
    for (const [key, label] of [["resolution", "Resolution"], ["motion", "Motion"], ["artifacts", "Artifacts"]]) {
      const value = findings[key];
      if (value === undefined || value === null) continue;
      const rendered = typeof value === "object" ? JSON.stringify(value) : String(value);
      card.appendChild(el("p", "text-[11px] text-slate-400", `${label}: ${rendered.slice(0, 80)}`));
    }
    grid.appendChild(card);
  }
}

function renderApprovals(detail) {
  const box = el("div");
  const history = Array.isArray(detail.approval_history) ? detail.approval_history : [];
  if (history.length === 0) {
    box.appendChild(el("p", "text-xs text-slate-600", "No approvals recorded."));
  }
  for (const item of history) {
    box.appendChild(el("p", "", `${item.gate || "—"} · ${item.status} · ${fmtTime(item.created_at)}`));
  }
  const container = document.getElementById("m-approvals");
  clear(container);
  container.appendChild(box);
}

/* ---------- actions ---------- */

async function approve() {
  const detail = state.detail;
  if (!detail) return;
  const gate = detail.pending_approval_gate;
  if (!gate) { toast("No pending gate on this workflow", true); return; }
  try {
    await api("/api/operator/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow_id: detail.workflow_id, gate, action: "approve" }),
    });
    toast("Approved — advancing workflow");
    closeModal();
    await loadBoard();
  } catch (error) {
    toast(error.message, true);
  }
}

async function rework() {
  const feedback = document.getElementById("m-feedback").value.trim();
  if (!feedback) { toast("Rework requires feedback", true); return; }
  const detail = state.detail;
  if (!detail) return;
  try {
    await api("/api/operator/rework", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workflow_id: detail.workflow_id,
        gate: detail.pending_approval_gate,
        action: "rework",
        feedback,
      }),
    });
    toast("Rework requested");
    closeModal();
    await loadBoard();
  } catch (error) {
    toast(error.message, true);
  }
}

/* ---------- live SSE ---------- */

function connectStream() {
  const status = document.getElementById("status");
  const source = new EventSource("/api/operator/stream");

  source.onopen = () => {
    status.textContent = "live";
    status.className = "px-2 py-1 rounded bg-emerald-500/15 text-emerald-300";
  };
  source.onerror = () => {
    status.textContent = "reconnecting…";
    status.className = "px-2 py-1 rounded bg-amber-500/15 text-amber-300";
  };
  source.onmessage = (event) => {
    console.log("SSE event:", event.data);
    loadBoard().catch(() => {});
  };
}

/* ---------- bootstrap ---------- */

document.getElementById("refresh").addEventListener("click", () => loadBoard().catch((e) => toast(e.message, true)));
document.getElementById("m-close").addEventListener("click", closeModal);
document.getElementById("m-approve").addEventListener("click", approve);
document.getElementById("m-rework").addEventListener("click", rework);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModal(); });

loadBoard().catch((error) => toast(error.message, true));
connectStream();