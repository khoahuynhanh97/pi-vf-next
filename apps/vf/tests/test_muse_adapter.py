from dataclasses import replace
from pathlib import Path
import json
import pytest

from test_agent_contracts import profile
from video_factory.integrations.muse_adapter import MuseVideoAdapter
from video_factory.integrations.ports import VideoGenerationRequest, EndKeyframeInput
from video_factory.integrations.keyframe_provider_factory import resolve_keyframe_provider, KeyframeProviderUnavailable
from video_factory.integrations.provider_selection import VideoProviderSelector


def adapter(tmp_path):
    calls=[]
    p=replace(profile(),provider_kind='muse',image_provider_kind='muse',supports_negative_prompt=False)
    a=MuseVideoAdapter(python=Path(__file__),bridge=Path(__file__),job_root=tmp_path/'jobs',
        media_roots=(tmp_path,),session_root=tmp_path/'session',run_id='run',profile=p,
        launcher=lambda *args,**kwargs:calls.append((args,kwargs)))
    ref=tmp_path/'source.png'
    ref.write_bytes(b'fake offline reference')
    request=VideoGenerationRequest('op1','owner','project','s1','picks exactly two pieces',8,(str(ref),))
    return a,request,calls


def test_duplicate_and_restart_never_spawn_twice(tmp_path):
    a,request,calls=adapter(tmp_path)
    assert a.submit(request).status=='submitted'
    assert a.submit(request).status=='unknown'
    assert a.get_status('op1').error_code=='RECONCILIATION_REQUIRED'
    assert len(calls)==1 and calls[0][1]['shell'] is False
    assert 'main.py' not in str(calls)
    with pytest.raises(ValueError,match='input changed'):
        a.submit(replace(request,prompt='new action'))
    assert len(calls)==1


def test_unsupported_end_frame_and_default_factory_cannot_select_muse(tmp_path):
    a,request,calls=adapter(tmp_path)
    with pytest.raises(ValueError):
        a.submit(replace(request,end_keyframe=EndKeyframeInput('end','path','sha','package','qc')))
    with pytest.raises(KeyframeProviderUnavailable):
        resolve_keyframe_provider(tmp_path,image_provider_kind='muse',muse_adapter=a)
    image,kind=resolve_keyframe_provider(tmp_path,image_provider_kind='muse',creative_mode='four_agent',muse_adapter=a)
    assert kind=='muse' and image.bridge_adapter is a
    with pytest.raises(ValueError,match='pinned'):
        VideoProviderSelector({'muse':a})
    assert VideoProviderSelector({'muse':a},preferred_provider_kind='muse',allow_pre_submit_fallback=False).providers['muse'] is a
    assert calls==[]
