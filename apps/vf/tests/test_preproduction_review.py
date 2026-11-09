import hashlib
from PIL import Image

from video_factory.creative.agents.critic import CreativeCritic, review_input_digest
from video_factory.creative.ports import MediaReference, StructuredModelResult


def media_fixture(tmp_path):
    path = tmp_path/'synthetic.png'
    Image.new('RGB', (16,16), 'red').save(path)
    return (MediaReference(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), 'synthetic_source'),)


class FakeCriticModel:
    def __init__(self, verdict='PASS', incomplete=False):
        self.calls = 0
        self.verdict, self.incomplete = verdict, incomplete

    def generate_media_json(self, **kwargs):
        self.calls += 1
        for ref in kwargs['media']:
            ref.content()
        return StructuredModelResult({'verdict':self.verdict, 'findings':[],
            'coverage':{'method':'actual_pixels', 'media_sha256':[] if self.incomplete else [r.sha256 for r in kwargs['media']],
                        'scene_ids':['s1'], 'limitations':'synthetic test pixels'}}, '{}', 'fake', 'fake-vision')


def test_review_is_bound_to_bytes_intent_and_profile_and_skips(tmp_path):
    media = media_fixture(tmp_path)
    context = {'scene_ids':['s1'], 'action':'pick two', 'profile':'v1'}
    model = FakeCriticModel()
    critic = CreativeCritic(model, receipt_root=tmp_path/'reviews')
    first = critic.review(stage='preproduction', context=context, media=media)
    assert first.verdict == 'PASS'
    assert critic.review(stage='preproduction', context=context, media=media) == first
    assert model.calls == 1
    changed = {**context, 'action':'put down'}
    assert review_input_digest('preproduction', changed, media) != first.input_digest
    critic.review(stage='preproduction', context=changed, media=media)
    assert model.calls == 2
    (tmp_path/'synthetic.png').write_bytes(b'changed')
    assert critic.review(stage='preproduction', context=context, media=media).verdict == 'UNKNOWN'
    assert model.calls == 2


def test_unsupported_path_only_and_incomplete_coverage_never_pass(tmp_path):
    media = media_fixture(tmp_path)
    context = {'scene_ids':['s1']}
    assert CreativeCritic(object()).review(stage='preproduction', context=context, media=media).verdict == 'UNKNOWN'
    assert CreativeCritic(FakeCriticModel(incomplete=True)).review(stage='preproduction', context=context, media=media).verdict == 'UNKNOWN'


def test_timeout_is_unknown_without_quality_generation(tmp_path):
    class TimeoutModel:
        def generate_media_json(self, **kwargs):
            raise TimeoutError('fake timeout')
    assert CreativeCritic(TimeoutModel()).review(stage='preproduction', context={'scene_ids':['s1']},
                                               media=media_fixture(tmp_path)).verdict == 'UNKNOWN'


def test_unchanged_unknown_review_is_reused_without_duplicate_content_call(tmp_path):
    media = media_fixture(tmp_path)
    context = {'scene_ids':['s1']}
    model = FakeCriticModel('UNKNOWN')
    critic = CreativeCritic(model, receipt_root=tmp_path/'reviews')

    first = critic.review(stage='preproduction', context=context, media=media)
    second = critic.review(stage='preproduction', context=context, media=media)

    assert first.verdict == second.verdict == 'UNKNOWN'
    assert first.input_digest == second.input_digest
    assert model.calls == 1


def test_critic_prompt_version_invalidates_review_digest(tmp_path, monkeypatch):
    import video_factory.creative.agents.critic as critic_module
    context={'scene_ids':['s1']}
    media=media_fixture(tmp_path)
    first=review_input_digest('preproduction',context,media)
    monkeypatch.setattr(critic_module,'CRITIC_PROMPT_VERSION','creative-critic-next')
    assert review_input_digest('preproduction',context,media) != first



