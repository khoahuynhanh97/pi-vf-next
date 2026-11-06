"""Sample real clip/final pixels and both sides of joins with explicit coverage."""
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import os
import av

from ..ports import MediaReference
from .critic import review_input_digest
from .contracts import CreativeReview
from .gates import board_media


def configured_content_critic(package, receipt_root):
    from video_factory.handlers.common import create_configured_creative_generator
    from .critic import CreativeCritic

    return CreativeCritic(create_configured_creative_generator(creative_mode='legacy').model,
                          receipt_root=Path(receipt_root) / str(package['workflow_id']))


def review_production_final(*, path, package, operations, receipt_root):
    from .continuity import scene_start_bindings

    critic = configured_content_critic(package, receipt_root) if package.get('qc_level') == 'high' else None
    result = review_video(path=path, package=package, critic=critic, stage='final',
                          start_bindings=scene_start_bindings(operations))
    result['qc_level'] = package.get('qc_level', 'middle')
    return result


def review_video(*, path, package, critic, stage, scene_id=None, start_bindings=None):
    from video_factory.integrations.composer import probe_video
    path=Path(path)
    probe=probe_video(path,decode_all=True)
    sha=probe.sha256
    scenes=[s for s in package['scenes'] if scene_id is None or s['scene_id']==scene_id]
    if stage == 'clip' and (scene_id is None or len(scenes) != 1):
        raise ValueError('clip review requires one known scene')
    if stage == 'final' and critic is None:
        if package.get('qc_level') == 'high':
            raise ValueError('high QC requires a final content critic')
        cursor=sum(float(scene['duration_seconds']) for scene in scenes)
        failures=[]
        if (not (16 <= probe.duration_seconds <= 50) or (probe.width, probe.height) not in ((720, 1280), (1080, 1920)) or
                abs(probe.fps-30)>.01 or probe.audio_present):
            failures.append('final_profile_mismatch')
        if abs(cursor-probe.duration_seconds)>.5:
            failures.append('scene_duration_mismatch')
        technical={'verdict':'FAIL' if failures else 'PASS','failures':failures,
            'reason':', '.join(failures) or 'decoded stream matches current profile',
            'sha256':sha,'probe':asdict(probe)}
        context={'video_sha256':sha,'scene_ids':[s['scene_id'] for s in scenes],
            'scenes':scenes,'required_timestamps':[],
            'sampling_limitation':'Final content review is not performed in the default MVP flow.',
            'package_digest':package['package_digest']}
        sample_root=path.parent/'creative_samples'/sha
        sample_root.mkdir(parents=True,exist_ok=True)
        result={'creative_mode':'four_agent','sha256':sha,'technical_qc':technical,
            'review_context':context,'review_media':[], 'content_assessment':'not_performed',
            'human_review':'not_performed','creative_lineage':package}
        (sample_root/'final_assessment.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        return result
    targets={0.,max(0.,probe.duration_seconds-1/max(probe.fps,1))}
    cursor=0.
    for scene in scenes:
        duration=float(scene['duration_seconds'])
        targets.update(cursor+duration*f for f in (.05,.25,.5,.75,.95))
        targets.update(cursor+t for t in (.25, .5, .75, 1.) if t < duration)
        if cursor:
            targets.update((max(0.,cursor-.05),min(probe.duration_seconds-.05,cursor+.05)))
        cursor+=duration
    sample_root=path.parent/'creative_samples'/sha
    sample_root.mkdir(parents=True,exist_ok=True)
    media=list(board_media(package))
    bindings = start_bindings or {}
    if stage == 'clip':
        bindings = {key: binding for key, binding in bindings.items() if key == f'{scene_id}-start'}
    if bindings:
        for index, ref in enumerate(media):
            frame_id = ref.role.removeprefix('keyframe:')
            binding = bindings.get(frame_id)
            if binding and ref.role.startswith('keyframe:'):
                media[index] = MediaReference(binding['frame_path'], binding['frame_sha256'], ref.role)
    if stage == 'clip':
        frame_roles = {f'keyframe:{scene_id}-start', f'keyframe:{scene_id}-end'}
        media = [ref for ref in media if not ref.role.startswith('keyframe:') or ref.role in frame_roles]
    timestamps=[]
    for index,target in enumerate(sorted(t for t in targets if t < probe.duration_seconds)):
        with av.open(str(path)) as container:
            stream=container.streams.video[0]
            container.seek(max(0,int((target-1)/stream.time_base)),stream=stream)
            best=None
            delta=float('inf')
            for frame in container.decode(stream):
                timestamp=float(frame.time or 0)
                if abs(timestamp-target)<delta:
                    best,delta=frame,abs(timestamp-target)
                if timestamp>=target:
                    break
            if best is None:
                raise ValueError('video sample missing')
            actual=round(float(best.time or 0),6)
            sample=sample_root/f'{index:03d}.png'
            best.to_image().save(sample)
            timestamps.append(actual)
            media.append(MediaReference(str(sample),hashlib.sha256(sample.read_bytes()).hexdigest(),
                                        f'{stage}:{scene_id or "final"}:t={actual}'))
    context={'video_sha256':sha,'scene_ids':[s['scene_id'] for s in scenes],
             'scenes':scenes,'required_timestamps':timestamps,
             'sampling_limitation':'Sparse decoded frames at actions, endpoints and both sides of joins; intermediate frames unverified.'}
    if bindings:
        context['continuity_bindings'] = bindings
    if stage == 'clip':
        from ..contracts import ResearchDossier
        from .contracts import BackendProfile, ScenePromptBundle
        context.update(product_truth_digest=ResearchDossier.from_dict(package['research_dossier']).digest(),
            backend_profile_digest=BackendProfile.from_dict(package['backend_profile']).digest,
            scene_prompt_digest=ScenePromptBundle.from_dict(package['scene_prompts']).for_scene(scene_id).digest)
    else:
        context['package_digest'] = package['package_digest']
    technical_failures=[]
    if stage=='final' and (not (2.0 <= probe.duration_seconds <= 60) or (probe.width, probe.height) not in ((720, 1280), (1080, 1920)) or
                            abs(probe.fps-30)>.01 or probe.audio_present):
        technical_failures.append('final_profile_mismatch')
    if abs(cursor-probe.duration_seconds)>.5:
        technical_failures.append('scene_duration_mismatch')
    technical={'verdict':'FAIL' if technical_failures else 'PASS','failures':technical_failures,
        'reason':', '.join(technical_failures) or 'decoded stream matches current profile',
        'sha256':sha,'probe':asdict(probe)}
    review=critic.review(stage=stage,context=context,media=tuple(media))
    result={'creative_mode':'four_agent','sha256':sha,'technical_qc':technical,
        'final_review' if stage=='final' else 'clip_review':review.to_dict(),
        'review_context':context,'review_media':[asdict(r) for r in media],
        'review_input_digests':{stage:review.input_digest},
        'content_assessment':'machine_passed' if review.verdict=='PASS' else review.verdict,
        'human_review':'not_performed'}
    if stage=='final':
        result['creative_lineage']=package
    (sample_root/f'{stage}_assessment.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return result


def technical_failure_code(stage, *, content_verdict, technical_verdict):
    prefix='CLIP' if stage=='clip' else 'FINAL'
    if technical_verdict!='PASS':
        return f'{prefix}_TECHNICAL_{technical_verdict}'
    if content_verdict is not None and content_verdict!='PASS':
        return f'{prefix}_CONTENT_{content_verdict}'
    return None


def check_completed_clips(package, clips, critic, *, start_bindings=None):
    """Gate high-QC batches on the current pixels before composition."""
    if package.get('qc_level') != 'high':
        return None
    for scene_id, path in clips:
        options = {'start_bindings': start_bindings} if start_bindings else {}
        assessment = review_video(path=path, package=package, critic=critic, stage='clip', scene_id=scene_id, **options)
        code = technical_failure_code('clip', content_verdict=assessment['clip_review']['verdict'],
                                      technical_verdict=assessment['technical_qc']['verdict'])
        if code:
            return scene_id, code, assessment
    return None


def repair_clip_duration(*, path, package, critic, scene_id, assessment):
    """Correct an evidenced scene-duration mismatch, then re-run clip QC before use."""
    if (assessment.get('clip_review', {}).get('verdict') != 'PASS' or
            assessment.get('technical_qc', {}).get('failures') != ['scene_duration_mismatch']):
        return None
    scenes = [scene for scene in package.get('scenes', []) if scene.get('scene_id') == scene_id]
    if len(scenes) != 1:
        return None
    from video_factory.integrations.composer import normalize_video_duration
    source = Path(path)
    staged = source.with_name(source.stem + '.duration-repair.mp4')
    try:
        normalize_video_duration(source, staged, float(scenes[0]['duration_seconds']))
        checked = review_video(path=staged, package=package, critic=critic, stage='clip', scene_id=scene_id)
        failure = technical_failure_code('clip', content_verdict=checked['clip_review']['verdict'],
            technical_verdict=checked['technical_qc']['verdict'])
        if failure:
            return None
        os.replace(staged, source)
        return checked
    finally:
        staged.unlink(missing_ok=True)


def require_final_pass(artifact):
    from video_factory.integrations.composer import probe_video
    file_path = artifact.get('file_path') or artifact.get('video_url') or artifact.get('path')
    if not file_path or not Path(file_path).is_file():
        raise ValueError('current final technical profile failed: missing file')
    probe=probe_video(Path(file_path),decode_all=True)
    if (probe.sha256!=artifact.get('sha256') or probe.frame_count <= 0 or
            not (2.0 <= probe.duration_seconds <= 60) or (probe.width, probe.height) not in ((720, 1280), (1080, 1920)) or
            abs(probe.fps-30)>.01 or probe.audio_present):
        raise ValueError('current final technical profile failed')
    technical = artifact.get('technical_qc')
    if isinstance(technical, dict) and (technical.get('verdict') != 'PASS' or
            str(technical.get('sha256') or '').removeprefix('sha256:') != probe.sha256):
        raise ValueError('current final technical profile failed')


def require_final_content_pass(artifact):
    """Validate an explicitly supplied optional content receipt for current pixels."""
    require_final_pass(artifact)
    from video_factory.integrations.composer import probe_video
    from .critic import review_input_digest
    path=artifact.get('file_path') or artifact.get('video_url') or artifact.get('path')
    probe=probe_video(Path(path),decode_all=True)
    if artifact.get('final_review') and artifact.get('review_media'):
        media=tuple(MediaReference(**r) for r in artifact['review_media'])
        for ref in media:
            ref.content()
        context=artifact.get('review_context', {})
        review=CreativeReview.from_dict(artifact['final_review'])
        coverage=review.coverage
        reviewed_media=[str(value).removeprefix('sha256:') for value in coverage.get('media_sha256', [])]
        expected_media=[ref.sha256.removeprefix('sha256:') for ref in media]
        if context.get('video_sha256')!=probe.sha256 or review.verdict!='PASS' or review.stage!='final' or (
            review.input_digest!=review_input_digest('final',context,media)):
            raise ValueError('current final content PASS required')
        if (coverage.get('method')!='actual_pixels' or reviewed_media!=expected_media or
            coverage.get('scene_ids')!=context.get('scene_ids') or
            not set(context.get('required_timestamps',[])).issubset(coverage.get('timestamps',[]))):
            raise ValueError('current final review coverage is incomplete or does not match its attached media')
    else:
        raise ValueError('optional final content receipt is missing')
