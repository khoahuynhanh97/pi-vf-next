/**
 * Video Factory Studio — Main Application Controller
 * UI-Only. Vanilla ES Modules, zero external framework/CDN.
 */

import {
  loadCandidates,
  loadBoard,
  loadDetail,
  loadVideos,
  loadProductWorkflows,
  startBatch,
  approveGate,
  retryQuarantinedWorkflow,
  correctSourceInput,
  loadWorkflowActions,
  executeWorkflowAction,
  loadCommandReceipt,
  deleteWorkflows
} from "./studio-api.mjs";

import {
  flattenBoard,
  classifyMedia,
  extractImageUrl,
  computeCandidateFingerprint,
  readBatchReceipt,
  formatDateTime,
  sortListRows,
  shortenId,
  getColumnLabel,
  getCardStateLabel,
  canSelectCandidate,
  filterCandidates,
  filterBoardCards,
  buildPendingIntent,
  buildProductResults,
  validatePendingIntent
} from "./studio-model.mjs";

import { ProductList, WorkflowWorkspace, releaseImageDrafts, node } from "./studio-workspace.mjs";
import { productRows, currentStep } from "./studio-workspace-model.mjs";

// Global App State
const state = {
  currentRoute: "products",
  listFilters: { group: "attention", search: "", status: "all", step: "all", sort: "priority", page: 1 },
  selectedStep: null,
  authStopped: false,
  detailSignature: "",
  completedVideoChecks: new Set(),
  activeWorkflowId: null,
  activeDetailTab: "overview",
  routeGeneration: 0,

  // Cached data
  candidatesData: { source_date: "", candidates: [], loadedAt: null, error: null },
  boardData: { columns: {}, loadedAt: null, error: null },
  videosData: { videos: [], loadedAt: null, error: null },
  resultsData: { error: null },
  activeDetail: { workflow: null, loadedAt: null, error: null },

  // Filters & Selections
  selectedCandidateKeys: new Set(),
  tableSort: {},
  tableSelection: {},
  candidateSearch: "",
  candidateStatusFilter: "all",
  qcLevel: "high",
  disableKeyframeQC: false,
  targetDate: "",

  boardSearch: "",
  boardColumnFilter: "all",

  // Mutating lock & polling
  isMutating: false,
  pollTimer: null,
  pollBackoffStep: 0,
  activeAbortController: null
};

const STORAGE_PENDING_KEY = "vf-studio.pending-intent.v1";

// DOM Elements cache
const el = {
  appRoot: document.getElementById("app-root"),
  pageTitle: document.getElementById("page-title"),
  sourceIndicator: document.getElementById("source-indicator"),
  syncStatus: document.getElementById("sync-status"),
  statusBar: document.getElementById("status-bar"),
  btnRefresh: document.getElementById("btn-refresh"),

  navProducts: document.getElementById("nav-intake"),
  navResults: document.getElementById("nav-results"),
  navProduction: document.getElementById("nav-production"),
  navIssues: document.getElementById("nav-issues"),
  navVideos: document.getElementById("nav-videos"),

  badgeProducts: document.getElementById("badge-products"),
  badgeProduction: document.getElementById("badge-production"),
  badgeIssues: document.getElementById("badge-issues"),
  badgeVideos: document.getElementById("badge-videos"),

  detailModal: document.getElementById("detail-modal"),
  detailTitle: document.getElementById("detail-title"),
  detailWorkflowId: document.getElementById("detail-workflow-id"),
  detailBody: document.getElementById("detail-body"),
  detailFooterInfo: document.getElementById("detail-footer-info"),
  detailFooterActions: document.getElementById("detail-footer-actions"),
  btnCloseDetail: document.getElementById("btn-close-detail"),

  previewModal: document.getElementById("preview-modal"),
  previewTitle: document.getElementById("preview-title"),
  previewBody: document.getElementById("preview-body"),
  btnClosePreview: document.getElementById("btn-close-preview")
};

// ==========================================
// Notifications & Status Bar
// ==========================================
function showStatus(message, type = "info", permanent = false) {
  if (!el.statusBar) return;
  el.statusBar.textContent = message;
  el.statusBar.className = `vf-status-bar ${type}`;
  el.statusBar.style.display = "flex";
  if (!permanent) {
    setTimeout(() => {
      if (el.statusBar.textContent === message) {
        el.statusBar.style.display = "none";
      }
    }, 6000);
  }
}

function clearStatus() {
  if (!el.statusBar) return;
  el.statusBar.style.display = "none";
  el.statusBar.textContent = "";
}

function updateSyncTime(loadedAt, isStale = false) {
  if (!el.syncStatus) return;
  if (!loadedAt) {
    el.syncStatus.textContent = "Chưa có dữ liệu";
    return;
  }
  const timeStr = formatDateTime(loadedAt);
  el.syncStatus.textContent = isStale
    ? `Dữ liệu cũ lúc: ${timeStr} (Cần làm mới)`
    : `Dữ liệu tải lúc: ${timeStr}`;
}

// ==========================================
// Pending Intent & Web Lock Guard
// ==========================================
function hasUnresolvedPendingIntent() {
  try {
    const raw = sessionStorage.getItem(STORAGE_PENDING_KEY);
    if (!raw) return false;
    const intent = JSON.parse(raw);
    return validatePendingIntent(intent);
  } catch {
    return false;
  }
}

function checkSessionPendingIntent() {
  try {
    const raw = sessionStorage.getItem(STORAGE_PENDING_KEY);
    if (!raw) return;
    const intent = JSON.parse(raw);
    if (validatePendingIntent(intent)) {
      const targetsStr = intent.targets.slice(0, 3).join(", ") + (intent.targets.length > 3 ? "..." : "");
      showStatus(
        `Chưa xác định kết quả từ yêu cầu (${intent.kind} trên [${targetsStr}]) ở phiên trước. Vui lòng kiểm tra danh sách Sản xuất; hệ thống không tự gửi lại.`,
        "warning",
        true
      );
      if (intent.kind === 'command' && intent.requestId) {
        const check = document.createElement('button');
        check.type = 'button'; check.className = 'vf-btn vf-btn-secondary'; check.textContent = 'Kiểm tra receipt';
        check.addEventListener('click', async () => {
          check.disabled = true;
          try {
            const response = await loadCommandReceipt(window.fetch, intent.targets[0], intent.requestId);
            if (!response.ok) throw new Error(response.body?.detail || 'Không đọc được receipt');
            if (['accepted', 'rejected'].includes(response.body.state)) {
              const current = JSON.parse(sessionStorage.getItem(STORAGE_PENDING_KEY) || 'null');
              if (current?.requestId === intent.requestId) clearPendingIntent();
              showStatus(response.body.state === 'accepted' ? 'Server xác nhận đã tiếp nhận command. Xem tiến trình workflow; không cần gửi lại.' : `Server đã từ chối command: ${response.body.error}`, response.body.state === 'accepted' ? 'info' : 'warning', true);
            } else {
              check.textContent = 'Chưa rõ kết quả — kiểm tra lại';
            }
          } catch (error) { check.textContent = error.message; }
          finally { check.disabled = false; }
        });
        el.statusBar.appendChild(check);
      }
      if (intent.kind === 'delete') {
        const check=node('button','vf-btn vf-btn-secondary','Kiểm tra kết quả xóa'); check.type='button';
        check.addEventListener('click',async()=>{
          check.disabled=true;
          try {
            const checks=await Promise.all(intent.targets.map(id=>loadDetail(window.fetch,id)));
            if(checks.some(r=>!r.ok && r.status!==404)) throw new Error('Chưa đọc được trạng thái đầy đủ; kiểm tra lại sau.');
            clearPendingIntent();
            showStatus(`Đã kiểm tra: ${checks.filter(r=>r.status===404).length} workflow không còn, ${checks.filter(r=>r.ok).length} còn tồn tại. Làm mới và xem điều kiện trước khi chọn xóa tiếp.`, 'info',true);
          } catch(error) {check.textContent=error.message;check.disabled=false;}
        });el.statusBar.append(check);
      }
    }
  } catch (err) {
    console.error("Lỗi đọc pending intent:", err);
  }
}

function savePendingIntent(intent) {
  try {
    sessionStorage.setItem(STORAGE_PENDING_KEY, JSON.stringify(intent));
  } catch (err) {
    throw new Error("Không thể ghi intent vào sessionStorage: " + err.message);
  }
}

function clearPendingIntent() {
  try {
    sessionStorage.removeItem(STORAGE_PENDING_KEY);
  } catch (err) {
    console.error("Lỗi xoá pending intent:", err);
  }
}

// ==========================================
// Navigation & Routing
// ==========================================
function parseHashRoute() {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return { route: "products", workflowId: null };
  if (hash === "results") {
    return { route: "products", workflowId: null };
  }
  if (hash === "products") {
    return { route: "products", workflowId: null };
  }
  if (hash === "intake") return { route: "intake", workflowId: null };
  if (hash.startsWith("products/workflow/")) {
    try { return { route: "production", workflowId: decodeURIComponent(hash.slice("products/workflow/".length)) }; } catch { return { route: "products", workflowId: null }; }
  }
  if (hash === "production") {
    return { route: "production", workflowId: null };
  }
  if (hash.startsWith("production/workflow/")) {
    const rawId = hash.replace("production/workflow/", "");
    return { route: "production", workflowId: (() => { try { return decodeURIComponent(rawId); } catch { return null; } })() };
  }
  if (hash === "issues") {
    return { route: "issues", workflowId: null };
  }
  if (hash === "videos") {
    return { route: "videos", workflowId: null };
  }
  return { route: "products", workflowId: null };
}

function handleRoute() {
  stopPolling();
  const { route, workflowId } = parseHashRoute();
  state.currentRoute = route;
  state.activeWorkflowId = workflowId;
  state.routeGeneration += 1;

  // Update navigation visual state
  [el.navResults, el.navProducts, el.navProduction, el.navIssues, el.navVideos].forEach((item) => {
    if (!item) return;
    const itemRoute = item.getAttribute("data-route");
    item.classList.toggle("active", itemRoute === (workflowId ? "products" : route));
  });

  // Update Topbar
  if (route === "intake") {
    el.pageTitle.textContent = "Chọn & chạy batch";
    el.sourceIndicator.textContent = "Chọn sản phẩm từ PI để chạy qua flow hiện có";
  } else if (route === "results") {
    el.pageTitle.textContent = "Kết quả";
    el.sourceIndicator.textContent = "Trạng thái PI, workflow và MP4 đã kiểm tra";
  } else if (route === "products") {
    el.pageTitle.textContent = "Sản phẩm";
    el.sourceIndicator.textContent = state.candidatesData.source_date
      ? `Ngày nguồn: ${state.candidatesData.source_date}`
      : "Dữ liệu ứng viên theo ngày";
  } else if (route === "production") {
    el.pageTitle.textContent = "Sản xuất";
    el.sourceIndicator.textContent = "Workflow sản xuất video (giới hạn 100)";
  } else if (route === "issues") {
    el.pageTitle.textContent = "Cần xử lý";
    el.sourceIndicator.textContent = "Các workflow cần kiểm tra hoặc phê duyệt";
  } else if (route === "videos") {
    el.pageTitle.textContent = "Video hoàn tất";
    el.sourceIndicator.textContent = "MP4 qua kiểm tra kỹ thuật";
  }

  // Open one page and issue one read; no duplicate load/abort race.

  // Handle workflow modal if specified in hash
  if (workflowId) {
    openDetailModal(workflowId);
  } else {
    closeDetailModal(false);
    renderView();
    loadViewData();
  }

  // Schedule next poll
  scheduleNextPoll();
}

