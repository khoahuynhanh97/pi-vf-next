from fractions import Fraction
import av
import numpy as np

from video_factory.integrations.composer import concatenate_videos, probe_video, check_black_bars_in_video


def test_silent_composition_preserves_duration_and_timeline_border_sampling(tmp_path):
    source=tmp_path/'synthetic.mp4'
    with av.open(str(source),'w') as out:
        stream=out.add_stream('libx264',rate=24)
        stream.width,stream.height,stream.pix_fmt=90,160,'yuv420p'
        audio=out.add_stream('aac',rate=44100)
        for i in range(48):
            frame=av.VideoFrame.from_ndarray(np.full((160,90,3),80+i,dtype=np.uint8),format='rgb24')
            frame.pts=i
            frame.time_base=Fraction(1,24)
            for packet in stream.encode(frame): out.mux(packet)
        for packet in stream.encode(): out.mux(packet)
        sound=av.AudioFrame.from_ndarray(np.zeros((2,88200),dtype=np.float32),format='fltp',layout='stereo')
        sound.sample_rate=44100
        for packet in audio.encode(sound): out.mux(packet)
        for packet in audio.encode(): out.mux(packet)
    final=tmp_path/'silent.mp4'
    concatenate_videos([source],final,target_width=90,target_height=160,target_fps=30,silent=True)
    probe=probe_video(final)
    assert abs(probe.duration_seconds-2) < .1
    with av.open(str(final)) as container:
        assert len(container.streams.audio) == 0
        assert container.streams.video[0].average_rate == 30
    coverage=check_black_bars_in_video(final,num_samples=5)
    assert coverage['samples_checked'] == 5
    assert coverage['sample_timestamps'][-1] > 1.8


