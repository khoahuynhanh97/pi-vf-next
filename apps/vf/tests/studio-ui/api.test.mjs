import test from "node:test";
import assert from "node:assert/strict";
import {
  requestJson,
  loadCandidates,
  loadBoard,
  loadDetail,
  loadVideos,
  loadProductWorkflows,
  startBatch,
  approveGate,
  retryQuarantinedWorkflow
} from "../../src/video_factory/application/static/studio-api.mjs";
import { cases } from "./fixture-data.mjs";

test("requestJson parses 200 OK JSON response", async () => {
  const mockFetch = async (path, opts) => ({
    status: 200,
    ok: true,
    text: async () => JSON.stringify({ ok: true, data: "hello" })
  });

  const res = await requestJson(mockFetch, "/test");
  assert.equal(res.status, 200);
  assert.equal(res.ok, true);
  assert.deepEqual(res.body, { ok: true, data: "hello" });
});

test("requestJson treats HTTP 200 with body.ok === false as ok: false", async () => {
  const mockFetch = async () => ({
    status: 200,
    ok: true,
    text: async () => JSON.stringify({ ok: false, error: "LOGICAL_FAIL" })
  });

  const res = await requestJson(mockFetch, "/test");
  assert.equal(res.status, 200);
  assert.equal(res.ok, false);
  assert.equal(res.body.error, "LOGICAL_FAIL");
});

test("requestJson preserves body for non-2xx status (409 Conflict partial)", async () => {
  const mockFetch = async () => ({
    status: 409,
    ok: false,
    text: async () => JSON.stringify(cases.partial)
  });

  const res = await requestJson(mockFetch, "/api/personal/product-video/start-batch", { method: "POST" });
  assert.equal(res.status, 409);
  assert.equal(res.ok, false);
  assert.equal(res.body.error, "BATCH_START_FAILED");
  assert.equal(res.body.started.length, 1);
  assert.equal(res.body.skipped.length, 1);
});

test("requestJson marks resultUnknown: true when POST fetch throws network/connection error", async () => {
  const mockFetch = async () => {
    throw new TypeError("Failed to fetch (network drop)");
  };

  await assert.rejects(
    async () => {
      await requestJson(mockFetch, "/api/personal/product-video/start-batch", { method: "POST" });
    },
    (err) => {
      assert.equal(err.resultUnknown, true);
      assert.ok(err.message.includes("Failed to fetch"));
      return true;
    }
  );
});

test("requestJson marks resultUnknown: false when GET fetch throws network error", async () => {
  const mockFetch = async () => {
    throw new TypeError("Failed to fetch (network drop)");
  };

  await assert.rejects(
    async () => {
      await requestJson(mockFetch, "/api/operator/kanban", { method: "GET" });
    },
    (err) => {
      assert.equal(err.resultUnknown, false);
      return true;
    }
  );
});

test("requestJson throws error with resultUnknown: true when POST returns non-JSON", async () => {
  const mockFetch = async () => ({
    status: 502,
    ok: false,
    text: async () => "<html>502 Bad Gateway</html>"
  });

  await assert.rejects(
    async () => {
      await requestJson(mockFetch, "/api/personal/product-video/start-batch", { method: "POST" });
    },
    (err) => {
      assert.equal(err.resultUnknown, true);
      assert.equal(err.status, 502);
      return true;
    }
  );
});

test("requestJson throws error with resultUnknown: false when GET returns non-JSON", async () => {
  const mockFetch = async () => ({
    status: 500,
    ok: false,
    text: async () => "Internal Error"
  });

  await assert.rejects(
    async () => {
      await requestJson(mockFetch, "/api/operator/kanban", { method: "GET" });
    },
    (err) => {
      assert.equal(err.resultUnknown, false);
      assert.equal(err.status, 500);
      return true;
    }
  );
});

test("loadCandidates sends GET to candidates with optional target_date", async () => {
  let calledPath = "";
  const mockFetch = async (path) => {
    calledPath = path;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify(cases.candidates)
    };
  };

  await loadCandidates(mockFetch, "2026-10-24");
  assert.equal(calledPath, "/api/personal/product-video/candidates?target_date=2026-10-24");

  await loadCandidates(mockFetch, null);
  assert.equal(calledPath, "/api/personal/product-video/candidates");

  await loadCandidates(mockFetch, "2026-10-24", null, "low");
  assert.equal(calledPath, "/api/personal/product-video/candidates?target_date=2026-10-24&qc_level=low");
  await loadCandidates(mockFetch, null, null, "middle");
  assert.equal(calledPath, "/api/personal/product-video/candidates?qc_level=middle");
});

test("startBatch defaults to high and transmits explicit low or middle unchanged", async () => {
  let sentBody;
  const mockFetch = async (_path, opts) => {
    sentBody = JSON.parse(opts.body);
    return { status: 202, ok: true, text: async () => JSON.stringify({ ok: true }) };
  };
  await startBatch(mockFetch, ["p1"], null, "low");
  assert.equal(sentBody.qc_level, "low");
  assert.deepEqual(sentBody.product_keys, ["p1"]);
  await startBatch(mockFetch, ["p1"]);
  assert.equal(sentBody.qc_level, "high");
  await startBatch(mockFetch, ["p1"], null, "middle");
  assert.equal(sentBody.qc_level, "middle");
});

