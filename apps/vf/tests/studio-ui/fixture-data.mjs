export const cases = {
  candidates: {
    ok: true,
    source_date: "2026-10-24",
    candidates: [
      {
        product_key: "p1",
        name: "Sản phẩm mẫu",
        brand: "Hãng mẫu",
        model: "M1",
        status: "unprocessed",
        images: [
          {
            asset_id: "img-01",
            asset_url: "/fixture-media/img1.jpg",
            original_url: "https://example.com/cdn/p1.jpg"
          }
        ],
        rejection_reason: null
      },
      {
        product_key: "p-running",
        name: "Đang sản xuất",
        brand: "Hãng mẫu",
        model: "M-running",
        workflow_id: "wf1",
        status: "in_production",
        images: [],
        rejection_reason: null
      },
      {
        product_key: "p-done",
        name: "Đã xong",
        brand: "Hãng mẫu",
        model: "M-done",
        workflow_id: "wf3",
        status: "completed",
        images: [],
        rejection_reason: null
      },
      {
        product_key: "p-technical",
        name: "Lỗi export",
        brand: "Hãng mẫu",
        model: "M-error",
        workflow_id: "wf4",
        status: "completed",
        images: [],
        rejection_reason: null
      },
      {
        product_key: "p2",
        name: "Thiếu nguồn",
        brand: "Hãng mẫu",
        model: "M2",
        status: "invalid",
        images: [],
        rejection_reason: "MISSING_SOURCE"
      },
      {
        product_key: "p-xss",
        name: "<img src=x onerror=alert(1)> XSS Test",
        brand: "<b>Bold</b>",
        model: "X1",
        status: "unprocessed",
        images: [
          {
            asset_id: "img-xss",
            asset_url: "javascript:alert('xss')",
            original_url: "javascript:alert('xss')"
          }
        ],
        rejection_reason: null
      }
    ]
  },
  candidatesChangedData: {
    ok: true,
    source_date: "2026-10-24",
    candidates: [
      {
        product_key: "p1",
        name: "Sản phẩm mẫu ĐÃ ĐỔI TÊN",
        brand: "Hãng mẫu",
        model: "M1-Changed",
        status: "unprocessed",
        images: [
          {
            asset_id: "img-01-changed",
            asset_url: "/fixture-media/img1-changed.jpg",
            original_url: "https://example.com/cdn/p1-changed.jpg"
          }
        ],
        rejection_reason: null
      },
      {
        product_key: "p2",
        name: "Thiếu nguồn",
        brand: "Hãng mẫu",
        model: "M2",
        status: "invalid",
        images: [],
        rejection_reason: "MISSING_SOURCE"
      }
    ]
  },
  board: {
    columns: {
      in_production: [
        {
          workflow_id: "wf1",
          product_name: "Sản phẩm mẫu",
          state: "rendering",
          version: 3,
          created_at: "2026-10-24T00:00:00Z"
        }
      ],
      error: [
        {
          workflow_id: "wf2",
          product_name: "Cần kiểm tra",
          state: "failed",
          version: 2
        }
      ],
      completed: [
        {
          workflow_id: "wf3",
          product_name: "Đã xong",
          state: "completed",
          version: 8
        }
      ],
      future_state: [
        {
          workflow_id: "wf4",
          product_name: "Trạng thái mới",
          state: "new_state",
          version: 1
        }
      ]
    },
    timestamp: "2026-10-24T00:00:00Z"
  },
  detail: {
    workflow_id: "wf1",
    state: "rendering",
    version: 3,
    pending_approval_gate: null,
    source_context: {
      product_key: "p1",
      brand: "Hãng mẫu",
      model: "M1"
    },
    creative_brief: {
      concept: "Quảng cáo hiện đại",
      tone: "năng động"
    },
    scene_plan: {
      scenes: [
        { scene_order: 1, narration: "Giới thiệu" },
        { scene_order: 2, narration: "Tính năng" }
      ]
    },
    scene_operations: [
      {
        scene_order: 1,
        attempt: 1,
        status: "succeeded",
        operation_id: "op1",
        output_path: "C:/worker/clip.mp4",
        updated_at: "2026-10-24T00:00:00Z"
      }
    ],
    qc_reports: [
      {
        stage: "video",
        verdict: "PASS",
        score: 0.95,
        evaluator_name: "auto-qc",
        created_at: "2026-10-24T00:05:00Z"
      }
    ],
    budget_ledger: [
      {
        reservation_id: "res1",
        capability_id: "render",
        amount: 0.15,
        currency: "USD",
        state: "settled",
        created_at: "2026-10-24T00:01:00Z"
      }
    ],
    approval_history: []
  },
  partial: {
    ok: false,
    error: "BATCH_START_FAILED",
    started: [
      { product_key: "p1", workflow_id: "wf1" }
    ],
    skipped: [
      { product_key: "p2", reason: "MISSING_SOURCE" }
    ],
    started_workflows: ["wf1"]
  },
  approvePending: {
    workflow_id: "wf5",
    product_name: "Sản phẩm chờ duyệt",
    state: "AwaitingBriefApproval",
    version: 4,
    pending_approval_gate: "creative_brief",
    pending_approval_id: "ap1",
    source_context: { product_key: "p5" }
  },
  approveChanged: {
    workflow_id: "wf5",
    product_name: "Sản phẩm chờ duyệt",
    state: "rendering",
    version: 5,
    pending_approval_gate: null,
    pending_approval_id: null
  },
  videos: {
    ok: true,
    videos: [
      {
        workflow_id: "wf3",
        product_key: "p-done",
        product_name: "Đã xong",
        video_url: "/fixture-media/preview.mp4",
        qc_verdict: null,
        result_status: "completed"
      },
      {
        workflow_id: "wf6",
        product_name: "Path local",
        video_url: "C:/worker/final.mp4",
        qc_verdict: null,
        result_status: "completed"
      },
      {
        workflow_id: "wf7",
        product_name: "Link ngoài",
        video_url: "https://example.com/cdn/video7.mp4",
        qc_verdict: null,
        result_status: "completed"
      },
      {
        workflow_id: "wf8",
        product_name: "XSS URL",
        video_url: "javascript:alert('xss')",
        qc_verdict: null,
        result_status: "completed"
      },
      {
        workflow_id: "wf4",
        product_key: "p-technical",
        product_name: "Lỗi export",
        video_url: null,
        result_status: "technical_error",
        technical_error: "SHA MP4 không khớp"
      }
    ]
  },
  productWorkflows: {
    ok: true,
    product_key: "p1",
    workflows: [
      {
        workflow_id: "wf1",
        state: "rendering",
        version: 3,
        created_at: "2026-10-24T00:00:00Z",
        video_url: null,
        thumbnail_url: null
      }
    ]
  }
};
