import test from 'node:test';
import assert from 'node:assert/strict';
import { pipeline, currentStep, sourceCorrectionAvailable, gateNotes, productRows, filterRows } from '../../src/video_factory/application/static/studio-workspace-model.mjs';

test('missing journal never marks preceding steps complete', () => {
  const steps = pipeline({ state: 'rendering' });
  assert.equal(steps.find(s => s.key === 'VIDEO_RENDER').status, 'running');
  assert.equal(steps.filter(s => s.status === 'completed').length, 0);
  assert.equal(steps[0].status, 'unrecorded');
});
test('journal carries uncertainty, attempts and recorded times without fake percentages', () => {
  const wf = { state: 'WaitingForOperator', pipeline_steps: [{ step_name: 'VIDEO_RENDER', status: 'uncertain', attempt_number: 2, duration_ms: 7000 }] };
  assert.equal(currentStep(wf), 'VIDEO_RENDER');
  assert.equal(pipeline(wf)[7].status, 'uncertain');
  assert.equal(pipeline(wf)[7].attempt_number, 2);
});
test('source correction requires a known source failure before downstream work', () => {
  const wf = { state: 'skipped', failure_code: 'IMAGE_QC_FAILED', source_context: { source: 'hermes_product_video_exchange' } };
  assert.equal(sourceCorrectionAvailable(wf), true);
  assert.equal(sourceCorrectionAvailable({ ...wf, scene_plan: {} }), false);
  assert.equal(sourceCorrectionAvailable({ ...wf, state: 'rendering' }), false);
  assert.equal(sourceCorrectionAvailable({ ...wf, failure_code: 'QC_OUTCOME_UNCERTAIN' }), false);
});
test('blocked notes sort before successes and retain the exact reason', () => {
  const notes = gateNotes({ failure_reason: '<script>unsafe</script>', state: 'skipped', qc_reports: [{ stage: 'image', verdict: 'PASS', score: 95 }] });
  assert.equal(notes[0].severity, 'blocked');
  assert.equal(notes[0].text, '<script>unsafe</script>');
  assert.equal(notes[1].severity, 'success');
});
test('list includes board-only and video-only products without duplicate candidate', () => {
  const rows = productRows([{ product_key: 'p1', workflow_id: 'w1', name: 'Một', status: 'in_production' }], {
    in_production: [{ workflow_id: 'w1', product_key: 'p1', state: 'rendering' }],
    error: [{ workflow_id: 'w2', product_key: 'p2', product_name: 'Hai', state: 'skipped', failure_reason: 'Sai ảnh' }]
  }, [{ workflow_id: 'w3', product_key: 'p3', product_name: 'Ba', result_status: 'completed', video_url: '/mp4' }]);
  assert.equal(rows.length, 3);
  assert.equal(rows.find(r => r.productKey === 'p2').group, 'attention');
  assert.equal(rows.find(r => r.productKey === 'p2').reason, 'Sai ảnh');
  assert.equal(rows.find(r => r.productKey === 'p3').group, 'done');
});
test('hundreds of rows filter by group, step, reason and sort blocked first', () => {
  const rows = Array.from({ length: 400 }, (_, i) => ({ name: `Sản phẩm ${i}`, productKey: `p${i}`, group: 'attention', status: i === 399 ? 'blocked' : 'running', stage: 'IMAGE_QC', reason: i === 399 ? 'Sai variant' : '' }));
  assert.equal(filterRows(rows)[0].status, 'blocked');
  assert.equal(filterRows(rows, { search: 'variant', step: 'IMAGE_QC' }).length, 1);
  assert.equal(filterRows(rows, { step: 'EXPORT' }).length, 0);
});

test('an old QC failure is history after a current pass, not a new blocker', () => {
  const notes = gateNotes({ state: 'rendering', qc_reports: [
    { stage: 'image', verdict: 'FAIL', created_at: '2026-10-26T10:00:00Z' },
    { stage: 'image', verdict: 'PASS', created_at: '2026-10-26T10:01:00Z' }
  ] });
  assert.equal(notes.filter(n => n.severity === 'blocked').length, 0);
  assert.match(notes[0].text, /Lịch sử QC/);
});
test('board updates override stale candidate state and bind by explicit product key', () => {
  const rows = productRows([{ product_key: 'p1', name: 'Một', status: 'invalid', rejection_reason: 'Old error' }], {
    in_production: [{ workflow_id: 'w1', product_key: 'p1', state: 'rendering' }]
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].detailWorkflowId, 'w1');
  assert.equal(rows[0].status, 'running');
  assert.equal(rows[0].reason, '');
});
