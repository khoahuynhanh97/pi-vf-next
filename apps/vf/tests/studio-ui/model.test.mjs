import test from "node:test";
import assert from "node:assert/strict";
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
  filterCandidates,
  canSelectCandidate,
  filterBoardCards,
  buildProductResults,
  buildPendingIntent,
  validatePendingIntent
} from "../../src/video_factory/application/static/studio-model.mjs";
import { cases } from "./fixture-data.mjs";

test('list sort handles Vietnamese names, numeric values and missing dates without mutating rows', () => {
  const rows = [{name:'Bánh 10',version:10,updated_at:null},{name:'Bánh 2',version:2,updated_at:'2026-11-01T01:00:00Z'},{name:'Áo',version:1,updated_at:'2026-10-01T01:00:00Z'}];
  assert.deepEqual(sortListRows(rows,'name','asc').map(r=>r.name),['Áo','Bánh 2','Bánh 10']);
  assert.deepEqual(sortListRows(rows,'version','desc').map(r=>r.version),[10,2,1]);
  assert.deepEqual(sortListRows(rows,'updated_at','asc').map(r=>r.version),[1,2,10]);
  assert.equal(rows[0].version,10);
});

test("batch selection includes safe product-lock checkpoints, not active runs", () => {
  assert.equal(canSelectCandidate({ status: "unprocessed" }), true);
  assert.equal(canSelectCandidate({ status: "awaiting_product_lock" }), true);
  assert.equal(canSelectCandidate({ status: "in_production" }), false);
  assert.equal(canSelectCandidate({ status: "waiting_for_operator" }), false);
  assert.equal(canSelectCandidate({ status: "blocked" }), false);
});

test("flattenBoard extracts cards with column names including unknown columns", () => {
  const cards = flattenBoard(cases.board.columns);
  assert.equal(cards.length, 4);

  const cols = cards.map((c) => c.column);
  assert.ok(cols.includes("in_production"));
  assert.ok(cols.includes("error"));
  assert.ok(cols.includes("completed"));
  assert.ok(cols.includes("future_state")); // Preserves unknown column!

  const futureCard = cards.find((c) => c.column === "future_state");
  assert.equal(futureCard.card.product_name, "Trạng thái mới");
});

test("flattenBoard handles empty or null columns safely", () => {
  assert.deepEqual(flattenBoard(null), []);
  assert.deepEqual(flattenBoard(undefined), []);
  assert.deepEqual(flattenBoard({ col1: null, col2: "not-array" }), []);
});

test("extractImageUrl extracts url from object contract or string", () => {
  // Real API object format
  assert.equal(
    extractImageUrl({ asset_url: "/api/assets/img1.jpg", original_url: "https://example.com/p1.jpg" }),
    "/api/assets/img1.jpg"
  );
  assert.equal(
    extractImageUrl({ original_url: "https://example.com/p1.jpg" }),
    "https://example.com/p1.jpg"
  );
  assert.equal(
    extractImageUrl({ url: "/fixture/preview.jpg" }),
    "/fixture/preview.jpg"
  );
  // Legacy string format
  assert.equal(extractImageUrl("/fixture/preview.jpg"), "/fixture/preview.jpg");
  // Empty or invalid
  assert.equal(extractImageUrl(null), null);
  assert.equal(extractImageUrl(undefined), null);
  assert.equal(extractImageUrl({}), null);
});

test("computeCandidateFingerprint computes consistent fingerprint and detects changes", () => {
  const cand = {
    product_key: "p1",
    name: "Sản phẩm A",
    brand: "Hãng A",
    model: "M1",
    status: "unprocessed",
    rejection_reason: null,
    images: [{ asset_url: "/img1.jpg" }]
  };

  const fp1 = computeCandidateFingerprint(cand);
  const fp2 = computeCandidateFingerprint({ ...cand });
  assert.equal(fp1, fp2);

  // Change name
  const fpNameChanged = computeCandidateFingerprint({ ...cand, name: "Sản phẩm B" });
  assert.notEqual(fp1, fpNameChanged);

  // Change model
  const fpModelChanged = computeCandidateFingerprint({ ...cand, model: "M2" });
  assert.notEqual(fp1, fpModelChanged);

  // Change status
  const fpStatusChanged = computeCandidateFingerprint({ ...cand, status: "invalid" });
  assert.notEqual(fp1, fpStatusChanged);

  // Change images
  const fpImagesChanged = computeCandidateFingerprint({ ...cand, images: [{ asset_url: "/img2.jpg" }] });
  assert.notEqual(fp1, fpImagesChanged);
});

