"""Admission and resume decisions; never sends or changes product/QC policy."""
import re


def is_ui_mismatch(code: str) -> bool:
    return bool(re.fullmatch(r'GOOGLE_FLOW_[A-Z0-9_]*(?:SELECTOR_NOT_UNIQUE|PANEL_UNVERIFIED|MODE_UNVERIFIED|UI_UNVERIFIED|916_UNVERIFIED|REFERENCE_UPLOAD_UNVERIFIED)', str(code)))


def resume_decision(operation_phase, contract_verified, code_verified, unrelated_pause):
    if unrelated_pause:
        return {'action': 'stop', 'reason': 'unrelated_pipeline_pause'}
    if operation_phase in {'submitted', 'running', 'completed'}:
        return {'action': 'reuse', 'reason': 'existing_provider_operation'}
    if operation_phase not in {'not_sent', 'confirmed_not_accepted'}:
        return {'action': 'stop', 'reason': 'submission_reconciliation_required'}
    if not contract_verified or not code_verified:
        return {'action': 'stop', 'reason': 'verified_contract_and_code_required'}
    return {'action': 'resume', 'reason': 'same_operation_eligible'}
