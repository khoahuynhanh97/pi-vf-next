"""Explicit Muse image operation; shares the same bridge/session Send protection."""
from .ports import KeyframeGenerationResult


class MuseKeyframeAdapter:
    provider_kind='muse'

    def __init__(self,bridge_adapter):
        self.bridge_adapter=bridge_adapter

    def submit(self,request):
        data=self.bridge_adapter._submit_operation(operation_id=request.operation_id,scene_id=request.frame_id,
            prompt=request.prompt,refs=request.reference_image_paths,duration=0,negative=request.negative_prompt,
            kind='image',truth=request.provider_options.get('product_truth_digest',''))
        return self._result(request.operation_id,data)

    def _result(self,operation_id,data):
        return KeyframeGenerationResult(operation_id=operation_id,status=data['status'],provider_operation_id=operation_id,
            image_path=data.get('output_path'),error_code='RECONCILIATION_REQUIRED' if data['status']=='unknown' else '',
            metadata={**data,'provider_kind':'muse','model':'not_exposed'})

    def get_status(self,operation_id):
        return self._result(operation_id,self.bridge_adapter._receipt(operation_id))
