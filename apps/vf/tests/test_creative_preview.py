from video_factory.cli import build_parser


def test_preview_requires_local_inputs_and_has_no_generation_flag():
    args = build_parser().parse_args(['creative-preview','--dossier','d.json','--profile','p.json','--output','out'])
    assert args.func.__name__ == 'cmd_creative_preview'
    assert not hasattr(args, 'auto')


def test_real_role_schemas_preview_and_unchanged_rerun_are_offline(tmp_path):
    import hashlib,json
    from dataclasses import replace
    from PIL import Image
    from test_creative_generator import _dossier,_valid_brief
    from test_creative_handler_wiring import _brief,_script
    from test_agent_contracts import profile
    from video_factory.creative.agents.service import AgenticCreativeGenerator
    from video_factory.creative.agents.preview import write_preview
    from video_factory.creative.ports import StructuredModelResult

    dossier=_dossier()
    assets=[]
    for index,asset in enumerate(dossier.eligible_assets):
        path=tmp_path/f'synthetic-reference-{index}.png'
        Image.new('RGB',(90,160),('red','blue')[index]).save(path)
        assets.append(replace(asset,path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    dossier=replace(dossier,product_identity=replace(dossier.product_identity,brand='Fixture',model='Synthetic case',display_name='Synthetic case'),
                    eligible_assets=tuple(assets),dossier_digest='')

    class FakeModel:
        calls=0
        def generate_media_json(self,**kwargs):
            self.calls+=1
            for ref in kwargs['media']: ref.content()
            schema=kwargs['schema_name']
            if schema=='creative-brief-v2':
                data={**_valid_brief(dossier),'target_duration_seconds':32}
            elif schema=='script-scene-plan-v2':
                # Recover the exact approved brief carried in the actual request.
                text=kwargs['user_prompt']
                brief=None
                decoder=json.JSONDecoder()
                for index,char in enumerate(text):
                    if char=='{':
                        try: value,_=decoder.raw_decode(text[index:])
                        except ValueError: continue
                        if isinstance(value,dict) and value.get('schema_version')=='creative-brief-v2':
                            brief=value
                            break
                assert brief
                template=_script(_brief(dossier))
                scenes=[]
                for i in range(4):
                    old=template.scenes[min(i,2)]
                    scenes.append(replace(old,scene_id=f's{i+1}',order=i+1,duration_seconds=8,
                        narration='',overlay_text='',claim_refs=('claim-01',),
                        product_placement='center on desk',lighting='daylight',continuity_from_previous='same case',
                        continuity_to_next='same case in same hand',product_identity_cues=('exact case',),
                        required_visible_details=('case shape',),reference_requirements=('case reference',),
                        subject_action=('Lift the case','Place the case on the desk','Carry the case toward the door','Hold the case at the door')[i],
                        product_interaction='Hand holds the documented case'))
                data={**template.to_dict(),'brief_digest':brief['brief_digest'],'scenes':[
                    __import__('dataclasses').asdict(s) for s in scenes],'target_duration_seconds':32}
                data.pop('script_digest',None)
            elif schema=='scene-prompt-bundle-v1':
                context=json.loads(kwargs['user_prompt'])
                data={k:context[k] for k in ('script_digest','backend_profile_digest','playbook_digest')}
                data['prompts']=[{'scene_id':s['scene_id'],'start_prompt':s['start_state'],'end_prompt':'',
                    'video_prompt':s['subject_action'],'negative_prompt':'','reference_asset_ids':s['reference_asset_ids']}
                    for s in context['script']['scenes']]
            elif schema=='creative-review-v1':
                context=json.loads(kwargs['user_prompt'])
                data={'verdict':'PASS','findings':[],'coverage':{'method':'actual_pixels',
                    'media_sha256':[r.sha256 for r in kwargs['media']],'scene_ids':context['scene_ids'],
                    'limitations':'Synthetic fixture only'}}
            else: raise AssertionError(schema)
            return StructuredModelResult(data,json.dumps(data),'fake','fake-vision')

    model=FakeModel()
    generator=AgenticCreativeGenerator(model,profile=profile(),category='gadget_demo',visible_details=('visible_controls',))
    root=write_preview(dossier=dossier,generator=generator,output=tmp_path/'preview')
    assert model.calls==4
    manifest=json.loads((root/'preview_manifest.json').read_text(encoding='utf8'))
    assert manifest['preproduction_review']=='PASS' and manifest['render_ready'] is False
    assert write_preview(dossier=dossier,generator=generator,output=tmp_path/'preview')==root
    assert model.calls==4