// ==========================================
// Data Fetching per View
// ==========================================
async function loadViewData(isBackgroundPoll = false) {
  if (isBackgroundPoll && state.authStopped) return;
  const currentGen = state.routeGeneration;
  const currentRoute = state.currentRoute;

  if (state.activeAbortController) {
    state.activeAbortController.abort();
  }
  state.activeAbortController = new AbortController();
  const signal = state.activeAbortController.signal;

  try {
    if (state.activeWorkflowId) { await refreshActiveDetail(signal); return; }
    if (currentRoute === "products") {
      let [candidateRes, boardRes, videoRes] = await Promise.all([
        (isBackgroundPoll && state.candidatesData.loadedAt)
          ? Promise.resolve({ ok: true, body: state.candidatesData })
          : loadCandidates(window.fetch, state.targetDate || null, signal, state.qcLevel),
        loadBoard(window.fetch, signal),
        isBackgroundPoll && state.videosData.loadedAt
          ? Promise.resolve({ ok: true, body: state.videosData }) : loadVideos(window.fetch, signal)
      ]);
      if (currentGen !== state.routeGeneration) return;
      // Final-media verification decodes files on the server. Recheck once when
      // a completed workflow first appears, never on every polling interval.
      const completed = flattenBoard(boardRes.body?.columns).filter(({ card }) => String(card.state).toLowerCase() === "completed").map(({ card }) => card.workflow_id);
      if (isBackgroundPoll && completed.some(id => !state.completedVideoChecks.has(id))) {
        videoRes = await loadVideos(window.fetch, signal);
        if (currentGen !== state.routeGeneration) return;
      }
      if (videoRes.ok) completed.forEach(id => state.completedVideoChecks.add(id));
      const errors = [];
      for (const response of [candidateRes, boardRes, videoRes]) {
        if (!response.ok) errors.push(response.body?.error || response.body?.detail || `Lỗi tải dữ liệu (${response.status})`);
        if (response.status === 401 || response.status === 403) {
          state.authStopped = true; stopPolling();
          showStatus(`Lỗi phân quyền (${response.status}). Đã dừng cập nhật tự động.`, "error", true);
        }
      }
      if (candidateRes.ok) {
        state.candidatesData = {
          source_date: candidateRes.body?.source_date || "",
          candidates: Array.isArray(candidateRes.body?.candidates) ? candidateRes.body.candidates : [],
          loadedAt: new Date(),
          error: null
        };
        if (!state.targetDate && state.candidatesData.source_date) state.targetDate = state.candidatesData.source_date;
      }
      if (boardRes.ok) {
        state.boardData = { columns: boardRes.body?.columns || {}, loadedAt: new Date(), error: null };
      }
      if (videoRes.ok) {
        state.videosData = {
          videos: Array.isArray(videoRes.body?.videos) ? videoRes.body.videos : [],
          loadedAt: new Date(),
          error: null
        };
      }
      state.resultsData.error = errors.join("; ");
      updateSyncTime(new Date());
      updateBadges();
      renderView();
    } else if (currentRoute === "intake") {
      if (!isBackgroundPoll || !state.candidatesData.loadedAt) {
        const res = await loadCandidates(window.fetch, state.targetDate || null, signal, state.qcLevel);
        if (currentGen !== state.routeGeneration) return;

        if (!res.ok) {
          if (res.status === 401 || res.status === 403) {
            state.authStopped = true; stopPolling();
            state.pollBackoffStep = 0;
            state.candidatesData.error = `Lỗi phân quyền (${res.status}): Truy cập bị từ chối.`;
            showStatus(`Lỗi phân quyền (${res.status}). Đã dừng cập nhật tự động.`, "error", true);
            renderView();
            return;
          }
          state.candidatesData.error = res.body?.error || `Lỗi tải sản phẩm (${res.status})`;
          renderView();
          state.pollBackoffStep += 1;
          scheduleNextPoll();
          return;
        }

        state.candidatesData = {
          source_date: res.body?.source_date || "",
          candidates: Array.isArray(res.body?.candidates) ? res.body.candidates : [],
          loadedAt: new Date(),
          error: null
        };
        if (!state.targetDate && state.candidatesData.source_date) {
          state.targetDate = state.candidatesData.source_date;
        }
        el.sourceIndicator.textContent = `Ngày nguồn: ${state.candidatesData.source_date || "—"}`;
        updateSyncTime(state.candidatesData.loadedAt);
        renderView();
      }
    } else if (currentRoute === "production" || currentRoute === "issues") {
      const res = await loadBoard(window.fetch, signal);
      if (currentGen !== state.routeGeneration) return;

      if (!res.ok) {
        if (res.status === 401 || res.status === 403) {
          state.authStopped = true; stopPolling();
          state.pollBackoffStep = 0;
          state.boardData.error = `Lỗi phân quyền (${res.status}): Truy cập bị từ chối.`;
          showStatus(`Lỗi phân quyền (${res.status}). Đã dừng cập nhật tự động.`, "error", true);
          renderView();
          return;
        }
        state.boardData.error = res.body?.error || `Lỗi tải danh sách sản xuất (${res.status})`;
        renderView();
        state.pollBackoffStep += 1;
        scheduleNextPoll();
        return;
      }

      state.boardData = {
        columns: res.body?.columns || {},
        loadedAt: new Date(),
        error: null
      };
      updateSyncTime(state.boardData.loadedAt);
      updateBadges();
      renderView();
    } else if (currentRoute === "videos") {
      if (!isBackgroundPoll || !state.videosData.loadedAt) {
        const res = await loadVideos(window.fetch, signal);
        if (currentGen !== state.routeGeneration) return;

        if (!res.ok) {
          if (res.status === 401 || res.status === 403) {
            state.authStopped = true; stopPolling();
            state.pollBackoffStep = 0;
            state.videosData.error = `Lỗi phân quyền (${res.status}): Truy cập bị từ chối.`;
            showStatus(`Lỗi phân quyền (${res.status}). Đã dừng cập nhật tự động.`, "error", true);
            renderView();
            return;
          }
          state.videosData.error = res.body?.error || `Lỗi tải danh sách video (${res.status})`;
          renderView();
          state.pollBackoffStep += 1;
          scheduleNextPoll();
          return;
        }

        state.videosData = {
          videos: Array.isArray(res.body?.videos) ? res.body.videos : [],
          loadedAt: new Date(),
          error: null
        };
        updateSyncTime(state.videosData.loadedAt);
        updateBadges();
        renderView();
      }
    }

    // If modal is open, refresh detail as well
    if (state.activeWorkflowId) {
      await refreshActiveDetail(signal);
    }

    state.pollBackoffStep = 0; // Reset backoff on success
  } catch (err) {
    if (err.name === "AbortError") return;
    console.error("Lỗi tải dữ liệu view:", err);

    if (err.status === 401 || err.status === 403) {
      state.authStopped = true; stopPolling();
      state.pollBackoffStep = 0;
      showStatus("Lỗi phân quyền (401/403). Đã dừng cập nhật tự động.", "error", true);
      return;
    }

    // Mark error in state
    if (currentRoute === "intake") state.candidatesData.error = err.message;
    if (currentRoute === "production" || currentRoute === "issues") state.boardData.error = err.message;
    if (currentRoute === "videos") state.videosData.error = err.message;
    if (currentRoute === "products") state.resultsData.error = err.message;

    renderView();
    state.pollBackoffStep += 1;
    scheduleNextPoll();
  }
}

async function refreshActiveDetail(signal = null) {
  const id = state.activeWorkflowId;
  const generation = state.routeGeneration;
  if (!id) return;
  try {
    const res = await loadDetail(window.fetch, id, signal);
    if (id !== state.activeWorkflowId || generation !== state.routeGeneration) return;
    if (res.status === 401 || res.status === 403) {
      state.authStopped = true; stopPolling();
      showStatus("Truy cập bị từ chối. Đã dừng cập nhật tự động.", "error", true);
    }
    if (!res.ok) {
      state.activeDetail.error = res.body?.detail || res.body?.error || `Lỗi tải chi tiết (${res.status})`;
      state.detailSignature = "";
      renderDetailModalBody(); return;
    }
    state.activeDetail = { workflow: res.body, loadedAt: new Date(), error: null };
    const signature = JSON.stringify(res.body);
    const mediaPlaying = [...el.detailBody.querySelectorAll("video")].some(v => !v.paused);
    const editing = el.detailBody.contains(document.activeElement) && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
    if (signature !== state.detailSignature && !mediaPlaying && !editing && !state.isMutating) {
      state.detailSignature = signature;
      renderDetailModalBody();
    }
    updateSyncTime(state.activeDetail.loadedAt);
  } catch (err) {
    if (err.name === "AbortError" || id !== state.activeWorkflowId || generation !== state.routeGeneration) return;
    state.activeDetail.error = err.message; state.detailSignature = "";
    state.pollBackoffStep += 1;
    renderDetailModalBody();
  }
}

function updateBadges() {
  const cards = flattenBoard(state.boardData.columns);
  const issues = cards.filter(({ column, card }) => {
    const s = String(card.state || "").toLowerCase();
    return column === "error" || s.includes("fail") || s.includes("waiting") || s.includes("awaiting");
  });

  if (el.badgeProduction) {
    el.badgeProduction.textContent = String(cards.length);
    el.badgeProduction.style.display = cards.length > 0 ? "inline-flex" : "none";
  }
  if (el.badgeIssues) {
    el.badgeIssues.textContent = String(issues.length);
    el.badgeIssues.style.display = issues.length > 0 ? "inline-flex" : "none";
  }
  if (el.badgeVideos && state.videosData.videos) {
    el.badgeVideos.textContent = String(state.videosData.videos.length);
    el.badgeVideos.style.display = state.videosData.videos.length > 0 ? "inline-flex" : "none";
  }
}

// ==========================================
// Unified Polling Scheduler (Single Authority)
// ==========================================
function stopPolling() {
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function scheduleNextPoll(forceDelayMs = null) {
  stopPolling();

  // 1. Hidden tab stops polling
  if (document.visibilityState === "hidden" || state.authStopped) return;

  // 2. Results and board views refresh so active products can progress visibly.
  const isBoardRoute = state.currentRoute === "production" || state.currentRoute === "issues" || state.currentRoute === "products";
  const isDetailOpen = Boolean(state.activeWorkflowId);
  if (!isBoardRoute && !isDetailOpen) return;

  // 3. Determine delay: respect backoff if in error state
  let delay = 0;
  if (forceDelayMs != null) {
    delay = forceDelayMs;
  } else if (state.pollBackoffStep > 0) {
    const backoffIntervals = [30000, 60000, 120000];
    delay = backoffIntervals[Math.min(state.pollBackoffStep - 1, backoffIntervals.length - 1)];
    showStatus(`Mất kết nối dữ liệu. Thử lại sau ${delay / 1000}s...`, "warning");
  } else if (isDetailOpen) {
    delay = 10000; // 10s for detail
  } else {
    delay = 15000; // 15s for board
  }

  state.pollTimer = setTimeout(async () => {
    await loadViewData(true);
    scheduleNextPoll();
  }, delay);
}

// Page visibility listener
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    stopPolling();
    if (state.activeAbortController) {
      state.activeAbortController.abort();
      state.activeAbortController = null;
    }
  } else {
    if (state.authStopped) return;
    // When tab becomes visible again, trigger single fresh GET then schedule next
    loadViewData(true).then(() => scheduleNextPoll());
  }
});

