"""Digest-bound catalog handoff stored on existing workflow artifacts.

Readiness is recomputed from the saved source/QC and script dependencies. A
published envelope alone is never authority to generate media.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .contracts import CreativeBriefV2, ResearchDossier, ScriptScenePlanV2, canonical_digest
from .agents.contracts import BackendProfile, ScenePromptBundle
from .agents.playbooks import select_playbook
from .agents.strategist import dossier_media
from video_factory.qc.normalization import resource_pack_fingerprint


@dataclass(frozen=True)
class PreparedProduct:
    product_key: str
    source_snapshot_digest: str
    asset_digests: tuple[str, ...]
    brief_digest: str
    script_digest: str
    source_qc_receipts: tuple[dict[str, Any], ...]
    script_review_digest: str
    creative_profile_digest: str
    artifact_refs: dict[str, str]

    @property
    def digest(self) -> str:
        return canonical_digest(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), 'digest':self.digest}


def creative_profile_digest(run_config: dict[str, Any]) -> str:
    profile = BackendProfile.from_dict(run_config['profile'])
    recipe = select_playbook(run_config['category'], profile, tuple(run_config['visible_details']))
    payload = {'profile':profile.digest, 'playbook':recipe['digest'],
               'category':run_config['category'], 'roles':'four-agent-v1'}
    qc_level = str(run_config.get('qc_level') or 'middle').lower()
    if qc_level != 'middle' or run_config.get('disable_keyframe_qc'):
        payload.update(qc_level=qc_level, disable_keyframe_qc=bool(run_config.get('disable_keyframe_qc')))
    return canonical_digest(payload)


def current_prepared_product(connection, workflow_id: str, aggregate: dict[str, Any], provider_mode: str = 'configured') -> PreparedProduct:
    """Validate existing receipts and actual source bytes without evaluating them."""
    from video_factory.handlers.common import _locked_research_dossier
    from video_factory.qc.models import QCStage
    from video_factory.qc.validation import require_current_pass_report, require_current_report

    locked = aggregate.get('locked_pack')
    pack = aggregate.get('resource_pack')
    if not isinstance(locked, dict) or not isinstance(pack, dict):
        raise ValueError('SOURCE_LOCK_REQUIRED: approved locked resource pack missing')
    low_qc = str((aggregate.get('creative_policy') or {}).get('qc_level') or 'middle').lower() == 'low'
    report = (require_current_report(connection, workflow_id=workflow_id,
        stage=QCStage.IMAGE, artifact=pack, owner_user_id=aggregate.get('owner_user_id'),
        project_id=aggregate.get('project_id'), provider_mode=provider_mode, allow_missing=True)
        if low_qc else require_current_pass_report(connection, workflow_id=workflow_id,
            stage=QCStage.IMAGE, artifact=pack, owner_user_id=aggregate.get('owner_user_id'),
            project_id=aggregate.get('project_id'), provider_mode=provider_mode))
    dossier = ResearchDossier.from_dict(_locked_research_dossier(workflow_id=workflow_id,
        aggregate=aggregate, locked_pack=locked, connection=connection))
    scene = aggregate.get('scene_plan')
    if not isinstance(scene, dict) or scene.get('creative_mode') != 'four_agent':
        raise ValueError('PREPARED_SCRIPT_REQUIRED: four-agent scene artifact missing')
    stored_dossier = ResearchDossier.from_dict(scene['research_dossier'])
    if stored_dossier.digest() != dossier.digest():
        raise ValueError('SOURCE_STALE: approved dossier no longer matches current source QC')
    script = ScriptScenePlanV2.from_dict(scene)
    brief = CreativeBriefV2.from_dict(scene['creative_brief'])
    if brief.dossier_digest != dossier.digest() or script.brief_digest != brief.digest():
        raise ValueError('SCRIPT_STALE: brief/script source bindings changed')
    bundle = ScenePromptBundle.from_dict(scene['scene_prompts'])
    profile = BackendProfile.from_dict(scene['backend_profile'])
    policy = aggregate['creative_policy']
    if profile.digest != BackendProfile.from_dict(policy['profile']).digest:
        raise ValueError('PROFILE_STALE: script backend profile no longer matches preparation')
    if bundle.script_digest != script.digest() or bundle.backend_profile_digest != profile.digest:
        raise ValueError('PROMPT_STALE: prompt dependency binding changed')
    recipe = select_playbook(policy['category'], profile, tuple(policy['visible_details']))
    if bundle.playbook_digest != recipe['digest']:
        raise ValueError('PLAYBOOK_STALE: prepared prompts require explicit preparation')
    for ref in dossier_media(dossier):
        ref.content()
    source = aggregate.get('research_result') or {}
    snapshot = source.get('raw_result') if isinstance(source.get('raw_result'), dict) else source
    context = aggregate.get('source_context') or {}
    key = str(context.get('canonical_product_key') or dossier.product_identity.canonical_product_id
              or aggregate.get('product_key') or '')
    if not key:
        raise ValueError('canonical product key required')
    return PreparedProduct(product_key=key, source_snapshot_digest=canonical_digest(snapshot),
        asset_digests=tuple(a.sha256 for a in dossier.eligible_assets), brief_digest=brief.digest(),
        script_digest=script.digest(), source_qc_receipts=(report.to_dict(),) if report else ({
            'stage': 'image', 'verdict': 'not_performed',
            'artifact_digest': resource_pack_fingerprint(pack), 'qc_level': 'low',
        },),
        # Keep the durable legacy field stable for prepared products created
        # before the MVP removed the duplicate preproduction critic.
        script_review_digest=(None if 'preproduction_threshold' in scene else
                               (scene.get('preproduction_review') or {}).get('digest')) or
            canonical_digest({'script':script.digest(), 'scene_prompts':bundle.digest}),
        creative_profile_digest=creative_profile_digest(policy),
        artifact_refs={'source_workflow_id':workflow_id, 'preparation_run_id':str(policy['preparation_id']),
                       'project_id':str(aggregate['project_id']), 'scene_plan':'scene_plan',
                       'source_pack':'resource_pack'})