test("classifyMedia classifies local paths without web url", () => {
  const origin = "http://127.0.0.1:18742";
  const r1 = classifyMedia("C:/worker/final.mp4", origin);
  assert.equal(r1.kind, "local");
  assert.equal(r1.url, null);

  const r2 = classifyMedia("D:\\media\\clip.mp4", origin);
  assert.equal(r2.kind, "local");
  assert.equal(r2.url, null);

  const r3 = classifyMedia("\\\\server\\share\\video.mp4", origin);
  assert.equal(r3.kind, "local");
  assert.equal(r3.url, null);

  const r4 = classifyMedia("file:///C:/worker/final.mp4", origin);
  assert.equal(r4.kind, "local");
  assert.equal(r4.url, null);
});

test("classifyMedia handles same-origin and external HTTP(S) URLs safely", () => {
  const origin = "http://127.0.0.1:18742";
  const same = classifyMedia("/fixture-media/preview.mp4", origin);
  assert.equal(same.kind, "same-origin");
  assert.equal(same.url, "http://127.0.0.1:18742/fixture-media/preview.mp4");

  const ext = classifyMedia("https://example.com/cdn/video7.mp4", origin);
  assert.equal(ext.kind, "external");
  assert.equal(ext.url, "https://example.com/cdn/video7.mp4");
});

test("classifyMedia blocks dangerous URLs (javascript, data, vbscript, credentials)", () => {
  const origin = "http://127.0.0.1:18742";
  assert.equal(classifyMedia("javascript:alert(1)", origin).kind, "unavailable");
  assert.equal(classifyMedia("data:text/html,<b>xss</b>", origin).kind, "unavailable");
  assert.equal(classifyMedia("vbscript:msgbox(1)", origin).kind, "unavailable");
  assert.equal(classifyMedia("http://admin:pass@example.com/video.mp4", origin).kind, "unavailable");
  assert.equal(classifyMedia("", origin).kind, "unavailable");
  assert.equal(classifyMedia(null, origin).kind, "unavailable");
});

test("readBatchReceipt parses started and skipped lists with reasons", () => {
  const receipt = readBatchReceipt({ status: 409, ok: false, body: cases.partial });
  assert.equal(receipt.unknown, false);
  assert.equal(receipt.message, "BATCH_START_FAILED");
  assert.equal(receipt.started.length, 1);
  assert.equal(receipt.started[0].product_key, "p1");
  assert.equal(receipt.started[0].workflow_id, "wf1");
  assert.equal(receipt.skipped.length, 1);
  assert.equal(receipt.skipped[0].product_key, "p2");
  assert.equal(receipt.skipped[0].reason, "MISSING_SOURCE");
});

test("readBatchReceipt marks unknown on 5xx status", () => {
  const receipt = readBatchReceipt({ status: 503, ok: false, body: { error: "SERVICE_UNAVAILABLE" } });
  assert.equal(receipt.unknown, true);
  assert.equal(receipt.message, "SERVICE_UNAVAILABLE");
});

test("shortenId truncates long UUIDs for UI display", () => {
  assert.equal(shortenId("12345678-1234-1234-1234-1234567890ab"), "12345678...90ab");
  assert.equal(shortenId("wf1"), "wf1");
  assert.equal(shortenId(null), "—");
});

test("getColumnLabel provides human-friendly labels while preserving raw column text", () => {
  assert.equal(getColumnLabel("in_production"), "Đang sản xuất");
  assert.equal(getColumnLabel("completed"), "Hoàn tất");
  assert.equal(getColumnLabel("error"), "Lỗi / Cần xử lý");
  assert.equal(getColumnLabel("future_state"), "future_state"); // Unrecognized column preserved!
});

