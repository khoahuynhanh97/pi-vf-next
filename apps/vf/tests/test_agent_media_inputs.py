import base64
import hashlib
from dataclasses import replace

import pytest
from PIL import Image

from video_factory.creative.ports import MediaReference
from video_factory.creative.model_adapter import NineRouterHermesStructuredCreativeModel
from video_factory.creative.contracts import ProductIdentityV2, ResearchDossier, EligibleAsset


def test_actual_image_bytes_and_roles_reach_transport(tmp_path):
    path = tmp_path/'ref.png'
    Image.new('RGB', (12, 16), 'red').save(path)
    data = path.read_bytes()
    ref = MediaReference(str(path), hashlib.sha256(data).hexdigest(), 'approved_source:a')

    class Transport:
        def complete(self, **kwargs):
            self.payload = kwargs
            return {'choices': [{'message': {'content': '{}'}}]}, {'upstream_model': 'fake-vision'}

    transport = Transport()
    model = NineRouterHermesStructuredCreativeModel(transport)
    result = model.generate_media_json(system_prompt='review', user_prompt='look', schema_name='test',
                                       json_schema={}, temperature=0, media=(ref,))
    content = transport.payload['messages'][1]['content']
    assert content[1]['text'].startswith('Media role: approved_source:a;')
    assert base64.b64decode(content[2]['image_url']['url'].split(',')[1]) == data
    assert result.model == 'fake-vision'
    with pytest.raises(ValueError, match='SHA mismatch'):
        replace(ref, sha256='0'*64).content()


def test_food_identity_has_no_fabricated_brand_or_model():
    identity = ProductIdentityV2('', '', 'Source snack', identity_mode='source_product',
        canonical_product_id='upstream:snack-1', source_snapshot_digest='sha256:'+'f'*64)
    dossier = ResearchDossier.create(workflow_id='test', product_identity=identity, verified_claims=(),
        eligible_assets=(EligibleAsset('a','a.png','a'*64,'qc'), EligibleAsset('b','b.png','b'*64,'qc')))
    assert dossier.product_identity.brand == dossier.product_identity.model == ''
    assert ResearchDossier.from_dict(dossier.to_dict()).digest() == dossier.digest()
    with pytest.raises(ValueError):
        replace(identity, canonical_product_id='')
    with pytest.raises(ValueError):
        replace(identity, identity_mode='sku')
    with pytest.raises(ValueError, match='distinct'):
        replace(dossier, eligible_assets=(dossier.eligible_assets[0], replace(dossier.eligible_assets[1], sha256='a'*64)))


def test_source_food_mode_survives_real_dossier_seam(tmp_path,monkeypatch):
    from video_factory.handlers import common
    from video_factory.qc.models import QCReport,QCStage,QCVerdict
    from video_factory.creative.generator import SingleModelCreativeGenerator
    assets=[]
    for i in range(2):
        path=tmp_path/f'food-fixture-{i}.png'
        Image.new('RGB',(16,16),('red','blue')[i]).save(path)
        assets.append({'asset_id':f'a{i}','uri':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                       'eligible':True,'visible_details':['synthetic spiral shape']})
    report=QCReport(report_id='fixture-qc',workflow_id='food',stage=QCStage.IMAGE,artifact_id='rp',
        artifact_version=1,artifact_digest='sha256:pack',reference_digests=[],evaluator_name='fake',
        evaluator_version='1',rubric_version='1',created_at='2026-10-23T00:00:00Z',score=90,
        verdict=QCVerdict.PASS,findings={'eligible_reference_manifest':assets})
    class Repository:
        def get_latest_report(self,*args): return report
    monkeypatch.setattr(common,'QCReportRepository',lambda connection:Repository())
    raw={'canonical_products':[{'canonical_product_id':'upstream:food'}],'snapshot_id':'snapshot-food'}
    result=common._locked_research_dossier(workflow_id='food',connection=object(),locked_pack={},aggregate={
        'resource_pack':{'canonical_product_id':'upstream:food','product_name':'Synthetic food',
            'product_identity':{'identity_mode':'source_product','display_name':'Synthetic food'}},
        'research_result':{'raw_result':raw}})
    dossier=ResearchDossier.from_dict(result)
    assert dossier.product_identity.brand==dossier.product_identity.model==''
    assert dossier.product_identity.canonical_product_id=='upstream:food'
    assert SingleModelCreativeGenerator(object())._has_admissible_claims(dossier)
