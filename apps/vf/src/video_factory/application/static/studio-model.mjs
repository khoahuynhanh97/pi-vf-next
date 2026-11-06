/**
 * Video Factory Studio — Pure Data Projections & Model Helpers
 * Zero DOM, zero network, zero side-effects.
 */

export function flattenBoard(columns) {
  if (!columns || typeof columns !== "object") return [];
  return Object.entries(columns).flatMap(([column, cards]) =>
    (Array.isArray(cards) ? cards : []).map((card) => ({ column, card }))
  );
}

export function extractImageUrl(imageEntry) {
  if (!imageEntry) return null;
  if (typeof imageEntry === "string") return imageEntry.trim() || null;
  if (typeof imageEntry === "object") {
    const raw = imageEntry.asset_url || imageEntry.original_url || imageEntry.url || null;
    return typeof raw === "string" ? raw.trim() || null : null;
  }
  return null;
}

export function classifyMedia(raw, origin) {
  if (typeof raw !== "string" || !raw.trim()) {
    return { kind: "unavailable", url: null };
  }
  const trimmed = raw.trim();
  // Check local filesystem or UNC paths
  if (
    /^[a-zA-Z]:[\\/]/.test(trimmed) ||
    trimmed.startsWith("\\\\") ||
    /^file:/i.test(trimmed)
  ) {
    return { kind: "local", url: null };
  }

  // Block dangerous schemes before URL resolution
  if (/^(javascript|data|vbscript):/i.test(trimmed)) {
    return { kind: "unavailable", url: null };
  }

  try {
    const baseOrigin = origin && origin.startsWith("http") ? origin : "http://127.0.0.1";
    const url = new URL(trimmed, baseOrigin);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      return { kind: "unavailable", url: null };
    }
    const isSame = origin ? url.origin === origin : false;
    return {
      kind: isSame ? "same-origin" : "external",
      url: url.href
    };
  } catch {
    return { kind: "unavailable", url: null };
  }
}

export function readBatchReceipt(envelope) {
  const body = envelope?.body ?? {};
  return {
    started: Array.isArray(body.started) ? body.started : [],
    skipped: Array.isArray(body.skipped) ? body.skipped : [],
    unknown: (envelope?.status ?? 0) >= 500,
    message: typeof body.error === "string" ? body.error : (typeof body.detail === "string" ? body.detail : null)
  };
}

export function computeCandidateFingerprint(candidate) {
  if (!candidate || typeof candidate !== "object") return "";
  const imagesStr = Array.isArray(candidate.images)
    ? candidate.images.map((img) => extractImageUrl(img) || "").join("|")
    : "";
  return [
    candidate.product_key ?? "",
    candidate.name ?? "",
    candidate.brand ?? "",
    candidate.model ?? "",
    candidate.status ?? "",
    candidate.rejection_reason ?? "",
    imagesStr
  ].join("::");
}

export function canSelectCandidate(candidate) {
  return ["unprocessed", "awaiting_product_lock", "ready_to_resume"].includes(
    String(candidate?.status || "").toLowerCase()
  );
}

export function sortListRows(rows, key, direction = 'desc') {
  const date = /(?:_at|_date)$/.test(key);
  const value = row => date ? Date.parse(row[key]) : row[key];
  return [...rows].sort((a,b) => {
    const x=value(a), y=value(b);
    const missing = v => v == null || v === '' || (date && !Number.isFinite(v));
    if (missing(x) || missing(y)) return Number(missing(x)) - Number(missing(y));
    const result = typeof x === 'number' && typeof y === 'number' ? x-y : String(x).localeCompare(String(y),'vi',{numeric:true});
    return direction === 'asc' ? result : -result;
  });
}

export function formatDateTime(isoString) {
  if (!isoString) return "—";
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return String(isoString);
    return d.toLocaleString("vi-VN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
        hour12: false,
        timeZone: 'Asia/Bangkok'
    });
  } catch {
    return String(isoString);
  }
}

export function shortenId(id, head = 8, tail = 4) {
  if (!id || typeof id !== "string") return "—";
  if (id.length <= head + tail + 3) return id;
  return `${id.slice(0, head)}...${id.slice(-tail)}`;
}

const COLUMN_LABELS = {
  awaiting_product_lock: "Chờ khoá sản phẩm",
  ready_to_resume: "Có thể tiếp tục",
  awaiting_brief_approval: "Chờ duyệt Brief",
  awaiting_scene_plan_approval: "Chờ duyệt Cảnh",
  awaiting_storyboard_approval: "Chờ duyệt Storyboard",
  in_production: "Đang sản xuất",
  awaiting_final_approval: "Chờ duyệt cuối",
  completed: "Hoàn tất",
  error: "Lỗi / Cần xử lý"
};

