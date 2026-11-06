"""Four roles share contracts; worker/outbox still own transitions and attempts."""
from ..generator import SingleModelCreativeGenerator
from .strategist import CreativeStrategist
from .director import VisualDirector
from .playbooks import select_playbook
import json
from dataclasses import replace
from ..contracts import canonical_digest


class AgenticCreativeGenerator(SingleModelCreativeGenerator):
    creative_mode = 'four_agent'

    def __init__(self, model, *, profile, category, visible_details, source_verifier=None):
        super().__init__(model, source_verifier)
        self.profile = profile
        self.recipe = select_playbook(category, profile, tuple(visible_details))

    def create_direction(self, dossier):
        return CreativeStrategist(self.model, recipe=self.recipe, profile=self.profile,
            source_verifier=self.source_verifier).create_direction(dossier)

    def create_script(self, dossier, brief, *, revision_feedback=''):
        return self.director().create_script(dossier, brief, revision_feedback=revision_feedback)

    def director(self):
        return VisualDirector(self.model, recipe=self.recipe, profile=self.profile, source_verifier=self.source_verifier)

    def create_scene_prompts(self, dossier, script, profile=None):
        if profile is not None and profile != self.profile:
            raise ValueError('run profile cannot change')
        return self.director().create_scene_prompts(dossier, script)

    def prepare_targeted_repair(self, *, dossier, script, scene_ref, review, connection):
        from .contracts import ScenePromptBundle
        from .repair import RepairPlanner, revise_scene_prompt
        from video_factory.qc.retry_policy import claim_qc_attempt, complete_qc_attempt, QCQuotaExhaustedError

        if review.verdict != 'FAIL':
            return None
        try:
            attempt = claim_qc_attempt(connection, workflow_id=dossier.workflow_id,
                retry_type='creative_rewrite', item_id='product', input_hash=review.input_digest,
                operation_id='rewrite-'+review.digest, max_recoveries=2)
        except QCQuotaExhaustedError:
            return None
        old = connection.execute('SELECT reason_text FROM workflow_qc_retries WHERE id=?', (attempt['attempt_id'],)).fetchone()
        if old and old[0]:
            row = connection.execute('SELECT status, verdict, action_taken, attempt_number FROM workflow_qc_retries WHERE id=?',
                                     (attempt['attempt_id'],)).fetchone()
            def replay_unknown(reason_text, attempt_id):
                try:
                    saved_result=json.loads(reason_text)
                    saved_review=saved_result.get('review')
                    from .contracts import CreativeReview
                    if CreativeReview.from_dict(saved_review).verdict == 'UNKNOWN':
                        return {'repair_unknown':True,'review':saved_review,'attempt_id':attempt_id}
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass
                return None
            if row and (row[0] == 'technical_unknown' or row[1] == 'unknown'):
                return replay_unknown(old[0],attempt['attempt_id'])
            if row and row[1] == 'fail':
                row = connection.execute("""SELECT id, status, verdict, action_taken, reason_text FROM workflow_qc_retries
                    WHERE workflow_id=? AND retry_type='creative_rewrite' AND item_id='product'
                      AND attempt_number>? ORDER BY attempt_number DESC LIMIT 1""",
                    (dossier.workflow_id, row[3])).fetchone()
                if row and (row[1] == 'technical_unknown' or row[2] == 'unknown'):
                    return replay_unknown(row[4],row[0])
                if not row or row[1] != 'completed' or row[2] != 'pass' or not row[4]:
                    return None
                saved_text, action = row[4], row[3]
            else:
                if not row or row[0] != 'completed' or row[1] != 'pass':
                    return None
                saved_text, action = old[0], row[2]
            saved=json.loads(saved_text)
            if action == 'reassemble_without_generation':
                return saved if isinstance(saved, dict) and saved.get('assembly_repair') else None
            if saved.get('blocked_input') or not all(key in saved for key in ('scene_prompts', 'script_digest')):
                return None
            from ..contracts import ScriptScenePlanV2
            from .contracts import CreativeReview, ScenePromptBundle
            ScriptScenePlanV2.from_dict(saved)
            ScenePromptBundle.from_dict(saved['scene_prompts'])
            if ('preproduction_review' in saved and
                    CreativeReview.from_dict(saved['preproduction_review']).verdict != 'PASS'):
                return None
            return saved
        if attempt['reused']:
            return None  # Interrupted planner call: reconcile its existing claim, never call it again blindly.
        invariants = {'product_truth':dossier.digest(), 'logical_scene_ids':[s.scene_id for s in script.scenes],
            'duration':script.target_duration_seconds, 'audio_policy':'silent'}
        proposal = RepairPlanner(self.model).propose(review=review, context={
            'current_digest':review.input_digest, 'protected_invariants':invariants,
            'attempts_used':attempt['attempt_number']})
        if proposal.scope=='input':
            complete_qc_attempt(connection,attempt_id=attempt['attempt_id'],verdict='blocked_input',
                action_taken='stop_for_source_evidence',reason_text=json.dumps({
                    'blocked_input':True,'proposal':proposal.to_dict()},ensure_ascii=False))
            return None
        if proposal.scope=='assembly':
            if proposal.changes.get('rebuild') is not True or set(proposal.changes)-{'rebuild','reason'}:
                raise ValueError('only deterministic reassembly is supported for assembly repair')
            result={'assembly_repair':proposal.to_dict(),'scene_ref':scene_ref,'review_digest':review.digest}
            complete_qc_attempt(connection,attempt_id=attempt['attempt_id'],verdict='pass',
                action_taken='reassemble_without_generation',reason_text=json.dumps(result,ensure_ascii=False))
            return result
        original_bundle=ScenePromptBundle.from_dict(scene_ref['scene_prompts'])
        if proposal.scope in {'scene','storyboard'}:
            from ..contracts import CreativeBriefV2
            brief=CreativeBriefV2.from_dict(scene_ref['creative_brief'])
            rewritten=self.create_script(dossier,brief,revision_feedback=json.dumps({
                'proposal':proposal.to_dict(),'review':review.to_dict(),
                'old_script':script.to_dict(),'protected_invariants':invariants},ensure_ascii=False))
            if ({s.scene_id for s in rewritten.scenes}!={s.scene_id for s in script.scenes} or
                rewritten.target_duration_seconds!=script.target_duration_seconds):
                raise ValueError('script revision cannot rename scenes or change total duration')
            affected=set(proposal.scene_ids)
            rewritten=replace(rewritten, scenes=tuple(
                new if old.scene_id in affected else old
                for old, new in zip(script.scenes, rewritten.scenes)
            ), script_digest="")
            rewritten=replace(rewritten, script_digest=rewritten.digest())
            authored=self.create_scene_prompts(dossier,rewritten)
            revised=replace(authored,prompts=tuple(authored.for_scene(p.scene_id) if p.scene_id in affected else p
                                                  for p in original_bundle.prompts),
                            script_digest=rewritten.digest())
            script=rewritten
        else:
            revised = revise_scene_prompt(director=self.director(), dossier=dossier, script=script,
                bundle=original_bundle, proposal=proposal,review=review,protected_invariants=invariants)
        from .director import bind_identity_lock
        revised = bind_identity_lock(revised, dossier)
        check = None
        if review.stage == 'preproduction':
            # Existing in-flight legacy repairs keep their old recheck contract.
            from .critic import CreativeCritic
            from .gates import preproduction_context
            from .strategist import dossier_media
            check = CreativeCritic(self.model, receipt_root='data/creative_reviews/'+dossier.workflow_id).review(
                stage='preproduction', context=preproduction_context(dossier,script,revised,self.profile),
                media=dossier_media(dossier))
            if check.verdict != 'PASS':
                complete_qc_attempt(connection, attempt_id=attempt['attempt_id'],
                    status='technical_unknown' if check.verdict == 'UNKNOWN' else 'completed',
                    verdict=check.verdict.lower(), action_taken='preproduction_recheck_failed',
                    reason_text=json.dumps({'review':check.to_dict(),'proposal':proposal.to_dict()},ensure_ascii=False))
                if check.verdict == 'UNKNOWN':
                    return {'repair_unknown':True,'review':check.to_dict(),'attempt_id':attempt['attempt_id']}
                return None
        result = {**scene_ref, **script.to_dict(), 'scene_prompts':revised.to_dict(),
                  'repair_history':[*scene_ref.get('repair_history', []), {'proposal':proposal.to_dict(),
                      'review':review.to_dict(), 'before_bundle':scene_ref['scene_prompts'], 'after_bundle':revised.digest}]}
        if review.stage != 'preproduction':
            result.pop('preproduction_review', None)
            digests = dict(result.get('review_input_digests') or {})
            digests.pop('preproduction', None)
            result['review_input_digests'] = digests
        elif check is not None:
            result['preproduction_review'] = check.to_dict()
            result.setdefault('review_input_digests', {})['preproduction'] = check.input_digest
        complete_qc_attempt(connection, attempt_id=attempt['attempt_id'], verdict='pass',
            output_hash=canonical_digest(result), action_taken='targeted_prompt_repair',
            reason_text=json.dumps(result,ensure_ascii=False))
        return result
