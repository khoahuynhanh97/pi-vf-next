/**
 * Video Factory Studio — Independent Fixture Mock Server
 * Pure Node.js http server. Zero backend/DB/services imports.
 * Binds strictly to 127.0.0.1:18742.
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cases } from "./fixture-data.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const STATIC_DIR = path.resolve(__dirname, "../../src/video_factory/application/static");

// Parse CLI flags
const args = process.argv.slice(2);
let scenario = "happy";
let port = 18742;

args.forEach((arg) => {
  if (arg.startsWith("--scenario=")) {
    scenario = arg.split("=")[1];
  } else if (arg.startsWith("--port=")) {
    port = parseInt(arg.split("=")[1], 10);
  }
});

// In-memory request trace log
const requestTraces = [];
let detailGetCount = 0;
let commandAccepted = false;

function logTrace(req, body = null) {
  const record = {
    timestamp: new Date().toISOString(),
    method: req.method,
    url: req.url,
    headers: req.headers,
    body: body ? JSON.parse(body) : null
  };
  requestTraces.push(record);
}

function sendJson(res, statusCode, data) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-cache"
  });
  res.end(JSON.stringify(data));
}

function sendText(res, statusCode, text, contentType = "text/plain") {
  res.writeHead(statusCode, {
    "Content-Type": `${contentType}; charset=utf-8`,
    "Cache-Control": "no-cache"
  });
  res.end(text);
}

const MIME_TYPES = {
  ".html": "text/html",
  ".css": "text/css",
  ".mjs": "application/javascript",
  ".js": "application/javascript",
  ".json": "application/json",
  ".mp4": "video/mp4"
};

const server = http.createServer((req, res) => {
  const parsedUrl = new URL(req.url, `http://127.0.0.1:${port}`);
  const pathname = parsedUrl.pathname;

  let body = "";
  req.on("data", (chunk) => {
    body += chunk;
  });

  req.on("end", () => {
    logTrace(req, body || null);

    // ──────────────────────────────────────────
    // Fixture Trace Endpoints (for smoke verification)
    // ──────────────────────────────────────────
    if (pathname === "/__trace" && req.method === "GET") {
      return sendJson(res, 200, { scenario, count: requestTraces.length, traces: requestTraces });
    }
    if (pathname === "/__trace/clear" && req.method === "POST") {
      requestTraces.length = 0;
      return sendJson(res, 200, { ok: true });
    }
    if (pathname === "/__scenario" && req.method === "POST") {
      const data = body ? JSON.parse(body) : {};
      if (data.scenario) {
        scenario = data.scenario;
        detailGetCount = 0;
        commandAccepted = false;
      }
      return sendJson(res, 200, { ok: true, scenario });
    }

    // ──────────────────────────────────────────
    // Scenario: Unauthorized Check (GET /api/ -> 403)
    // ──────────────────────────────────────────
    if (scenario === "unauthorized" && pathname.startsWith("/api/")) {
      return sendJson(res, 403, { ok: false, error: "FORBIDDEN" });
    }

    // ──────────────────────────────────────────
    // Static Assets
    // ──────────────────────────────────────────
    if (
      pathname.startsWith("/operator/assets/") ||
      pathname.startsWith("/static/") ||
      pathname === "/operator/assets"
    ) {
      let fileName = pathname.replace(/^(\/operator\/assets|\/static)/, "");
      if (fileName.startsWith("/")) fileName = fileName.slice(1);
      if (!fileName || fileName === "studio.html") fileName = "studio.html";

      const filePath = path.join(STATIC_DIR, fileName);
      if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
        const ext = path.extname(filePath);
        const mime = MIME_TYPES[ext] || "text/plain";
        const content = fs.readFileSync(filePath);
        res.writeHead(200, { "Content-Type": `${mime}; charset=utf-8` });
        return res.end(content);
      }
      return sendText(res, 404, "Asset Not Found");
    }

    // Fake preview media
    if (pathname.startsWith("/fixture-media/")) {
      const ext = path.extname(pathname);
      const mime = ext === ".jpg" || ext === ".jpeg" ? "image/jpeg" : "video/mp4";
      res.writeHead(200, { "Content-Type": mime, "Content-Length": "0" });
      return res.end();
    }

    // ──────────────────────────────────────────
    // API Endpoints
    // ──────────────────────────────────────────
    // 1. GET /api/personal/product-video/candidates
    if (pathname === "/api/personal/product-video/candidates" && req.method === "GET") {
      if (scenario === "empty") return sendJson(res, 200, { ok: true, source_date: "2026-10-24", candidates: [] });
      if (scenario === "many") return sendJson(res, 200, { ...cases.candidates, candidates: Array.from({ length: 350 }, (_, i) => ({ product_key: `many-${i}`, name: `Sản phẩm ${i}`, status: "in_production", workflow_id: `wf-many-${i}`, images: [] })) });
      if (scenario === "changed-data") {
        return sendJson(res, 200, cases.candidatesChangedData);
      }
      return sendJson(res, 200, cases.candidates);
    }

    // 2. POST /api/personal/product-video/start-batch
    if (pathname === "/api/personal/product-video/start-batch" && req.method === "POST") {
      if (scenario === "network-drop") {
        // Abruptly destroy the socket to simulate lost network connection on POST
        return res.destroy();
      }
      if (scenario === "partial") {
        return sendJson(res, 409, cases.partial);
      }
      if (scenario === "unknown") {
        return sendJson(res, 503, { ok: false, error: "SERVICE_UNAVAILABLE" });
      }
      const parsedBody = body ? JSON.parse(body) : {};
      const started = (parsedBody.product_keys || []).map((k, i) => ({
        product_key: k,
        workflow_id: `wf-mock-${k}-${Date.now()}`
      }));
      return sendJson(res, 200, {
        ok: true,
        started,
        skipped: [],
        started_workflows: started.map((s) => s.workflow_id)
      });
    }

    // 3. GET /api/operator/kanban
    if (pathname === "/api/operator/kanban" && req.method === "GET") {
      if (scenario === "empty" || scenario === "many") return sendJson(res, 200, { columns: {} });
      return sendJson(res, 200, cases.board);
    }

    if (/^\/api\/operator\/workflow\/[^/]+\/actions\/[^/]+$/.test(pathname) && req.method === 'GET') {
      return sendJson(res, 200, { state: commandAccepted ? 'accepted' : 'unresolved' });
    }
    if (/^\/api\/operator\/workflow\/[^/]+\/actions$/.test(pathname)) {
      if (req.method === 'GET') {
        const commandScenario = ['command', 'command-unknown', 'command-stale'].includes(scenario);
        return sendJson(res, 200, {
          ok: true, workflow_id: 'wf1', version: commandScenario ? 3 : 2,
          options: commandScenario ? [{ id: 'recover-keyframe:operation-a', steps: ['KEYFRAME_GENERATION'], enabled: true,
            label: 'Chạy lại keyframe scene1 (chưa Send)', command: 'recover-keyframe', effect: 'Phục hồi đúng operation; giữ các frame đã hoàn tất.' }] : [],
          unavailable_reasons: { ALL: 'Reset xóa workflow/lịch sử; không dùng sau Product Lock.', VIDEO_RENDER: 'Không có command render hợp lệ tại trạng thái này.', IMAGE_QC: 'Thay ảnh trong trang sản phẩm để kiểm tra lại.' }
        });
      }
      if (req.method === 'POST') {
        commandAccepted = true;
        if (scenario === 'command-unknown') return sendJson(res, 503, { detail: 'UNKNOWN_COMMAND_RESULT' });
        return sendJson(res, 202, { ok: true, reused: false, execution: 'studio_selected_batch', command: 'recover-keyframe' });
      }
    }

    // 4. GET /api/operator/workflow/:workflow_id
    if (pathname.startsWith("/api/operator/workflow/") && req.method === "GET") {
      const id = decodeURIComponent(pathname.replace("/api/operator/workflow/", ""));

      if (['command', 'command-unknown', 'command-stale'].includes(scenario)) {
        detailGetCount++;
        return sendJson(res, 200, { workflow_id: id, product_name: 'V8 Pro ANC · fixture', state: 'creating_storyboard',
          version: commandAccepted || (scenario === 'command-stale' && detailGetCount > 1) ? 4 : 3,
          source_context: { product_key: 'p-running' },
          ...(commandAccepted ? { pipeline_steps: [{ step_name: 'KEYFRAME_GENERATION', status: 'pending' }] } : {
            quarantined_job: { quarantined: true, job_name: 'product_video.storyboard', step: 'KEYFRAME_GENERATION', reason: 'GOOGLE_FLOW_KEYFRAME_FAILED: GOOGLE_FLOW_NEW_PROJECT_SELECTOR_NOT_UNIQUE', recovery_available: false }
          })
        });
      }

      if (["blocked", "upload-unknown"].includes(scenario)) return sendJson(res, 200, {
        workflow_id: id, product_name: "Orico PW11 · fixture", state: "skipped", version: 2,
        failure_code: "IMAGE_QC_FAILED", failure_reason: "Ảnh nguồn không thể hiện rõ đúng variant. Cần thay ảnh trước Product Lock.",
        source_context: { source: "hermes_product_video_exchange", product_key: "p-running", images: [] },
        pipeline_steps: [
          { step_name: "INPUT_ADMISSION", status: "completed", started_at: "2026-10-26T10:00:00Z", finished_at: "2026-10-26T10:00:02Z", duration_ms: 2000 },
          { step_name: "IMAGE_QC", status: "failed", attempt_number: 2, provider: "image-qc", started_at: "2026-10-26T10:00:02Z", finished_at: "2026-10-26T10:00:08Z", duration_ms: 6000, message: "Ảnh không đủ rõ để xác nhận variant", next_action: "Thay bộ ảnh đúng SKU rồi kiểm tra lại" }
        ], qc_reports: [{ stage: "image", verdict: "FAIL", score: 30, findings: { reason: "Bao bì / variant chưa rõ" }, evaluator_name: "fixture-qc", created_at: "2026-10-26T10:00:08Z" }]
      });

      if (scenario === "changed-gate") {
        detailGetCount++;
        // First GET returns approvePending; second GET returns approveChanged
        if (detailGetCount > 1) {
          return sendJson(res, 200, cases.approveChanged);
        }
        return sendJson(res, 200, cases.approvePending);
      }

      if (scenario === "missing-data") {
        return sendJson(res, 200, {
          workflow_id: id,
          state: "rendering",
          version: 1,
          pending_approval_gate: null,
          product_name: "Workflow thiếu dữ liệu"
        });
      }

      if (id === "wf5") {
        return sendJson(res, 200, cases.approvePending);
      }

      return sendJson(res, 200, { ...cases.detail, workflow_id: id });
    }

    // 5. POST /api/operator/approve
    if (pathname === "/api/operator/approve" && req.method === "POST") {
      if (scenario === "approve-400") {
        return sendJson(res, 400, { ok: false, error: "Only 'approve' action is currently supported" });
      }
      if (scenario === "approve-403") {
        return sendJson(res, 403, { ok: false, error: "FORBIDDEN" });
      }
      if (scenario === "unknown") {
        return sendJson(res, 503, { ok: false, error: "GATE_APPROVE_ERROR" });
      }
      const parsedBody = body ? JSON.parse(body) : {};
      return sendJson(res, 200, {
        workflow_id: parsedBody.workflow_id || "wf-approved",
        status: "approved"
      });
    }

    if (pathname.endsWith("/source-input") && req.method === "POST") {
      if (scenario === "upload-unknown") return sendJson(res, 503, { detail: "Unknown receipt" });
      return sendJson(res, 200, { ok: true, state: "building_resource_pack", workflow_id: "wf1", reused: false });
    }

    // 6. GET /api/operator/product/:product_key/workflows
    if (pathname.startsWith("/api/operator/product/") && pathname.endsWith("/workflows") && req.method === "GET") {
      const match = pathname.match(/^\/api\/operator\/product\/(.+)\/workflows$/);
      const productKey = match ? decodeURIComponent(match[1]) : "unknown";
      return sendJson(res, 200, { ...cases.productWorkflows, product_key: productKey });
    }

    // 7. GET /api/personal/product-video/videos
    if (pathname === "/api/personal/product-video/videos" && req.method === "GET") {
      if (scenario === "empty" || scenario === "many") return sendJson(res, 200, { videos: [] });
      return sendJson(res, 200, cases.videos);
    }

    // Default 404
    sendText(res, 404, `Not Found: ${pathname}`);
  });
});

server.listen(port, "127.0.0.1", () => {
  console.log(`[FIXTURE-SERVER] Listening on http://127.0.0.1:${port}`);
  console.log(`[FIXTURE-SERVER] Scenario: ${scenario}`);
});

export { server, port };