def test_full_final_profile_has_current_sha_review_and_explicit_machine_status(tmp_path):
    import hashlib,json
    from dataclasses import replace
    from PIL import Image
    from test_creative_generator import _dossier
    from video_factory.creative.agents.critic import CreativeCritic
    from video_factory.creative.agents.contracts import CreativeReview
    from video_factory.creative.agents.video_review import review_video,require_final_pass,require_final_content_pass
    from video_factory.creative.ports import StructuredModelResult
    from video_factory.creative.contracts import canonical_digest
    import pytest

    assets=[]
    dossier=_dossier()
    for index,asset in enumerate(dossier.eligible_assets):
        path=tmp_path/f'fixture-{index}.png'
        Image.new('RGB',(12,16),('red','blue')[index]).save(path)
        assets.append(replace(asset,path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    dossier=replace(dossier,eligible_assets=tuple(assets),dossier_digest='')
    dossier=replace(dossier,dossier_digest=dossier.digest())
    final=tmp_path/'synthetic-32s.mp4'
    pixels=np.full((1920,1080,3),120,dtype=np.uint8)
    with av.open(str(final),'w') as out:
        stream=out.add_stream('libx264',rate=30)
        stream.width,stream.height,stream.pix_fmt=1080,1920,'yuv420p'
        stream.options={'preset':'ultrafast','crf':'35','threads':'2'}
        for i in range(960):
            frame=av.VideoFrame.from_ndarray(pixels,format='rgb24')
            frame.pts=i
            frame.time_base=Fraction(1,30)
            for packet in stream.encode(frame): out.mux(packet)
        for packet in stream.encode(): out.mux(packet)
    class FakeVision:
        calls=0
        def generate_media_json(self,**kwargs):
            self.calls+=1
            context=json.loads(kwargs['user_prompt'])
            return StructuredModelResult({'verdict':'PASS','findings':[],'coverage':{
                'method':'actual_pixels','media_sha256':[r.sha256 for r in kwargs['media']],
                'scene_ids':context['scene_ids'],'timestamps':context['required_timestamps'],
                'limitations':'Synthetic fixture; fake assessment, not product fidelity proof'}},'{}','fake','fake-vision')
    package={'research_dossier':dossier.to_dict(),'generated_keyframes':[],
        'scenes':[{'scene_id':f's{i+1}','duration_seconds':8,'subject_action':'synthetic fixture'} for i in range(4)],
        'package_digest':canonical_digest({'synthetic':True})}
    model=FakeVision()
    assessment=review_video(path=final,package=package,critic=CreativeCritic(model,receipt_root=tmp_path/'reviews'),stage='final')
    assert assessment['technical_qc']['verdict']=='PASS'
    assert assessment['human_review']=='not_performed'
    require_final_pass({**assessment,'file_path':str(final)})
    assert model.calls==1
    assert len(assessment['review_context']['required_timestamps'])>=20
    timestamps = assessment['review_context']['required_timestamps']
    assert all(any(abs(t - (start + 0.5)) < 0.04 for t in timestamps) for start in (0, 8, 16, 24))
    repeat=review_video(path=final,package=package,critic=CreativeCritic(model,receipt_root=tmp_path/'reviews'),stage='final')
    assert repeat['final_review']==assessment['final_review'] and model.calls==1
    from video_factory.qc.video_qc import run_video_qc
    from video_factory.qc.models import NormalizedProduct
    from video_factory.qc.repository import QCReportRepository
    from video_factory.qc.validation import require_current_pass_report
    import sqlite3
    class NoSecondContentReview:
        evaluator_name='must_not_run'
        evaluator_version='1'
        model='must-not-run'
        def evaluate_multiple_images(self, **kwargs):
            raise AssertionError('final content was already reviewed for this exact SHA')
    storyboard={
        'scene_boundaries':[
            {'scene_number':i+1,'start_seconds':i*8,'duration_seconds':8}
            for i in range(4)
        ],
        'frames':[{'description':'synthetic fixture'} for _ in range(4)],
    }
    qc_repo=QCReportRepository(sqlite3.connect(':memory:'))
    video_report=run_video_qc('wf-reuse-final-review',final,storyboard,
        NormalizedProduct(brand='Fixture',model='Synthetic1'),[assets[0].path],
        expected_duration=32,evaluator=NoSecondContentReview(),
        repo=qc_repo,
        owner_user_id='owner',project_id='project',artifact_id='artifact',
        four_agent=True,final_review_evidence={**assessment,'file_path':str(final)})
    assert video_report.findings['content_evidence_source']=='creative_final_review'
    assert video_report.verdict.value=='pass'
    assert video_report.provider_mode=='fake'
    assert video_report.score==0  # no fabricated numeric score for the reused holistic review
    assert len(video_report.findings['visual_scenes'])==4
    current_artifact={**assessment,'file_path':str(final),'artifact_id':'artifact',
        'creative_mode':'four_agent',
        'artifact_version':1,'sha256':assessment['sha256']}
    require_current_pass_report(qc_repo,workflow_id='wf-reuse-final-review',stage='video',
        artifact=current_artifact,
        owner_user_id='owner',project_id='project',provider_mode='fake')
    assert qc_repo.get_verified_pass_report(owner_user_id='owner',project_id='project',
        workflow_id='wf-reuse-final-review',artifact_id='artifact',artifact_version=1,
        stage=video_report.stage,expected_sha256=assessment['sha256'],
        artifact_path=str(final)) is None
    qc_repo.save_report(replace(video_report,report_id='qc-reused-final-with-score',
        score=100,provider_mode='configured'))
    assert qc_repo.get_verified_pass_report(owner_user_id='owner',project_id='project',
        workflow_id='wf-reuse-final-review',artifact_id='artifact',artifact_version=1,
        stage=video_report.stage,expected_sha256=assessment['sha256'],
        artifact_path=str(final)) is None
    from video_factory.application.operator_handler import _require_current_pass_qc
    with pytest.raises(ValueError,match='QC_SCORE_NOT_APPLICABLE'):
        _require_current_pass_qc(qc_repo.conn,'wf-reuse-final-review',video_report.stage,
            current_artifact,provider_mode='configured')
    invalid=replace(CreativeReview.from_dict(assessment['final_review']),verdict='UNKNOWN').to_dict()
    with pytest.raises(ValueError,match='content PASS'):
        require_final_content_pass({**assessment,'file_path':str(final),'final_review':invalid})
    incomplete=replace(CreativeReview.from_dict(assessment['final_review']),coverage={
        **CreativeReview.from_dict(assessment['final_review']).coverage,
        'timestamps':CreativeReview.from_dict(assessment['final_review']).coverage['timestamps'][:-1],
    }).to_dict()
    with pytest.raises(ValueError,match='coverage'):
        require_final_content_pass({**assessment,'file_path':str(final),'final_review':incomplete})


def test_technical_failure_keeps_content_pass_classification():
    import pytest
    from video_factory.creative.agents.video_review import technical_failure_code
    from video_factory.mcp_adapters import _check_for_errors
    from video_factory.job_contracts import TerminalJobError
    assert technical_failure_code('clip',content_verdict='PASS',technical_verdict='FAIL') == 'CLIP_TECHNICAL_FAIL'
    assert technical_failure_code('final',content_verdict='PASS',technical_verdict='FAIL') == 'FINAL_TECHNICAL_FAIL'
    assert technical_failure_code('final',content_verdict='FAIL',technical_verdict='PASS') == 'FINAL_CONTENT_FAIL'
    with pytest.raises(TerminalJobError) as caught:
        _check_for_errors({'status':'failed','code':'FINAL_TECHNICAL_FAIL','creative_failure':{'assessment':{'technical_qc':{'verdict':'FAIL'}}}})
    assert caught.value.code == 'FINAL_TECHNICAL_FAIL'
    assert caught.value.artifact_ref['assessment']['technical_qc']['verdict'] == 'FAIL'


def test_final_technical_assessment_does_not_call_content_critic(tmp_path, monkeypatch):
    import pytest
    from video_factory.creative.agents import video_review
    from video_factory.creative.agents.critic import CreativeCritic
    from video_factory.integrations.composer import compute_file_sha256, VideoProbe

    monkeypatch.setattr(
        CreativeCritic,
        "review",
        lambda *args, **kwargs: pytest.fail("final technical review called the content critic"),
    )

    final = tmp_path / 'final.mp4'
    final.write_bytes(b'video bytes')
    probe = VideoProbe(path=str(final), sha256=compute_file_sha256(final), duration_seconds=32.0,
        fps=30.0, width=1080, height=1920, codec='h264', audio_present=False, frame_count=960)
    monkeypatch.setattr('video_factory.integrations.composer.probe_video', lambda *a, **k: probe)

    assessment = video_review.review_video(path=final, package={'scenes':[{'scene_id':'s1',
        'duration_seconds':32}], 'package_digest':'board'}, critic=None, stage='final')

    assert assessment['technical_qc']['verdict'] == 'PASS'
    assert assessment['human_review'] == 'not_performed'
    assert 'final_review' not in assessment
    video_review.require_final_pass({**assessment, 'file_path':str(final)})


def test_final_technical_assessment_still_rejects_corrupt_or_bad_profile(tmp_path, monkeypatch):
    import pytest
    from video_factory.creative.agents import video_review
    from video_factory.integrations.composer import compute_file_sha256, VideoProbe

    final = tmp_path / 'final.mp4'
    final.write_bytes(b'video bytes')
    probe = VideoProbe(path=str(final), sha256=compute_file_sha256(final), duration_seconds=8.0,
        fps=30.0, width=1080, height=1920, codec='h264', audio_present=False, frame_count=240)
    monkeypatch.setattr('video_factory.integrations.composer.probe_video', lambda *a, **k: probe)
    assessment = video_review.review_video(path=final, package={'scenes':[{'scene_id':'s1',
        'duration_seconds':8}], 'package_digest':'board'}, critic=None, stage='final')
    assert assessment['technical_qc']['verdict'] == 'FAIL'
    with pytest.raises(ValueError, match='technical'):
        video_review.require_final_pass({**assessment, 'file_path':str(final)})


def test_clip_duration_repair_rechecks_and_replaces_only_after_pass(tmp_path, monkeypatch):
    from pathlib import Path
    from video_factory.creative.agents import video_review
    from video_factory.integrations.composer import probe_video

    source = tmp_path / 'clip.mp4'
    with av.open(str(source), 'w') as out:
        stream = out.add_stream('libx264', rate=24)
        stream.width, stream.height, stream.pix_fmt = 90, 160, 'yuv420p'
        for index in range(24):
            frame = av.VideoFrame.from_ndarray(np.full((160, 90, 3), 90, dtype=np.uint8), format='rgb24')
            frame.pts, frame.time_base = index, Fraction(1, 24)
            for packet in stream.encode(frame): out.mux(packet)
        for packet in stream.encode(): out.mux(packet)

    package = {'scenes': [{'scene_id': 'scene-1', 'duration_seconds': 1.5}]}
    original = probe_video(source).sha256
    assessment = {'clip_review': {'verdict': 'PASS'},
        'technical_qc': {'verdict': 'FAIL', 'failures': ['scene_duration_mismatch']}}
    calls = []
    def recheck(**kwargs):
        calls.append(kwargs['path'])
        sha = probe_video(kwargs['path']).sha256
        return {'clip_review': {'verdict': 'PASS'},
            'technical_qc': {'verdict': 'PASS', 'failures': [], 'sha256': sha}, 'sha256': sha}
    monkeypatch.setattr(video_review, 'review_video', recheck)

    repaired = video_review.repair_clip_duration(path=source, package=package, critic=object(),
        scene_id='scene-1', assessment=assessment)

    assert repaired['technical_qc']['verdict'] == 'PASS'
    assert probe_video(source).frame_count == 36
    assert probe_video(source).sha256 != original
    assert calls and not Path(calls[0]).exists()