def test_scored_preproduction_has_weighted_threshold_and_hard_failures(tmp_path):
    from dataclasses import replace

    class ScoringModel(FakeCriticModel):
        def generate_media_json(self, **kwargs):
            result = super().generate_media_json(**kwargs)
            result.parsed['scores'] = dict(identity_claims=30, feasibility=25, silent_hook=19,
                                           continuity=14, variety=10)
            result.parsed['hard_failures'] = self.hard_failures
            return replace(result, total_tokens=123)

    model = ScoringModel()
    model.hard_failures = []
    critic = CreativeCritic(model)
    context = {'scene_ids': ['s1'], 'preproduction_threshold': 80}
    media = media_fixture(tmp_path)
    passed = critic.review(stage='preproduction', context=context, media=media)
    assert passed.verdict == 'PASS'
    assert passed.coverage['scorecard']['total'] == 98
    assert passed.evaluator_receipt['total_tokens'] == 123
    stricter = critic.review(stage='preproduction', context={**context, 'preproduction_threshold': 99}, media=media)
    assert stricter.verdict == 'FAIL' and stricter.input_digest != passed.input_digest
    model.hard_failures = ['impossible_action']
    blocked = critic.review(stage='preproduction', context=context, media=media)
    assert blocked.verdict == 'FAIL' and blocked.coverage['scorecard']['total'] == 98



def test_new_catalog_policy_defaults_to_observe_and_reserves_enforce():
    import pytest
    from test_agent_contracts import profile
    from video_factory.automation.coordinator import ProductVideoCoordinator

    config = dict(profile=profile().to_dict(), category='gadget_demo', visible_details=['visible_controls'],
                  max_scenes=4, image_call_limit=8, video_call_limit=4)
    make = ProductVideoCoordinator._catalog_run_policy
    policy = make(config, preparation_id='prep', product_key='key')
    assert (policy['preproduction_gate'], policy['preproduction_threshold']) == ('observe', 80)
    with pytest.raises(ValueError, match='not enabled'):
        make({**config, 'preproduction_gate': 'enforce'}, preparation_id='prep', product_key='key')


def test_v2_preproduction_review_scores_and_scene_pov_ratios(tmp_path):
    from dataclasses import replace

    class ScoringModelV2(FakeCriticModel):
        def __init__(self, scores=None, scene_pov=None, hard_failures=None):
            super().__init__()
            self.scores = scores or dict(
                identity_claims=25,
                feasibility=20,
                pov_experience=20,
                visual_hook=15,
                continuity=10,
                variety=10,
            )
            self.scene_pov = scene_pov if scene_pov is not None else [
                {'scene_id': 's1', 'kind': 'experiential'},
                {'scene_id': 's2', 'kind': 'display'},
            ]
            self.hard_failures = hard_failures or []

        def generate_media_json(self, **kwargs):
            result = super().generate_media_json(**kwargs)
            result.parsed['scores'] = dict(self.scores)
            result.parsed['scene_pov'] = list(self.scene_pov)
            result.parsed['hard_failures'] = list(self.hard_failures)
            expected_cov = kwargs.get('user_prompt')
            if expected_cov:
                import json as _json
                try:
                    payload = _json.loads(expected_cov)
                    exp = payload.get('expected_coverage', {})
                    if 'scene_ids' in exp:
                        result.parsed['coverage']['scene_ids'] = list(exp['scene_ids'])
                    if 'media_sha256' in exp:
                        result.parsed['coverage']['media_sha256'] = list(exp['media_sha256'])
                except Exception:
                    pass
            return replace(result, total_tokens=150)

    media = media_fixture(tmp_path)
    context = {
        'scene_ids': ['s1', 's2'],
        'rubric_version': 'v2',
        'director_config_version': 'scene-bible-v1',
        'scene_bible': {'scenes': [{'id': 's1'}, {'id': 's2'}]},
        'preproduction_threshold': 80,
    }

    model = ScoringModelV2()
    critic = CreativeCritic(model, receipt_root=tmp_path / 'reviews')
    review = critic.review(stage='preproduction', context=context, media=media)

    assert review.verdict == 'PASS'
    scorecard = review.coverage['scorecard']
    expected_scores = dict(
        identity_claims=25,
        feasibility=20,
        pov_experience=20,
        visual_hook=15,
        continuity=10,
        variety=10,
    )
    assert scorecard['scores'] == expected_scores
    assert scorecard['total'] == sum(expected_scores.values())
    assert scorecard['n_experiential'] == 1
    assert scorecard['n_display'] == 1
    assert scorecard['scene_pov'] == [
        {'scene_id': 's1', 'kind': 'experiential'},
        {'scene_id': 's2', 'kind': 'display'},
    ]

    receipt = review.evaluator_receipt
    assert receipt['rubric_version'] == 'v2'
    assert receipt['director_config_version'] == 'scene-bible-v1'


