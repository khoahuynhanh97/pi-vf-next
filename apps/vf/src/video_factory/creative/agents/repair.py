"""Finding-specific proposals; no provider sends or autonomous retry counters."""
import json
from dataclasses import replace

from .contracts import RepairProposal, ScenePrompt
from .schemas import PROMPT_REPAIR_SCHEMA, REPAIR_PROPOSAL_SCHEMA, with_output_schema


def validate_repair(proposal, *, current_digest, review, protected_invariants=None):
    if review.verdict != 'FAIL':
        raise ValueError('only evidenced content FAIL can trigger quality repair')
    if proposal.base_digest != current_digest or review.input_digest != current_digest:
        raise ValueError('stale repair base digest')
    findings = {f.finding_id:f for f in review.findings if f.blocking}
    if not proposal.finding_ids or not set(proposal.finding_ids).issubset(findings):
        raise ValueError('unknown or nonblocking repair findings')
    affected = {findings[f].scene_id for f in proposal.finding_ids if findings[f].scene_id}
    if set(proposal.scene_ids) != affected:
        raise ValueError('repair scene scope differs from findings')
    if protected_invariants is not None and proposal.protected_invariants != protected_invariants:
        raise ValueError('protected invariant changed')
    if set(proposal.changes) & set(proposal.protected_invariants):
        raise ValueError('cannot rewrite protected invariant')
    if proposal.scope == 'assembly' and any(k in proposal.changes for k in ('prompt','scene','video_prompt')):
        raise ValueError('assembly repair must not regenerate video')
    if proposal.scope in {'prompt','scene','keyframe','storyboard'} and review.stage not in proposal.recheck_stages:
        raise ValueError(f'creative revision requires a {review.stage} recheck')


class RepairPlanner:
    def __init__(self, model):
        self.model = model

    def propose(self, *, review, context):
        if review.verdict != 'FAIL':
            raise ValueError('UNKNOWN/infra/uncertain cannot trigger regeneration')
        result = self.model.generate_json(system_prompt=with_output_schema('Repair Planner repair-planner-v1. Diagnose only the supplied blocking '
            'findings. Propose the smallest change to the affected logical scene/frame. Return RepairProposal fields: '
            'base_digest,finding_ids,scope,scene_ids,diagnosis,changes,protected_invariants,recheck_stages. '
            'Never add product facts, reset quotas, rename scenes or Send. Missing source means scope=input. '
            f'Keep protected_invariants exactly as supplied. Recheck the repaired {review.stage} artifact.', REPAIR_PROPOSAL_SCHEMA),
            user_prompt=json.dumps({'review':review.to_dict(),'context':context}, ensure_ascii=False),
            schema_name='repair-proposal-v1', json_schema=REPAIR_PROPOSAL_SCHEMA, temperature=0)
        if result.parse_error or not isinstance(result.parsed, dict):
            raise ValueError('invalid repair proposal JSON')
        data = dict(result.parsed)
        data.pop('digest',None)
        data.pop('evaluator_receipt',None)
        properties = REPAIR_PROPOSAL_SCHEMA['properties']
        if set(data) != set(properties):
            raise ValueError('invalid repair proposal JSON: missing or unexpected fields')
        for key, schema in properties.items():
            value = data[key]
            kind = schema['type']
            valid = (isinstance(value, str) if kind == 'string' else
                     isinstance(value, dict) if kind == 'object' else
                     isinstance(value, list) and all(isinstance(item, str) for item in value))
            if not valid or ('enum' in schema and value not in schema['enum']):
                raise ValueError(f'invalid repair proposal JSON: invalid {key}')
            if kind == 'array':
                allowed = schema['items'].get('enum')
                if (len(value) < schema.get('minItems', 0) or
                        (allowed is not None and any(item not in allowed for item in value))):
                    raise ValueError(f'invalid repair proposal JSON: invalid {key}')
        data['evaluator_receipt']={'provider':result.provider,'model':result.model,
            'route':dict(result.route_receipt or {}),'prompt_version':'repair-planner-v1'}
        for key in ('finding_ids','scene_ids'):
            data[key] = tuple(data.get(key) or ())
        recheck = list(data.get('recheck_stages') or ())
        if data.get('scope') in {'prompt','scene','keyframe','storyboard'} and review.stage not in recheck:
            recheck.append(review.stage)
        data['recheck_stages'] = tuple(recheck)
        proposal = RepairProposal(**data)
        validate_repair(proposal, current_digest=context['current_digest'], review=review,
                        protected_invariants=context['protected_invariants'])
        return proposal


