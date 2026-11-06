from unittest.mock import MagicMock
from PIL import Image

from video_factory.mcp_adapters import MCPProductIntelligenceAdapter
from video_factory.qc.service import ProductVideoQCService
from video_factory.storage_infra.sqlite import connect


def test_pi_pack_adapter_preserves_source_identity_without_inventing_model():
    identity={'name':'Jisulife Fan 7 5000mAh','brand':'Jisulife','model':None}
    raw={'resource_pack_id':'pack','pack_status':'ready_to_lock','media_count':2,
         'product_identity':identity,'assets':[{'asset_id':'one'},{'asset_id':'two'}]}
    adapter=MCPProductIntelligenceAdapter({'build':lambda payload:raw}, {'build_resource_pack':'build'})
    pack=adapter.build_resource_pack({'snapshot_id':'snapshot','persistence_status':'saved'})
    assert pack['product_identity']==identity
    assert pack['product_identity']['model'] is None


def test_source_qc_reads_identity_from_historical_pi_envelope(tmp_path,monkeypatch):
    image=tmp_path/'source.png';Image.new('RGB',(10,10),'blue').save(image)
    identity={'name':'Jisulife Fan 7 5000mAh','brand':'Jisulife','model':None}
    pack={'resource_pack_id':'pack','version':1,'assets':[{'asset_id':'source','local_path':str(image)}],
          'raw_result':{'product_identity':identity}}
    observed=[]
    def select(**kwargs):
        observed.append(kwargs['product'])
        return [],[],{'status':'failed','code':'IMAGE_QC_FAILED'}
    monkeypatch.setattr('video_factory.qc.selection.select_distinct_pass_images',select)
    conn=connect(tmp_path/'worker.db')
    evaluator=MagicMock(evaluator_name='offline',evaluator_version='1',model='offline')
    ProductVideoQCService(conn,evaluator=evaluator).select_image_references('workflow',resource_pack=pack,provider_mode='fake')
    assert len(observed)==1
    assert observed[0].brand=='Jisulife'
    assert observed[0].display_name==identity['name']
    assert observed[0].model=='Unknown'
    conn.close()