// ==========================================
// View Renderers
// ==========================================
function renderView() {
  if (!el.appRoot) return;
  if (state.activeWorkflowId) return;
  const focused = el.appRoot.contains(document.activeElement) ? document.activeElement : null;
  const focusKey = focused?.id || focused?.getAttribute('aria-label');
  const caret = focused?.selectionStart;
  el.appRoot.replaceChildren(); // Safely clear children

  if (state.currentRoute === "products") {
    renderProductWorkspace();
  } else if (state.currentRoute === "intake") {
    renderProductsView();
  } else if (state.currentRoute === "production") {
    renderProductionView();
  } else if (state.currentRoute === "issues") {
    renderIssuesView();
  } else if (state.currentRoute === "videos") {
    renderVideosView();
  }
  if (focusKey) {
    const next = [...el.appRoot.querySelectorAll('[id], [aria-label]')].find(n => n.id === focusKey || n.getAttribute('aria-label') === focusKey);
    next?.focus({ preventScroll: true });
    if (caret != null && next?.setSelectionRange) next.setSelectionRange(caret, caret);
  }
}

function renderProductWorkspace() {
  if (state.resultsData.error) {
    const error = document.createElement("p"); error.className = "vf-status-bar error";
    error.textContent = `Một số dữ liệu chưa tải được: ${state.resultsData.error}`;
    el.appRoot.append(error);
  }
  if (!state.candidatesData.loadedAt && !state.resultsData.error) {
    const loading = document.createElement("p"); loading.className = "vf-empty-state"; loading.textContent = "Đang tải sản phẩm…"; el.appRoot.append(loading); return;
  }
  const rows = productRows(state.candidatesData.candidates, state.boardData.columns, state.videosData.videos);
  el.appRoot.append(ProductList(rows, state.listFilters, (patch, preserveFocus = false) => {
    Object.assign(state.listFilters, patch);
    const input = document.getElementById("workspace-search");
    const caret = input?.selectionStart;
    renderView();
    if (preserveFocus) { const next = document.getElementById("workspace-search"); next?.focus(); if (caret != null) next?.setSelectionRange(caret, caret); }
  }));
}

function createSourceCorrectionForm(workflowId) {
  const panel = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "Sửa ảnh / thông tin đầu vào";
  panel.appendChild(summary);
  const form = document.createElement("form");
  const fields = {};
  for (const [key, label, multiline] of [
    ["images", "Đường dẫn ảnh trên máy chạy Studio (mỗi dòng một ảnh)", true],
    ["name", "Tên sản phẩm đã xác minh"], ["brand", "Thương hiệu"],
    ["model", "Model"], ["variant", "Variant / phiên bản"],
    ["reason", "Lý do sửa (bắt buộc)"]
  ]) {
    const wrapper = document.createElement("label");
    wrapper.textContent = label;
    const input = document.createElement(multiline ? "textarea" : "input");
    input.className = "vf-input";
    input.setAttribute("aria-label", label);
    if (key === "reason") { input.required = true; input.maxLength = 500; }
    wrapper.appendChild(input);
    form.appendChild(wrapper);
    fields[key] = input;
  }
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "vf-btn vf-btn-secondary vf-btn-sm";
  submit.textContent = "Lưu input đã sửa";
  const message = document.createElement("div");
  message.setAttribute("role", "status");
  form.append(submit, message);
  form.className = "vf-source-correction";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.disabled || state.isMutating) return;
    submit.disabled = true;
    try {
      const productInfo = {};
      for (const key of ["name", "brand", "model", "variant"]) {
        if (fields[key].value.trim()) productInfo[key] = fields[key].value.trim();
      }
      await onUploadSource(state.activeDetail.workflow, {
        image_paths: fields.images.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean),
        product_info: productInfo, reason: fields.reason.value.trim()
      });
    } catch (error) {
      message.textContent = error.resultUnknown
        ? "Chưa rõ kết quả lưu. Tải lại trạng thái trước khi thử tiếp."
        : error.message;
    } finally {
      state.isMutating = false;
      submit.disabled = false;
    }
  });
  panel.appendChild(form);
  return panel;
}

