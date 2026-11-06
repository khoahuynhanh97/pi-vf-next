"""Basic source QC contract; all evaluator ports are offline."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
import socket

import pytest
from PIL import Image, ImageDraw

from video_factory.qc.evaluator import validate_qc_response
from video_factory.qc.fingerprint import minimum_pass_score
from video_factory.qc.image_qc import evaluate_image_item, run_image_qc
from video_factory.qc.models import NormalizedProduct, QCStage, QCVerdict
from video_factory.qc.service import _image_qc_context_hash


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Source QC policy tests must not open network connections')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)


def response(score=70, **changes):
    raw = dict(identity_score=40, clarity_score=20, usability_score=score-60,
        total_score=score, verdict='pass', eligible=True, hard_failure=False,
        source_branding_detected=False, identity_mismatch=False,
        view_angle='rear', shot_type='detail', visible_details=['matching shape'],
        scene_suitability=['rear'], risk_flags=[], text_coverage_percent=20,
        reason='Clean usable matching product')
    raw.update(changes)
    return raw


@pytest.fixture
def inputs(tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f'{i}.png'
        img = Image.new('RGB', (100, 100), 'white')
        draw = ImageDraw.Draw(img)
        if i:
            draw.ellipse((5, 10, 85, 95), fill='blue')
        else:
            draw.rectangle((25, 5, 65, 75), fill='red')
        img.save(p)
        paths.append(p)
    product = NormalizedProduct(brand='QCY', model='T13 ANC', display_name='QCY T13 ANC')
    ev = Mock(fake_mode=True, evaluator_name='offline', evaluator_version='1', model='fake')
    ev.evaluate_image.return_value = response()
    ev.evaluate_multiple_images.return_value = response(67)
    return paths, product, ev


@pytest.mark.parametrize('score', [66, 67, 70])
def test_clean_low_score_is_eligible(inputs, score):
    paths, product, ev = inputs
    ev.evaluate_image.return_value = response(score)
    item = evaluate_image_item(asset_id='front', local_path=paths[0], product=product, evaluator=ev)
    assert item.eligible and item.score == score


@pytest.mark.parametrize('defect', ['identity_mismatch'])
def test_concrete_defect_still_fails(inputs, defect):
    paths, product, ev = inputs
    ev.evaluate_image.return_value = response(70, **{defect: True}, verdict='fail', eligible=False, hard_failure=True)
    item = evaluate_image_item(asset_id='bad', local_path=paths[0], product=product, evaluator=ev)
    assert not item.eligible and item.verdict == QCVerdict.FAIL


def test_non_obscuring_branding_does_not_disqualify_a_matching_reference(inputs):
    paths, product, ev = inputs
    ev.evaluate_image.return_value = response(
        verdict='fail', eligible=False, source_branding_detected=True,
        reason='Minor seller branding; product remains visible',
    )
    item = evaluate_image_item(asset_id='front', local_path=paths[0], product=product, evaluator=ev)
    assert item.eligible and item.verdict == QCVerdict.PASS


def test_image_qc_passes_with_one_usable_reference_and_ignores_wrong_variant_candidate(inputs):
    paths, product, ev = inputs
    ev.evaluate_image.side_effect = [
        response(75, verdict='fail', eligible=False, hard_failure=True,
                 identity_mismatch=True, risk_flags=['wrong_variant']),
        response(66),
    ]
    pack = dict(resource_pack_id='pack-one-reference', version=1, owner_user_id='owner', project_id='project',
        assets=[dict(asset_id=f'a{i}', local_path=str(path)) for i, path in enumerate(paths)])

    report, eligible = run_image_qc('wf-one-reference', pack, product, ev)

    assert report.verdict == QCVerdict.PASS
    assert [item.asset_id for item in eligible] == ['a1']
    assert report.findings['eligible_count'] == 1
    assert report.findings['items'][0]['findings']['identity_mismatch'] is True
    assert ev.evaluate_image.call_count == 2


@pytest.mark.parametrize('changes', [dict(total_score=99), dict(eligible=False), dict(identity_mismatch=True)])
def test_invalid_or_contradictory_response_is_schema_unknown(inputs, changes):
    paths, product, ev = inputs
    ev.evaluate_image.return_value = response(**changes)
    item = evaluate_image_item(asset_id='bad', local_path=paths[0], product=product, evaluator=ev)
    assert item.verdict == QCVerdict.UNKNOWN and not item.eligible
    assert item.findings['schema_invalid'] and item.findings['usage_status'] == 'consumed'


def test_source_images_are_checked_individually(inputs):
    paths, product, ev = inputs
    anchor = evaluate_image_item(asset_id='front', local_path=paths[0], product=product, evaluator=ev)
    item = evaluate_image_item(asset_id='rear', local_path=paths[1], product=product, evaluator=ev, identity_reference=anchor)
    assert item.eligible
    assert ev.evaluate_image.call_count == 2
    ev.evaluate_multiple_images.assert_not_called()


def test_reference_evaluation_does_not_reuse_mutated_anchor_bytes(inputs):
    paths, product, ev = inputs
    anchor = evaluate_image_item(asset_id='front', local_path=paths[0], product=product, evaluator=ev)
    paths[0].write_bytes(b'changed')
    item = evaluate_image_item(asset_id='rear', local_path=paths[1], product=product, evaluator=ev, identity_reference=anchor)
    assert item.eligible and item.verdict == QCVerdict.PASS
    assert ev.evaluate_image.call_count == 2
    ev.evaluate_multiple_images.assert_not_called()


def test_uncertain_identity_remains_unresolved_and_is_not_a_pass(inputs):
    paths, product, ev = inputs
    ev.evaluate_image.return_value = response(verdict='unknown', eligible=False)
    item = evaluate_image_item(asset_id='rear', local_path=paths[1], product=product, evaluator=ev)
    assert not item.eligible and item.verdict == QCVerdict.UNKNOWN
    ev.evaluate_multiple_images.assert_not_called()


def test_source_mode_changes_context_and_other_stage_thresholds_stay(inputs):
    _, product, ev = inputs
    other = deepcopy(product)
    other.source_context['identity_mode'] = 'source_product'
    assert _image_qc_context_hash(product, ev) != _image_qc_context_hash(other, ev)
    assert minimum_pass_score(QCStage.IMAGE) == 0
    assert minimum_pass_score(QCStage.KEYFRAME) == 0
    assert minimum_pass_score(QCStage.VIDEO) == 80


def test_low_score_references_remain_eligible_without_a_shared_score_gate(inputs):
    paths, product, ev = inputs
    ev.evaluate_image.return_value = response(66)
    pack = dict(resource_pack_id='pack', version=1, owner_user_id='owner', project_id='project',
        assets=[dict(asset_id=f'a{i}', local_path=str(p)) for i,p in enumerate(paths)])
    report, eligible = run_image_qc('wf', pack, product, ev)
    assert report.verdict == QCVerdict.PASS and report.score < 80
    assert [it.asset_id for it in eligible] == ['a0', 'a1']