test("loadBoard sends GET to kanban endpoint", async () => {
  let calledPath = "";
  const mockFetch = async (path) => {
    calledPath = path;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify(cases.board)
    };
  };

  const res = await loadBoard(mockFetch);
  assert.equal(calledPath, "/api/operator/kanban");
  assert.ok(res.body.columns.in_production);
});

test("loadDetail sends GET to workflow with URL encoding", async () => {
  let calledPath = "";
  const mockFetch = async (path) => {
    calledPath = path;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify(cases.detail)
    };
  };

  await loadDetail(mockFetch, "wf/1?special#1");
  assert.equal(calledPath, "/api/operator/workflow/wf%2F1%3Fspecial%231");
});

test("loadVideos sends GET to videos endpoint", async () => {
  let calledPath = "";
  const mockFetch = async (path) => {
    calledPath = path;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify(cases.videos)
    };
  };

  await loadVideos(mockFetch);
  assert.equal(calledPath, "/api/personal/product-video/videos");
});

test("loadProductWorkflows sends GET to product workflows endpoint", async () => {
  let calledPath = "";
  const mockFetch = async (path) => {
    calledPath = path;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify(cases.productWorkflows)
    };
  };

  await loadProductWorkflows(mockFetch, "p 1");
  assert.equal(calledPath, "/api/operator/product/p%201/workflows");
});

test("startBatch sends POST with exact contract body", async () => {
  let calledPath = "";
  let calledOpts = null;
  const mockFetch = async (path, opts) => {
    calledPath = path;
    calledOpts = opts;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify({ ok: true, started: [{ workflow_id: "wf1" }], skipped: [] })
    };
  };

  await startBatch(mockFetch, ["p1", "p2"], "2026-10-24");
  assert.equal(calledPath, "/api/personal/product-video/start-batch");
  assert.equal(calledOpts.method, "POST");
  assert.equal(calledOpts.headers["Content-Type"], "application/json");

  const sentBody = JSON.parse(calledOpts.body);
  assert.deepEqual(sentBody, {
    product_keys: ["p1", "p2"],
    auto: false,
    qc_level: "high",
    target_date: "2026-10-24"
  });
  // Ensure no uncontracted fields
  assert.equal(Object.keys(sentBody).length, 4);
});

test("approveGate sends POST with exact contract body and no cost/provider overrides", async () => {
  let calledPath = "";
  let calledOpts = null;
  const mockFetch = async (path, opts) => {
    calledPath = path;
    calledOpts = opts;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify({ workflow_id: "wf5", status: "approved" })
    };
  };

  await approveGate(mockFetch, "wf5", "creative_brief");
  assert.equal(calledPath, "/api/operator/approve");
  assert.equal(calledOpts.method, "POST");
  assert.equal(calledOpts.headers["Content-Type"], "application/json");

  const sentBody = JSON.parse(calledOpts.body);
  assert.deepEqual(sentBody, {
    workflow_id: "wf5",
    gate: "creative_brief",
    action: "approve"
  });
  // Ensure no uncontracted cost or provider fields are injected
  assert.equal(sentBody.qc_authorization, undefined);
  assert.equal(sentBody.qc_max_cost_usd, undefined);
  assert.equal(sentBody.render_provider_kind, undefined);
});

test("approveGate returns ok: false on 400 or 403 error responses", async () => {
  const mockFetch400 = async () => ({
    status: 400,
    ok: false,
    text: async () => JSON.stringify({ detail: "Only 'approve' action is currently supported" })
  });

  const res400 = await approveGate(mockFetch400, "wf5", "creative_brief");
  assert.equal(res400.status, 400);
  assert.equal(res400.ok, false);
  assert.equal(res400.body.detail, "Only 'approve' action is currently supported");

  const mockFetch403 = async () => ({
    status: 403,
    ok: false,
    text: async () => JSON.stringify({ ok: false, error: "FORBIDDEN" })
  });

  const res403 = await approveGate(mockFetch403, "wf5", "creative_brief");
  assert.equal(res403.status, 403);
  assert.equal(res403.ok, false);
  assert.equal(res403.body.error, "FORBIDDEN");
});

test("retryQuarantinedWorkflow posts once to the workflow recovery action", async () => {
  let calledPath = "";
  let calledOptions;
  const mockFetch = async (path, options) => {
    calledPath = path;
    calledOptions = options;
    return { status: 200, ok: true, text: async () => JSON.stringify({ ok: true, status: "recovered" }) };
  };

  const result = await retryQuarantinedWorkflow(mockFetch, "wf/brief 1");
  assert.equal(result.ok, true);
  assert.equal(calledPath, "/api/operator/workflow/wf%2Fbrief%201/retry-quarantined");
  assert.equal(calledOptions.method, "POST");
  assert.deepEqual(JSON.parse(calledOptions.body), {});
});
