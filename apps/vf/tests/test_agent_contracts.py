from dataclasses import replace

import pytest

from video_factory.creative.agents.contracts import (
    BackendProfile, CreativeReview, ReviewFinding, ScenePrompt, ScenePromptBundle,
)
from video_factory.creative.contracts import canonical_digest


def profile():
    return BackendProfile('google_flow', 'google_flow', 'test-v1', (4,6,8),
                          ('reference',), 2, False, True, 'silent')


def test_canonical_digest_ignores_mapping_order():
    assert canonical_digest({'a': 1, 'b': 2}) == canonical_digest({'b': 2, 'a': 1})


def test_bundle_binds_prompt_profile_and_reference_order():
    prompt = ScenePrompt('s1', 'hand ready', '', 'picks exactly two pieces', '', ('a', 'b'))
    bundle = ScenePromptBundle('script', profile().digest, 'playbook', (prompt,))
    assert replace(bundle, prompts=(replace(prompt, video_prompt='puts two pieces down'),)).digest != bundle.digest
    assert replace(bundle, backend_profile_digest='changed').digest != bundle.digest
    assert replace(bundle, prompts=(replace(prompt, reference_asset_ids=('b', 'a')),)).digest != bundle.digest
    assert ScenePromptBundle.from_dict(bundle.to_dict()) == bundle


def test_review_rejects_invalid_verdict_and_unbound_pass():
    with pytest.raises(ValueError):
        CreativeReview('keyframe', 'APPROVED', 'input', (), {}, {})
    with pytest.raises(ValueError):
        CreativeReview('keyframe', 'PASS', '', (), {}, {})
    with pytest.raises(ValueError):
        CreativeReview('keyframe', 'PASS', 'input', (), {}, {})


def test_review_rejects_unknown_scene_and_pass_with_blocking_finding():
    finding = ReviewFinding('f1', 'other', 'identity', True, 'red', 'blue', (), ('frame1',), .9)
    review = CreativeReview('keyframe', 'FAIL', 'input', (finding,), {}, {})
    with pytest.raises(ValueError):
        review.validate_scenes(('s1',))
    with pytest.raises(ValueError):
        replace(review, verdict='PASS')


def test_content_reference_identity_survives_copy_but_not_changed_json_or_bytes():
    from test_creative_generator import _dossier
    dossier=replace(_dossier(),reference_identity_mode='content',dossier_digest='')
    moved=replace(dossier,eligible_assets=tuple(replace(a,path='copied/'+a.asset_id+'.png') for a in dossier.eligible_assets))
    assert moved.digest()==dossier.digest()
    assert replace(dossier,use_cases=('different intent',)).digest()!=dossier.digest()
    assert replace(dossier,eligible_assets=(replace(dossier.eligible_assets[0],sha256='f'*64),dossier.eligible_assets[1])).digest()!=dossier.digest()


def test_creative_review_from_dict_tolerates_missing_finding_id():
    raw = {
        'stage': 'clip',
        'verdict': 'FAIL',
        'input_digest': 'test_input',
        'findings': [{
            'scene_id': 'scene_2_detail',
            'code': 'framing_defect',
            'blocking': True,
            'expected': 'visible rim',
            'observed': 'cropped rim',
            'reference_ids': ['ref1'],
            'evidence_locations': ['t=1.0'],
            'confidence': 0.95,
        }],
        'coverage': {'method': 'actual_pixels'},
        'evaluator_receipt': {'prompt_version': 'test'},
    }
    rev = CreativeReview.from_dict(raw)
    assert len(rev.findings) == 1
    assert rev.findings[0].finding_id.startswith('finding-scene_2_detail-framing_defect')
    assert rev.findings[0].blocking is True
