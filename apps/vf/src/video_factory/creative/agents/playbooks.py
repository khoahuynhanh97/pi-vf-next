"""Local recipes only: never download or execute an upstream skill at runtime."""
from pathlib import Path
import yaml

from ..contracts import canonical_digest
from .contracts import BackendProfile

ROOT = Path(__file__).parent.parent / 'playbooks'


def select_playbook(category: str, profile: BackendProfile, visible_details: tuple[str, ...]) -> dict[str, object]:
    if category not in {'food_pov', 'gadget_demo', 'home_appliance'}:
        raise ValueError('unsupported creative category')
    recipe = yaml.safe_load((ROOT / f'{category}.yaml').read_text(encoding='utf-8'))
    missing = set(recipe['prerequisites']) - set(visible_details)
    if missing:
        raise ValueError(f'playbook prerequisites missing: {sorted(missing)}')
    if 'reference' not in profile.image_modes:
        raise ValueError('playbook requires reference-image capability')
    recipe['sources'] = yaml.safe_load((ROOT / 'sources.yaml').read_text(encoding='utf-8'))
    recipe['digest'] = canonical_digest(recipe)
    return recipe
