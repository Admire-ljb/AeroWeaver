from sim.mpe_catalog import MPE2_SCENARIOS, list_mpe2_environments, role_mapping_for_scenario


def test_official_mpe2_catalog_is_complete():
    assert len(MPE2_SCENARIOS) == 12
    rows = list_mpe2_environments()
    assert len(rows) == 12
    assert {row["id"] for row in rows} == {
        "simple",
        "simple_adversary",
        "simple_crypto",
        "simple_formation",
        "simple_line",
        "simple_push",
        "simple_reference",
        "simple_speaker_listener",
        "simple_spread",
        "simple_tag",
        "simple_world_comm",
        "collect_treasure",
    }