// View 1: Products
function renderProductsView() {
  const container = document.createElement("div");

  // Toolbar
  const toolbar = document.createElement("div");
  toolbar.className = "vf-toolbar";

  // Left toolbar: search & filter
  const left = document.createElement("div");
  left.className = "vf-toolbar-left";

  const dateInput = document.createElement("input");
  dateInput.type = "date";
  dateInput.className = "vf-input";
  dateInput.value = state.targetDate || "";
  dateInput.title = "Chọn ngày nguồn";
  dateInput.addEventListener("change", (e) => {
    state.targetDate = e.target.value;
    loadViewData();
  });
  left.appendChild(dateInput);

  const searchInput = document.createElement("input");
  searchInput.type = "text";
  searchInput.className = "vf-input";
  searchInput.placeholder = "Tìm theo tên, hãng, model...";
  searchInput.value = state.candidateSearch;
  searchInput.addEventListener("input", (e) => {
    state.candidateSearch = e.target.value;
    renderProductsTable(tableContainer, selectAllCheckbox, btnStart);
  });
  left.appendChild(searchInput);

  const statusSelect = document.createElement("select");
  statusSelect.className = "vf-select";
  const opts = [
    { value: "all", label: "Tất cả trạng thái" },
    { value: "unprocessed", label: "Chưa xử lý (unprocessed)" },
    { value: "valid", label: "Hợp lệ (valid)" },
    { value: "invalid", label: "Không hợp lệ (invalid)" }
  ];
  opts.forEach((o) => {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    if (state.candidateStatusFilter === o.value) opt.selected = true;
    statusSelect.appendChild(opt);
  });
  statusSelect.addEventListener("change", (e) => {
    state.candidateStatusFilter = e.target.value;
    renderProductsTable(tableContainer, selectAllCheckbox, btnStart);
  });
  left.appendChild(statusSelect);

  toolbar.appendChild(left);

  const qcLevel = document.createElement("select");
  qcLevel.className = "vf-select";
  qcLevel.setAttribute("aria-label", "Mức QC");
  for (const [value, label] of [["low", "QC thấp — kiểm tra hạn chế"], ["middle", "QC vừa (mặc định)"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = state.qcLevel === value;
    qcLevel.appendChild(option);
  }
  qcLevel.addEventListener("change", async () => {
    state.qcLevel = qcLevel.value;
    state.disableKeyframeQC = state.qcLevel === "low" && state.disableKeyframeQC;
    if (state.qcLevel !== "low") state.disableKeyframeQC = false;
    const refreshed = await loadCandidates(window.fetch, state.targetDate || null, null, state.qcLevel);
    if (refreshed.ok) {
      state.candidatesData = { source_date: refreshed.body?.source_date || "",
        candidates: refreshed.body?.candidates || [], loadedAt: new Date(), error: null };
    }
    renderView();
  });
  left.appendChild(qcLevel);
  if (state.qcLevel === "low") {
    const disableQcLabel = document.createElement("label");
    const disableQc = document.createElement("input");
    disableQc.type = "checkbox";
    disableQc.checked = state.disableKeyframeQC;
    disableQc.addEventListener("change", () => { state.disableKeyframeQC = disableQc.checked; });
    disableQcLabel.append(disableQc, document.createTextNode(" Tắt QC keyframe"));
    left.appendChild(disableQcLabel);
  }

  // Right toolbar: Start batch button
  const right = document.createElement("div");
  right.className = "vf-toolbar-right";

  const btnStart = document.createElement("button");
  btnStart.type = "button";
  btnStart.className = "vf-btn vf-btn-primary";
  btnStart.id = "btn-start-batch";
  btnStart.textContent = `Chạy (${state.selectedCandidateKeys.size})`;
  btnStart.disabled = state.selectedCandidateKeys.size === 0 || state.isMutating;
  btnStart.addEventListener("click", onStartBatchClick);
  right.appendChild(btnStart);

  toolbar.appendChild(right);
  container.appendChild(toolbar);

  // Table Container
  const tableContainer = document.createElement("div");
  tableContainer.className = "vf-table-container";

  // Select all checkbox header
  const selectAllCheckbox = document.createElement("input");
  selectAllCheckbox.type = "checkbox";

  container.appendChild(tableContainer);
  el.appRoot.appendChild(container);

  renderProductsTable(tableContainer, selectAllCheckbox, btnStart);
}

function renderProductsTable(container, selectAllCheckbox, btnStart) {
  container.replaceChildren();

  if (state.candidatesData.error) {
    const errBox = document.createElement("div");
    errBox.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = `Lỗi tải danh sách sản phẩm: ${state.candidatesData.error}`;
    errBox.appendChild(p);
    container.appendChild(errBox);
    return;
  }

  const rawList = state.candidatesData.candidates;
  const filtered = sortedTableRows('intake',filterCandidates(rawList, state.candidateSearch, state.candidateStatusFilter).map(c=>({...c,source_date:c.source_date || state.candidatesData.source_date})));

  if (filtered.length === 0) {
    const emptyBox = document.createElement("div");
    emptyBox.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = rawList.length === 0 ? "Không có sản phẩm nào cho ngày này." : "Không có kết quả khớp với bộ lọc.";
    emptyBox.appendChild(p);
    container.appendChild(emptyBox);
    return;
  }

  const table = document.createElement("table");
  table.className = "vf-table";

  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");

  const thCheck = document.createElement("th");
  thCheck.style.width = "40px";

  // Sync select all state
  const validVisibleKeys = filtered
    .filter(canSelectCandidate)
    .map((c) => c.product_key);

  selectAllCheckbox.checked =
    validVisibleKeys.length > 0 && validVisibleKeys.every((k) => state.selectedCandidateKeys.has(k));
  selectAllCheckbox.onchange = (e) => {
    if (e.target.checked) {
      validVisibleKeys.forEach((k) => state.selectedCandidateKeys.add(k));
    } else {
      validVisibleKeys.forEach((k) => state.selectedCandidateKeys.delete(k));
    }
    btnStart.textContent = `Chạy (${state.selectedCandidateKeys.size})`;
    btnStart.disabled = state.selectedCandidateKeys.size === 0 || state.isMutating;
    renderProductsTable(container, selectAllCheckbox, btnStart);
  };
  thCheck.appendChild(selectAllCheckbox);
  headerRow.appendChild(thCheck);

  const thImg = document.createElement("th");
  thImg.textContent = "Ảnh";
  thImg.style.width = "60px";
  headerRow.appendChild(thImg);

  tableHeaders(headerRow,'intake',[['name','Tên sản phẩm'],['brand','Hãng / Model'],['status','Trạng thái'],['rejection_reason','Ghi chú / Lý do'],['source_date','Ngày nguồn PI'],[null,'Thao tác']],()=>renderProductsTable(container,selectAllCheckbox,btnStart));

  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  filtered.forEach((c) => {
    const row = document.createElement("tr");
    const isSelectable = canSelectCandidate(c);
    const isInvalid = String(c.status || "").toLowerCase() === "invalid";

    // Checkbox
    const tdCheck = document.createElement("td");
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.value = c.product_key;
    chk.checked = state.selectedCandidateKeys.has(c.product_key);
    chk.disabled = !isSelectable || state.isMutating;
    chk.addEventListener("change", (e) => {
      if (e.target.checked) {
        state.selectedCandidateKeys.add(c.product_key);
      } else {
        state.selectedCandidateKeys.delete(c.product_key);
      }
      btnStart.textContent = `Chạy (${state.selectedCandidateKeys.size})`;
      btnStart.disabled = state.selectedCandidateKeys.size === 0 || state.isMutating;
      selectAllCheckbox.checked =
        validVisibleKeys.length > 0 && validVisibleKeys.every((k) => state.selectedCandidateKeys.has(k));
    });
    tdCheck.appendChild(chk);
    row.appendChild(tdCheck);

    // Image: supports object with asset_url/original_url or legacy string
    const tdImg = document.createElement("td");
    if (Array.isArray(c.images) && c.images.length > 0) {
      const rawUrl = extractImageUrl(c.images[0]);
      const media = classifyMedia(rawUrl, window.location.origin);
      if (media.kind === "same-origin" && media.url) {
        const img = document.createElement("img");
        img.src = media.url;
        img.alt = "Ảnh sản phẩm";
        img.style.width = "40px";
        img.style.height = "40px";
        img.style.objectFit = "cover";
        img.style.borderRadius = "4px";
        tdImg.appendChild(img);
      } else {
        tdImg.textContent = "—";
      }
    } else {
      tdImg.textContent = "—";
    }
    row.appendChild(tdImg);

    // Product Name (textContent for XSS safety!)
    const tdName = document.createElement("td");
    const nameSpan = document.createElement("span");
    nameSpan.style.fontWeight = "600";
    nameSpan.textContent = c.name || "Không có tên";
    tdName.appendChild(nameSpan);
    row.appendChild(tdName);

    // Brand / Model
    const tdBrand = document.createElement("td");
    tdBrand.textContent = [c.brand || "—", c.model || "—", c.variant].filter(Boolean).join(" / ");
    row.appendChild(tdBrand);

    // Status
    const tdStatus = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `vf-badge ${isInvalid ? "vf-badge-alert" : "vf-badge-info"}`;
    badge.textContent = getCardStateLabel(c.status || "unprocessed");
    tdStatus.appendChild(badge);
    row.appendChild(tdStatus);

    // Rejection reason
    const tdReason = document.createElement("td");
    tdReason.textContent = c.rejection_reason || "—";
    if (c.rejection_reason) tdReason.style.color = "var(--vf-danger)";
    row.appendChild(tdReason);
    row.append(node('td','',c.source_date || '—'));

    // Actions (Product History)
    const tdAction = document.createElement("td");
    const btnHistory = document.createElement("button");
    btnHistory.type = "button";
    btnHistory.className = "vf-btn vf-btn-secondary vf-btn-sm";
    btnHistory.textContent = "Lịch sử video";
    btnHistory.addEventListener("click", () => showProductWorkflows(c.product_key, c.name));
    tdAction.appendChild(btnHistory);
    if (c.status === "blocked" && c.workflow_id) tdAction.appendChild(createSourceCorrectionForm(c.workflow_id));
    row.appendChild(tdAction);

    tbody.appendChild(row);
  });

  table.appendChild(tbody);
  container.appendChild(table);
}

// View 2: Production (Kanban/Table)
function sortedTableRows(scope, rows) {
  const sort = state.tableSort[scope] || {key:scope === 'intake' ? 'source_date' : 'updated_at', direction:'desc'};
  return sortListRows(rows, sort.key, sort.direction);
}

function tableHeaders(row, scope, columns, rerender) {
  const sort = state.tableSort[scope] || {key:scope === 'intake' ? 'source_date' : 'updated_at', direction:'desc'};
  for (const [key,label] of columns) {
    const th = node('th');
    if (key) {
      th.setAttribute('aria-sort', sort.key === key ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none');
      const b = node('button','vf-sort-button', `${label}${sort.key === key ? sort.direction === 'asc' ? ' ↑' : ' ↓' : ' ↕'}`);
      b.type='button'; b.addEventListener('click',()=>{
        state.tableSort[scope]={key,direction:sort.key === key && sort.direction === 'asc' ? 'desc' : sort.key === key ? 'asc' : /_at$|_date$/.test(key) ? 'desc' : 'asc'};
        rerender();
      }); th.append(b);
    } else th.textContent=label;
    row.append(th);
  }
}

function tableBulk(container, header, scope, rows) {
  const selection=state.tableSelection[scope] ||= new Map();
  const toolbar=node('div','vf-bulk-toolbar');
  const summary=node('span','vf-secondary');
  const remove=node('button','vf-btn vf-btn-danger'); remove.type='button';
  remove.addEventListener('click',()=>deleteDialog([...selection.values()]));
  const clear=node('button','vf-btn vf-btn-secondary','Bỏ chọn'); clear.type='button';
  const master=node('input'); master.type='checkbox'; master.setAttribute('aria-label','Chọn tất cả hàng đang hiển thị');
  const checkboxes=[];
  const eligible=rows.filter(r=>r.workflow_id && Number.isInteger(r.version));
  function update() {
    summary.textContent=`Đã chọn ${selection.size} · Chọn tất cả chỉ áp dụng các hàng đang hiển thị`;
    remove.textContent=`Xóa vĩnh viễn (${selection.size})`; remove.disabled=!selection.size || state.isMutating;
    const n=eligible.filter(r=>selection.has(r.workflow_id)).length;
    master.checked=eligible.length>0 && n===eligible.length; master.indeterminate=n>0 && n<eligible.length; master.disabled=!eligible.length;
    for(const [box,item] of checkboxes) box.checked=selection.has(item.workflow_id);
  }
  clear.addEventListener('click',()=>{selection.clear();update();});
  master.addEventListener('change',()=>{for(const item of eligible) master.checked ? selection.set(item.workflow_id,item) : selection.delete(item.workflow_id);update();});
  toolbar.append(summary,clear,remove);container.append(toolbar);
  const th=node('th');th.append(master);header.append(th);update();
  return (tr,item)=>{
    const td=node('td');const box=node('input');box.type='checkbox';box.disabled=!eligible.includes(item);
    if(box.disabled) box.title='Thiếu phiên bản workflow; làm mới dữ liệu trước khi chọn xóa.';
    box.setAttribute('aria-label',`Chọn ${item.product_name || item.workflow_id}`);
    box.addEventListener('change',()=>{box.checked ? selection.set(item.workflow_id,item) : selection.delete(item.workflow_id);update();});
    checkboxes.push([box,item]);td.append(box);tr.append(td);update();
  };
}

function deleteDialog(rows) {
  const dialog=node('dialog','vf-rerun-dialog');dialog.setAttribute('aria-label','Xác nhận xóa vĩnh viễn');
  const list=node('ul');for(const r of rows) list.append(node('li','',r.product_name || r.workflow_id));
  const text=node('input','vf-input');text.setAttribute('aria-label','Nhập XÓA VĨNH VIỄN');text.autocomplete='off';
  const feedback=node('div','vf-upload-feedback');feedback.setAttribute('role','status');
  const confirm=node('button','vf-btn vf-btn-danger','Xóa vĩnh viễn');confirm.type='button';confirm.disabled=true;
  const cancel=node('button','vf-btn vf-btn-secondary','Hủy');cancel.type='button';cancel.addEventListener('click',()=>dialog.close());
  text.addEventListener('input',()=>confirm.disabled=text.value!=='XÓA VĨNH VIỄN');
  let running=false;
  confirm.addEventListener('click',async()=>{
    if(running || state.isMutating || hasUnresolvedPendingIntent()) {feedback.textContent='Còn yêu cầu chưa rõ kết quả; kiểm tra trạng thái trước.';return;}
    running=true;state.isMutating=true;confirm.disabled=true;cancel.disabled=true;text.disabled=true;
    savePendingIntent(buildPendingIntent('delete',rows.map(r=>r.workflow_id)));
    try {
      const res=await deleteWorkflows(window.fetch,rows.map(r=>({workflow_id:r.workflow_id,expected_version:r.version})),text.value);
      if(res.status>=500) throw Object.assign(new Error('Chưa rõ kết quả xóa. Kiểm tra trạng thái; không tự gửi lại.'),{resultUnknown:true});
      clearPendingIntent();if(!res.ok) throw new Error(res.body?.detail || 'Không thể xóa');
      const results=res.body.results || [];
      const count=results.filter(r=>['deleted','already_deleted'].includes(r.status)).length;
      feedback.replaceChildren(node('p','',`Đã xóa ${count}/${rows.length}.`));
      for(const result of results) {
        const name=rows.find(r=>r.workflow_id===result.workflow_id)?.product_name || result.workflow_id;
        const line=node('p',result.status==='blocked' ? 'vf-command-error' : '',`${name}: ${result.reason || 'Đã xóa'}${result.deleted_files?.length ? ` · ${result.deleted_files.length} file đã xóa` : ''}${result.shared_files_preserved?.length ? ` · giữ ${result.shared_files_preserved.length} file dùng chung` : ''}`);
        feedback.append(line);
        if(result.status!=='blocked') for(const selection of Object.values(state.tableSelection)) selection.delete(result.workflow_id);
      }
      confirm.hidden=true;showStatus(`Đã xóa ${count}/${rows.length} workflow. Kết quả từng hàng nằm trong hộp thoại.`,count===rows.length?'info':'warning',true);
    } catch(error) {feedback.classList.add('vf-command-error');feedback.setAttribute('role','alert');feedback.textContent=error.message;confirm.hidden=true;}
    finally {running=false;state.isMutating=false;cancel.disabled=false;cancel.textContent='Đóng';await loadViewData();}
  });
  dialog.addEventListener('cancel',e=>{if(running)e.preventDefault();});dialog.addEventListener('close',()=>dialog.remove(),{once:true});
  dialog.append(node('h2','',`Xóa vĩnh viễn ${rows.length} workflow?`),node('p','','Xóa workflow và file sinh ra trong workspace, không thể hoàn tác. Giữ nguồn PI, file dùng chung và sổ chi phí/operation để bảo đảm đối soát. Item có job hoặc outcome chưa rõ sẽ bị chặn. Nếu lỗi file giữa chừng, xem số file đã xóa trong kết quả.'),list,node('label','','Nhập XÓA VĨNH VIỄN để xác nhận'),text,feedback,confirm,cancel);
  document.body.append(dialog);dialog.showModal();
}

function renderProductionView() {
  const container = document.createElement("div");

  const toolbar = document.createElement("div");
  toolbar.className = "vf-toolbar";

  const left = document.createElement("div");
  left.className = "vf-toolbar-left";

  const searchInput = document.createElement("input");
  searchInput.type = "text";
  searchInput.className = "vf-input";
  searchInput.placeholder = "Tìm theo workflow ID, tên sản phẩm...";
  searchInput.value = state.boardSearch;
  searchInput.addEventListener("input", (e) => {
    state.boardSearch = e.target.value;
    renderBoardTable(tableContainer);
  });
  left.appendChild(searchInput);

  const colSelect = document.createElement("select");
  colSelect.className = "vf-select";
  const allCards = flattenBoard(state.boardData.columns);
  const distinctCols = Array.from(new Set(allCards.map((c) => c.column)));

  const defaultOpt = document.createElement("option");
  defaultOpt.value = "all";
  defaultOpt.textContent = `Tất cả cột (${allCards.length})`;
  colSelect.appendChild(defaultOpt);

  distinctCols.forEach((col) => {
    const opt = document.createElement("option");
    opt.value = col;
    opt.textContent = getColumnLabel(col);
    if (state.boardColumnFilter === col) opt.selected = true;
    colSelect.appendChild(opt);
  });
  colSelect.addEventListener("change", (e) => {
    state.boardColumnFilter = e.target.value;
    renderBoardTable(tableContainer);
  });
  left.appendChild(colSelect);
  toolbar.appendChild(left);

  const right = document.createElement("div");
  right.className = "vf-toolbar-right";
  const countLabel = document.createElement("span");
  countLabel.className = "vf-footer-label";
  countLabel.textContent = `${allCards.length} workflow trong dữ liệu đã tải (tối đa 100 theo API)`;
  right.appendChild(countLabel);
  toolbar.appendChild(right);

  container.appendChild(toolbar);

  const tableContainer = document.createElement("div");
  tableContainer.className = "vf-table-container";
  container.appendChild(tableContainer);

  el.appRoot.appendChild(container);

  renderBoardTable(tableContainer);
}

function renderBoardTable(container) {
  container.replaceChildren();

  if (state.boardData.error) {
    const errBox = document.createElement("div");
    errBox.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = `Lỗi tải danh sách sản xuất: ${state.boardData.error}`;
    errBox.appendChild(p);
    container.appendChild(errBox);
    return;
  }

  const allCards = flattenBoard(state.boardData.columns);
  const filtered = sortedTableRows('production', filterBoardCards(allCards, state.boardSearch, state.boardColumnFilter).map(({column,card})=>({...card,column})));

  if (filtered.length === 0) {
    const emptyBox = document.createElement("div");
    emptyBox.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = allCards.length === 0 ? "Chưa có workflow nào đang sản xuất." : "Không có kết quả khớp bộ lọc.";
    emptyBox.appendChild(p);
    container.appendChild(emptyBox);
    return;
  }

  const table = document.createElement("table");
  table.className = "vf-table";

  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");

  const selectRow = tableBulk(container,headerRow,'production',filtered);
  tableHeaders(headerRow,'production',[['workflow_id','Workflow ID'],['product_name','Sản phẩm'],['state','Trạng thái'],['column','Cột Kanban'],['version','Phiên bản'],['created_at','Thời gian tạo'],['updated_at','Cập nhật (Bangkok)'],[null,'Thao tác']],()=>renderBoardTable(container));
  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  filtered.forEach((card) => {
    const column=card.column;
    const row = document.createElement("tr");
    selectRow(row,card);

    // Workflow ID
    const tdId = document.createElement("td");
    const idCode = document.createElement("span");
    idCode.className = "vf-code-badge";
    idCode.textContent = shortenId(card.workflow_id);
    idCode.title = card.workflow_id;
    tdId.appendChild(idCode);
    row.appendChild(tdId);

    // Product Name
    const tdName = document.createElement("td");
    tdName.textContent = card.product_name || "—";
    tdName.style.fontWeight = "500";
    row.appendChild(tdName);

    // State
    const tdState = document.createElement("td");
    const stateBadge = document.createElement("span");
    const isErr = column === "error" || String(card.state || "").includes("fail");
    stateBadge.className = `vf-badge ${isErr ? "vf-badge-alert" : "vf-badge-info"}`;
    stateBadge.textContent = `${getCardStateLabel(card.state)} (${card.state || "—"})`;
    tdState.appendChild(stateBadge);
    row.appendChild(tdState);

    // Column
    const tdCol = document.createElement("td");
    tdCol.textContent = getColumnLabel(column);
    row.appendChild(tdCol);

    // Version
    const tdVer = document.createElement("td");
    tdVer.textContent = card.version != null ? `v${card.version}` : "—";
    row.appendChild(tdVer);

    // Created At
    const tdTime = document.createElement("td");
    tdTime.textContent = formatDateTime(card.created_at);
    row.appendChild(tdTime);
    row.append(node('td','',formatDateTime(card.updated_at)));

    // Action: Open Detail
    const tdAction = document.createElement("td");
    const btnDetail = document.createElement("button");
    btnDetail.type = "button";
    btnDetail.className = "vf-btn vf-btn-secondary vf-btn-sm";
    btnDetail.textContent = "Chi tiết";
    btnDetail.addEventListener("click", () => {
      window.location.hash = `#production/workflow/${encodeURIComponent(card.workflow_id)}`;
    });
    tdAction.appendChild(btnDetail);
    row.appendChild(tdAction);

    tbody.appendChild(row);
  });

  table.appendChild(tbody);
  container.appendChild(table);
}

// View 3: Issues / Need Attention
function renderIssuesView() {
  const container = document.createElement("div");

  const notice = document.createElement("div");
  notice.className = "vf-notice-box";
  notice.style.marginBottom = "20px";
  notice.textContent =
    "Hiển thị các workflow có trạng thái lỗi hoặc đang chờ duyệt từ dữ liệu Kanban hiện có (giới hạn 100 theo API). API chưa cung cấp heartbeat riêng của worker.";
  container.appendChild(notice);

  const tableContainer = document.createElement("div");
  tableContainer.className = "vf-table-container";

  if (state.boardData.error) {
    const errBox = document.createElement("div");
    errBox.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = `Lỗi tải dữ liệu cần xử lý: ${state.boardData.error}`;
    errBox.appendChild(p);
    tableContainer.appendChild(errBox);
    container.appendChild(tableContainer);
    el.appRoot.appendChild(container);
    return;
  }

  const allCards = flattenBoard(state.boardData.columns);
  const issueCards = sortedTableRows('issues',allCards.filter(({ column, card }) => {
    const s = String(card.state || "").toLowerCase();
    return column === "error" || s.includes("fail") || s.includes("waiting") || s.includes("awaiting");
  }).map(({column,card})=>({...card,column})));

  if (issueCards.length === 0) {
    const empty = document.createElement("div");
    empty.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = "Không có workflow nào cần can thiệp.";
    empty.appendChild(p);
    tableContainer.appendChild(empty);
  } else {
    const table = document.createElement("table");
    table.className = "vf-table";

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    const selectRow=tableBulk(tableContainer,headerRow,'issues',issueCards);
    tableHeaders(headerRow,'issues',[['workflow_id','Workflow ID'],['product_name','Sản phẩm'],['column','Loại cần xử lý'],['state','Trạng thái'],['created_at','Thời gian tạo'],['updated_at','Cập nhật (Bangkok)'],[null,'Thao tác']],renderView);
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    issueCards.forEach((card) => {
      const column=card.column;
      const row = document.createElement("tr");
      selectRow(row,card);

      const tdId = document.createElement("td");
      const idCode = document.createElement("span");
      idCode.className = "vf-code-badge";
      idCode.textContent = shortenId(card.workflow_id);
      tdId.appendChild(idCode);
      row.appendChild(tdId);

      const tdName = document.createElement("td");
      tdName.textContent = card.product_name || "—";
      row.appendChild(tdName);

      const tdKind = document.createElement("td");
      const s = String(card.state || "").toLowerCase();
      const badge = document.createElement("span");
      if (s.includes("awaiting") || column.includes("awaiting")) {
        badge.className = "vf-badge vf-badge-warning";
        badge.textContent = "Chờ phê duyệt";
      } else {
        badge.className = "vf-badge vf-badge-alert";
        badge.textContent = "Lỗi / Cần kiểm tra";
      }
      tdKind.appendChild(badge);
      row.appendChild(tdKind);

      const tdState = document.createElement("td");
      tdState.textContent = card.state || "—";
      row.appendChild(tdState);
      row.append(node('td','',formatDateTime(card.created_at)),node('td','',formatDateTime(card.updated_at)));

      const tdAction = document.createElement("td");
      const btnDetail = document.createElement("button");
      btnDetail.type = "button";
      btnDetail.className = "vf-btn vf-btn-secondary vf-btn-sm";
      btnDetail.textContent = "Mở xử lý";
      btnDetail.addEventListener("click", () => {
        window.location.hash = `#production/workflow/${encodeURIComponent(card.workflow_id)}`;
      });
      tdAction.appendChild(btnDetail);
      row.appendChild(tdAction);

      tbody.appendChild(row);
    });

    table.appendChild(tbody);
    tableContainer.appendChild(table);
  }

  container.appendChild(tableContainer);
  el.appRoot.appendChild(container);
}

// View 4: Completed Videos
function renderVideosView() {
  const container = document.createElement("div");

  const notice = document.createElement("div");
  notice.className = "vf-notice-box";
  notice.style.marginBottom = "20px";
  notice.textContent =
    "Chỉ MP4 tồn tại, giải mã được, có frame/duration hợp lệ và SHA khớp mới được ghi nhận thành công. Bạn tự xem nội dung video.";
  container.appendChild(notice);

  const tableContainer = document.createElement("div");
  tableContainer.className = "vf-table-container";

  if (state.videosData.error) {
    const errBox = document.createElement("div");
    errBox.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = `Lỗi tải danh sách video: ${state.videosData.error}`;
    errBox.appendChild(p);
    tableContainer.appendChild(errBox);
    container.appendChild(tableContainer);
    el.appRoot.appendChild(container);
    return;
  }

  const list = sortedTableRows('videos',state.videosData.videos.map(v=>({...v,updated_at:v.updated_at || v.completed_at || v.created_at})));
  if (list.length === 0) {
    const emptyBox = document.createElement("div");
    emptyBox.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = "Chưa có video hoàn tất nào.";
    emptyBox.appendChild(p);
    tableContainer.appendChild(emptyBox);
  } else {
    const table = document.createElement("table");
    table.className = "vf-table";

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    const selectRow=tableBulk(tableContainer,headerRow,'videos',list);
    tableHeaders(headerRow,'videos',[['workflow_id','Workflow ID'],['product_name','Sản phẩm'],['result_status','Kiểm tra MP4'],[null,'Đường dẫn / Video'],['completed_at','Hoàn tất (Bangkok)'],['updated_at','Cập nhật'],[null,'Thao tác']],renderView);
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");

    list.forEach((v) => {
      const row = document.createElement("tr");
      selectRow(row,v);

      const tdId = document.createElement("td");
      const idCode = document.createElement("span");
      idCode.className = "vf-code-badge";
      idCode.textContent = shortenId(v.workflow_id);
      tdId.appendChild(idCode);
      row.appendChild(tdId);

      const tdName = document.createElement("td");
      tdName.textContent = v.product_name || "—";
      row.appendChild(tdName);

      const tdQc = document.createElement("td");
      const qcBadge = document.createElement("span");
      const technicalFailure = v.result_status === "technical_error";
      qcBadge.className = `vf-badge ${technicalFailure ? "vf-badge-alert" : "vf-badge-success"}`;
      qcBadge.textContent = technicalFailure ? "Lỗi kỹ thuật" : "Đạt kỹ thuật";
      tdQc.appendChild(qcBadge);
      row.appendChild(tdQc);

      const tdMedia = document.createElement("td");
      if (technicalFailure) {
        tdMedia.textContent = v.technical_error || "MP4 không qua kiểm tra kỹ thuật.";
        row.appendChild(tdMedia);
        row.append(node('td','',formatDateTime(v.completed_at)),node('td','',formatDateTime(v.updated_at)));
        const tdErrorAction = document.createElement("td");
        tdErrorAction.textContent = "Mở chi tiết workflow để xử lý";
        row.appendChild(tdErrorAction);
        tbody.appendChild(row);
        return;
      }
      const media = classifyMedia(v.video_url, window.location.origin);

      if (media.kind === "local") {
        const span = document.createElement("div");
        span.className = "vf-local-path";
        span.textContent = v.video_url;
        const note = document.createElement("div");
        note.style.fontSize = "11px";
        note.style.color = "var(--vf-text-dim)";
        note.textContent = "(Đường dẫn trên máy chạy — chưa có URL xem qua web)";
        tdMedia.appendChild(span);
        tdMedia.appendChild(note);
      } else if (media.kind === "same-origin" || media.kind === "external") {
        const link = document.createElement("span");
        link.textContent = media.url;
        link.className = "vf-local-path";
        tdMedia.appendChild(link);
      } else {
        tdMedia.textContent = "—";
      }
      row.appendChild(tdMedia);

      row.append(node('td','',formatDateTime(v.completed_at)),node('td','',formatDateTime(v.updated_at)));
      const tdAction = document.createElement("td");
      if (media.kind === "same-origin" && media.url) {
        const btnPreview = document.createElement("button");
        btnPreview.type = "button";
        btnPreview.className = "vf-btn vf-btn-secondary vf-btn-sm";
        btnPreview.textContent = "Xem video";
        btnPreview.addEventListener("click", () => openPreviewModal(v.product_name || "Video", media.url));
        tdAction.appendChild(btnPreview);
      } else if (media.kind === "external" && media.url) {
        const btnLink = document.createElement("a");
        btnLink.className = "vf-btn vf-btn-secondary vf-btn-sm";
        btnLink.href = media.url;
        btnLink.target = "_blank";
        btnLink.rel = "noopener noreferrer";
        btnLink.textContent = "Mở liên kết ngoài";
        tdAction.appendChild(btnLink);
      } else if (media.kind === "local") {
        const btnCopy = document.createElement("button");
        btnCopy.type = "button";
        btnCopy.className = "vf-btn vf-btn-secondary vf-btn-sm";
        btnCopy.textContent = "Sao chép path";
        btnCopy.addEventListener("click", () => {
          navigator.clipboard.writeText(v.video_url);
          showStatus("Đã sao chép đường dẫn file!", "info");
        });
        tdAction.appendChild(btnCopy);
      }
      row.appendChild(tdAction);

      tbody.appendChild(row);
    });

    table.appendChild(tbody);
    tableContainer.appendChild(table);
  }

  container.appendChild(tableContainer);
  el.appRoot.appendChild(container);
}

// ==========================================
// Workflow Detail Modal & Tabs
// ==========================================
function openDetailModal(workflowId) {
  state.activeWorkflowId = workflowId;
  state.selectedStep = null; state.detailSignature = "";
  state.activeDetail = { workflow: null, loadedAt: null, error: null };
  state.activeDetailTab = "overview";
  releaseImageDrafts(workflowId);
  el.detailModal.hidden = false; el.appRoot.hidden = true;
  el.detailTitle.textContent = "Chi tiết sản phẩm"; el.detailWorkflowId.textContent = workflowId;
  el.detailTitle.tabIndex = -1; el.detailTitle.focus({ preventScroll: true });
  el.pageTitle.textContent = "Xưởng video";
  el.sourceIndicator.textContent = "Chi tiết sản phẩm / workflow";
  updateDetailTabsUI(); renderDetailModalBody();
  window.scrollTo(0, 0); loadViewData();
}
function closeDetailModal(updateHash = true) {
  state.activeWorkflowId = null;
  state.activeDetail = { workflow: null, loadedAt: null, error: null };
  state.detailSignature = "";
  el.detailModal.hidden = true; el.appRoot.hidden = false;
  releaseImageDrafts();
  if (updateHash) window.location.hash = "#products";
}

function updateDetailTabsUI() {
  const tabs = ["overview", "scenes", "qc", "history"];
  tabs.forEach((tabName) => {
    const btn = document.getElementById(`tab-btn-${tabName}`);
    if (!btn) return;
    const isActive = state.activeDetailTab === tabName;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
  });
}

function renderDetailModalBody() {
  const focusStep = document.activeElement?.closest('.vf-step-timeline') != null;
  el.detailBody.replaceChildren();
  el.detailFooterInfo.replaceChildren();
  el.detailFooterActions.replaceChildren();

  if (state.activeDetail.error) {
    const err = document.createElement("div");
    err.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = `Lỗi tải chi tiết: ${state.activeDetail.error}`;
    err.appendChild(p);
    el.detailBody.appendChild(err);
    return;
  }

  const wf = state.activeDetail.workflow;
  if (!wf) {
    const loading = document.createElement("div");
    loading.className = "vf-empty-state";
    const p = document.createElement("p");
    p.textContent = "Đang tải dữ liệu...";
    loading.appendChild(p);
    el.detailBody.appendChild(loading);
    return;
  }

  // Footer Info
  const timeLabel = document.createElement("span");
  timeLabel.style.fontSize = "12px";
  timeLabel.style.color = "var(--vf-text-muted)";
  timeLabel.textContent = `Dữ liệu tải lúc: ${formatDateTime(state.activeDetail.loadedAt)}`;
  el.detailFooterInfo.appendChild(timeLabel);

  el.detailTitle.textContent = wf.product_name || [wf.source_context?.brand, wf.source_context?.model].filter(Boolean).join(" ") || "Chi tiết sản phẩm";
  if (state.activeDetailTab === "overview") {
    if (!state.selectedStep) state.selectedStep = currentStep(wf) || "INPUT_ADMISSION";
    el.detailBody.append(WorkflowWorkspace(wf, state.selectedStep, {
      select: step => { state.selectedStep = step; renderDetailModalBody(); },
      approve: () => onApproveGateClick(wf),
      resume: () => onRetryQuarantinedClick(wf),
      upload: payload => onUploadSource(wf, payload),
      refresh: () => refreshActiveDetail(),
      loadActions: async snapshot => {
        const res = await loadWorkflowActions(window.fetch, snapshot.workflow_id);
        if (!res.ok) throw new Error(res.status === 404 ? 'API command chưa được nạp trên tiến trình web. Cần cập nhật web API trước khi dùng nút này.' : res.body?.detail || 'Không tải được command');
        return res.body;
      },
      executeAction: onWorkflowAction
    }));
    if (focusStep) el.detailBody.querySelector('.vf-step-button.selected')?.focus({ preventScroll: true });
    if (["skipped", "waitingforoperator"].includes(String(wf.state || "").replace(/_/g, "").toLowerCase())) {
      const advanced = createSourceCorrectionForm(wf.workflow_id);
      el.detailBody.append(advanced);
    }
  } else if (state.activeDetailTab === "scenes") renderTabScenes(wf);
  else if (state.activeDetailTab === "qc") renderTabQc(wf);
  else if (state.activeDetailTab === "history") renderTabHistory(wf);
}

async function onUploadSource(snapshot, payload) {
  if (state.isMutating || hasUnresolvedPendingIntent()) throw new Error("Đang có yêu cầu chưa rõ kết quả; kiểm tra trạng thái trước khi gửi tiếp.");
  state.isMutating = true;
  let sent = false;
  try {
    const fresh = await loadDetail(window.fetch, snapshot.workflow_id);
    if (!fresh.ok || fresh.body.version !== snapshot.version || fresh.body.state !== snapshot.state) throw new Error("Workflow đã thay đổi. Làm mới trước khi thay ảnh.");
    savePendingIntent(buildPendingIntent("source-input", [snapshot.workflow_id]));
    sent = true;
    const res = await correctSourceInput(window.fetch, snapshot.workflow_id, { ...payload, expected_version: snapshot.version });
    if (res.status >= 500) throw Object.assign(new Error("Chưa rõ server đã nhận ảnh chưa. Làm mới và đối soát; hệ thống không tự gửi lại."), { resultUnknown: true });
    clearPendingIntent();
    if (!res.ok) throw new Error(res.body?.detail || res.body?.error || "Không thể thay ảnh");
    showStatus("Đã lưu input và xếp lại QC ảnh nguồn. Bấm “Chạy tiếp từ bước này” để tiếp tục pipeline.", "info", true);
  } catch (error) {
    if (error.resultUnknown && sent) throw new Error("Chưa xác định kết quả thay ảnh. Kiểm tra trạng thái trước khi gửi tiếp; không tự gửi lại.");
    throw error;
  } finally { state.isMutating = false; state.detailSignature = ""; }
  await refreshActiveDetail();
}

async function onWorkflowAction(snapshot, option, version, note) {
  if (state.isMutating || hasUnresolvedPendingIntent()) throw new Error('Đang có yêu cầu chưa rõ kết quả; đối soát trước khi gửi tiếp.');
  state.isMutating = true;
  let sent = false;
  try {
    const fresh = await loadDetail(window.fetch, snapshot.workflow_id);
    if (!fresh.ok || fresh.body.version !== version || state.activeWorkflowId !== snapshot.workflow_id) throw new Error('Workflow đã thay đổi; làm mới trước khi chạy command.');
    const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
    const requestId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    const intent = { ...buildPendingIntent('command', [snapshot.workflow_id]), requestId, actionId: option.id };
    savePendingIntent(intent); sent = true;
    const res = await executeWorkflowAction(window.fetch, snapshot.workflow_id, { action_id: option.id, expected_version: version, request_id: requestId, note });
    if (res.status >= 500) throw Object.assign(new Error('Chưa xác định kết quả command. Kiểm tra workflow và receipt; không tự gửi lại.'), { resultUnknown: true });
    clearPendingIntent();
    if (!res.ok) throw new Error(res.body?.detail || res.body?.error || `Command bị từ chối (${res.status})`);
    showStatus('Đã nhận command. Runner Studio sẽ xử lý đúng sản phẩm và checkpoint đã chọn.', 'info', true);
    return res.body;
  } catch (error) {
    if (sent && error.resultUnknown) {
      checkSessionPendingIntent();
      throw new Error('Chưa rõ server đã nhận command chưa. Làm mới và kiểm tra; không gửi lại khi chưa đối soát.');
    }
    throw error;
  } finally {
    state.isMutating = false; state.detailSignature = '';
    await refreshActiveDetail();
  }
}

function renderTabScenes(wf) {
  const scenePlan = wf.scene_plan || {};
  const scenes = Array.isArray(scenePlan.scenes) ? scenePlan.scenes : [];
  const ops = Array.isArray(wf.scene_operations) ? wf.scene_operations : [];

  const h3Plan = document.createElement("h3");
  h3Plan.style.fontSize = "14px";
  h3Plan.style.fontWeight = "600";
  h3Plan.textContent = `Kế hoạch cảnh (${scenes.length > 0 ? scenes.length + " cảnh" : "Tổng cảnh chưa có dữ liệu"})`;
  el.detailBody.appendChild(h3Plan);

  if (scenes.length === 0) {
    const empty = document.createElement("div");
    empty.className = "vf-notice-box";
    empty.textContent = "Chưa có dữ liệu phân cảnh (scene_plan).";
    el.detailBody.appendChild(empty);
  } else {
    const table = document.createElement("table");
    table.className = "vf-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["Thứ tự", "Lời thoại (Narration)", "Mô tả hình ảnh (Visual Prompt)"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    scenes.forEach((s) => {
      const row = document.createElement("tr");
      const tdOrder = document.createElement("td");
      tdOrder.textContent = `Cảnh ${s.scene_order || "—"}`;
      row.appendChild(tdOrder);

      const tdNarr = document.createElement("td");
      tdNarr.textContent = s.narration || "—";
      row.appendChild(tdNarr);

      const tdPrompt = document.createElement("td");
      tdPrompt.textContent = s.visual_prompt || "—";
      row.appendChild(tdPrompt);

      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    el.detailBody.appendChild(table);
  }

  // Operations
  const h3Ops = document.createElement("h3");
  h3Ops.style.fontSize = "14px";
  h3Ops.style.fontWeight = "600";
  h3Ops.style.marginTop = "20px";
  h3Ops.textContent = `Tiến độ render cảnh (${ops.length} operation)`;
  el.detailBody.appendChild(h3Ops);

  if (ops.length === 0) {
    const emptyOps = document.createElement("div");
    emptyOps.className = "vf-notice-box";
    emptyOps.textContent = "Chưa có lượt render cảnh nào (scene_operations).";
    el.detailBody.appendChild(emptyOps);
  } else {
    const tableOps = document.createElement("table");
    tableOps.className = "vf-table";
    const theadOps = document.createElement("thead");
    const trOps = document.createElement("tr");
    ["Cảnh", "Lần thử", "Trạng thái", "Đường dẫn file (Local)", "Cập nhật"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      trOps.appendChild(th);
    });
    theadOps.appendChild(trOps);
    tableOps.appendChild(theadOps);

    const tbodyOps = document.createElement("tbody");
    ops.forEach((o) => {
      const row = document.createElement("tr");
      const tdOrd = document.createElement("td");
      tdOrd.textContent = `Cảnh ${o.scene_order || "—"}`;
      row.appendChild(tdOrd);

      const tdAtt = document.createElement("td");
      tdAtt.textContent = `Lần ${o.attempt || 1}`;
      row.appendChild(tdAtt);

      const tdSt = document.createElement("td");
      const b = document.createElement("span");
      b.className = `vf-badge ${o.status === "succeeded" ? "vf-badge-success" : "vf-badge-info"}`;
      b.textContent = o.status || "—";
      tdSt.appendChild(b);
      row.appendChild(tdSt);

      const tdPath = document.createElement("td");
      if (o.output_path) {
        const code = document.createElement("span");
        code.className = "vf-local-path";
        code.textContent = o.output_path;
        tdPath.appendChild(code);
      } else {
        tdPath.textContent = "—";
      }
      row.appendChild(tdPath);

      const tdTime = document.createElement("td");
      tdTime.textContent = formatDateTime(o.updated_at);
      row.appendChild(tdTime);

      tbodyOps.appendChild(row);
    });
    tableOps.appendChild(tbodyOps);
    el.detailBody.appendChild(tableOps);
  }
}

function renderTabQc(wf) {
  const qcList = Array.isArray(wf.qc_reports) ? wf.qc_reports : [];
  const budgetList = Array.isArray(wf.budget_ledger) ? wf.budget_ledger : [];

  const h3Qc = document.createElement("h3");
  h3Qc.style.fontSize = "14px";
  h3Qc.style.fontWeight = "600";
  h3Qc.textContent = `Báo cáo QC (${qcList.length})`;
  el.detailBody.appendChild(h3Qc);

  if (qcList.length === 0) {
    const emptyQc = document.createElement("div");
    emptyQc.className = "vf-notice-box";
    emptyQc.textContent = "Chưa có báo cáo QC.";
    el.detailBody.appendChild(emptyQc);
  } else {
    const table = document.createElement("table");
    table.className = "vf-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["Giai đoạn", "Kết quả (Verdict)", "Điểm (Score)", "Người/Tool đánh giá", "Thời gian"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    qcList.forEach((q) => {
      const row = document.createElement("tr");
      const tdStg = document.createElement("td");
      tdStg.textContent = q.stage || "—";
      row.appendChild(tdStg);

      const tdVer = document.createElement("td");
      const b = document.createElement("span");
      b.className = `vf-badge ${q.verdict === "PASS" ? "vf-badge-success" : "vf-badge-alert"}`;
      b.textContent = q.verdict || "—";
      tdVer.appendChild(b);
      row.appendChild(tdVer);

      const tdSco = document.createElement("td");
      tdSco.textContent = q.score != null ? String(q.score) : "—";
      row.appendChild(tdSco);

      const tdEval = document.createElement("td");
      tdEval.textContent = q.evaluator_name || "—";
      row.appendChild(tdEval);

      const tdTime = document.createElement("td");
      tdTime.textContent = formatDateTime(q.created_at);
      row.appendChild(tdTime);

      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    el.detailBody.appendChild(table);
  }

  // Budget
  const h3Bud = document.createElement("h3");
  h3Bud.style.fontSize = "14px";
  h3Bud.style.fontWeight = "600";
  h3Bud.style.marginTop = "20px";
  h3Bud.textContent = `Sổ quỹ & Giữ chỗ ngân sách (${budgetList.length})`;
  el.detailBody.appendChild(h3Bud);

  const budgetNotice = document.createElement("div");
  budgetNotice.className = "vf-notice-box";
  budgetNotice.textContent = "Định mức hiển thị là khoản giữ chỗ (reservation), không phải chi phí đã trừ thực tế.";
  el.detailBody.appendChild(budgetNotice);

  if (budgetList.length === 0) {
    const emptyBud = document.createElement("div");
    emptyBud.className = "vf-notice-box";
    emptyBud.textContent = "Chưa có bản ghi ngân sách.";
    el.detailBody.appendChild(emptyBud);
  } else {
    const tableBud = document.createElement("table");
    tableBud.className = "vf-table";
    const theadBud = document.createElement("thead");
    const trBud = document.createElement("tr");
    ["Reservation ID", "Năng lực (Capability)", "Số lượng", "Đơn vị", "Trạng thái", "Thời gian"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      trBud.appendChild(th);
    });
    theadBud.appendChild(trBud);
    tableBud.appendChild(theadBud);

    const tbodyBud = document.createElement("tbody");
    budgetList.forEach((b) => {
      const row = document.createElement("tr");
      const tdId = document.createElement("td");
      tdId.textContent = shortenId(b.reservation_id);
      row.appendChild(tdId);

      const tdCap = document.createElement("td");
      tdCap.textContent = b.capability_id || "—";
      row.appendChild(tdCap);

      const tdAmt = document.createElement("td");
      tdAmt.textContent = b.amount != null ? String(b.amount) : "—";
      row.appendChild(tdAmt);

      const tdCur = document.createElement("td");
      tdCur.textContent = b.currency || "USD";
      row.appendChild(tdCur);

      const tdSt = document.createElement("td");
      const bSt = document.createElement("span");
      bSt.className = "vf-badge vf-badge-info";
      bSt.textContent = b.state || "—";
      tdSt.appendChild(bSt);
      row.appendChild(tdSt);

      const tdTime = document.createElement("td");
      tdTime.textContent = formatDateTime(b.created_at);
      row.appendChild(tdTime);

      tbodyBud.appendChild(row);
    });
    tableBud.appendChild(tbodyBud);
    el.detailBody.appendChild(tableBud);
  }
}

function renderTabHistory(wf) {
  const historyList = Array.isArray(wf.approval_history) ? wf.approval_history : [];

  const h3Hist = document.createElement("h3");
  h3Hist.style.fontSize = "14px";
  h3Hist.style.fontWeight = "600";
  h3Hist.textContent = `Lịch sử phê duyệt (${historyList.length})`;
  el.detailBody.appendChild(h3Hist);

  if (historyList.length === 0) {
    const empty = document.createElement("div");
    empty.className = "vf-notice-box";
    empty.textContent = "Chưa có lịch sử phê duyệt.";
    el.detailBody.appendChild(empty);
  } else {
    const table = document.createElement("table");
    table.className = "vf-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["Request ID", "Trạng thái", "Thời gian"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    historyList.forEach((h) => {
      const row = document.createElement("tr");
      const tdId = document.createElement("td");
      tdId.textContent = shortenId(h.request_id);
      row.appendChild(tdId);

      const tdSt = document.createElement("td");
      const b = document.createElement("span");
      b.className = `vf-badge ${h.status === "approved" ? "vf-badge-success" : "vf-badge-info"}`;
      b.textContent = h.status || "—";
      tdSt.appendChild(b);
      row.appendChild(tdSt);

      const tdTime = document.createElement("td");
      tdTime.textContent = formatDateTime(h.created_at);
      row.appendChild(tdTime);

      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    el.detailBody.appendChild(table);
  }
}

// Product Workflows modal/popup
async function showProductWorkflows(productKey, productName) {
  try {
    const res = await loadProductWorkflows(window.fetch, productKey);
    const list = Array.isArray(res.body?.workflows) ? res.body.workflows : [];
    if (list.length === 0) {
      alert(`Sản phẩm [${productName}] chưa có workflow nào trước đây.`);
      return;
    }
    const lines = list.map((w) => `• ${w.workflow_id} [${w.state || "—"}] - v${w.version} (${formatDateTime(w.created_at)})`).join("\n");
    alert(`Lịch sử workflows của [${productName}] (tối đa 10 video theo API):\n\n${lines}`);
  } catch (err) {
    alert(`Lỗi tải lịch sử sản phẩm: ${err.message}`);
  }
}

// Preview Modal
function openPreviewModal(title, url) {
  el.previewTitle.textContent = title;
  el.previewBody.replaceChildren();

  const video = document.createElement("video");
  video.controls = true;
  video.autoplay = false;
  video.src = url;
  el.previewBody.appendChild(video);

  if (typeof el.previewModal.showModal === "function") {
    el.previewModal.showModal();
  } else {
    el.previewModal.style.display = "block";
  }
}

function closePreviewModal() {
  if (el.previewBody) {
    const video = el.previewBody.querySelector("video");
    if (video) video.pause();
    el.previewBody.replaceChildren();
  }
  if (typeof el.previewModal.close === "function") {
    el.previewModal.close();
  } else {
    el.previewModal.style.display = "none";
  }
}

// ==========================================
// Task 4: Manual Start & Approve Guards
// ==========================================
async function onStartBatchClick() {
  if (state.isMutating) return;
  if (state.selectedCandidateKeys.size === 0) return;

  // Block new mutation if an unresolved pending intent already exists
  if (hasUnresolvedPendingIntent()) {
    showStatus(
      "Đang có yêu cầu trước đó chưa rõ kết quả. Vui lòng đối soát danh sách workflow trước khi thực hiện thao tác mới.",
      "warning",
      true
    );
    return;
  }

  const requestDate = state.targetDate || null;
  const requestQc = state.qcLevel;
  const requestDisableQC = state.disableKeyframeQC;
  const requestGeneration = state.routeGeneration;
  const btnStart = document.getElementById("btn-start-batch");
  if (btnStart) btnStart.disabled = true;
  state.isMutating = true;

  try {
    // 1. Snapshot selection before preflight await
    const selectedKeys = Array.from(state.selectedCandidateKeys);
    const snapshotSourceDate = state.candidatesData.source_date || state.targetDate || "";
    const selectionSnapshot = new Map();
    for (const k of selectedKeys) {
      const cand = state.candidatesData.candidates.find((c) => c.product_key === k);
      if (cand) {
        selectionSnapshot.set(k, {
          candidate: cand,
          fingerprint: computeCandidateFingerprint(cand)
        });
      }
    }

    // 2. Preflight check: Re-fetch candidates to ensure keys and data are still valid
    showStatus("Đang kiểm tra dữ liệu ứng viên trước khi tạo...", "info");
    let preflight;
    try {
      preflight = await loadCandidates(window.fetch, requestDate, null, requestQc);
    } catch (netErr) {
      showStatus(`Không thể kết nối để kiểm tra ứng viên: ${netErr.message}. Đã hủy thao tác.`, "warning");
      return;
    }

    if (!preflight.ok) {
      showStatus(`Lỗi kiểm tra ứng viên (${preflight.status}). Đã hủy thao tác.`, "warning");
      return;
    }

    const freshCandidates = Array.isArray(preflight.body?.candidates) ? preflight.body.candidates : [];
    const freshSourceDate = preflight.body?.source_date || "";

    // Verify source_date integrity
    if (snapshotSourceDate && freshSourceDate && snapshotSourceDate !== freshSourceDate) {
      showStatus(`Ngày nguồn dữ liệu đã thay đổi (${snapshotSourceDate} -> ${freshSourceDate}). Vui lòng tải lại và chọn lại.`, "warning");
      state.candidatesData = {
        source_date: freshSourceDate,
        candidates: freshCandidates,
        loadedAt: new Date(),
        error: null
      };
      state.selectedCandidateKeys.clear();
      renderView();
      return;
    }

    // Verify each selected product's existence, fingerprint, and valid status
    const freshMap = new Map(freshCandidates.map((c) => [c.product_key, c]));
    for (const key of selectedKeys) {
      const snap = selectionSnapshot.get(key);
      const fresh = freshMap.get(key);

      if (!fresh) {
        showStatus(`Sản phẩm [${key}] không còn tồn tại trong dữ liệu mới. Đã hủy thao tác.`, "warning");
        state.candidatesData.candidates = freshCandidates;
        state.selectedCandidateKeys.clear();
        renderView();
        return;
      }

      const freshFingerprint = computeCandidateFingerprint(fresh);
      if (snap && snap.fingerprint !== freshFingerprint) {
        showStatus(`Dữ liệu của sản phẩm [${fresh.name || key}] đã thay đổi trên hệ thống. Đã hủy thao tác để đảm bảo an toàn.`, "warning");
        state.candidatesData.candidates = freshCandidates;
        state.selectedCandidateKeys.clear();
        renderView();
        return;
      }

      if (String(fresh.status || "").toLowerCase() === "invalid") {
        showStatus(`Sản phẩm [${fresh.name || key}] không ở trạng thái hợp lệ. Đã hủy thao tác.`, "warning");
        state.candidatesData.candidates = freshCandidates;
        state.selectedCandidateKeys.clear();
        renderView();
        return;
      }
    }

    // Verify selection was not changed during preflight
    const currentSelection = Array.from(state.selectedCandidateKeys);
    if (
      currentSelection.length !== selectedKeys.length ||
      !currentSelection.every((k) => selectedKeys.includes(k))
    ) {
      showStatus("Lựa chọn sản phẩm đã thay đổi trong quá trình kiểm tra. Đã hủy thao tác.", "warning");
      return;
    }

    if ((state.targetDate || null) !== requestDate || state.qcLevel !== requestQc || state.disableKeyframeQC !== requestDisableQC || state.routeGeneration !== requestGeneration) {
      showStatus("Ngày nguồn hoặc cấu hình đã thay đổi trong lúc kiểm tra. Đã hủy yêu cầu.", "warning"); return;
    }
    // 3. Persist the immutable request context.
    const intent = buildPendingIntent("start", selectedKeys, requestDate);
    savePendingIntent(intent);

    // 4. Mutate: POST /api/personal/product-video/start-batch
    showStatus("Đang gửi yêu cầu tạo video...", "info");
    const res = await startBatch(window.fetch, selectedKeys, requestDate,
      requestQc, requestDisableQC);

    // 5. Handle response (200, 409 partial, client error, or 5xx unknown)
    const receipt = readBatchReceipt(res);

    if (receipt.unknown || res.status >= 500) {
      // 5xx status: do NOT clear pending intent!
      showStatus(
        "Chưa xác định kết quả từ yêu cầu tạo video; vui lòng kiểm tra danh sách Sản xuất. Hệ thống không tự động gửi lại.",
        "warning",
        true
      );
      state.selectedCandidateKeys.clear();
      renderView();
      return;
    }

    if (!res.ok && res.status !== 409) {
      // 400, 403, 422: definitive client error
      clearPendingIntent();
      const errMsg = res.body?.error || res.body?.detail || `Yêu cầu không hợp lệ (${res.status})`;
      showStatus(`Lỗi bắt đầu batch (${res.status}): ${errMsg}`, "error");
      return;
    }

    // Clear intent on definitive success or known partial
    clearPendingIntent();
    const startedCount = receipt.started.length;
    const skippedCount = receipt.skipped.length;

    let msg = `Đã nhận yêu cầu Chạy: ${startedCount} sản phẩm`;
    if (skippedCount > 0) {
      const skipReasons = receipt.skipped.map((s) => `${s.product_key}: ${s.reason}`).join(", ");
      msg += `, ${skippedCount} sản phẩm bị bỏ qua (${skipReasons})`;
    }

    showStatus(msg, skippedCount > 0 ? "warning" : "info");
    state.selectedCandidateKeys.clear();

    // Keep the user on the per-product result summary while workers progress.
    window.location.hash = "#results";
  } catch (err) {
    console.error("Lỗi khi bắt đầu batch:", err);
    if (err.resultUnknown) {
      // 5xx, socket failure, or unparseable JSON: keep pending intent!
      showStatus(
        "Chưa xác định kết quả từ yêu cầu tạo video; vui lòng kiểm tra danh sách Sản xuất. Hệ thống không tự động gửi lại.",
        "warning",
        true
      );
    } else {
      clearPendingIntent();
      showStatus(`Lỗi bắt đầu batch: ${err.message}`, "error");
    }
  } finally {
    state.isMutating = false;
    if (btnStart) btnStart.disabled = false;
  }
}

async function onApproveGateClick(wfSnapshot) {
  if (state.isMutating) return;
  if (!wfSnapshot || !wfSnapshot.pending_approval_gate) return;

  // Block new mutation if an unresolved pending intent already exists
  if (hasUnresolvedPendingIntent()) {
    showStatus(
      "Đang có yêu cầu trước đó chưa rõ kết quả. Vui lòng đối soát trạng thái trước khi thực hiện phê duyệt mới.",
      "warning",
      true
    );
    return;
  }

  const btnApprove = document.getElementById("btn-approve-gate");
  if (btnApprove) btnApprove.disabled = true;
  state.isMutating = true;

  try {
    // 1. Preflight check: Re-fetch detail to verify version and gate match
    showStatus("Đang kiểm tra trạng thái gate trước khi duyệt...", "info");
    let preflight;
    try {
      preflight = await loadDetail(window.fetch, wfSnapshot.workflow_id);
    } catch (netErr) {
      showStatus(`Không thể kết nối để kiểm tra gate: ${netErr.message}. Đã hủy thao tác.`, "warning");
      return;
    }

    if (!preflight.ok) {
      showStatus(`Lỗi kiểm tra gate (${preflight.status}). Đã hủy thao tác.`, "warning");
      return;
    }

    const fresh = preflight.body;
    if (
      fresh.version !== wfSnapshot.version ||
      fresh.pending_approval_gate !== wfSnapshot.pending_approval_gate
    ) {
      showStatus("Trạng thái workflow hoặc gate đã thay đổi. Không thể thực hiện phê duyệt.", "warning");
      state.activeDetail.workflow = fresh;
      renderDetailModalBody();
      return;
    }

    // 2. Persist pending intent
    const intent = buildPendingIntent("approve", [wfSnapshot.workflow_id]);
    savePendingIntent(intent);

    // 3. Mutate: POST /api/operator/approve
    showStatus(`Đang phê duyệt gate [${wfSnapshot.pending_approval_gate}]...`, "info");
    const res = await approveGate(window.fetch, wfSnapshot.workflow_id, wfSnapshot.pending_approval_gate);

    if (res.status >= 500) {
      showStatus(
        "Chưa xác định kết quả phê duyệt; vui lòng làm mới chi tiết để kiểm tra. Hệ thống không tự động gửi lại.",
        "warning",
        true
      );
      return;
    }

    if (!res.ok) {
      // 400, 403, 404: definitive client rejection
      clearPendingIntent();
      const errMsg = res.body?.error || res.body?.detail || `Thao tác bị từ chối (${res.status})`;
      showStatus(`Lỗi phê duyệt (${res.status}): ${errMsg}`, "error");
      await refreshActiveDetail();
      return;
    }

    // 4. Success (only on res.ok === true)
    clearPendingIntent();
    showStatus(`Đã phê duyệt thành công gate [${wfSnapshot.pending_approval_gate}]`, "info");

    // Refresh detail and board
    await refreshActiveDetail();
    await loadViewData(true);
  } catch (err) {
    console.error("Lỗi khi phê duyệt:", err);
    if (err.resultUnknown) {
      showStatus(
        "Chưa xác định kết quả phê duyệt; vui lòng làm mới chi tiết để kiểm tra. Hệ thống không tự động gửi lại.",
        "warning",
        true
      );
    } else {
      clearPendingIntent();
      showStatus(`Lỗi phê duyệt: ${err.message}`, "error");
    }
  } finally {
    state.isMutating = false;
    if (btnApprove) btnApprove.disabled = false;
  }
}

async function onRetryQuarantinedClick(wfSnapshot) {
  if (state.isMutating || !wfSnapshot?.workflow_id) return;
  if (hasUnresolvedPendingIntent()) { showStatus("Đang có yêu cầu chưa rõ kết quả; đối soát trước khi tiếp tục.", "warning", true); return; }
  const button = document.getElementById("btn-retry-quarantined");
  if (button) button.disabled = true;
  state.isMutating = true;
  try {
    showStatus("Đang xác minh lại ảnh nguồn, job và lease...", "info");
    const fresh = await loadDetail(window.fetch, wfSnapshot.workflow_id);
    if (!fresh.ok || fresh.body.version !== wfSnapshot.version || !fresh.body.quarantined_job?.recovery_available) {
      showStatus("Workflow đã thay đổi; làm mới trước khi tiếp tục.", "warning"); return;
    }
    savePendingIntent(buildPendingIntent("resume", [wfSnapshot.workflow_id]));
    const response = await retryQuarantinedWorkflow(window.fetch, wfSnapshot.workflow_id);
    if (response.status >= 500) throw Object.assign(new Error("Kết quả tiếp tục chưa xác định"), { resultUnknown: true });
    clearPendingIntent();
    if (!response.ok) {
      showStatus(`Chưa thể tiếp tục: ${response.body?.detail || response.body?.error || response.status}`, "error");
      await refreshActiveDetail();
      return;
    }
    showStatus(response.body.status === "already_queued"
      ? "Job đã được xếp tiếp tục; không tạo job trùng."
      : `Đã tiếp tục ${wfSnapshot.product_name || "sản phẩm"}; job cũ đang chạy.`, "info");
    await refreshActiveDetail();
    await loadViewData(true);
  } catch (err) {
    showStatus(err.resultUnknown
      ? "Chưa rõ server đã nhận yêu cầu tiếp tục chưa. Làm mới trạng thái; không gửi lại khi job đang chạy."
      : `Lỗi tiếp tục workflow: ${err.message}`, err.resultUnknown ? "warning" : "error", !!err.resultUnknown);
    await refreshActiveDetail();
  } finally {
    state.isMutating = false;
    if (button) button.disabled = false;
  }
}

// ==========================================
// Event Listeners & Bootstrap
// ==========================================
window.addEventListener("hashchange", handleRoute);

el.btnRefresh?.addEventListener("click", () => {
  state.authStopped = false;
  showStatus("Đang làm mới dữ liệu...", "info");
  loadViewData();
});

el.btnCloseDetail?.addEventListener("click", () => closeDetailModal(true));
el.btnClosePreview?.addEventListener("click", closePreviewModal);

// Close on Escape or click outside
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (document.querySelector('.vf-rerun-dialog[open]')) return;
    if (el.previewModal.open) {
      closePreviewModal();
    } else if (!el.detailModal.hidden) {
      closeDetailModal(true);
    }
  }
});