test("filterCandidates filters by text search and status", () => {
  const list = cases.candidates.candidates;
  const filtered = filterCandidates(list, "Đang sản xuất", "all");
  assert.equal(filtered.length, 1);
  assert.equal(filtered[0].product_key, "p-running");

  const invalidOnly = filterCandidates(list, "", "invalid");
  assert.equal(invalidOnly.length, 1);
  assert.equal(invalidOnly[0].product_key, "p2");
});

test("buildProductResults hides identifiers and classifies product outcomes", () => {
  const candidates = [
    { product_key: "p1", name: "Thành công", status: "completed", workflow_id: "wf1" },
    { product_key: "p2", name: "Thiếu ảnh", status: "invalid", rejection_reason: "no readable image" },
    { product_key: "p6", name: "Thiếu variant", status: "invalid", rejection_reason: "PRODUCT_VARIANT_REQUIRED: verify the exact product variant in PI" },
    { product_key: "p3", name: "Đang chạy", status: "in_production", workflow_id: "wf3", qc_level: "low" },
    { product_key: "p4", name: "QC kỹ thuật", status: "completed", workflow_id: "wf4" },
    { product_key: "p5", name: "Đã block", status: "blocked", rejection_reason: "KEYFRAME_CONTENT_FAIL" }
  ];
  const results = buildProductResults(candidates, [], [
    { workflow_id: "wf1", product_key: "p1", result_status: "completed", video_url: "C:/out/p1.mp4" },
    { workflow_id: "wf4", product_key: "p4", result_status: "technical_error", technical_error: "SHA mismatch" }
  ]);

  assert.deepEqual(results.map(({ name, status }) => [name, status]), [
    ["Thành công", "completed"],
    ["Thiếu ảnh", "blocked"],
    ["Thiếu variant", "blocked"],
    ["Đang chạy", "running"],
    ["QC kỹ thuật", "technical_error"],
    ["Đã block", "blocked"]
  ]);
  assert.equal("workflow_id" in results[0], false);
  assert.equal(results[0].videoUrl, "C:/out/p1.mp4");
  assert.equal(results.find(({ name }) => name === "Đang chạy").qcLevel, "low");
  assert.match(results[1].fix, /ảnh/);
  const missingVariant = results.find(({ name }) => name === "Thiếu variant");
  assert.match(missingVariant.reason, /PRODUCT_VARIANT_REQUIRED/);
  assert.match(missingVariant.fix, /variant/);
  assert.deepEqual(results.summary, { completed: 1, blocked: 3, technicalError: 1, running: 1, waiting: 0 });
});

test("pendingIntent serialization and validation", () => {
  const intent = buildPendingIntent("start", ["p1", "p2"], "2026-10-24");
  assert.equal(intent.kind, "start");
  assert.deepEqual(intent.targets, ["p1", "p2"]);
  assert.equal(intent.sourceDate, "2026-10-24");
  assert.ok(intent.createdAt);

  assert.equal(validatePendingIntent(intent), true);
  assert.equal(validatePendingIntent({}), false);
  assert.equal(validatePendingIntent(null), false);
});


test("quarantine overrides stale in_production state after refresh", () => {
  const candidates = [{name: "Stopped", workflow_id: "wf-stopped", status: "in_production"}];
  const result = buildProductResults(candidates, {error: [{workflow_id: "wf-stopped", state: "rendering", quarantined_job: {reason: "download failed"}}]}, []);
  assert.equal(result[0].status, "technical_error");
  assert.equal(result.summary.running, 0);
  const unresolved = buildProductResults(candidates, {error: [{workflow_id: "wf-stopped", quarantined_job: {reason: "OPERATION_OUTCOME_UNCERTAIN"}}]}, []);
  assert.equal(unresolved[0].status, "waiting");
});

test("AwaitingSceneApproval remains waiting instead of being labeled running", () => {
  const candidates = [{ name: "Waiting", workflow_id: "wf-waiting", status: "in_production" }];
  const columns = {
    awaiting: [{ workflow_id: "wf-waiting", state: "AwaitingSceneApproval" }]
  };
  const result = buildProductResults(candidates, columns, []);

  assert.equal(result[0].status, "waiting");
  assert.match(result[0].label, /chờ/i);
  assert.equal(result.summary.running, 0);
  assert.equal(result.summary.waiting, 1);
});