def test_v2_preproduction_review_duplicate_or_missing_scene_classification_makes_unknown(tmp_path):
    class MalformedPovModel(FakeCriticModel):
        def __init__(self, scene_pov):
            super().__init__()
            self.scene_pov = scene_pov

        def generate_media_json(self, **kwargs):
            result = super().generate_media_json(**kwargs)
            result.parsed['scores'] = dict(
                identity_claims=25,
                feasibility=20,
                pov_experience=20,
                visual_hook=15,
                continuity=10,
                variety=10,
            )
            result.parsed['scene_pov'] = self.scene_pov
            result.parsed['hard_failures'] = []
            return result

    media = media_fixture(tmp_path)
    context = {
        'scene_ids': ['s1', 's2'],
        'rubric_version': 'v2',
        'director_config_version': 'scene-bible-v1',
    }

    # Missing classification for 's2'
    missing_model = MalformedPovModel(scene_pov=[{'scene_id': 's1', 'kind': 'experiential'}])
    critic_missing = CreativeCritic(missing_model, receipt_root=tmp_path / 'reviews_missing')
    review_missing = critic_missing.review(stage='preproduction', context=context, media=media)
    assert review_missing.verdict == 'UNKNOWN'
    assert review_missing.evaluator_receipt['rubric_version'] == 'v2'
    assert review_missing.evaluator_receipt['director_config_version'] == 'scene-bible-v1'

    # Duplicate classification for 's1'
    duplicate_model = MalformedPovModel(scene_pov=[
        {'scene_id': 's1', 'kind': 'experiential'},
        {'scene_id': 's1', 'kind': 'display'},
    ])
    critic_dup = CreativeCritic(duplicate_model, receipt_root=tmp_path / 'reviews_dup')
    review_dup = critic_dup.review(stage='preproduction', context=context, media=media)
    assert review_dup.verdict == 'UNKNOWN'
    assert review_dup.evaluator_receipt['rubric_version'] == 'v2'
    assert review_dup.evaluator_receipt['director_config_version'] == 'scene-bible-v1'


def test_v2_preproduction_prior_review_stale_when_scene_bible_changes(tmp_path):
    media = media_fixture(tmp_path)
    base_context = {
        'scene_ids': ['s1'],
        'rubric_version': 'v2',
        'director_config_version': 'scene-bible-v1',
        'scene_bible': {'scenes': [{'id': 's1', 'shot': 'wide'}]},
    }
    model = FakeCriticModel()
    critic = CreativeCritic(model, receipt_root=tmp_path / 'reviews')

    first = critic.review(stage='preproduction', context=base_context, media=media)
    assert first.verdict == 'PASS'
    assert model.calls == 1

    changed_bible_context = {
        **base_context,
        'scene_bible': {'scenes': [{'id': 's1', 'shot': 'close-up'}]},
    }

    assert review_input_digest('preproduction', changed_bible_context, media) != first.input_digest
    second = critic.review(stage='preproduction', context=changed_bible_context, media=media)
    assert second.input_digest != first.input_digest
    assert model.calls == 2


