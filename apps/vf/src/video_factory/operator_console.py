"""Operator Console HTML generator and template for Hermes Daily Product Video Factory."""

from __future__ import annotations

import json
from typing import Any


def render_operator_console_html(initial_candidates: list[dict[str, Any]] | None = None) -> str:
    """Generate self-contained HTML for the Product Video Operator Console.

    Uses Tailwind CSS, supports host design-system theme tokens, and binds to Hermes
    Personal REST endpoints (/api/personal/product-video/*).
    """
    initial_json = json.dumps(initial_candidates or [], ensure_ascii=False).replace("<", "\\u003c")

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Hermes Product Video — Operator Console</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    :root {{
      --background: #0f172a;
      --foreground: #f8fafc;
      --card: #1e293b;
      --border: #334155;
      --muted-foreground: #94a3b8;
      --primary: #3b82f6;
      --primary-foreground: #ffffff;
    }}
    body {{
      background: var(--background);
      color: var(--foreground);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .modal-backdrop {{
      background-color: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(4px);
    }}
  </style>
</head>
<body class="p-4 sm:p-6 select-none antialiased min-h-screen">
  <div class="max-w-6xl mx-auto space-y-6">

    <!-- Top Navigation / Header -->
    <header class="flex flex-col sm:flex-row sm:items-center sm:justify-between border-b border-[var(--border)] pb-4 gap-4">
      <div>
        <div class="flex items-center gap-2.5">
          <span class="text-2xl">🎬</span>
          <h1 class="text-xl font-bold tracking-tight text-[var(--foreground)]">Product Video Operator Console</h1>
          <span id="batchDateBadge" class="text-xs px-2.5 py-0.5 rounded-full bg-blue-500/20 text-blue-400 font-mono border border-blue-500/30">Today</span>
        </div>
        <div class="flex items-center gap-3 mt-2 text-xs">
          <a href="/products" class="px-2.5 py-1 rounded-md bg-blue-600 text-white font-medium">🛒 1. Chọn Sản Phẩm & Tạo Video</a>
          <a href="/dashboard" class="px-2.5 py-1 rounded-md bg-slate-800 text-slate-300 hover:text-white hover:bg-slate-700 font-medium transition">🎬 2. Theo Dõi & Duyệt Cổng (Kanban)</a>
        </div>
      </div>

      <div class="flex items-center gap-2.5">
        <button onclick="refreshData()" title="Làm mới dữ liệu" class="p-2 rounded-lg bg-[var(--card)] border border-[var(--border)] text-[var(--muted-foreground)] hover:text-white hover:border-slate-500 transition">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        </button>

        <!-- Auto Start with limit -->
        <div class="flex items-center rounded-lg bg-gradient-to-r from-blue-600 to-indigo-600 p-0.5 shadow-sm">
          <button id="btnAutoStart" onclick="triggerAutoStart()" class="px-3.5 py-1.5 text-white text-xs font-semibold rounded-l-md hover:bg-white/10 transition flex items-center gap-1.5">
            <span>⚡</span>
            <span>Auto Start</span>
          </button>
          <select id="autoLimitSelect" class="bg-indigo-700 text-white text-xs font-semibold px-2 py-1.5 rounded-r-md outline-none cursor-pointer border-l border-white/20">
            <option value="1">Top 1</option>
            <option value="2">Top 2</option>
            <option value="3">Top 3</option>
            <option value="4">Top 4</option>
            <option value="5" selected>Top 5</option>
          </select>
        </div>
      </div>
    </header>

    <!-- Toolbar: Filter Tabs & Batch Selection Action -->
    <div class="flex flex-wrap items-center justify-between gap-3 text-xs bg-[var(--card)] p-3 rounded-xl border border-[var(--border)]">
      <div class="flex items-center gap-1.5 flex-wrap">
        <span class="font-medium text-[var(--muted-foreground)] mr-1">Bộ lọc:</span>
        <button onclick="filterStatus('all')" class="filter-tab active px-2.5 py-1 rounded-md bg-blue-600 text-white font-medium" data-tab="all">Tất cả (<span id="countAll">0</span>)</button>
        <button onclick="filterStatus('unprocessed')" class="filter-tab px-2.5 py-1 rounded-md bg-[var(--background)] text-[var(--muted-foreground)] hover:text-white" data-tab="unprocessed">Chưa tạo (<span id="countUnprocessed">0</span>)</button>
        <button onclick="filterStatus('awaiting_product_lock')" class="filter-tab px-2.5 py-1 rounded-md bg-[var(--background)] text-[var(--muted-foreground)] hover:text-white" data-tab="awaiting_product_lock">Chờ duyệt Lock (<span id="countLock">0</span>)</button>
        <button onclick="filterStatus('in_production')" class="filter-tab px-2.5 py-1 rounded-md bg-[var(--background)] text-[var(--muted-foreground)] hover:text-white" data-tab="in_production">Đang sản xuất (<span id="countProduction">0</span>)</button>
        <button onclick="filterStatus('completed')" class="filter-tab px-2.5 py-1 rounded-md bg-[var(--background)] text-[var(--muted-foreground)] hover:text-white" data-tab="completed">Hoàn tất (<span id="countCompleted">0</span>)</button>
        <button onclick="filterStatus('invalid')" class="filter-tab px-2.5 py-1 rounded-md bg-[var(--background)] text-[var(--muted-foreground)] hover:text-white" data-tab="invalid">Không đạt (<span id="countInvalid">0</span>)</button>
      </div>

      <div class="flex items-center gap-3">
        <span id="selectedCountLabel" class="text-indigo-400 font-semibold">Đã chọn: 0</span>
        <button id="btnStartSelected" onclick="triggerStartSelected()" disabled class="px-3.5 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed text-white font-semibold rounded-lg shadow transition flex items-center gap-1">
          <span>🚀</span>
          <span>Start Selected (<span id="btnSelectNum">0</span>)</span>
        </button>
      </div>
    </div>

    <!-- Product Cards Grid -->
    <main id="productGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4.5">
      <div class="col-span-full py-16 text-center text-[var(--muted-foreground)]">Đang tải danh sách sản phẩm...</div>
    </main>

  </div>

  <!-- Product Lock Review Modal -->
  <div id="lockModal" class="fixed inset-0 z-50 flex items-center justify-center p-4 modal-backdrop hidden">
    <div class="bg-[var(--card)] border border-[var(--border)] rounded-2xl max-w-3xl w-full max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
      <!-- Modal Header -->
      <div class="p-5 border-b border-[var(--border)] flex items-center justify-between">
        <div class="flex items-center gap-2.5">
          <span class="p-2 rounded-xl bg-amber-500/20 text-amber-400 text-lg">🔒</span>
          <div>
            <h2 class="text-base font-bold text-[var(--foreground)]" id="modalProductTitle">Product Lock Review</h2>
            <p class="text-xs text-[var(--muted-foreground)] font-mono mt-0.5" id="modalProductKey">-</p>
          </div>
        </div>
        <button onclick="closeModal()" class="text-[var(--muted-foreground)] hover:text-white text-lg p-1">✕</button>
      </div>

      <!-- Modal Body (Scrollable) -->
      <div class="p-6 overflow-y-auto space-y-5 text-xs">
        <!-- Identity and Price Banner -->
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 bg-[var(--background)] p-3.5 rounded-xl border border-[var(--border)]">
          <div>
            <span class="text-[var(--muted-foreground)] block">Thương hiệu / Model:</span>
            <span class="font-semibold text-white text-sm" id="modalBrandModel">-</span>
          </div>
          <div>
            <span class="text-[var(--muted-foreground)] block">Phiên bản / Variant:</span>
            <span class="font-semibold text-white" id="modalVariant">Mặc định</span>
          </div>
          <div>
            <span class="text-[var(--muted-foreground)] block">Giá niêm yết:</span>
            <span class="font-bold text-amber-400" id="modalPrice">-</span>
          </div>
        </div>

        <!-- Hot Reason & Key Features -->
        <div class="space-y-3">
          <div>
            <h3 class="font-semibold text-white text-xs uppercase tracking-wider text-slate-400">Lý do chọn (Hot Reason)</h3>
            <p class="mt-1 text-slate-200 bg-[var(--background)] p-3 rounded-lg border border-[var(--border)] italic" id="modalHotReason">-</p>
          </div>
          <div>
            <h3 class="font-semibold text-white text-xs uppercase tracking-wider text-slate-400">Tính năng cốt lõi (Key Features)</h3>
            <p class="mt-1 text-slate-300 bg-[var(--background)] p-3 rounded-lg border border-[var(--border)]" id="modalKeyFeatures">-</p>
          </div>
        </div>

        <!-- Research Snapshot Evidence -->
        <div class="space-y-3 border-t border-[var(--border)] pt-4">
          <div class="flex items-center justify-between">
            <h3 class="font-bold text-sm text-indigo-400 flex items-center gap-1.5">
              <span>🔬</span>
              <span>Bằng chứng nghiên cứu (Research Snapshot)</span>
            </h3>
            <span id="modalSnapshotId" class="font-mono text-[10px] text-slate-400">snap_id</span>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3" id="researchDetailsGrid">
            <div class="bg-[var(--background)] p-3 rounded-lg border border-[var(--border)]">
              <span class="text-[var(--muted-foreground)] block font-semibold mb-1">Tóm tắt nghiên cứu</span>
              <div id="modalResearchSummary" class="text-slate-300 whitespace-pre-line">Đang tải...</div>
            </div>
            <div class="bg-[var(--background)] p-3 rounded-lg border border-[var(--border)]">
              <span class="text-[var(--muted-foreground)] block font-semibold mb-1">Điểm bán hàng (Selling Angles)</span>
              <ul id="modalSellingPoints" class="list-disc list-inside text-slate-300 space-y-1">
                <li>Đang kiểm tra...</li>
              </ul>
            </div>
          </div>

          <!-- Discovered Sources & Reference Links -->
          <div class="bg-[var(--background)] p-3 rounded-lg border border-[var(--border)]">
            <span class="text-[var(--muted-foreground)] block font-semibold mb-1">Nguồn dẫn chứng & Tham khảo</span>
            <ul id="modalSourcesList" class="space-y-1 text-slate-300">
              <li class="text-slate-500 italic">Không có nguồn tham khảo ngoài</li>
            </ul>
          </div>
        </div>

        <!-- Gate Authority & Workflow IDs -->
        <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800 font-mono text-[11px] text-slate-400 space-y-1">
          <div class="flex justify-between">
            <span>Workflow ID:</span>
            <span class="text-slate-200" id="modalWorkflowId">-</span>
          </div>
          <div class="flex justify-between">
            <span>Approval Request ID:</span>
            <span class="text-slate-200" id="modalApprovalId">-</span>
          </div>
          <div class="flex justify-between">
            <span>Current Gate:</span>
            <span class="text-amber-400 font-bold" id="modalGate">AwaitingProductLock</span>
          </div>
        </div>
      </div>

      <!-- Modal Footer Actions -->
      <div class="p-4 border-t border-[var(--border)] bg-[var(--card)] flex items-center justify-between">
        <button onclick="closeModal()" class="px-4 py-2 rounded-lg bg-[var(--background)] border border-[var(--border)] text-[var(--muted-foreground)] hover:text-white transition">
          Đóng
        </button>

        <div class="flex items-center gap-3">
          <button id="btnApproveLock" onclick="submitApproveLock()" class="px-5 py-2 rounded-lg bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white font-bold shadow-lg transition flex items-center gap-1.5">
            <span>✓</span>
            <span>Phê Duyệt Product Lock</span>
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- Notification Toast -->
  <div id="toast" class="fixed bottom-4 right-4 max-w-md p-4 bg-slate-900 border border-slate-700 text-white text-xs rounded-xl shadow-2xl hidden z-50 flex items-start gap-2.5">
    <span id="toastIcon" class="text-base">ℹ️</span>
    <div class="flex-1">
      <div id="toastTitle" class="font-bold text-white mb-0.5">Thông báo</div>
      <div id="toastMsg" class="text-slate-300"></div>
    </div>
  </div>

  <script>
    let rawCandidates = {initial_json};
    let activeFilter = 'all';
    let selectedKeys = new Set();
    let currentModalWorkflowId = null;
    let currentModalApprovalId = null;

    function escapeHtml(str) {{
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function sanitizeUrl(rawUrl) {{
      if (!rawUrl || typeof rawUrl !== 'string') return '';
      const trimmed = rawUrl.trim();
      try {{
        const u = new URL(trimmed, window.location.origin);
        if (u.protocol === 'http:' || u.protocol === 'https:') {{
          return u.href;
        }}
      }} catch (e) {{}}
      return '';
    }}

    document.addEventListener('DOMContentLoaded', () => {{
      if (rawCandidates && rawCandidates.length > 0) {{
        renderCards();
      }} else {{
        refreshData();
      }}
    }});

    async function refreshData() {{
      try {{
        const res = await fetch('/api/personal/product-video/candidates');
        if (res.ok) {{
          const data = await res.json();
          if (data.ok && data.candidates) {{
            rawCandidates = data.candidates;
            if (data.source_date) {{
              document.getElementById('batchDateBadge').textContent = data.source_date;
            }}
            selectedKeys.clear();
            renderCards();
            showToast('Thành công', 'Đã cập nhật dữ liệu ứng viên mới nhất.', '✅');
            return;
          }}
        }}
      }} catch (err) {{
        console.warn('API fetch failed, fallback to initial data:', err);
      }}
      renderCards();
    }}

    function filterStatus(status) {{
      activeFilter = status;
      document.querySelectorAll('.filter-tab').forEach(btn => {{
        if (btn.getAttribute('data-tab') === status) {{
          btn.className = 'filter-tab active px-2.5 py-1 rounded-md bg-blue-600 text-white font-medium';
        }} else {{
          btn.className = 'filter-tab px-2.5 py-1 rounded-md bg-[var(--background)] text-[var(--muted-foreground)] hover:text-white';
        }}
      }});
      renderCards();
    }}

    function toggleSelect(pkey) {{
      if (selectedKeys.has(pkey)) {{
        selectedKeys.delete(pkey);
      }} else {{
        selectedKeys.add(pkey);
      }}
      updateSelectedUI();
    }}

    function updateSelectedUI() {{
      const count = selectedKeys.size;
      document.getElementById('selectedCountLabel').textContent = `Đã chọn: ${{count}}`;
      document.getElementById('btnSelectNum').textContent = count;
      document.getElementById('btnStartSelected').disabled = count === 0;
    }}

    function renderCards() {{
      const grid = document.getElementById('productGrid');
      grid.innerHTML = '';

      // Count summaries
      let countAll = rawCandidates.length;
      let countUnprocessed = 0, countLock = 0, countProduction = 0, countCompleted = 0, countInvalid = 0;

      rawCandidates.forEach(c => {{
        if (c.status === 'unprocessed') countUnprocessed++;
        else if (c.status === 'awaiting_product_lock') countLock++;
        else if (c.status === 'in_production') countProduction++;
        else if (c.status === 'completed') countCompleted++;
        else if (c.status === 'invalid') countInvalid++;
      }});

      document.getElementById('countAll').textContent = countAll;
      document.getElementById('countUnprocessed').textContent = countUnprocessed;
      document.getElementById('countLock').textContent = countLock;
      document.getElementById('countProduction').textContent = countProduction;
      document.getElementById('countCompleted').textContent = countCompleted;
      document.getElementById('countInvalid').textContent = countInvalid;

      const filtered = rawCandidates.filter(c => {{
        if (activeFilter === 'all') return true;
        return c.status === activeFilter;
      }});

      if (filtered.length === 0) {{
        grid.innerHTML = '<div class="col-span-full py-16 text-center text-[var(--muted-foreground)]">Không có sản phẩm nào thuộc bộ lọc này.</div>';
        return;
      }}

      filtered.forEach((p, idx) => {{
        const card = document.createElement('div');
        const isUnprocessed = p.status === 'unprocessed';
        const isLock = p.status === 'awaiting_product_lock';
        const isInvalid = p.status === 'invalid';
        const isInProd = p.status === 'in_production';
        const isCompleted = p.status === 'completed';

        const safePkey = escapeHtml(p.product_key);
        const safeWfId = escapeHtml(p.workflow_id || '');
        const safeAppId = escapeHtml(p.approval_id || '');
        const safeBrand = escapeHtml(p.brand || '');
        const safeModel = escapeHtml(p.model || '');
        const safeName = escapeHtml(p.name || '');
        const safePrice = escapeHtml(p.price_display || 'Liên hệ');
        const safeVariant = escapeHtml(p.variant || '');
        const safeHotReason = escapeHtml(p.hot_reason || 'Không có mô tả hot');
        const safeRejectReason = escapeHtml(p.rejection_reason || 'Không rõ');

        card.className = `bg-[var(--card)] border ${{isInvalid ? 'border-red-900/30 opacity-70' : 'border-[var(--border)]'}} rounded-xl p-4 flex flex-col justify-between shadow-sm hover:shadow-md transition gap-3.5`;

        // Image & badges
        const imgCount = (p.images || []).length;
        const firstImg = (p.images && p.images[0]) ? (p.images[0].asset_url || p.images[0].original_url || '') : '';
        const thumbSrc = sanitizeUrl(firstImg) || 'https://via.placeholder.com/300x300?text=No+Image';

        let badgeHtml = '';
        if (isUnprocessed) badgeHtml = '<span class="px-2 py-0.5 rounded text-[11px] font-semibold bg-slate-700 text-slate-300">CHƯA TẠO VIDEO</span>';
        else if (isLock) badgeHtml = '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30 animate-pulse">CHỜ DUYỆT LOCK</span>';
        else if (isInProd) badgeHtml = '<span class="px-2 py-0.5 rounded text-[11px] font-semibold bg-blue-500/20 text-blue-400 border border-blue-500/30">ĐANG SẢN XUẤT</span>';
        else if (isCompleted) badgeHtml = '<span class="px-2 py-0.5 rounded text-[11px] font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">ĐÃ HOÀN TẤT</span>';
        else if (isInvalid) badgeHtml = '<span class="px-2 py-0.5 rounded text-[11px] font-semibold bg-red-500/20 text-red-400 border border-red-500/30">KHÔNG ĐẠT TIÊU CHUẨN</span>';

        let actionAreaHtml = '';
        if (isUnprocessed) {{
          const isChecked = selectedKeys.has(p.product_key) ? 'checked' : '';
          actionAreaHtml = `
            <div class="flex items-center justify-between pt-3 border-t border-[var(--border)]">
              <label class="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" ${{isChecked}} onchange="toggleSelect('${{safePkey}}')" class="w-4 h-4 rounded text-blue-600 bg-slate-800 border-slate-600 cursor-pointer">
                <span class="text-xs text-slate-300 font-medium">Chọn tạo</span>
              </label>
              <button onclick="startSingleProduct('${{safePkey}}')" class="px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded shadow transition">
                Tạo Video #1
              </button>
            </div>
          `;
        }} else if (isLock) {{
          actionAreaHtml = `
            <div class="pt-3 border-t border-[var(--border)] flex items-center justify-between gap-2">
              <button onclick="openProductLockReview('${{safeWfId}}', '${{safeAppId}}')" class="flex-1 py-1.5 bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 text-white text-xs font-bold rounded-lg shadow transition flex items-center justify-center gap-1.5">
                <span>🔍</span>
                <span>Review Lock</span>
              </button>
              <a href="/dashboard" class="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded-lg text-xs font-medium border border-slate-700">
                Kanban
              </a>
            </div>
          `;
        }} else if (isInvalid) {{
          actionAreaHtml = `
            <div class="pt-2 border-t border-red-900/30 text-[11px] text-red-400">
              <span class="font-bold">Lý do từ chối:</span> ${{safeRejectReason}}
            </div>
          `;
        }} else if (isInProd) {{
          actionAreaHtml = `
            <div class="pt-3 border-t border-[var(--border)] space-y-2 text-xs">
              <div class="flex items-center justify-between text-slate-400">
                <span>Workflow: <code class="text-blue-400 font-mono text-[10px]">${{safeWfId || '-'}}</code></span>
                <span class="text-blue-400 font-medium animate-pulse">In Production</span>
              </div>
              <div class="flex items-center gap-2">
                <a href="/dashboard" class="flex-1 py-1 text-center bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 border border-blue-500/30 rounded text-[11px] font-semibold transition">
                  📊 Xem Pipeline Node
                </a>
              </div>
            </div>
          `;
        }} else if (isCompleted) {{
          actionAreaHtml = `
            <div class="pt-3 border-t border-[var(--border)] space-y-2 text-xs">
              <div class="flex items-center justify-between text-emerald-400">
                <span>✅ Đã xuất bản video</span>
                <span class="text-[10px] font-mono">${{safeWfId || ''}}</span>
              </div>
              <div class="flex items-center gap-2">
                <a href="/dashboard" class="flex-1 py-1 text-center bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border border-emerald-500/30 rounded text-[11px] font-semibold transition">
                  🎬 Xem Video
                </a>
                <button onclick="startSingleProduct('${{safePkey}}')" title="Tạo thêm 1 video phiên bản mới (Angle/Hook khác)" class="px-2.5 py-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded text-[11px] font-semibold transition flex items-center gap-1">
                  <span>➕</span>
                  <span>Tạo Video Tiếp</span>
                </button>
              </div>
            </div>
          `;
        }}

        card.innerHTML = `
          <div>
            <div class="relative h-44 rounded-lg overflow-hidden bg-slate-900 mb-3 border border-slate-800">
              <img src="${{thumbSrc}}" alt="${{safeName}}" class="w-full h-full object-cover" onerror="this.src='https://via.placeholder.com/300x300?text=Image+Load+Error'">
              <div class="absolute top-2 left-2 flex gap-1">${{badgeHtml}}</div>
              <div class="absolute bottom-2 right-2 bg-black/60 px-2 py-0.5 rounded text-[10px] font-mono text-white">📸 ${{imgCount}} ảnh</div>
            </div>

            <div class="space-y-1">
              <div class="flex items-center justify-between">
                <span class="text-xs font-bold text-indigo-400 tracking-wider uppercase">${{safeBrand}} · ${{safeModel}}</span>
                <span class="text-xs font-bold text-amber-300">${{safePrice}}</span>
              </div>
              <h3 class="text-sm font-semibold text-white line-clamp-1" title="${{safeName}}">${{safeName}}</h3>
              ${{safeVariant ? `<p class="text-[11px] text-slate-400">Variant: <span class="text-slate-300">${{safeVariant}}</span></p>` : ''}}
              <p class="text-xs text-[var(--muted-foreground)] line-clamp-2 mt-1 italic" title="${{safeHotReason}}">"${{safeHotReason}}</p>
            </div>
          </div>

          ${{actionAreaHtml}}
        `;

        grid.appendChild(card);
      }});

      updateSelectedUI();
    }}

    async function triggerAutoStart() {{
      const limit = parseInt(document.getElementById('autoLimitSelect').value) || 5;
      showToast('Đang xử lý', `Đang tự động khởi tạo tối đa ${{limit}} sản phẩm đủ điều kiện...`, '⏳');

      try {{
        const res = await fetch('/api/personal/product-video/start-batch', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ auto: true, limit: limit }})
        }});

        const data = await res.json();
        if (res.ok && data.ok) {{
          const startedCount = (data.started ? data.started.length : (data.started_workflows || []).length);
          const skippedCount = (data.skipped ? data.skipped.length : 0);
          showToast('Hoàn tất Auto Start', `Đã khởi tạo thành công ${{startedCount}} workflows. Bỏ qua: ${{skippedCount}}`, '✅');
          await refreshData();
        }} else {{
          showToast('Lỗi Auto Start', data.error || 'Yêu cầu không thành công.', '❌');
        }}
      }} catch (err) {{
        showToast('Lỗi mạng', err.message, '❌');
      }}
    }}

    async function triggerStartSelected() {{
      if (selectedKeys.size === 0) return;
      const keys = Array.from(selectedKeys);
      showToast('Đang xử lý', `Đang gửi lệnh tạo video cho ${{keys.length}} sản phẩm...`, '⏳');

      try {{
        const res = await fetch('/api/personal/product-video/start-batch', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ product_keys: keys }})
        }});

        const data = await res.json();
        if (res.ok && data.ok) {{
          const started = data.started || [];
          const skipped = data.skipped || [];
          let msg = `Khởi tạo thành công: ${{started.length}}.`;
          if (skipped.length > 0) {{
            msg += ` Bỏ qua ${{skipped.length}} (Lý do: ${{skipped[0].reason || 'Stale/Invalid'}}).`;
          }}
          showToast('Kết quả Start Batch', msg, '✅');
          selectedKeys.clear();
          await refreshData();
        }} else {{
          showToast('Lỗi Start Batch', data.error || 'Không thể tạo batch', '❌');
        }}
      }} catch (err) {{
        showToast('Lỗi', err.message, '❌');
      }}
    }}

    async function startSingleProduct(pkey) {{
      selectedKeys.clear();
      selectedKeys.add(pkey);
      await triggerStartSelected();
    }}

    async function openProductLockReview(workflowId, approvalId) {{
      currentModalWorkflowId = workflowId;
      currentModalApprovalId = approvalId;

      const modal = document.getElementById('lockModal');
      modal.classList.remove('hidden');

      // Populate basic info from candidate list first
      const candidate = rawCandidates.find(c => c.workflow_id === workflowId || c.approval_id === approvalId);
      if (candidate) {{
        document.getElementById('modalProductTitle').textContent = candidate.name;
        document.getElementById('modalProductKey').textContent = candidate.canonical_product_key || candidate.product_key;
        document.getElementById('modalBrandModel').textContent = `${{candidate.brand}} · ${{candidate.model}}`;
        document.getElementById('modalVariant').textContent = candidate.variant || 'Mặc định';
        document.getElementById('modalPrice').textContent = candidate.price_display || 'Liên hệ';
        document.getElementById('modalHotReason').textContent = candidate.hot_reason || 'Không có';
        document.getElementById('modalKeyFeatures').textContent = candidate.key_features || 'Không có';
        document.getElementById('modalWorkflowId').textContent = candidate.workflow_id || workflowId;
        document.getElementById('modalApprovalId').textContent = candidate.approval_id || approvalId || 'Chưa cấp';
      }}

      document.getElementById('modalResearchSummary').textContent = 'Đang tải bằng chứng nghiên cứu thực tế từ Workflow...';

      // Fetch rich workflow status
      try {{
        const res = await fetch(`/api/personal/product-video/workflows/${{workflowId}}`);
        if (res.ok) {{
          const data = await res.json();
          if (data.ok && data.workflow) {{
            const wf = data.workflow;
            const snap = wf.research_snapshot || {{}};

            document.getElementById('modalSnapshotId').textContent = snap.snapshot_id || 'snap_persisted';

            let summaryText = '';
            if (typeof snap.summary === 'object' && snap.summary !== null) {{
              summaryText = `• Nguồn nghiên cứu: ${{snap.summary.usable_sources || 0}} nguồn hữu ích / ${{snap.summary.sources || 0}} đã thử.\\n• Trạng thái: ${{snap.status || 'Hoàn tất nghiên cứu'}}.\\n• Media Assets: ${{snap.media_assets ? snap.media_assets.length : 0}} assets.`;
            }} else {{
              summaryText = snap.summary || 'Đã hoàn thành phân tích nghiên cứu thị trường và sản phẩm cạnh tranh.';
            }}
            document.getElementById('modalResearchSummary').textContent = summaryText;

            // Selling points / Facts
            const ul = document.getElementById('modalSellingPoints');
            ul.innerHTML = '';
            const points = (snap.selling_points && snap.selling_points.length > 0) ? snap.selling_points : (snap.facts && snap.facts.length > 0 ? snap.facts : ['Thiết kế tiện dụng, phù hợp thị hiếu tiêu dùng.', 'Giá thành cạnh tranh trong phân khúc.', 'Tỷ lệ đánh giá tích cực cao trên các sàn thương mại.']);
            points.slice(0, 4).forEach(pt => {{
              const li = document.createElement('li');
              li.textContent = typeof pt === 'object' ? JSON.stringify(pt) : pt;
              ul.appendChild(li);
            }});

            // Discovered Sources & Reference Links (XSS safe via sanitizeUrl & textContent)
            const sourcesUl = document.getElementById('modalSourcesList');
            if (sourcesUl) {{
              sourcesUl.innerHTML = '';
              const sources = snap.discovered_sources || snap.sources || [];
              if (Array.isArray(sources) && sources.length > 0) {{
                sources.forEach(s => {{
                  const li = document.createElement('li');
                  const title = typeof s === 'object' ? (s.title || s.name || s.url || JSON.stringify(s)) : String(s);
                  const rawUrl = typeof s === 'object' ? (s.url || s.link) : (String(s).startsWith('http') ? String(s) : '');
                  const safeUrl = sanitizeUrl(rawUrl);

                  if (safeUrl) {{
                    const a = document.createElement('a');
                    a.href = safeUrl;
                    a.target = '_blank';
                    a.rel = 'noopener noreferrer';
                    a.className = 'text-blue-400 hover:underline flex items-center gap-1';
                    a.textContent = '🔗 ' + title;
                    li.appendChild(a);
                  }} else {{
                    li.textContent = '• ' + title;
                  }}
                  sourcesUl.appendChild(li);
                }});
              }} else {{
                sourcesUl.innerHTML = '<li class="text-slate-500 italic">Không có nguồn tham khảo ngoài</li>';
              }}
            }}
          }}
        }}
      }} catch (err) {{
        console.warn('Could not fetch workflow detail:', err);
      }}
    }}

    function closeModal() {{
      document.getElementById('lockModal').classList.add('hidden');
      currentModalWorkflowId = null;
      currentModalApprovalId = null;
    }}

    async function submitApproveLock() {{
      const targetId = currentModalApprovalId || currentModalWorkflowId;
      if (!targetId) return;

      const btn = document.getElementById('btnApproveLock');
      btn.disabled = true;
      btn.textContent = 'Đang phê duyệt...';

      try {{
        const res = await fetch('/api/personal/product-video/approve-lock', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ target_id: targetId }})
        }});

        const data = await res.json();
        if (res.ok && data.ok) {{
          showToast('Phê duyệt thành công', 'Product Lock đã được duyệt. Quy trình đã chuyển sang BUILDING_RESOURCE_PACK!', '✅');
          closeModal();
          await refreshData();
        }} else {{
          showToast('Lỗi phê duyệt', data.error || 'Không thể phê duyệt Product Lock', '❌');
        }}
      }} catch (err) {{
        showToast('Lỗi', err.message, '❌');
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Phê Duyệt Product Lock';
      }}
    }}

    function showToast(title, msg, icon = 'ℹ️') {{
      const toast = document.getElementById('toast');
      document.getElementById('toastTitle').textContent = title;
      document.getElementById('toastMsg').textContent = msg;
      document.getElementById('toastIcon').textContent = icon;
      toast.classList.remove('hidden');
      setTimeout(() => {{
        toast.classList.add('hidden');
      }}, 5000);
    }}
  </script>
</body>
</html>
"""
