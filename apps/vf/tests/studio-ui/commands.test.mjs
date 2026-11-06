import test from 'node:test';
import assert from 'node:assert/strict';
import { loadWorkflowActions, executeWorkflowAction } from '../../src/video_factory/application/static/studio-api.mjs';
import { currentStep, gateNotes, pipeline } from '../../src/video_factory/application/static/studio-workspace-model.mjs';

test('workflow actions use encoded identity and a fixed structured command endpoint', async () => {
  const calls = [];
  const fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify({ ok: true, options: [] }), { status: 200 }); };
  await loadWorkflowActions(fetch, 'wf/a');
  const payload = { action_id: 'recover-keyframe:operation-a', expected_version: 4, request_id: '21bcbaf0-33e3-4f56-9c4c-a440eb9c082a', note: 'Đã sửa selector' };
  await executeWorkflowAction(fetch, 'wf/a', payload);
  assert.equal(calls[0].url, '/api/operator/workflow/wf%2Fa/actions');
  assert.equal(calls[0].options.method, 'GET');
  assert.equal(calls[1].url, '/api/operator/workflow/wf%2Fa/actions');
  assert.deepEqual(JSON.parse(calls[1].options.body), payload);
});
test('storyboard quarantine is not mislabeled as Creative Brief by stale inspection metadata', () => {
  const wf = { state: 'creating_storyboard', quarantined_job: { quarantined: true, step: 'CREATIVE_BRIEF', job_name: 'product_video.storyboard', reason: 'GOOGLE_FLOW_KEYFRAME_FAILED: selector' } };
  assert.equal(currentStep(wf), 'KEYFRAME_GENERATION');
  assert.equal(gateNotes(wf)[0].gate, 'KEYFRAME_GENERATION');
});
test('legacy detail without job_name still does not call a storyboard failure a Brief failure', () => {
  assert.equal(currentStep({ state: 'creating_storyboard', quarantined_job: { step: 'CREATIVE_BRIEF' } }), 'KEYFRAME_GENERATION');
});
test('an actually requeued job shows pending, with the old error kept as history', () => {
  const wf = { state: 'creating_brief', failure_reason: 'Previous failure',
    execution: { state: 'pending', job_name: 'product_video.creative_brief', step: 'CREATIVE_BRIEF' },
    pipeline_steps: [{ step_name: 'CREATIVE_BRIEF', status: 'failed', message: 'Previous failure', finished_at: '2026-10-26T01:00:00Z' }] };
  const step = pipeline(wf).find(s => s.key === 'CREATIVE_BRIEF');
  assert.equal(step.status, 'pending');
  assert.equal(step.finished_at, null);
  assert.equal(gateNotes(wf).some(n => n.severity === 'blocked'), false);
});
