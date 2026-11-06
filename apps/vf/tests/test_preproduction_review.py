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
