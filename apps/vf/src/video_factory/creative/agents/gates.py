"""Stable stage contexts, checked against current pixels before downstream work."""
from ..ports import MediaReference
from dataclasses import asdict, fields
from .contracts import CreativeReview
from .critic import review_input_digest
from .strategist import dossier_media


def preproduction_context(dossier, script, bundle, profile):
    return {'dossier':dossier.to_dict(), 'script':script.to_dict(), 'bundle':bundle.to_dict(),
            'profile':profile.to_dict(), 'scene_ids':[s.scene_id for s in script.scenes]}


def scored_preproduction_inputs(artifact, threshold):
    from ..contracts import ResearchDossier, ScriptScenePlanV2

    dossier = ResearchDossier.from_dict(artifact['research_dossier'])
    script = {field.name: artifact[field.name] for field in fields(ScriptScenePlanV2) if field.name in artifact}
    context = {'dossier': artifact['research_dossier'], 'script': script,
               'bundle': artifact['scene_prompts'], 'profile': artifact['backend_profile'],
               'brief': artifact['creative_brief'], 'preproduction_threshold': threshold,
               'scene_ids': [scene['scene_id'] for scene in script['scenes']]}
    if 'rubric_version' in artifact:
        context['rubric_version'] = artifact['rubric_version']
    if 'director_config_version' in artifact:
        context['director_config_version'] = artifact['director_config_version']
    return context, dossier_media(dossier)


def current_preproduction_review(artifact):
    raw = artifact.get('preproduction_review')
    threshold = artifact.get('preproduction_threshold')
    if not isinstance(raw, dict) or type(threshold) is not int:
        return None
    try:
        review = CreativeReview.from_dict(raw)
        context, media = scored_preproduction_inputs(artifact, threshold)
        if review.stage != 'preproduction' or review.input_digest != review_input_digest('preproduction', context, media):
            return None
        receipt = review.evaluator_receipt or {}
        if artifact.get('rubric_version') == 'v2':
            if receipt.get('rubric_version') != 'v2':
                return None
            if receipt.get('director_config_version') != artifact.get('director_config_version'):
                return None
        if review.verdict != 'UNKNOWN':
            for ref in media:
                ref.content()
        return review
    except (KeyError, TypeError, ValueError, OSError):
        return None


def frame_context(dossier, script, bundle, profile, frame):
    scene = next(s for s in script.scenes if s.scene_id == frame['scene_id'])
    return {'dossier':dossier.to_dict(), 'scene':asdict(scene), 'frame':frame,
            'scene_prompt_digest':bundle.for_scene(scene.scene_id).digest,
            'profile':profile.to_dict(), 'scene_ids':[scene.scene_id]}


def board_media(package):
    from ..contracts import ResearchDossier
    dossier = ResearchDossier.from_dict(package['research_dossier'])
    return dossier_media(dossier) + tuple(MediaReference(f['image_path'], f['sha256'],
        f"keyframe:{f['frame_id']}") for f in package['generated_keyframes'])


def require_storyboard_qc_pass(package, *, qc_level='middle'):
    if package.get('creative_mode') != 'four_agent':
        return None
    from video_factory.automation.creative_policy import normalize_qc_level

    qc_level = normalize_qc_level(qc_level)
    receipt = package.get('keyframe_qc_receipt') or {}
    findings = receipt.get('findings') if isinstance(receipt.get('findings'), dict) else {}
    items = findings.get('items') if isinstance(findings.get('items'), list) else []
    if qc_level == 'low':
        if (receipt.get('stage') != 'keyframe' or not package.get('package_digest')
                or receipt.get('artifact_digest') != package.get('package_digest')):
            raise ValueError('current keyframe QC record bound to the storyboard is required')
        return {'verdict': receipt.get('verdict', 'not_performed'),
                'reason': findings.get('reason') or findings.get('error') or
                          (items[0].get('reason') if items and isinstance(items[0], dict) else 'Keyframe QC skipped by low level')}
    if (receipt.get('stage') != 'keyframe' or receipt.get('verdict') != 'pass'
            or not package.get('package_digest')
            or receipt.get('artifact_digest') != package.get('package_digest')
            or findings.get('hard_failure') is True or not items
            or any(not isinstance(item, dict) or item.get('verdict') != 'pass'
                   or item.get('hard_failure') is True for item in items)):
        raise ValueError('current keyframe QC PASS required')