export function getColumnLabel(columnKey) {
  if (!columnKey) return "—";
  return COLUMN_LABELS[columnKey] || columnKey;
}

export function getCardStateLabel(state) {
  if (!state) return "Chưa xác định";
  const s = String(state).toLowerCase();
  const steps = {
    completed: "Hoàn tất", blocked: "BLOCKED", technical_error: "Lỗi kỹ thuật",
    awaiting_product_lock: "Chờ xác nhận sản phẩm", waiting_for_operator: "Cần xử lý",
    in_production: "Đang xử lý",
    unprocessed: "Chờ chạy", ready_to_resume: "Chờ tiếp tục", invalid: "Cần sửa input",
    building_resource_pack: "Kiểm tra ảnh nguồn", retrying_image_qc: "Kiểm tra lại ảnh nguồn",
    locking_resource_pack: "Xác nhận sản phẩm", creating_brief: "Lập kịch bản",
    creating_scene_plan: "Lập phân cảnh", creating_storyboard: "Tạo keyframe / storyboard",
    exporting: "Ghép / lưu MP4", skipped: "BLOCKED", waitingforoperator: "Cần xử lý"
  };
  if (steps[s]) return steps[s];
  if (s.includes("awaitingbrief")) return "Chờ duyệt Brief";
  if (s.includes("awaitingscene")) return "Chờ duyệt Cảnh";
  if (s.includes("awaitingstoryboard")) return "Chờ duyệt Storyboard";
  if (s.includes("awaitingfinal")) return "Chờ duyệt Cuối";
  if (s.includes("rendering")) return "Đang render cảnh";
  if (s.includes("research")) return "Đang phân tích";
  if (s.includes("completed") || s === "done") return "Đã hoàn thành";
  if (s.includes("failed") || s.includes("error")) return "Lỗi";
  return String(state);
}

export function filterCandidates(candidates, search = "", statusFilter = "all") {
  if (!Array.isArray(candidates)) return [];
  const q = (search || "").toLowerCase().trim();
  const st = (statusFilter || "all").toLowerCase();

  return candidates.filter((c) => {
    if (st !== "all" && String(c.status || "").toLowerCase() !== st) {
      return false;
    }
    if (!q) return true;
    const name = String(c.name || "").toLowerCase();
    const brand = String(c.brand || "").toLowerCase();
    const model = String(c.model || "").toLowerCase();
    const key = String(c.product_key || "").toLowerCase();
    return name.includes(q) || brand.includes(q) || model.includes(q) || key.includes(q);
  });
}

export function filterBoardCards(cards, search = "", columnFilter = "all") {
  if (!Array.isArray(cards)) return [];
  const q = (search || "").toLowerCase().trim();
  const col = (columnFilter || "all").toLowerCase();

  return cards.filter(({ column, card }) => {
    if (col !== "all" && column.toLowerCase() !== col) {
      return false;
    }
    if (!q) return true;
    const name = String(card.product_name || "").toLowerCase();
    const id = String(card.workflow_id || "").toLowerCase();
    const state = String(card.state || "").toLowerCase();
    return name.includes(q) || id.includes(q) || state.includes(q);
  });
}

