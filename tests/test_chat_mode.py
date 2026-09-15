from brain.chat_mode import unified_chat


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.messages = None

    def chat(self, messages, **_kwargs):
        self.messages = messages
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_auto_mode_preserves_executable_plan():
    client = FakeClient(
        'Preparing takeoff.\n```json\n'
        '{"plan": [{"step": 1, "skill": "takeoff", "robot": "UAV_1", "parameters": {}}]}'
        '\n```'
    )

    result = unified_chat("take off", [], client)

    assert result["type"] == "plan"
    assert result["plan"][0]["skill"] == "takeoff"


def test_conversation_only_mode_strips_plan_and_never_executes():
    client = FakeClient(
        'We can discuss takeoff safety.\n```json\n'
        '{"plan": [{"step": 1, "skill": "takeoff", "robot": "UAV_1", "parameters": {}}]}'
        '\n```'
    )

    result = unified_chat("take off", [], client, conversation_only=True)

    assert result["type"] == "chat"
    assert result["plan"] is None
    assert result["text"].startswith("Conversation only. No UAV action was executed.")
    assert "candidate skills: takeoff" in result["text"]
    assert "did not execute it" in result["text"]
    assert "CONVERSATION-ONLY MODE" in client.messages[0]["content"]


def test_conversation_only_mode_does_not_use_action_fallback_on_llm_error():
    client = FakeClient(RuntimeError("offline"))

    result = unified_chat("take off", [], client, conversation_only=True)

    assert result["type"] == "chat"
    assert result["plan"] is None


def test_uav_agent_identity_and_plan_are_bound_to_selected_robot():
    client = FakeClient(
        'Moving now.\n```json\n'
        '{"plan": [{"step": 1, "skill": "fly_to", "robot": "UAV_1", "parameters": {}}]}'
        '\n```'
    )

    result = unified_chat("fly north", [], client, robot_id="UAV-3")

    assert result["type"] == "plan"
    assert result["plan"][0]["robot"] == "UAV_3"
    assert "编号 UAV_3" in client.messages[0]["content"]
    assert "不能控制其他无人机" in client.messages[0]["content"]