def test_v2_preproduction_review_hard_failure_and_gate_receipt_checks(tmp_path):
    from dataclasses import replace
    from test_creative_generator import _dossier
    from video_factory.creative.agents.gates import current_preproduction_review, scored_preproduction_inputs
    from video_factory.creative.contracts import EligibleAsset

    class ScoringModelV2HardFail(FakeCriticModel):
        def generate_media_json(self, **kwargs):
            result = super().generate_media_json(**kwargs)
            result.parsed['scores'] = dict(
                identity_claims=25,
                feasibility=20,
                pov_experience=20,
                visual_hook=15,
                continuity=10,
                variety=10,
            )
            result.parsed['scene_pov'] = [{'scene_id': 's1', 'kind': 'experiential'}]
            result.parsed['hard_failures'] = ['wrong_product']
            return replace(result, total_tokens=150)

    # Temporary image for assets
    img = tmp_path / 'synthetic.png'
    Image.new('RGB', (16, 16), 'red').save(img)
    img_bytes = img.read_bytes()
    img_sha = hashlib.sha256(img_bytes).hexdigest()

    base_dossier = _dossier()
    dossier = replace(
        base_dossier,
        dossier_digest="",
        eligible_assets=(
            EligibleAsset('asset-1', str(img), img_sha, 'iqc-1'),
        ),
    )

    artifact_base = {
        'research_dossier': dossier.to_dict(),
        'target_duration_seconds': 6,
        'brief_digest': 'sha256:' + 'a' * 64,
        'scenes': [{
            'scene_id': 's1',
            'order': 1,
            'story_role': 'hook',
            'duration_seconds': 6,
            'narration': '',
            'overlay_text': '',
            'visual_objective': 'Trình diễn',
            'environment': 'studio',
            'subject_action': 'cầm sản phẩm',
            'product_interaction': 'trải nghiệm',
            'start_state': 'bắt đầu',
            'end_state': 'kết thúc',
            'visual_change': 'thay đổi',
            'camera_framing': 'medium',
            'camera_movement': 'static',
            'transition_out': 'cut',
        }],
        'scene_bible': {
            'environment': 'modern studio',
            'lighting': 'soft key light',
            'pov_hand': 'natural right hand',
            'camera_baseline': 'eye level 45mm',
        },
        'scene_prompts': {},
        'backend_profile': {},
        'creative_brief': {},
        'preproduction_threshold': 80,
        'rubric_version': 'v2',
        'director_config_version': 'scene-bible-v1',
    }

    context, media = scored_preproduction_inputs(artifact_base, 80)
    critic = CreativeCritic(ScoringModelV2HardFail())
    review = critic.review(stage='preproduction', context=context, media=media)
    assert review.verdict == 'FAIL'
    assert review.coverage['scorecard']['hard_failures'] == ['wrong_product']
    assert review.evaluator_receipt['rubric_version'] == 'v2'
    assert review.evaluator_receipt['director_config_version'] == 'scene-bible-v1'

    # Gate accepts matching review dict
    matched_artifact = {**artifact_base, 'preproduction_review': review.to_dict()}
    gate_review = current_preproduction_review(matched_artifact)
    assert gate_review is not None
    assert gate_review.verdict == 'FAIL'

    # Tampered receipt tags: remove digest or regenerate valid receipt to test gate not just envelope
    tampered_digest_artifact = {
        **matched_artifact,
        'preproduction_review': {**review.to_dict(), 'input_digest': ''},
    }
    assert current_preproduction_review(tampered_digest_artifact) is None

    # Mismatched rubric_version rejected by gate
    stale_rubric_artifact = {
        **matched_artifact,
        'preproduction_review': {
            **review.to_dict(),
            'evaluator_receipt': {**review.evaluator_receipt, 'rubric_version': 'v1'},
        },
    }
    assert current_preproduction_review(stale_rubric_artifact) is None

    # Mismatched director_config_version rejected by gate
    stale_director_artifact = {
        **matched_artifact,
        'preproduction_review': {
            **review.to_dict(),
            'evaluator_receipt': {**review.evaluator_receipt, 'director_config_version': 'scene-bible-v0'},
        },
    }
    assert current_preproduction_review(stale_director_artifact) is None

    # Changed nested scene_bible rejects old receipt
    changed_bible_artifact = {
        **matched_artifact,
        'scene_bible': {
            **artifact_base['scene_bible'],
            'lighting': 'harsh neon light',
        },
    }
    assert current_preproduction_review(changed_bible_artifact) is None