// Tab switching inside detail modal
["overview", "scenes", "qc", "history"].forEach((tab) => {
  const btn = document.getElementById(`tab-btn-${tab}`);
  if (!btn) return;
  btn.setAttribute("aria-controls", "detail-body");
  btn.addEventListener("keydown", event => {
    const tabs = ["overview", "scenes", "qc", "history"];
    const index = tabs.indexOf(tab);
    const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (next != null) { event.preventDefault(); const target = document.getElementById(`tab-btn-${tabs[next]}`); target?.click(); target?.focus(); }
  });
  btn.addEventListener("click", () => {
    state.activeDetailTab = tab;
    updateDetailTabsUI();
    if (state.activeDetail.workflow) {
      renderDetailModalBody();
    }
  });
});

el.btnRefresh?.addEventListener("click", () => scheduleNextPoll());
try { const savedTheme = localStorage.getItem("vf-studio.theme"); if (["light", "dark"].includes(savedTheme)) document.body.dataset.theme = savedTheme; } catch {}
document.getElementById("btn-theme")?.addEventListener("click", () => {
  const theme = document.body.dataset.theme === "light" ? "dark" : "light";
  document.body.dataset.theme = theme;
  try { localStorage.setItem("vf-studio.theme", theme); } catch {}
});

// App Entrypoint
function init() {
  checkSessionPendingIntent();
  handleRoute();
}

init();
