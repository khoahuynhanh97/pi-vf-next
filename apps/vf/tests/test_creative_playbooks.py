import pytest

from test_agent_contracts import profile
from video_factory.creative.agents.playbooks import select_playbook


@pytest.mark.parametrize('category,detail,action', [
    ('food_pov', 'food_shape', 'pick'),
    ('gadget_demo', 'visible_controls', 'place'),
    ('home_appliance', 'visible_controls', 'operate'),
])
def test_recipes_have_distinct_actions_and_versioned_provenance(category, detail, action):
    recipe = select_playbook(category, profile(), (detail,))
    assert action in ' '.join(recipe['action_patterns'])
    assert recipe['digest'] and recipe['sources']
    assert recipe['qc_criteria'] and recipe['repair_strategies']


def test_missing_assets_rejects_recipe():
    with pytest.raises(ValueError, match='prerequisites'):
        select_playbook('food_pov', profile(), ())
