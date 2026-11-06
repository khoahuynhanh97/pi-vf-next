"""Output contracts supplied to both structured transports and role prompts."""
from copy import deepcopy
import json

from ..prompts import SCRIPT_SCENE_PLAN_V2_SCHEMA


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


_STRING = {'type': 'string'}
_STRINGS = {'type': 'array', 'items': _STRING}

SCENE_PROMPT_SCHEMA = _object({
    'scene_id': _STRING,
    'start_prompt': _STRING,
    'end_prompt': _STRING,
    'video_prompt': _STRING,
    'negative_prompt': _STRING,
    'reference_asset_ids': _STRINGS,
})

PROMPT_REPAIR_SCHEMA = _object({
    'prompts': {'type': 'array', 'minItems': 1, 'items': SCENE_PROMPT_SCHEMA},
})

SCENE_PROMPT_BUNDLE_SCHEMA = _object({
    'schema_version': {'type': 'string', 'enum': ['scene-prompt-bundle-v1']},
    'script_digest': _STRING,
    'backend_profile_digest': _STRING,
    'playbook_digest': _STRING,
    'prompts': PROMPT_REPAIR_SCHEMA['properties']['prompts'],
})

CREATIVE_REVIEW_SCHEMA = _object({
    'verdict': {'type': 'string', 'enum': ['PASS', 'FAIL', 'UNKNOWN']},
    'findings': {'type': 'array', 'items': _object({
        'finding_id': _STRING,
        'scene_id': _STRING,
        'code': _STRING,
        'blocking': {'type': 'boolean'},
        'expected': _STRING,
        'observed': _STRING,
        'reference_ids': _STRINGS,
        'evidence_locations': {'type': 'array', 'items': _STRING, 'minItems': 1},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
    })},
    'coverage': _object({
        'method': {'type': 'string', 'enum': ['actual_pixels']},
        'media_sha256': _STRINGS,
        'scene_ids': _STRINGS,
        'timestamps': {'type': 'array', 'items': {'type': 'number'}},
        'limitations': _STRING,
    }),
})

REPAIR_PROPOSAL_SCHEMA = _object({
    'base_digest': _STRING,
    'finding_ids': {'type': 'array', 'items': _STRING, 'minItems': 1},
    'scope': {'type': 'string', 'enum': ['prompt', 'keyframe', 'scene', 'storyboard', 'assembly', 'input']},
    'scene_ids': _STRINGS,
    'diagnosis': _STRING,
    'changes': {'type': 'object', 'additionalProperties': True},
    'protected_invariants': {'type': 'object', 'additionalProperties': True},
    'recheck_stages': {'type': 'array', 'minItems': 1,
                       'items': {'type': 'string', 'enum': ['preproduction', 'keyframe', 'storyboard', 'clip', 'final']}},
})

# The four-agent parser requires an explicit omission decision even when END is used.
# Preserve the legacy schema contract for its other callers.
DIRECTOR_SCRIPT_SCHEMA = deepcopy(SCRIPT_SCENE_PLAN_V2_SCHEMA)
DIRECTOR_SCRIPT_SCHEMA['properties']['scenes']['items']['required'].append('end_keyframe_omission_reason')


def with_output_schema(prompt, schema):
    """Gateways may ignore response_format; expose the same exact contract as text."""
    return prompt + '\nOutput JSON schema:\n' + json.dumps(schema, ensure_ascii=False, sort_keys=True)
