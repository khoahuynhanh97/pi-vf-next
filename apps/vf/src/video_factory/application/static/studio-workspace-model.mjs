import { buildProductResults, flattenBoard, getCardStateLabel, extractImageUrl } from './studio-model.mjs';

// Names match workflows/steps.py. Missing journal entries never imply a pass.
export const STEPS = [
  ['INPUT_ADMISSION', 'Nhận sản phẩm'], ['IMAGE_QC', 'QC ảnh nguồn'],
  ['PRODUCT_LOCK', 'Khóa sản phẩm'], ['CREATIVE_BRIEF', 'Kịch bản'],
  ['SCENE_PLAN', 'Phân cảnh'], ['KEYFRAME_GENERATION', 'Tạo keyframe'],
  ['KEYFRAME_QC', 'QC keyframe'], ['VIDEO_RENDER', 'Render video'],
  ['VIDEO_QC', 'QC video'], ['EXPORT', 'Xuất MP4']
];
const STATE_STEP = {
  idle: 'INPUT_ADMISSION', researching: 'INPUT_ADMISSION',
  buildingresourcepack: 'IMAGE_QC', retryingimageqc: 'IMAGE_QC',
  awaitingproductlock: 'PRODUCT_LOCK', lockingresourcepack: 'PRODUCT_LOCK',
  creatingbrief: 'CREATIVE_BRIEF', awaitingbriefapproval: 'CREATIVE_BRIEF',
  creatingsceneplan: 'SCENE_PLAN', awaitingsceneapproval: 'SCENE_PLAN',
  creatingstoryboard: 'KEYFRAME_GENERATION', awaitingstoryboardapproval: 'KEYFRAME_QC',
  rendering: 'VIDEO_RENDER', awaitingfinalapproval: 'VIDEO_QC', exporting: 'EXPORT', completed: 'EXPORT'
};
export const normalize = value => String(value || '').replace(/[_ -]/g, '').toLowerCase();
export function artifact(value) {
  if (!value || typeof value !== 'object') return {};
  if (typeof value.payload === 'string') {
    try { return JSON.parse(value.payload); } catch { return value; }
  }
  return value;
}
export function currentStep(wf = {}) {
  const journal = Array.isArray(wf.pipeline_steps) ? wf.pipeline_steps : [];
  const active = [...journal].reverse().find(s => ['running', 'failed', 'uncertain', 'pending_operator'].includes(s.status));
  const jobSteps = { 'product_video.research': 'INPUT_ADMISSION', 'product_video.resource_pack': 'IMAGE_QC', 'product_video.creative_brief': 'CREATIVE_BRIEF', 'product_video.scene_plan': 'SCENE_PLAN', 'product_video.storyboard': 'KEYFRAME_GENERATION', 'product_video.render': 'VIDEO_RENDER', 'product_video.export': 'EXPORT' };
  const known = jobSteps[wf.quarantined_job?.job_name] || (STEPS.some(([key]) => key === wf.quarantined_job?.step) ? wf.quarantined_job.step : null);
  if (normalize(wf.state) === 'completed') return 'EXPORT';
  if (['pending', 'leased'].includes(wf.execution?.state) && jobSteps[wf.execution.job_name]) return jobSteps[wf.execution.job_name];
  const operatorStep = ({ image: 'IMAGE_QC', keyframe: 'KEYFRAME_QC', video: 'VIDEO_QC', direction: 'CREATIVE_BRIEF', script: 'SCENE_PLAN', storyboard: 'KEYFRAME_GENERATION' })[wf.operator_stage];
  const legacyMismatch = !wf.quarantined_job?.job_name && wf.quarantined_job?.step === 'CREATIVE_BRIEF' && normalize(wf.state) === 'creatingstoryboard';
  return operatorStep || (legacyMismatch ? 'KEYFRAME_GENERATION' : known) || active?.step_name
    || (['EXCHANGE_IMAGE_QC_FAILED', 'PRODUCT_IMAGE_QC_REJECTED', 'IMAGE_QC_FAILED'].includes(wf.failure_code) ? 'IMAGE_QC' : null)
    || STATE_STEP[normalize(wf.state)] || null;
}
export function pipeline(wf = {}) {
  const journal = new Map((wf.pipeline_steps || []).map(s => [s.step_name, s]));
  const current = currentStep(wf);
  return STEPS.map(([key, label]) => {
    const entry = journal.get(key);
    let status = entry?.status || 'unrecorded';
    if (!entry && key === current) {
      status = wf.quarantined_job || wf.failure_code || wf.operator_reason ? 'failed'
        : normalize(wf.state).startsWith('awaiting') ? 'pending_operator'
        : normalize(wf.state) === 'completed' ? 'completed' : 'running';
    }
    const queued = ['pending', 'leased'].includes(wf.execution?.state) && wf.execution.step === key;
    return { ...entry, key, label, status: queued ? (wf.execution.state === 'pending' ? 'pending' : 'running') : status,
      ...(queued ? { started_at: null, finished_at: null, duration_ms: null, attempt_number: null, provider: null } : {}),
      current: key === current, recorded: Boolean(entry) || queued };
  });
}
export function sourceCorrectionAvailable(wf = {}) {
  return wf.source_context?.source === 'hermes_product_video_exchange'
    && ['skipped', 'waitingforoperator'].includes(normalize(wf.state))
    && ['EXCHANGE_IMAGE_QC_FAILED', 'PRODUCT_IMAGE_QC_REJECTED', 'IMAGE_QC_FAILED'].includes(wf.failure_code)
    && (normalize(wf.state) !== 'waitingforoperator' || wf.operator_stage === 'image')
    && !['locked_pack', 'creative_brief', 'scene_plan', 'storyboard', 'render_result', 'export_result'].some(k => wf[k]);
}
export function gateNotes(wf = {}) {
  const notes = [];
  const activeBriefRecovery = ['running', 'already_queued'].includes(wf.quarantined_job?.status);
  const recovering = ['pending', 'leased'].includes(wf.execution?.state) && (!wf.quarantined_job || activeBriefRecovery) && !wf.operator_reason;
  const reason = wf.quarantined_job?.error || wf.quarantined_job?.reason || wf.operator_reason || wf.failure_reason || wf.skip_reason;
  if (reason) notes.push({ gate: currentStep(wf) || 'Pipeline', severity: recovering ? 'warning' : 'blocked', text: `${recovering ? 'Lỗi lượt trước · ' : ''}${String(reason)}`,
    time: wf.skipped_at || wf.updated_at || (wf.pipeline_steps || []).find(s => s.step_name === currentStep(wf))?.finished_at, action: recovering ? 'Job phục hồi đã được xếp hàng; theo dõi kết quả lượt mới.' : wf.next_action || wf.quarantined_job?.next_action || (sourceCorrectionAvailable(wf)
      ? 'Thay bộ ảnh rõ, đúng SKU rồi kiểm tra lại QC ảnh nguồn.' : 'Kiểm tra nguyên nhân rồi mở “Chạy lại từ bước…” để xem thao tác hợp lệ.') });
  for (const s of wf.pipeline_steps || []) {
    if (s.message || s.error_code || s.next_action) notes.push({ gate: s.step_name,
      severity: ['failed', 'uncertain'].includes(s.status) ? recovering && s.status === 'failed' ? 'warning' : 'blocked' : s.status === 'completed' ? 'success' : s.status === 'pending_operator' ? 'warning' : 'info',
      text: [s.error_code, s.message, s.retry_reason].filter(Boolean).join(' · '), time: s.finished_at || s.started_at, action: s.next_action,
      actor: s.provider, attempt: s.attempt_number });
  }
  const gates = { image: 'IMAGE_QC', keyframe: 'KEYFRAME_QC', video: 'VIDEO_QC', final: 'VIDEO_QC', brief: 'CREATIVE_BRIEF', creative_brief: 'CREATIVE_BRIEF', scene_plan: 'SCENE_PLAN', storyboard: 'KEYFRAME_QC' };
  const latest = new Map();
  for (const q of wf.qc_reports || []) {
    const previous = latest.get(q.stage);
    if (!previous || (Date.parse(q.created_at) || 0) >= (Date.parse(previous.created_at) || 0)) latest.set(q.stage, q);
  }
  for (const q of wf.qc_reports || []) {
    const gate = gates[q.stage] || q.stage;
    const historical = recovering || latest.get(q.stage) !== q || gate !== currentStep(wf) || normalize(wf.state) === 'completed';
    notes.push({ gate, severity: /pass/i.test(q.verdict) ? 'success' : /fail|reject/i.test(q.verdict) && !historical ? 'blocked' : 'warning',
      text: `${historical ? 'Lịch sử QC · ' : ''}${q.verdict || 'QC'}${q.score != null ? ` · ${q.score} điểm` : ''}`,
      findings: q.findings, time: q.created_at, actor: q.evaluator_name });
  }
  for (const a of wf.approval_history || []) notes.push({ gate: gates[a.gate] || a.gate, severity: /approved/i.test(a.status) ? 'success' : 'warning', text: a.status || 'Chờ duyệt', time: a.updated_at || a.created_at });
  return notes.sort((a, b) => (b.severity === 'blocked') - (a.severity === 'blocked'));
}
export function productRows(candidates = [], columns = {}, videos = []) {
  const cards = flattenBoard(columns).map(({ card }) => card).sort((a, b) => (Date.parse(b.updated_at || b.created_at) || 0) - (Date.parse(a.updated_at || a.created_at) || 0));
  const boardById = new Map(cards.map(c => [c.workflow_id, c]));
  const all = candidates.map(c => {
    const board = (c.workflow_id ? boardById.get(c.workflow_id) : null) || cards.find(b => b.product_key && b.product_key === c.product_key);
    if (!board) return c;
    const s = normalize(board.state);
    const status = s === 'completed' ? 'completed' : ['skipped', 'failed', 'error', 'waitingforoperator'].includes(s) ? 'waiting_for_operator'
      : STATE_STEP[s] ? 'in_production' : c.status;
    return { ...c, workflow_id: board.workflow_id, status, rejection_reason: board.failure_reason || board.operator_reason || (status === 'in_production' ? null : c.rejection_reason) };
  });
  const workflowIds = new Set(all.map(c => c.workflow_id).filter(Boolean));
  const productKeys = new Set(all.map(c => c.product_key));
  for (const item of [...cards, ...videos]) {
    if (workflowIds.has(item.workflow_id) || (item.product_key && productKeys.has(item.product_key))) continue;
    all.push({ product_key: item.product_key || `workflow:${item.workflow_id}`, workflow_id: item.workflow_id,
      name: item.product_name, status: item.state || item.result_status, images: item.thumbnail_url ? [item.thumbnail_url] : [] });
    workflowIds.add(item.workflow_id);
    if (item.product_key) productKeys.add(item.product_key);
  }
  const rows = buildProductResults(all, columns, videos);
  return rows.map((row, i) => {
    const source = all[i];
    const card = boardById.get(source.workflow_id) || {};
    const wf = { ...card, state: card.state || source.status };
    if (['failed', 'error', 'skipped', 'waitingforoperator'].includes(normalize(wf.state)) && !['completed', 'blocked', 'technical_error'].includes(row.status)) {
      row.status = 'blocked'; row.label = 'Cần xử lý';
    }
    return { ...row, group: row.status === 'completed' ? 'done' : !source.workflow_id && ['unprocessed', 'ready_to_resume', 'awaiting_product_lock'].includes(source.status) ? 'new' : 'attention',
      thumbnail: card.thumbnail_url || extractImageUrl(source.images?.[0]),
      updatedAt: card.updated_at || source.updated_at || card.created_at || source.created_at,
      workflow: wf, stage: currentStep(wf), reason: row.reason || card.failure_reason || card.operator_reason || source.rejection_reason || '',
      step: getCardStateLabel(wf.state) };
  });
}
export function filterRows(rows, { group = 'attention', search = '', status = 'all', step = 'all', sort = 'priority' } = {}) {
  const q = search.trim().toLocaleLowerCase('vi');
  const priority = { blocked: 0, technical_error: 1, waiting: 2, running: 3, completed: 4 };
  return rows.filter(r => r.group === group && (status === 'all' || r.status === status) && (step === 'all' || r.stage === step)
    && (!q || `${r.name} ${r.productKey} ${r.reason}`.toLocaleLowerCase('vi').includes(q)))
    .sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name, 'vi')
      : sort === 'priority' && priority[a.status] !== priority[b.status] ? (priority[a.status] ?? 5) - (priority[b.status] ?? 5)
      : (Date.parse(b.updatedAt) || 0) - (Date.parse(a.updatedAt) || 0));
}
