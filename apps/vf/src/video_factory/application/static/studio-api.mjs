/**
 * Video Factory Studio — API Transport Client
 * UI-only HTTP client strictly following existing API contracts.
 * No automatic retry on POST, no cost/provider injections.
 */

export async function requestJson(fetchImpl, path, options = {}) {
  const method = options.method || "GET";
  const fetchFn = fetchImpl || window.fetch.bind(window);

  let response;
  try {
    response = await fetchFn(path, {
      ...options,
      credentials: "same-origin",
      redirect: "error"
    });
  } catch (netErr) {
    // If a POST request fails at the network/socket level (timeout, connection reset, drop),
    // mark resultUnknown = true because the server may have received and processed the action.
    if (method === "POST") {
      netErr.resultUnknown = true;
    } else {
      netErr.resultUnknown = false;
    }
    throw netErr;
  }

  let raw;
  try {
    raw = await response.text();
  } catch (readErr) {
    const error = new Error("Không đọc được dữ liệu phản hồi từ server: " + readErr.message);
    error.resultUnknown = method === "POST";
    error.status = response.status;
    throw error;
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch (jsonErr) {
    const error = new Error("Không đọc được phản hồi JSON");
    error.resultUnknown = method === "POST";
    error.status = response.status;
    error.rawResponse = raw;
    throw error;
  }

  // ok is true only if HTTP status is 2xx AND body.ok is not explicitly false
  const isOk = response.ok && body?.ok !== false;
  return {
    status: response.status,
    ok: isOk,
    body
  };
}

export async function loadCandidates(fetchImpl, targetDate = null, signal = null, qcLevel = "high") {
  let path = "/api/personal/product-video/candidates";
  if (targetDate || qcLevel !== "high") {
    const params = new URLSearchParams();
    if (targetDate) params.set("target_date", targetDate);
    if (qcLevel !== "high") params.set("qc_level", qcLevel);
    path += `?${params.toString()}`;
  }
  return requestJson(fetchImpl, path, { method: "GET", signal });
}

export async function loadBoard(fetchImpl, signal = null) {
  return requestJson(fetchImpl, "/api/operator/kanban", { method: "GET", signal });
}

export async function loadDetail(fetchImpl, workflowId, signal = null) {
  const encodedId = encodeURIComponent(workflowId);
  return requestJson(fetchImpl, `/api/operator/workflow/${encodedId}`, { method: "GET", signal });
}

export async function loadVideos(fetchImpl, signal = null) {
  return requestJson(fetchImpl, "/api/personal/product-video/videos", { method: "GET", signal });
}

export async function loadProductWorkflows(fetchImpl, productKey, signal = null) {
  const encodedKey = encodeURIComponent(productKey);
  return requestJson(fetchImpl, `/api/operator/product/${encodedKey}/workflows`, { method: "GET", signal });
}

export async function startBatch(fetchImpl, productKeys, targetDate = null, qcLevel = "high", disableKeyframeQC = false) {
  const payload = {
    product_keys: productKeys,
    auto: false,
    qc_level: qcLevel
  };
  if (targetDate) {
    payload.target_date = targetDate;
  }
  if (disableKeyframeQC) payload.disable_keyframe_qc = true;
  return requestJson(fetchImpl, "/api/personal/product-video/start-batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export async function approveGate(fetchImpl, workflowId, gate) {
  const payload = {
    workflow_id: workflowId,
    gate,
    action: "approve"
  };
  return requestJson(fetchImpl, "/api/operator/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export async function retryQuarantinedWorkflow(fetchImpl, workflowId) {
  const encodedId = encodeURIComponent(workflowId);
  return requestJson(fetchImpl, `/api/operator/workflow/${encodedId}/retry-quarantined`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}"
  });
}

export async function correctSourceInput(fetchImpl, workflowId, payload) {
  return requestJson(fetchImpl, `/api/personal/product-video/workflows/${encodeURIComponent(workflowId)}/source-input`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export function deleteWorkflows(fetchImpl, workflows, confirmation) {
  return requestJson(fetchImpl, '/api/operator/workflows/delete', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({workflows, confirmation})
  });
}

export function loadWorkflowActions(fetchImpl, workflowId, signal = null) {
  return requestJson(fetchImpl, `/api/operator/workflow/${encodeURIComponent(workflowId)}/actions`, { method: 'GET', signal });
}

export function executeWorkflowAction(fetchImpl, workflowId, payload) {
  return requestJson(fetchImpl, `/api/operator/workflow/${encodeURIComponent(workflowId)}/actions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  });
}

export function loadCommandReceipt(fetchImpl, workflowId, requestId) {
  return requestJson(fetchImpl, `/api/operator/workflow/${encodeURIComponent(workflowId)}/actions/${encodeURIComponent(requestId)}`, { method: 'GET' });
}
