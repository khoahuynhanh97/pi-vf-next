import { classifyMedia, extractImageUrl, formatDateTime } from './studio-model.mjs';
import { STEPS, pipeline, gateNotes, currentStep, artifact, sourceCorrectionAvailable, filterRows } from './studio-workspace-model.mjs';

const STATUS = {
  completed: ['success', '✓', 'Hoàn thành'], succeeded: ['success', '✓', 'Hoàn thành'],
  running: ['info', '●', 'Đang chạy'], pending_operator: ['warning', '!', 'Chờ duyệt'],
  waiting: ['warning', '!', 'Đang chờ'], failed: ['danger', '!', 'Lỗi / bị chặn'],
  blocked: ['danger', '!', 'Bị chặn'], technical_error: ['danger', '!', 'Lỗi kỹ thuật'],
  uncertain: ['danger', '?', 'Chờ đối soát'], skipped: ['muted', '−', 'Bỏ qua'],
  pending: ['muted', '○', 'Chờ chạy'], unrecorded: ['muted', '○', 'Chưa ghi nhận']
};
export function node(tag, className = '', text = null) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = String(text);
  return n;
}
function button(text, action, className = 'vf-btn vf-btn-secondary') {
  const b = node('button', className, text); b.type = 'button'; b.addEventListener('click', action); return b;
}
export function StatusBadge(status, label = null) {
  const [tone, icon, title] = STATUS[status] || ['muted', '○', status || 'Chưa ghi nhận'];
  return node('span', `vf-status-chip ${tone}`, `${icon} ${label || title}`);
}
function MiniProgress(wf) {
  const track = node('div', 'vf-mini-track');
  track.setAttribute('aria-label', 'Tiến trình theo bước; bước chưa ghi nhận không có nghĩa đã hoàn thành');
  for (const s of pipeline(wf)) {
    const segment = node('span', `vf-track-segment ${STATUS[s.status]?.[0] || 'muted'}${s.current ? ' current' : ''}`);
    segment.title = `${s.label}: ${STATUS[s.status]?.[2] || s.status}`;
    track.append(segment);
  }
  return track;
}
function field(label, input) {
  const wrapper = node('label', 'vf-field'); wrapper.append(node('span', '', label), input); return wrapper;
}
function select(options, value, onChange, label) {
  const input = node('select', 'vf-input'); input.setAttribute('aria-label', label);
  for (const [key, text] of options) { const o = node('option', '', text); o.value = key; input.append(o); }
  input.value = value; input.addEventListener('change', () => onChange(input.value)); return input;
}
export function ProductList(rows, filters, onChange) {
  const root = node('section', 'vf-product-workspace');
  const intro = node('div', 'vf-list-heading');
  const title = node('div'); title.append(node('p', 'vf-eyebrow', 'XƯỞNG VIDEO'), node('h2', '', 'Sản phẩm của bạn'), node('p', 'vf-secondary', 'Ưu tiên xử lý điểm đang kẹt. Mở sản phẩm để xem ảnh, prompt và kết quả từng bước.'));
  const intake = node('a', 'vf-btn vf-btn-primary', 'Chọn sản phẩm / Chạy batch'); intake.href = '#intake'; intro.append(title, intake); root.append(intro);
  const tabs = node('div', 'vf-group-tabs'); tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', 'Nhóm sản phẩm');
  for (const [key, title] of [['attention', 'Cần xử lý'], ['new', 'Chưa tạo video'], ['done', 'Đã tạo video']]) {
    const tab = button(title, () => onChange({ group: key, page: 1 }));
    tab.className = `vf-group-tab${filters.group === key ? ' active' : ''}`;
    tab.setAttribute('aria-pressed', String(filters.group === key));
    tab.append(node('span', 'vf-tab-count', rows.filter(r => r.group === key).length)); tabs.append(tab);
  }
  root.append(tabs);
  const toolbar = node('div', 'vf-list-filters');
  const search = node('input', 'vf-input'); search.type = 'search'; search.placeholder = 'Tên, mã sản phẩm hoặc lý do bị chặn…'; search.value = filters.search; search.id = 'workspace-search';
  search.addEventListener('input', () => onChange({ search: search.value, page: 1 }, true));
  toolbar.append(field('Tìm sản phẩm', search), field('Trạng thái', select([['all', 'Tất cả trạng thái'], ['blocked', 'Bị chặn'], ['technical_error', 'Lỗi kỹ thuật'], ['waiting', 'Đang chờ'], ['running', 'Đang chạy'], ['completed', 'Hoàn thành']], filters.status, status => onChange({ status, page: 1 }), 'Trạng thái')),
    field('Bước', select([['all', 'Tất cả bước'], ...STEPS], filters.step, step => onChange({ step, page: 1 }), 'Bước pipeline')),
    field('Sắp xếp', select([['priority', 'Cần xử lý trước'], ['updated', 'Cập nhật mới nhất'], ['name', 'Tên A–Z']], filters.sort, sort => onChange({ sort, page: 1 }), 'Sắp xếp')));
  root.append(toolbar);
  const filtered = filterRows(rows, filters); const pages = Math.max(1, Math.ceil(filtered.length / 30));
  const page = Math.min(filters.page || 1, pages);
  const listing = node('div', 'vf-product-list'); listing.id = 'workspace-product-list';
  if (!filtered.length) listing.append(node('div', 'vf-empty-state', rows.length ? 'Không có sản phẩm phù hợp bộ lọc này.' : 'Chưa có sản phẩm. Chọn ngày nguồn trong “Chọn sản phẩm / Chạy batch” rồi làm mới.'));
  for (const row of filtered.slice((page - 1) * 30, page * 30)) {
    const card = node('article', `vf-product-row ${['blocked', 'technical_error'].includes(row.status) ? 'needs-action' : ''}`);
    const identity = node('div', 'vf-product-identity');
    const thumb = node('div', 'vf-product-thumb');
    const media = classifyMedia(row.thumbnail, location.origin);
    if (media.url) { const img = node('img'); img.src = media.url; img.alt = row.name; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; thumb.append(img); }
    else thumb.append(node('span', 'vf-secondary', 'Ảnh'));
    const copy = node('div'); copy.append(node('h3', '', row.name), node('p', 'vf-secondary vf-break', row.variant || row.productKey)); identity.append(thumb, copy);
    const progress = node('div', 'vf-row-progress'); progress.append(node('strong', '', row.step), MiniProgress(row.workflow));
    const status = node('div', 'vf-row-status'); status.append(StatusBadge(row.status, row.label));
    if (row.reason) { const reason = node('p', 'vf-row-reason', row.reason); reason.title = row.reason; status.append(reason); }
    const action = node('div', 'vf-row-action'); action.append(node('time', 'vf-secondary', formatDateTime(row.updatedAt)));
    const link = node('a', 'vf-btn vf-btn-secondary', row.detailWorkflowId ? 'Mở sản phẩm →' : 'Chọn để tạo →'); link.href = row.detailWorkflowId ? `#products/workflow/${encodeURIComponent(row.detailWorkflowId)}` : '#intake'; action.append(link);
    if (row.videoUrl) { const url = classifyMedia(row.videoUrl, location.origin); if (url.url) { const a = node('a', 'vf-link', 'Mở MP4'); a.href = url.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; action.append(a); } }
    card.append(identity, progress, status, action); listing.append(card);
  }
  root.append(listing);
  const pager = node('nav', 'vf-pagination'); pager.setAttribute('aria-label', 'Phân trang sản phẩm');
  const previous = button('← Trước', () => onChange({ page: page - 1 })); previous.disabled = page <= 1;
  const next = button('Sau →', () => onChange({ page: page + 1 })); next.disabled = page >= pages;
  pager.append(previous, node('span', 'vf-secondary', `${filtered.length} sản phẩm · Trang ${page}/${pages} · 30 mỗi trang`), next); root.append(pager);
  root.append(node('p', 'vf-secondary vf-data-limit', 'Số lượng theo dữ liệu đã tải: ứng viên của ngày nguồn, tối đa 100 workflow gần nhất và video trả về từ API. Bước chưa có nhật ký được ghi “Chưa ghi nhận”.'));
  return root;
}
export function StepTimeline(wf, selected, onSelect) {
  const nav = node('nav', 'vf-step-timeline'); nav.setAttribute('aria-label', 'Các bước pipeline');
  pipeline(wf).forEach((s, i) => {
    const b = button('', () => onSelect(s.key), `vf-step-button${selected === s.key ? ' selected' : ''}`);
    if (selected === s.key) b.setAttribute('aria-current', 'step');
    const mark = node('span', `vf-step-marker ${STATUS[s.status]?.[0] || 'muted'}`, s.status === 'completed' ? '✓' : i + 1);
    const info = node('span', 'vf-step-copy'); info.append(node('strong', '', s.label), node('small', '', STATUS[s.status]?.[2] || s.status));
    b.append(mark, info); nav.append(b);
  }); return nav;
}
export function GateNote(note) {
  const box = node('article', `vf-gate-note ${note.severity}`);
  const title = STEPS.find(([key]) => key === note.gate)?.[1] || note.gate || 'Pipeline';
  box.append(node('strong', '', `${note.severity === 'blocked' ? '! ' : ''}${title}`), node('p', '', note.text));
  if (note.action) box.append(node('p', 'vf-note-action', `Tiếp theo: ${note.action}`));
  if (note.findings && Object.keys(note.findings).length) {
    const more = node('details'); more.append(node('summary', '', 'Nhận xét QC'), node('pre', 'vf-raw-data', JSON.stringify(note.findings, null, 2))); box.append(more);
  }
  box.append(node('small', 'vf-secondary', [note.actor, note.attempt != null ? `Lần ${note.attempt}` : null, note.time ? formatDateTime(note.time) : 'Chưa có thời gian'].filter(Boolean).join(' · ')));
  return box;
}
function JsonPanel(title, data) {
  const d = node('details', 'vf-json-panel'); d.append(node('summary', '', title));
  d.append(node('pre', 'vf-raw-data', JSON.stringify(data, null, 2))); return d;
}
function Prompt(title, text) {
  const box = node('section', 'vf-prompt'); const h = node('div', 'vf-panel-heading');
  const copy = button('Sao chép', async () => { try { await navigator.clipboard.writeText(String(text)); copy.textContent = 'Đã sao chép'; } catch { copy.textContent = 'Chọn văn bản để sao chép'; } });
  h.append(node('h4', '', title), copy); box.append(h, node('pre', '', text)); return box;
}
function Media(raw, label, video = false) {
  const box = node('figure', 'vf-media-item'); const m = classifyMedia(raw, location.origin);
  if (m.url) {
    const onError = () => { if (!box.querySelector('.vf-media-error')) box.append(node('p', 'vf-media-error vf-secondary', 'Không tải được media. File có thể chưa có trên máy phục vụ hoặc SHA đã thay đổi. Kiểm tra lại ở bước tương ứng.')); };
    if (video) { const v = node('video'); v.src = m.url; v.controls = true; v.preload = 'none'; v.playsInline = true; v.addEventListener('error', onError); box.append(v); }
    else { const img = node('img'); img.src = m.url; img.alt = label; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.addEventListener('error', onError); box.append(img); }
    const a = node('a', 'vf-link', 'Mở media ↗'); a.href = m.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; box.append(a);
  } else {
    box.append(node('p', 'vf-secondary', m.kind === 'local' ? 'Đường dẫn trên máy chạy — chưa có URL xem qua web' : 'Chưa có media để xem'));
    if (m.kind === 'local') box.append(Prompt('Đường dẫn', raw));
  }
  box.append(node('figcaption', 'vf-secondary', label)); return box;
}
export function StepContent(wf, selected, onRun) {
  const body = node('section', 'vf-step-content');
  const s = pipeline(wf).find(s => s.key === selected);
  body.append(node('p', 'vf-eyebrow', `BƯỚC ${STEPS.findIndex(([k]) => k === selected) + 1} / ${STEPS.length}`), node('h2', '', s?.label || 'Chọn bước'), StatusBadge(s?.status));
  const meta = node('dl', 'vf-step-metadata');
  for (const [key, value] of [['Bắt đầu', formatDateTime(s?.started_at)], ['Kết thúc', formatDateTime(s?.finished_at)], ['Thời lượng', s?.duration_ms != null ? `${(s.duration_ms / 1000).toFixed(1)} giây` : 'Chưa ghi nhận'], ['Lần thực hiện', s?.attempt_number ?? 'Chưa ghi nhận'], ['Thực hiện bởi', s?.provider || 'Chưa ghi nhận']]) {
    const kv = node('div'); kv.append(node('dt', '', key), node('dd', '', value)); meta.append(kv);
  }
  body.append(meta);
  if (onRun) {
    body.append(button('Chạy tiếp từ bước này', onRun, 'vf-btn vf-btn-primary'),
      node('p', 'vf-secondary', 'Xem điều kiện và xác nhận trước khi chạy. Pipeline có thể tiếp tục các bước sau; giữ checkpoint và giới hạn đã dùng.'));
  }
  body.append(JsonPanel('Thông tin workflow', { workflow_id: wf.workflow_id, version: wf.version, state: wf.state, owner_user_id: wf.owner_user_id, pending_approval_id: wf.pending_approval_id, pending_approval_gate: wf.pending_approval_gate, created_at: wf.created_at }));
  if (!s?.recorded) body.append(node('p', 'vf-secondary', 'Chưa có nhật ký cho bước này. Nhãn bước hiện tại lấy từ trạng thái workflow; không suy ra các bước trước đã pass.'));
  const source = wf.source_context || {};
  if (['INPUT_ADMISSION', 'IMAGE_QC', 'PRODUCT_LOCK'].includes(selected)) {
    const gallery = node('div', 'vf-reference-gallery');
    for (const [i, img] of (source.images || []).entries()) gallery.append(Media(extractImageUrl(img) || img.absolute_path || img.local_path, `Ảnh nguồn ${i + 1}`));
    if (!gallery.children.length) gallery.append(node('p', 'vf-secondary', 'Chưa có ảnh nguồn.'));
    body.append(gallery, JsonPanel('Thông tin đầu vào', source));
    if (selected === 'PRODUCT_LOCK') body.append(JsonPanel('Bộ tài nguyên đã khóa', artifact(wf.locked_pack)));
  }
  if (selected === 'CREATIVE_BRIEF') {
    const brief = artifact(wf.creative_brief);
    for (const [key, label] of [['hook', 'Mở đầu'], ['creative_angle', 'Góc triển khai'], ['concept', 'Ý tưởng'], ['consumer_insight', 'Insight'], ['problem', 'Vấn đề'], ['promise', 'Thông điệp'], ['cta', 'Kêu gọi hành động']]) {
      if (brief[key]) body.append(Prompt(label, brief[key]));
    }
    body.append(JsonPanel('Kịch bản / creative brief đầy đủ', brief), JsonPanel('Dữ liệu nghiên cứu', wf.research_dossier || wf.research_snapshot || {}));
  }
  if (['SCENE_PLAN', 'KEYFRAME_GENERATION', 'KEYFRAME_QC', 'VIDEO_RENDER', 'VIDEO_QC'].includes(selected)) {
    const scenes = artifact(wf.scene_plan).scenes || [];
    for (const scene of scenes) {
      const card = node('article', 'vf-scene-card'); card.append(node('h3', '', `Cảnh ${scene.scene_order ?? scene.order ?? scene.scene_id ?? '—'}`));
      if (scene.narration) card.append(Prompt('Lời thoại / nội dung', scene.narration));
      if (scene.visual_prompt) card.append(Prompt('Prompt cảnh trong kế hoạch', scene.visual_prompt));
      if (scene.keyframe_prompt) card.append(Prompt('Prompt keyframe trong kế hoạch', scene.keyframe_prompt));
      if (scene.visual_objective) card.append(Prompt('Mục tiêu hình ảnh', scene.visual_objective));
      if (scene.subject_action) card.append(Prompt('Hành động trong cảnh', scene.subject_action));
      // Bind using explicit scene identity, never array position or file name.
      const operations = (wf.scene_operations || []).filter(o => scene.scene_id ? o.scene_id === scene.scene_id : scene.scene_order != null && o.scene_order === scene.scene_order);
      for (const op of operations) {
        if (op.render_prompt) card.append(Prompt('Prompt video ghi nhận trong request render', op.render_prompt));
        card.append(StatusBadge(op.status), Media(op.output_url || op.output_path, `Clip · lần ${op.attempt ?? '—'}`, true));
        if (op.reference_images?.length) card.append(JsonPanel('Ảnh reference trong request render', op.reference_images));
      }
      for (const frame of artifact(wf.storyboard).generated_keyframes || []) {
        if (scene.scene_id && frame.scene_id === scene.scene_id) {
          card.append(Media(frame.preview_url || frame.image_url || frame.image_path, `Keyframe ${frame.frame_role || frame.frame_id || ''}`));
          if (frame.prompt) card.append(Prompt('Prompt keyframe đã lưu', frame.prompt));
          if (frame.reference_image_paths?.length) card.append(JsonPanel('Reference của keyframe', frame.reference_image_paths));
        }
      }
      card.append(JsonPanel('Input / kế hoạch cảnh đầy đủ', scene));
      body.append(card);
    }
    if (!scenes.length) body.append(node('p', 'vf-secondary', 'Chưa có phân cảnh được lưu.'));
    body.append(JsonPanel('Storyboard / keyframe đã lưu', artifact(wf.storyboard)));
    const frames = artifact(wf.storyboard).generated_keyframes || [];
    if (frames.length && !scenes.length) { const gallery = node('div', 'vf-reference-gallery'); for (const f of frames) gallery.append(Media(f.preview_url || f.image_url || f.image_path || f.path, `Keyframe ${f.scene_id || f.scene_order || '—'}`)); body.append(gallery); }
    body.append(JsonPanel('Các lượt render được API trả về', wf.scene_operations || []));
  }
  if (selected === 'EXPORT') {
    const video = artifact(wf.export_result); body.append(Media(wf.verified_video_url || video.video_url || video.video_path || video.file_path, 'Video xuất cuối', true), JsonPanel('Kết quả export', video), JsonPanel('Kết quả render', artifact(wf.render_result)));
  }
  if (s?.recorded) body.append(JsonPanel('Nhật ký bước / mã lỗi / operation', s));
  return body;
}

