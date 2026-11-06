"""Small immutable envelopes shared by the four creative roles."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..contracts import canonical_digest


class Envelope:
    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), 'digest':self.digest}

    @property
    def digest(self) -> str:
        return canonical_digest(asdict(self))


@dataclass(frozen=True)
class BackendProfile(Envelope):
    provider_kind: str
    image_provider_kind: str
    version: str
    duration_options: tuple[int, ...]
    image_modes: tuple[str, ...]
    max_reference_images: int
    supports_end_frame: bool
    supports_negative_prompt: bool
    audio_policy: str

    def __post_init__(self):
        if not all((self.provider_kind, self.image_provider_kind, self.version, self.image_modes)):
            raise ValueError('profile requires explicit provider, version and image modes')
        if not self.duration_options or any(type(n) is not int or n <= 0 for n in self.duration_options):
            raise ValueError('profile requires verified duration options')
        if self.max_reference_images < 1 or self.audio_policy not in {'silent', 'native'}:
            raise ValueError('invalid reference or audio policy')

    @classmethod
    def from_dict(cls, data):
        result=cls(**{**{k:v for k,v in data.items() if k!='digest'}, 'duration_options': tuple(data['duration_options']),
                      'image_modes': tuple(data['image_modes'])})
        if data.get('digest',result.digest)!=result.digest:
            raise ValueError('profile digest mismatch')
        return result


@dataclass(frozen=True)
class ScenePrompt(Envelope):
    scene_id: str
    start_prompt: str
    end_prompt: str
    video_prompt: str
    negative_prompt: str
    reference_asset_ids: tuple[str, ...]

    def __post_init__(self):
        if not all((self.scene_id, self.start_prompt.strip(), self.video_prompt.strip(), self.reference_asset_ids)):
            raise ValueError('scene prompt requires scene, START, action and references')
        if len(set(self.reference_asset_ids)) != len(self.reference_asset_ids):
            raise ValueError('duplicate references')


@dataclass(frozen=True)
class ScenePromptBundle(Envelope):
    script_digest: str
    backend_profile_digest: str
    playbook_digest: str
    prompts: tuple[ScenePrompt, ...]
    schema_version: str = 'scene-prompt-bundle-v1'
    evaluator_receipt: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not all((self.script_digest, self.backend_profile_digest, self.playbook_digest, self.prompts)):
            raise ValueError('prompt bundle requires dependency bindings')
        if len({p.scene_id for p in self.prompts}) != len(self.prompts):
            raise ValueError('duplicate scene IDs')

    @classmethod
    def from_dict(cls, data):
        payload = {k: v for k, v in data.items() if k != 'digest'}
        payload['prompts'] = tuple(ScenePrompt(**{**p, 'reference_asset_ids': tuple(p['reference_asset_ids'])})
                                   for p in data['prompts'])
        result = cls(**payload)
        if data.get('digest', result.digest) != result.digest:
            raise ValueError('prompt bundle digest mismatch')
        return result

    def for_scene(self, scene_id: str) -> ScenePrompt:
        return next(p for p in self.prompts if p.scene_id == scene_id)


@dataclass(frozen=True)
class ReviewFinding(Envelope):
    finding_id: str
    scene_id: str
    code: str
    blocking: bool
    expected: str
    observed: str
    reference_ids: tuple[str, ...]
    evidence_locations: tuple[str, ...]
    confidence: float

    def __post_init__(self):
        if not all((self.finding_id, self.code, self.expected, self.observed, self.evidence_locations)):
            raise ValueError('finding needs diagnosis and evidence locations')
        if not 0 <= self.confidence <= 1 or type(self.blocking) is not bool:
            raise ValueError('invalid finding confidence or blocking flag')


@dataclass(frozen=True)
class CreativeReview(Envelope):
    stage: str
    verdict: str
    input_digest: str
    findings: tuple[ReviewFinding, ...]
    coverage: dict[str, Any]
    evaluator_receipt: dict[str, Any]

    def __post_init__(self):
        if self.stage not in {'preproduction', 'keyframe', 'storyboard', 'clip', 'final'}:
            raise ValueError('invalid review stage')
        if self.verdict not in {'PASS', 'FAIL', 'UNKNOWN'} or not self.input_digest:
            raise ValueError('invalid verdict or missing input binding')
        if len({f.finding_id for f in self.findings}) != len(self.findings):
            raise ValueError('duplicate finding IDs')
        if self.verdict == 'PASS' and (any(f.blocking for f in self.findings) or
                                      not self.coverage or not self.evaluator_receipt):
            raise ValueError('PASS requires coverage, evaluator and no blocking findings')

    def validate_scenes(self, scene_ids):
        if any(f.scene_id and f.scene_id not in scene_ids for f in self.findings):
            raise ValueError('finding references unknown scene')

    @classmethod
    def from_dict(cls, data):
        payload = {k: v for k, v in data.items() if k != 'digest'}
        findings = []
        for i, f in enumerate(data.get('findings') or ()):
            if isinstance(f, ReviewFinding):
                findings.append(f)
                continue
            f_dict = dict(f)
            fid = f_dict.get('finding_id') or f_dict.get('id') or f"finding-{f_dict.get('scene_id') or 'scene'}-{f_dict.get('code') or 'item'}-{i+1}"
            norm_finding = {
                'finding_id': str(fid),
                'scene_id': str(f_dict.get('scene_id') or ''),
                'code': str(f_dict.get('code') or 'unspecified'),
                'blocking': bool(f_dict.get('blocking', False)),
                'expected': str(f_dict.get('expected') or 'expected as specified'),
                'observed': str(f_dict.get('observed') or 'observed discrepancy'),
                'reference_ids': tuple(str(x) for x in (f_dict.get('reference_ids') or ())),
                'evidence_locations': tuple(str(x) for x in (f_dict.get('evidence_locations') or ('timeline',))),
                'confidence': float(f_dict.get('confidence') if f_dict.get('confidence') is not None else 1.0),
            }
            findings.append(ReviewFinding(**norm_finding))
        payload['findings'] = tuple(findings)
        result=cls(**payload)
        if data.get('digest',result.digest)!=result.digest:
            raise ValueError('review digest mismatch')
        return result


@dataclass(frozen=True)
class RepairProposal(Envelope):
    base_digest: str
    finding_ids: tuple[str, ...]
    scope: str
    scene_ids: tuple[str, ...]
    diagnosis: str
    changes: dict[str, Any]
    protected_invariants: dict[str, Any]
    recheck_stages: tuple[str, ...]
    evaluator_receipt: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.scope not in {'prompt', 'keyframe', 'scene', 'storyboard', 'assembly', 'input'}:
            raise ValueError('unsupported repair scope')
        if not all((self.base_digest, self.finding_ids, self.diagnosis, self.changes, self.protected_invariants,
                    self.recheck_stages)):
            raise ValueError('repair requires findings, delta, protected invariants and rechecks')
