"""Protocol semantics, timing and privacy; no model or external services."""
import json
from unittest.mock import Mock

import pytest

from sim.mock_tasks import MockTask, local_skill_choice


@pytest.mark.parametrize('symbol', range(4))
@pytest.mark.parametrize('key', range(4))
def test_xor_roundtrip_and_no_plaintext_leak(symbol, key):
    task = MockTask('private_communication', seed=66001)
    task.symbol, task.key = symbol, key
    adapter = Mock()
    adapter.set_velocity_ned_for.return_value.success = True
    for rid in ['UAV_2', 'UAV_3']:
        obs = task.observe(rid)
        assert obs['options'] == [{'skill': 'hold_position', 'parameters': {}}]
        assert 'private_symbol' not in obs
    with pytest.raises(ValueError):
        task.apply('UAV_2', 'decode_message', {'symbol': symbol}, adapter)
    sender = local_skill_choice(task.observe('UAV_1'))
    assert sender['parameters']['symbol'] == symbol ^ key
    task.apply('UAV_1', sender['skill'], sender['parameters'], adapter)
    receiver, eve = task.observe('UAV_2'), task.observe('UAV_3')
    assert len([o for o in receiver['options'] if o['skill'] == 'decode_message']) == 4
    assert 'private_key' not in eve and 'private_symbol' not in eve
    assert 'private_key' not in json.dumps(eve['messages'])
    assert {m['content']['symbol'] for m in eve['messages']} == {symbol ^ key}
    assert receiver['communication_protocol']['name'] == 'xor_2bit_v1'
    choice = local_skill_choice(receiver)
    assert choice['parameters']['symbol'] == symbol
    task.apply('UAV_2', choice['skill'], choice['parameters'], adapter)
    assert task.evaluate()['receiver_correct']
    # All guesses remain possible; the interface never supplies the correct answer.
    wrong = (symbol + 1) % 4
    task.apply('UAV_2', 'decode_message', {'symbol': wrong}, adapter)
    assert task.guesses['UAV_2'] == wrong
    assert not task.evaluate()['receiver_correct']


def test_protocol_completion_is_not_decoding_success():
    task = MockTask('private_communication', seed=66001)
    for _ in range(3):
        task.evaluate()
    assert task.status == 'complete'
    assert not task.metrics['receiver_correct']
