from brain.uav_agent_context import UAVAgentContextStore, assess_termination


class FakeTerminationClient:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, _messages, **_kwargs):
        return self.reply


def test_histories_are_isolated_per_uav():
    store = UAVAgentContextStore()
    store.append_history("UAV_1", "user", "search north")
    store.append_history("UAV-2", "user", "hold position")

    assert store.history("UAV_1") == [{"role": "user", "content": "search north"}]
    assert store.history("UAV_2") == [{"role": "user", "content": "hold position"}]


def test_control_authority_belongs_to_each_uav_not_commander():
    store = UAVAgentContextStore()
    store.ensure("COMMANDER")
    store.ensure("UAV_2")

    contexts = store.snapshot()["contexts"]

    assert contexts["COMMANDER"]["can_execute"] is False
    assert contexts["COMMANDER"]["control_owner"] == "none"
    assert contexts["UAV_2"]["can_execute"] is True
    assert contexts["UAV_2"]["control_owner"] == "UAV_2"


def test_uav_termination_vote_is_independent_and_structured():
    vote = assess_termination(
        "UAV_2",
        "Search the east sector",
        [{"id": "coverage", "description": "The east sector is covered."}],
        {"success": True, "summary": "East sector swept"},
        {"position": [10, 0, -5], "battery": 80},
        [],
        FakeTerminationClient(
            '{"ready_to_end":true,"reason":"Local sector complete",'
            '"evidence":["sweep reported"],"unmet_conditions":[]}'
        ),
    )

    assert vote["ready_to_end"] is True
    assert vote["reason"] == "Local sector complete"
    assert vote["evidence"] == ["sweep reported"]


def test_visible_messages_keep_sender_receiver_and_robot():
    store = UAVAgentContextStore()
    event = store.record_message(
        "UAV-3",
        "Operator",
        "UAV_3",
        "take off",
        kind="operator",
        intent="TASK",
    )

    assert event["robot_id"] == "UAV_3"
    assert event["sender"] == "Operator"
    assert event["receiver"] == "UAV_3"
    assert store.messages("UAV_3") == [event]


def test_history_is_bounded_without_cross_agent_eviction():
    store = UAVAgentContextStore(max_history=4)
    for index in range(6):
        store.append_history("UAV_1", "user", str(index))
    store.append_history("UAV_2", "user", "kept")

    assert [item["content"] for item in store.history("UAV_1")] == ["2", "3", "4", "5"]
    assert store.history("UAV_2")[0]["content"] == "kept"


def test_establish_links_creates_full_mesh_for_active_agents():
    store = UAVAgentContextStore()

    links = store.establish_links(["UAV_3", "UAV-1", "UAV_2"], "mission-1")

    assert {(link["source"], link["target"]) for link in links} == {
        ("UAV_1", "UAV_2"),
        ("UAV_1", "UAV_3"),
        ("UAV_2", "UAV_3"),
    }
    assert all(link["status"] == "active" for link in links)
    assert all(link["mission_id"] == "mission-1" for link in links)


def test_new_mission_deactivates_links_to_agents_not_in_the_team():
    store = UAVAgentContextStore()
    store.establish_links(["UAV_1", "UAV_2", "UAV_3"], "mission-1")

    active = store.establish_links(["UAV_1", "UAV_2"], "mission-2")
    snapshot = store.links()

    assert [link["id"] for link in active] == ["UAV_1|UAV_2"]
    assert (
        next(link for link in snapshot if link["id"] == "UAV_1|UAV_3")["status"]
        == "inactive"
    )