export function buildProductResults(candidates, columns, videos) {
  const boardByWorkflow = new Map(
    flattenBoard(columns).map(({ card }) => [card.workflow_id, card])
  );
  const videoByWorkflow = new Map((Array.isArray(videos) ? videos : []).map((video) => [video.workflow_id, video]));
  const rows = (Array.isArray(candidates) ? candidates : []).map((candidate) => {
    const workflowId = candidate.workflow_id || null;
    const board = workflowId ? boardByWorkflow.get(workflowId) : null;
    const video = workflowId ? videoByWorkflow.get(workflowId) : null;
    const reason = String(board?.quarantined_job?.reason || candidate.rejection_reason || board?.failure_code || board?.operator_reason || "");
    const status = String(candidate.status || "").toLowerCase();
    let resultStatus = "waiting";
    let label = "Chờ chạy";
    let fix = "";
    let videoUrl = null;

    if (video?.result_status === "completed") {
      resultStatus = "completed";
      label = "Thành công";
      videoUrl = video.video_url || null;
    } else if (video?.result_status === "technical_error") {
      resultStatus = "technical_error";
      label = "Lỗi kỹ thuật";
      fix = video.technical_error || "Kiểm tra file MP4 đã export rồi chạy lại kiểm tra.";
    } else if (board?.quarantined_job) {
      resultStatus = /UNKNOWN|UNCERTAIN|RECONCIL/i.test(reason) ? "waiting" : "technical_error";
      label = resultStatus === "waiting" ? "Chờ đối soát" : "Đã dừng / Lỗi kỹ thuật";
      fix = reason || "Pipeline đã dừng. Xem chi tiết và sửa nguyên nhân trước khi tiếp tục.";
    } else if (status === "completed") {
      resultStatus = "technical_error";
      label = "Lỗi kỹ thuật";
      fix = "Chưa tìm thấy MP4 vượt kiểm tra kỹ thuật cho workflow đã hoàn tất.";
    } else if (status === "invalid" || status === "waiting_for_operator" || status === "blocked" || status === "technical_error") {
      if (/UNKNOWN|UNCERTAIN|RECONCIL/i.test(reason)) {
        resultStatus = "waiting";
        label = "Chờ đối soát";
        fix = reason;
      } else if (status === "technical_error" || /TECHNICAL|PROVIDER|EXPORT|COMPOSITION|DECODE|SHA|TIMEOUT/i.test(reason)) {
        resultStatus = "technical_error";
        label = "Lỗi kỹ thuật";
        fix = reason || "Kiểm tra lỗi của worker/provider rồi tiếp tục workflow hiện tại.";
      } else {
        resultStatus = "blocked";
        label = "BLOCKED";
        fix = /IMAGE|REFERENCE|IDENTITY|VARIANT|MISSING_SOURCE|NO_TECHNICALLY_READABLE|ảnh|image/i.test(reason)
          ? (/^PRODUCT_VARIANT_REQUIRED/i.test(reason)
            ? "Xác minh đúng variant trong PI rồi publish READY hợp lệ để chạy lại."
            : "Thay ảnh nguồn rõ hơn, đúng sản phẩm/variant rồi kiểm tra lại.")
          : "Sửa thông tin hoặc nguồn sản phẩm theo lý do rồi kiểm tra lại.";
      }
    } else if (/^awaiting/i.test(String(board?.state || "")) || status.startsWith("awaiting_")) {
      resultStatus = "waiting";
      label = String(board?.state || "").toLowerCase() === "awaitingsceneapproval"
        ? "Chờ duyệt phân cảnh"
        : "Chờ bước tiếp theo";
      fix = reason || "Workflow đang chờ tại một gate; chưa có bước xử lý mới.";
    } else if (status === "ready_to_resume") {
      label = "Có thể tiếp tục";
      fix = "Chọn sản phẩm và bấm Chạy để tiếp tục.";
    } else if (status === "in_production" || status === "awaiting_product_lock" || board) {
      resultStatus = "running";
      label = status === "awaiting_product_lock" ? "Chờ xác nhận sản phẩm" : "Đang chạy";
    }

    return {
      name: candidate.name || [candidate.brand, candidate.model].filter(Boolean).join(" ") || "Sản phẩm không tên",
      productKey: candidate.product_key,
      variant: candidate.variant || candidate.model || "",
      step: getCardStateLabel(board?.state || status),
      qcLevel: video?.qc_level || candidate.qc_level || board?.qc_level || "middle",
      status: resultStatus,
      label,
      reason: reason || fix,
      fix,
      videoUrl,
      detailWorkflowId: workflowId
    };
  });
  rows.summary = rows.reduce((summary, row) => {
    if (row.status === "completed") summary.completed += 1;
    else if (row.status === "blocked") summary.blocked += 1;
    else if (row.status === "technical_error") summary.technicalError += 1;
    else if (row.status === "running") summary.running += 1;
    else summary.waiting += 1;
    return summary;
  }, { completed: 0, blocked: 0, technicalError: 0, running: 0, waiting: 0 });
  return rows;
}

export function buildPendingIntent(kind, targets, sourceDate = null) {
  return {
    kind,
    targets: Array.isArray(targets) ? targets : [],
    sourceDate: sourceDate || null,
    createdAt: new Date().toISOString()
  };
}

export function validatePendingIntent(intent) {
  if (!intent || typeof intent !== "object") return false;
  if (!["start", "approve", "source-input", "resume", "command", "delete"].includes(intent.kind)) return false;
  if (!Array.isArray(intent.targets) || intent.targets.length === 0) return false;
  if (typeof intent.createdAt !== "string") return false;
  return true;
}
