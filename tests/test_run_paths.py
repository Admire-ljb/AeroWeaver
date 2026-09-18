from pathlib import Path
import re
from urllib.parse import urlsplit


def test_repository_uses_role_based_top_level_layout():
    for directory in [
        "backend",
        "frontend",
        "deploy",
        "docs",
        "requirements",
        "scripts",
        "tests",
    ]:
        assert Path(directory).is_dir()

    for legacy_path in [
        "server.py",
        "ui",
        "requirements.txt",
        "requirements-mock.txt",
        "compose.yml",
        "compose.build.yml",
        "compose.gazebo.yml",
        "Dockerfile",
        "Dockerfile.gazebo",
    ]:
        assert not Path(legacy_path).exists()


def test_readme_documents_all_runnable_user_paths():
    for path in [Path("README.md"), Path("docs/README_CN.md")]:
        readme = path.read_text(encoding="utf-8")
        for expected in [
            "python -m venv .venv",
            "pip install -r requirements/mock.txt",
            "npm --prefix frontend ci",
            "npm --prefix frontend run build",
            "python backend/server.py",
            "http://127.0.0.1:5001",
        ]:
            assert expected in readme
        assert re.search(r"\]\([^)]*USAGE(?:_CN)?\.md\)", readme)

    # Detailed commands live in the linked guides, not on the project landing page.
    guides = list(Path("docs").glob("USAGE*.md"))
    if guides:
        assert {path.name for path in guides} >= {"USAGE.md", "USAGE_CN.md"}
        for path in guides:
            guide = path.read_text(encoding="utf-8")
            for expected in [
                "SIM_ADAPTER=mock AEROWEAVER_UAV_COUNT=3 python backend/server.py",
                "python -m pytest",
                "npm ci",
                "npm run build",
                "AIRSIM_HOST=127.0.0.1",
            ]:
                assert expected in guide
    else:
        # The legacy zh branch directs readers to the maintained main-branch guide.
        for path in [Path("README.md"), Path("docs/README_CN.md")]:
            assert (
                "https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/USAGE_CN.md"
                in path.read_text(encoding="utf-8")
            )


def test_readme_language_routes_and_web_console_assets_exist():
    readme = Path("README.md").read_text(encoding="utf-8")
    chinese = Path("docs/README_CN.md").read_text(encoding="utf-8")

    english_link = "[English](https://github.com/Admire-ljb/AeroWeaver/blob/main/README.md)"
    if "[中文]" in readme:
        assert "[中文](docs/README_CN.md)" in readme
    else:
        assert english_link in readme

    assert "[English](../README.md)" in chinese or english_link in chinese
    for text in [readme, chinese]:
        assert re.search(r"!\[[^\]]+\]\([^)]+\)", text)

    documents = [Path("README.md"), Path("docs/README_CN.md"), *Path("docs").glob("USAGE*.md")]
    for path in documents:
        text = path.read_text(encoding="utf-8")
        for url in re.findall(r"\]\(([^\s)]+)\)", text):
            target = urlsplit(url)
            if target.scheme or target.netloc or not target.path:
                continue
            assert (path.parent / target.path).exists(), f"{path}: missing {url}"


def test_compose_user_path_exists_and_uses_mock_adapter():
    compose = Path("deploy/compose.mock.yml")
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
    for path in ["README.md", "docs/README_CN.md"]:
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

    gazebo_compose = Path("deploy/compose.gazebo.yml").read_text(encoding="utf-8")
    for expected in [
        "SIM_ADAPTER: gazebo_direct",
        "PX4_GZ_WORLD: urban_rescue",
        "image: aeroweaver:gazebo",
        "build:",
        "5001:5001",
    ]:
        assert expected in gazebo_compose

    mock_dockerfile = Path("deploy/docker/Dockerfile.mock").read_text(encoding="utf-8")
    assert 'CMD ["python", "backend/server.py"]' in mock_dockerfile
    assert "/app/frontend/dist" in mock_dockerfile
