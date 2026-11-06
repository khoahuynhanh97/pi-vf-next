"""Pure gate policy: transitions remain owned by the current VF workflow engine."""
from video_factory.creative.contracts import canonical_digest


QC_LEVELS = ("low", "middle", "high")
# High requires content reviews of rendered clips and the current final video.


def preproduction_policy(config):
    mode = config.get("preproduction_gate", "observe")
    if mode == "enforce":
        raise ValueError("preproduction_gate=enforce is not enabled in this release")
    if mode != "observe":
        raise ValueError("preproduction_gate must be observe or enforce")
    threshold = config.get("preproduction_threshold", 80)
    if type(threshold) is not int or not 0 <= threshold <= 100:
        raise ValueError("preproduction_threshold must be an integer from 0 to 100")
    return {"preproduction_gate": mode, "preproduction_threshold": threshold}


def normalize_qc_level(value=None):
    level = str(value or "middle").strip().lower()
    if level not in QC_LEVELS:
        raise ValueError(f"qc_level must be one of: {', '.join(QC_LEVELS)}")
    return level


def is_low_qc(policy):
    return normalize_qc_level((policy or {}).get("qc_level")) == "low"


def evaluate_auto_gate(*, state, artifact, reviews, policy):
    qc_level = normalize_qc_level(policy.get('qc_level'))
    if policy.get('creative_mode') != 'four_agent' or policy.get('auto_export') is not True:
        return {'action':'stop', 'reason':'explicit four_agent auto policy required'}
    current = artifact.get('review_input_digests') or {}
    stages = {
        'AwaitingBriefApproval': (), 'AwaitingSceneApproval': (),
        'AwaitingStoryboardApproval': (),
        'AwaitingFinalApproval': ('final',) if qc_level == 'high' or (
            qc_level != 'low' and current.get('final')) else (),
    }
    if state not in stages:
        return {'action':'stop', 'reason':'unsupported gate'}
    if state == 'AwaitingStoryboardApproval' and qc_level != 'low':
        receipt = artifact.get('keyframe_qc_receipt') or {}
        findings = receipt.get('findings') if isinstance(receipt.get('findings'), dict) else {}
        items = findings.get('items') if isinstance(findings.get('items'), list) else []
        if (receipt.get('stage') != 'keyframe' or receipt.get('verdict') != 'pass'
                or not artifact.get('package_digest')
                or receipt.get('artifact_digest') != artifact.get('package_digest')
                or not items or any(item.get('verdict') != 'pass' or item.get('hard_failure') for item in items)):
            return {'action':'stop', 'reason':'current keyframe QC PASS receipt required'}
    if state == 'AwaitingFinalApproval' and qc_level == 'high':
        receipt = artifact.get('final_review') or {}
        context = artifact.get('review_context') or {}
        if (not current.get('final') or not artifact.get('sha256')
                or context.get('video_sha256') != artifact['sha256']
                or receipt.get('stage') != 'final' or receipt.get('input_digest') != current['final']):
            return {'action':'stop', 'reason':'current SHA-bound final content review required'}
    for stage in stages[state]:
        matches = [r for r in reviews if r.stage == stage and r.input_digest == current.get(stage)]
        if not matches:
            return {'action':'stop', 'reason':f'current {stage} review missing'}
        review = matches[-1]
        if qc_level == 'high' and stage == 'final' and artifact['final_review'].get('verdict') != review.verdict:
            return {'action':'stop', 'reason':'current final content review does not match receipt'}
        if review.verdict != 'PASS':
            return {'action':'repair' if review.verdict == 'FAIL' else 'stop', 'reason':stage+':'+review.verdict}
    if state == 'AwaitingFinalApproval':
        sha = artifact.get('sha256')
        technical = artifact.get('technical_qc') or {}
        if not sha or technical.get('sha256') != sha or technical.get('verdict') != 'PASS':
            return {'action':'stop','reason':'current final technical PASS required'}
    return {'action':'advance', 'reason':'all current required reviews passed',
        'actor':'creative_auto_policy', 'artifact_digest':canonical_digest(artifact), 'human_review':'not_performed'}