def revise_scene_prompt(*, director, dossier, script, bundle, proposal, review, protected_invariants):
    validate_repair(proposal, current_digest=review.input_digest, review=review,
                    protected_invariants=protected_invariants)
    if proposal.scope not in {'prompt','keyframe'}:
        raise ValueError('this boundary only revises frame/scene prompts')
    result = director.port(dossier).generate_json(system_prompt='Visual Director targeted-repair-v1. Return prompts for exactly '
        'the affected scene IDs. Apply a concrete delta for the supplied findings. Preserve reference IDs, intent and all '
        'protected invariants. Each prompt uses the ScenePrompt fields. Unaffected scenes are retained by code.',
        user_prompt=json.dumps({'proposal':proposal.to_dict(),'review':review.to_dict(),'script':script.to_dict(),
            'prompts':bundle.to_dict()}, ensure_ascii=False), schema_name='prompt-repair-v1',
        json_schema=PROMPT_REPAIR_SCHEMA, temperature=.1)
    if result.parse_error:
        raise ValueError(f'invalid Director repair JSON: {result.parse_error}; raw: {result.raw_text!r}')
    updates = {p['scene_id']:ScenePrompt(**{**p,'reference_asset_ids':bundle.for_scene(p['scene_id']).reference_asset_ids})
               for p in result.parsed['prompts']}
    if set(updates) != set(proposal.scene_ids):
        raise ValueError('repair changed scope')
    for scene_id, prompt in list(updates.items()):
        original=bundle.for_scene(scene_id)
        if prompt == original:
            raise ValueError('repair must have a concrete delta')
        scene = next(s for s in script.scenes if s.scene_id == scene_id)
        if scene.subject_action not in prompt.video_prompt:
            prompt = replace(prompt, video_prompt=f"{prompt.video_prompt.rstrip('. ')}. {scene.subject_action}")
            updates[scene_id] = prompt
        frame_id=str(review.evaluator_receipt.get('artifact_frame_id') or '')
        if review.stage=='keyframe' and frame_id==scene_id+'-end':
            if prompt.start_prompt!=original.start_prompt or prompt.negative_prompt!=original.negative_prompt:
                raise ValueError('END-only repair must preserve accepted START inputs')
        if review.stage=='clip' and proposal.scope=='prompt':
            if (prompt.start_prompt,prompt.end_prompt,prompt.negative_prompt)!=(original.start_prompt,original.end_prompt,original.negative_prompt):
                raise ValueError('clip prompt repair must preserve accepted keyframes')
        if getattr(script, 'scene_bible', None):
            from .director import bind_prompt_bible
            prompt = replace(
                prompt,
                start_prompt=bind_prompt_bible(prompt.start_prompt, script.scene_bible),
                video_prompt=bind_prompt_bible(prompt.video_prompt, script.scene_bible),
                end_prompt=bind_prompt_bible(prompt.end_prompt, script.scene_bible) if prompt.end_prompt else '',
            )
            updates[scene_id] = prompt
    revised = replace(bundle, prompts=tuple(updates.get(p.scene_id,p) for p in bundle.prompts),
        evaluator_receipt={'provider':result.provider,'model':result.model,'route':dict(result.route_receipt or {}),
                           'prompt_version':'visual-director-targeted-repair-v1','review_digest':review.digest})
    from .director import validate_bundle
    validate_bundle(revised, script, director.profile, director.recipe['digest'])
    return revised