const drafts = new Map();
export function releaseImageDrafts(exceptId = null) {
  for (const [id, draft] of drafts) if (id !== exceptId) { for (const image of draft.files) if (image.file) URL.revokeObjectURL(image.url); drafts.delete(id); }
}
export function ImageUploader(wf, onSubmit) {
  const root = node('section', 'vf-image-editor'); root.id = 'source-image-editor';
  root.append(node('p', 'vf-eyebrow', 'ẢNH VÀ THÔNG TIN SẢN PHẨM'), node('h3', '', 'Sửa input và kiểm tra lại'));
  if (!sourceCorrectionAvailable(wf)) {
    root.append(node('p', 'vf-secondary', 'Chỉ sửa input khi lỗi QC ảnh nguồn, trước Product Lock và chưa có công việc phía sau. Workflow này chưa đủ điều kiện; backend vẫn kiểm tra lại trước mọi thay đổi.')); return root;
  }
  const source = wf.source_context || {};
  const initialImages = () => (source.images || []).map((image, i) => ({
    sha256: String(image.sha256 || image.digest || '').replace(/^sha256:/, '').toLowerCase(),
    name: image.file_name || image.asset_id || `Ảnh nguồn ${i + 1}`,
    url: classifyMedia(extractImageUrl(image), location.origin).url
  }));
  const infoLabels = [['name', 'Tên sản phẩm'], ['brand', 'Thương hiệu'], ['model', 'Mẫu / model'], ['variant', 'Variant / phiên bản']];
  const initialInfo = () => Object.fromEntries(infoLabels.map(([key]) => [key, String(source[key] || '')]));
  const defaultReason = 'Sửa ảnh / thông tin đã đối chiếu đúng sản phẩm và variant.';
  const oldDraft = drafts.get(wf.workflow_id);
  if (oldDraft && oldDraft.version !== wf.version) {
    for (const image of oldDraft.files) if (image.file) URL.revokeObjectURL(image.url);
    drafts.delete(wf.workflow_id);
  }
  if (!drafts.has(wf.workflow_id)) drafts.set(wf.workflow_id, { version: wf.version, files: initialImages(), info: initialInfo(), reason: defaultReason });
  const draft = drafts.get(wf.workflow_id);
  const message = node('p', 'vf-upload-feedback'); message.setAttribute('role', 'status');
  const showMessage = (text, error = false) => { message.textContent = text; message.classList.toggle('vf-command-error', error); message.setAttribute('role', error ? 'alert' : 'status'); };
  const picker = node('input'); picker.type = 'file'; picker.multiple = true; picker.accept = 'image/jpeg,image/png,image/webp'; picker.id = 'source-image-files'; picker.setAttribute('aria-label', 'Chọn ảnh sản phẩm mẫu');
  const drop = node('label', 'vf-dropzone', 'Kéo ảnh vào đây hoặc chọn ảnh'); drop.htmlFor = picker.id;
  const previews = node('div', 'vf-upload-previews');
  const count = node('p', 'vf-secondary'); count.setAttribute('role', 'status');
  const run = button('Lưu input & kiểm tra lại', () => confirmRun(), 'vf-btn vf-btn-primary'); run.id = 'btn-upload-source';
  const changedInfo = () => Object.fromEntries(infoLabels.filter(([key]) => draft.info[key].trim() !== String(source[key] || '').trim()).map(([key]) => [key, draft.info[key].trim()]));
  const changed = () => draft.files.some(e => e.file) || JSON.stringify(draft.files.map(e => e.sha256)) !== JSON.stringify(initialImages().map(e => e.sha256)) || Object.keys(changedInfo()).length > 0;
  const valid = () => draft.files.length > 0 && draft.files.length <= 10 && draft.files.every(e => e.file || /^[a-f0-9]{64}$/.test(e.sha256)) && draft.reason.trim() && Object.values(changedInfo()).every(Boolean);
  const updateRun = () => { run.disabled = !valid() || !changed(); };
  const render = () => {
    previews.replaceChildren();
    draft.files.forEach((entry, i) => {
      const item = node('figure', 'vf-upload-item');
      if (entry.url) { const img = node('img'); img.src = entry.url; img.alt = entry.name; item.append(img); }
      else item.append(node('p', 'vf-secondary', 'Chưa có URL xem ảnh'));
      const primary = button(i === 0 ? '✓ Ảnh chính' : 'Chọn ảnh chính', () => { draft.files.splice(i, 1); draft.files.unshift(entry); render(); });
      primary.setAttribute('aria-label', `Chọn ${entry.name} làm ảnh chính`); primary.disabled = i === 0;
      const remove = button('Bỏ ảnh', () => { draft.files.splice(i, 1); if (entry.file) URL.revokeObjectURL(entry.url); render(); showMessage(draft.files.length ? 'Đã bỏ khỏi bộ chọn. Bấm Lưu để áp dụng; file gốc vẫn được giữ.' : 'Cần ít nhất một ảnh trước khi lưu.', !draft.files.length); });
      remove.setAttribute('aria-label', `Bỏ ảnh ${entry.name}`);
      item.append(node('figcaption', 'vf-secondary', `${entry.name} · ${entry.file ? 'Mới chọn' : 'Đang lưu'}`), primary, remove); previews.append(item);
      if (!entry.file && !/^[a-f0-9]{64}$/.test(entry.sha256)) item.append(node('p', 'vf-command-error', 'Ảnh thiếu SHA đã ghi nhận. Bỏ ảnh này và chọn lại từ máy trước khi lưu.'));
    });
    count.textContent = `${draft.files.length}/10 ảnh · ${draft.files.filter(e => !e.file).length} ảnh giữ lại · ${draft.files.filter(e => e.file).length} ảnh mới · Chưa lưu`;
    updateRun();
  };
  const add = files => {
    showMessage('');
    for (const file of files) {
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) { showMessage('Chỉ nhận JPEG, PNG, WebP.', true); continue; }
      if (draft.files.length >= 10 || file.size > 4 * 1024 * 1024 || draft.files.reduce((n, e) => n + (e.file?.size || 0), 0) + file.size > 6 * 1024 * 1024) { showMessage('Tối đa 10 ảnh, 4 MB mỗi ảnh mới, 6 MB tổng ảnh mới.', true); continue; }
      draft.files.push({ file, name: file.name, url: URL.createObjectURL(file) });
    } render(); picker.value = '';
  };
  picker.addEventListener('change', () => add(picker.files));
  for (const e of ['dragover', 'drop']) drop.addEventListener(e, ev => ev.preventDefault());
  drop.addEventListener('drop', ev => add(ev.dataTransfer.files));
  const info = node('div', 'vf-input-info');
  const inputs = {};
  for (const [key, label] of infoLabels) {
    const input = node('input', 'vf-input'); input.value = draft.info[key]; input.maxLength = 500;
    input.addEventListener('input', () => { draft.info[key] = input.value; updateRun(); });
    inputs[key] = input; info.append(field(label, input));
  }
  const reason = node('textarea', 'vf-input'); reason.value = draft.reason; reason.maxLength = 500; reason.addEventListener('input', () => { draft.reason = reason.value; updateRun(); });
  const reset = button('Hủy thay đổi', () => {
    for (const image of draft.files) if (image.file) URL.revokeObjectURL(image.url);
    draft.files = initialImages(); draft.info = initialInfo();
    draft.reason = defaultReason; reason.value = draft.reason;
    for (const [key] of infoLabels) inputs[key].value = draft.info[key];
    render(); showMessage('Đã khôi phục bộ ảnh và thông tin đang lưu. Chưa gửi yêu cầu.');
  });
  root.append(node('p', 'vf-secondary', 'Thêm ảnh giữ nguyên các ảnh còn trong bộ chọn. Bỏ ảnh chỉ bỏ khỏi bộ chọn, không xóa file hay lịch sử. Ảnh chính xếp trước; mọi ảnh vẫn phải qua QC. Chỉ sửa thông tin đã xác minh, không để trống trường đang có.'),
    info, button('Thêm ảnh', () => picker.click()), drop, picker, count, previews, reset, field('Lý do sửa input', reason), message, run);
  function confirmRun() {
    if (!valid() || !changed()) { showMessage('Cần ít nhất một ảnh hợp lệ, thông tin không trống và lý do sửa.', true); return; }
    const dialog = node('dialog', 'vf-rerun-dialog'); dialog.setAttribute('aria-label', 'Xác nhận thay ảnh và chạy lại');
    dialog.append(node('h2', '', 'Lưu input và xếp lại QC ảnh nguồn?'), node('p', '', 'Snapshot resource_pack cũ được thay thế; ảnh cũ và lịch sử lỗi vẫn giữ. Chỉ xếp lại QC ảnh nguồn; sau khi lưu, bấm Chạy tiếp từ bước này để tiếp tục pipeline.'),
      node('p', 'vf-secondary', `${draft.files.length} ảnh · Thông tin sửa: ${infoLabels.filter(([key]) => key in changedInfo()).map(([, label]) => label).join(', ') || 'Không đổi'}`));
    const actions = node('div', 'vf-panel-heading');
    const cancel = button('Hủy', () => dialog.close());
    const submit = button('Xác nhận & chạy lại', async () => {
      submit.disabled = true; cancel.disabled = true; picker.disabled = true; run.disabled = true;
      showMessage('Đang tải ảnh và gửi yêu cầu kiểm tra lại…'); dialog.close();
      root.inert = true;
      try {
        const uploads = [];
        const selection = [];
        for (const entry of draft.files) {
          if (entry.file) { selection.push({ upload_index: uploads.length }); uploads.push({ name: entry.file.name, content_base64: await fileBase64(entry.file) }); }
          else selection.push({ sha256: entry.sha256 });
        }
        await onSubmit({ uploaded_images: uploads, image_selection: selection, product_info: changedInfo(), reason: draft.reason.trim() });
        showMessage('Đã lưu input và xếp lại QC. Bấm Chạy tiếp từ bước này để tiếp tục.');
        releaseImageDrafts();
      } catch (err) { showMessage(err.message, true); }
      finally { root.inert = false; picker.disabled = false; run.disabled = !drafts.has(wf.workflow_id); }
    }, 'vf-btn vf-btn-primary');
    actions.append(cancel, submit); dialog.append(actions); dialog.addEventListener('close', () => dialog.remove(), { once: true }); document.body.append(dialog); dialog.showModal();
  }
  render(); return root;
}
function fileBase64(file) {
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('Không đọc được ảnh đã chọn.')); reader.readAsDataURL(file); });
}
export function RerunDialog(wf, selected, callbacks) {
  const dialog = node('dialog', 'vf-rerun-dialog'); dialog.setAttribute('aria-label', 'Chạy lại từ bước');
  dialog.append(node('h2', '', 'Chạy lại từ bước…'));
  const choice = select([['ALL', 'Chạy lại toàn bộ flow'], ...STEPS], selected || currentStep(wf) || 'INPUT_ADMISSION', () => {}, 'Bước chạy lại');
  const operation = node('select', 'vf-input'); operation.setAttribute('aria-label', 'Command thực hiện');
  const note = node('textarea', 'vf-input'); note.value = 'Yêu cầu chạy lại thủ công từ Studio.'; note.maxLength = 500;
  const explain = node('p', 'vf-secondary');
  const feedback = node('p', 'vf-upload-feedback', 'Đang tải command và điều kiện chạy…'); feedback.setAttribute('role', 'status');
  const showFeedback = (message, error = false) => {
    feedback.textContent = message;
    feedback.classList.toggle('vf-command-error', error);
    feedback.setAttribute('role', error ? 'alert' : 'status');
  };
  const imageAction = button('Mở khu vực thay ảnh', () => { dialog.close(); document.getElementById('source-image-editor')?.scrollIntoView({ behavior: 'smooth', block: 'center' }); });
  const cancel = button('Đóng', () => dialog.close());
  let data = null;
  let running = false;
  const submit = button('Xác nhận & chạy', async () => {
    const option = data?.options?.find(o => o.id === operation.value);
    if (running || !option?.enabled || !note.value.trim()) return;
    running = true; submit.disabled = true; cancel.disabled = true; choice.disabled = true; operation.disabled = true; note.disabled = true; imageAction.disabled = true;
    showFeedback('Đang kiểm tra checkpoint và gửi command…');
    try {
      const result = await callbacks.executeAction(wf, option, data.version, note.value.trim());
      showFeedback(result.reused ? 'Lệnh này đã được tiếp nhận trước đó; không chạy trùng.' : 'Đã tiếp nhận command và giao runner Studio xử lý đúng sản phẩm. Theo dõi tiến trình trên trang sản phẩm.');
      submit.hidden = true;
    } catch (error) {
      showFeedback(error.message, true);
      // A failed/ambiguous command requires fresh options and a fresh snapshot.
      // Closing/reopening never replays the previous POST.
      submit.disabled = true;
    } finally { running = false; cancel.disabled = false; }
  }, 'vf-btn vf-btn-primary');
  submit.disabled = true;
  const update = () => {
    const options = (data?.options || []).filter(o => o.steps.includes(choice.value));
    const previous = operation.value;
    operation.replaceChildren();
    for (const option of options) { const item = node('option', '', option.label); item.value = option.id; operation.append(item); }
    if (options.some(o => o.id === previous)) operation.value = previous;
    const selectedOption = options.find(o => o.id === operation.value);
    operation.disabled = !options.length;
    explain.textContent = selectedOption ? `${selectedOption.command}: ${selectedOption.effect}` : '';
    if (data) {
      const blocked = !selectedOption?.enabled;
      showFeedback(blocked
        ? selectedOption?.reason || data.unavailable_reasons?.[choice.value] || 'Chưa có thao tác an toàn cho bước này. Kiểm tra lỗi và checkpoint hiện tại trước khi tiếp tục.'
        : 'Chọn bước và xem phạm vi xử lý trước khi xác nhận.', blocked);
    }
    submit.disabled = !selectedOption?.enabled || !note.value.trim();
    imageAction.hidden = choice.value !== 'IMAGE_QC' || !sourceCorrectionAvailable(wf);
  };
  choice.addEventListener('change', update); operation.addEventListener('change', update); note.addEventListener('input', () => { const option = data?.options?.find(o => o.id === operation.value); submit.disabled = !option?.enabled || !note.value.trim(); });
  dialog.addEventListener('cancel', event => { if (running) event.preventDefault(); });
  dialog.append(feedback, field('Bước pipeline', choice), field('Thao tác hợp lệ', operation), explain, imageAction, field('Ghi chú chạy lại', note), submit, cancel);
  dialog.addEventListener('close', () => dialog.remove(), { once: true }); document.body.append(dialog); dialog.showModal();
  update();
  callbacks.loadActions(wf).then(response => {
    if (!dialog.isConnected) return;
    if (response.version !== wf.version) throw new Error('Workflow đã thay đổi. Đóng hộp thoại và làm mới trang sản phẩm.');
    data = response; update();
  }).catch(error => { showFeedback(error.message, true); submit.disabled = true; });
}
export function WorkflowWorkspace(wf, selected, callbacks) {
  const page = node('div', 'vf-workflow-workspace');
  const header = node('div', 'vf-workflow-heading');
  const title = node('div'); title.append(node('p', 'vf-eyebrow', wf.workflow_id), node('h2', '', wf.product_name || wf.source_context?.name || [wf.source_context?.brand, wf.source_context?.model].filter(Boolean).join(' ') || 'Sản phẩm'), node('p', 'vf-secondary', `Trạng thái: ${wf.state || 'Chưa ghi nhận'}`));
  const actions = node('div', 'vf-workflow-actions');
  if (callbacks.refresh) actions.append(button('Làm mới trạng thái', callbacks.refresh));
  if (sourceCorrectionAvailable(wf)) actions.append(button('Thay ảnh mẫu', () => document.getElementById('source-image-editor')?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 'vf-btn vf-btn-primary'));
  actions.append(button('Chạy lại từ bước…', () => RerunDialog(wf, selected, callbacks)));
  if (callbacks.approve && wf.pending_approval_gate) { const b = button(`Duyệt ${wf.pending_approval_gate}`, callbacks.approve, 'vf-btn vf-btn-success'); b.id = 'btn-approve-gate'; actions.append(b); }
  if (wf.quarantined_job?.recovery_available) { const b = button('Tiếp tục job hiện tại', callbacks.resume, 'vf-btn vf-btn-primary'); b.id = 'btn-retry-quarantined'; actions.append(b); }
  header.append(title, actions); page.append(header);
  const notes = gateNotes(wf);
  if (notes.some(n => n.severity === 'blocked')) { const banner = GateNote(notes.find(n => n.severity === 'blocked')); banner.classList.add('vf-blocked-banner'); banner.setAttribute('role', 'alert'); page.append(banner); }
  const layout = node('div', 'vf-workflow-columns');
  layout.append(StepTimeline(wf, selected, callbacks.select));
  const center = StepContent(wf, selected, () => RerunDialog(wf, selected, callbacks)); center.append(ImageUploader(wf, callbacks.upload)); layout.append(center);
  const aside = node('aside', 'vf-gate-notes'); aside.setAttribute('aria-label', 'Ghi chú theo gate'); aside.append(node('h3', '', 'Ghi chú theo gate'));
  const related = notes.filter(n => n.gate === selected || n.severity === 'blocked');
  for (const note of related) aside.append(GateNote(note));
  if (!related.length) aside.append(node('p', 'vf-secondary', 'Chưa có ghi chú cho bước này.'));
  if (notes.length > related.length) { const more = node('details'); more.append(node('summary', '', 'Ghi chú các gate khác')); for (const note of notes.filter(n => !related.includes(n))) more.append(GateNote(note)); aside.append(more); }
  layout.append(aside); page.append(layout); return page;
}
