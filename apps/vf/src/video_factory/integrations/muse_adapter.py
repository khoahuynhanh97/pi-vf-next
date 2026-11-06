"""Explicit Muse operation bridge. Never a fallback from Flow or Vertex."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
from .ports import VideoGenerationResult


class MuseVideoAdapter:
    provider_kind='muse'
    four_agent_execution=True

    def __init__(self, *, python, bridge, job_root, media_roots, session_root, run_id, profile, launcher=None):
        self.python,self.bridge=Path(python).resolve(),Path(bridge).resolve()
        self.job_root,self.session_root=Path(job_root).resolve(),Path(session_root).resolve()
        self.media_roots=tuple(Path(r).resolve() for r in media_roots)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',run_id):
            raise ValueError('invalid Muse run ID')
        if profile.provider_kind!='muse' or profile.supports_end_frame:
            raise ValueError('Muse profile must expose generic attachments without frame locking')
        self.run_id,self.profile,self.launcher=run_id,profile,launcher or subprocess.Popen

    def is_available(self):
        return self.python.is_file() and self.bridge.is_file()

    def _path(self,operation_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',operation_id):
            raise ValueError('invalid operation ID')
        return self.job_root/self.run_id/operation_id/'request.json'

    def _submit_operation(self, *, operation_id, scene_id, prompt, refs, duration, negative, kind, truth):
        if negative or len(refs)>self.profile.max_reference_images or not refs:
            raise ValueError('unsupported Muse negative prompt or reference mode')
        if kind=='video' and duration not in self.profile.duration_options:
            raise ValueError('unsupported Muse duration')
        media=[]
        for ref in refs:
            path=Path(ref).resolve()
            if not any(path.is_relative_to(root) for root in self.media_roots):
                raise ValueError('reference outside approved roots')
            media.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'role':'generic_attachment'})
        payload={'schema_version':'muse-operation-v1','operation_id':operation_id,'run_id':self.run_id,
            'scene_id':scene_id,'kind':kind,'prompt':prompt,'refs':media,'duration_seconds':duration,
            'profile':self.profile.to_dict(),'negative_prompt':negative,'product_truth_digest':truth}
        path=self._path(operation_id)
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            if json.loads(path.read_text(encoding='utf8'))!=json.loads(json.dumps(payload)):
                raise ValueError('operation identity input changed; reconcile before submitting')
            return self._receipt(operation_id)
        # Exclusive creation persists the identity before process launch. A crash here requires reconciliation.
        try:
            with path.open('x',encoding='utf8') as handle:
                json.dump(payload,handle,ensure_ascii=False,indent=2)
        except FileExistsError:
            return self._submit_operation(operation_id=operation_id,scene_id=scene_id,prompt=prompt,refs=refs,
                duration=duration,negative=negative,kind=kind,truth=truth)
        command=[str(self.python),str(self.bridge),'--request',str(path),'--job-root',str(self.job_root),
                 '--session-root',str(self.session_root)]
        for root in self.media_roots:
            command.extend(('--media-root',str(root)))
        self.launcher(command,shell=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {'operation_id':operation_id,'status':'submitted'}

    def _receipt(self,operation_id):
        path=self._path(operation_id).with_name('receipt.json')
        if not path.is_file():
            return {'operation_id':operation_id,'status':'unknown','reason':'receipt missing; reconcile, never respawn'}
        data=json.loads(path.read_text(encoding='utf8'))
        if data.get('operation_id')!=operation_id:
            raise ValueError('receipt identity mismatch')
        request=json.loads(self._path(operation_id).read_text(encoding='utf8'))
        expected=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if data.get('input_digest')!=expected:
            raise ValueError('receipt input digest mismatch')
        if data['status']=='completed':
            output=Path(data['output_path']).resolve()
            if not output.is_relative_to(self.job_root/self.run_id):
                raise ValueError('receipt output outside run')
            if hashlib.sha256(output.read_bytes()).hexdigest()!=data['output_sha256']:
                raise ValueError('output SHA mismatch')
        return data

    def submit(self,request):
        if request.end_keyframe is not None or request.request_audio:
            raise ValueError('Muse bridge has no first/last locking or requested audio contract')
        data=self._submit_operation(operation_id=request.request_id,scene_id=request.scene_id,prompt=request.prompt,
            refs=request.reference_image_paths,duration=request.duration_seconds,
            negative=request.provider_options.get('negativePrompt',''),kind='video',
            truth=request.provider_options.get('product_truth_digest',''))
        return self._result(request.request_id,data)

    def _result(self,operation_id,data):
        return VideoGenerationResult(request_id=operation_id,status=data['status'],provider_operation_id=operation_id,
            video_path=data.get('output_path'),error_code='RECONCILIATION_REQUIRED' if data['status']=='unknown' else '',
            metadata={**data,'provider_kind':'muse','model':'not_exposed'})

    def get_status(self,operation_id):
        return self._result(operation_id,self._receipt(operation_id))


def configured_muse_adapter(config):
    from video_factory.creative.agents.contracts import BackendProfile
    if config.get('creative_mode')!='four_agent' or config.get('provider_kind')!='muse':
        raise ValueError('Muse requires an explicit four_agent provider configuration')
    return MuseVideoAdapter(python=config['python'],bridge=config['bridge'],job_root=config['job_root'],
        media_roots=config['media_roots'],session_root=config['session_root'],run_id=config['run_id'],
        profile=BackendProfile.from_dict(config['profile']))
