from pathlib import Path


def test_readme_documents_all_runnable_user_paths():
    readme = Path("README.md").read_text(encoding="utf-8")
    for expected in [
        "SIM_ADAPTER=mock AEROWEAVER_UAV_COUNT=3 python server.py",
        "python -m pytest",
        "npm ci",
        "npm run build",
        "AIRSIM_HOST=127.0.0.1",
    ]:
        assert expected in readme
    assert (
        ("Manual Mode" in readme and "LLM Mode" in readme)
        or ("手动模式" in readme and "LLM 模式" in readme)
    )


def test_readme_language_routes_and_web_console_assets_exist():
    readme = Path("README.md").read_text(encoding="utf-8")
    chinese = Path("README_CN.md").read_text(encoding="utf-8")

    if "Manual Mode" in readme:
        assert "github.com/Admire-ljb/AeroWeaver/tree/zh" in readme
    else:
        assert "github.com/Admire-ljb/AeroWeaver/tree/main" in readme

    assert "github.com/Admire-ljb/AeroWeaver/tree/main" in chinese
    assert Path("docs/images/web-console.jpg").is_file()
    assert Path("docs/images/airsim-multi-uav.webp").is_file()
    assert "docs/images/web-console.jpg" in readme
    assert "docs/images/airsim-multi-uav.webp" in readme


def test_compose_user_path_exists_and_uses_mock_adapter():
    compose = Path("compose.yml")
    assert compose.exists()
    text = compose.read_text(encoding="utf-8")
    for expected in [
        "SIM_ADAPTER: mock",
        "AEROWEAVER_PORT: 5001",
        "5001:5001",
        "build:",
        "image: aeroweaver:mock",
        "/api/status",
    ]:
        assert expected in text


def test_readme_project_tree_does_not_claim_runtime_profile_files_are_shipped():
    for path in ["README.md", "README_CN.md"]:
        text = Path(path).read_text(encoding="utf-8")
        assert "MEMORY.md / SKILLS.md" not in text
        assert "robot_profile/MEMORY.md" not in text
        assert "robot_profile/SKILLS.md" not in text


def test_ci_and_compose_files_match_the_published_runtime():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    for expected in [
        "python -m pytest",
        "npm ci",
        "npm run lint",
        "npm run build",
    ]:
        assert expected in workflow

    gazebo_compose = Path("compose.gazebo.yml").read_text(encoding="utf-8")
    for expected in [
        "SIM_ADAPTER: gazebo_direct",
        "PX4_GZ_WORLD: urban_rescue",
        "image: aeroweaver:gazebo",
        "build:",
        "5001:5001",
    ]:
        assert expected in gazebo_compose

    build_compose = Path("compose.build.yml").read_text(encoding="utf-8")
    assert "build:" in build_compose
    assert "image: aeroweaver:demo" in build_compose
