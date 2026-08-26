import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_ENTRY = re.compile(r"^([A-Za-z0-9_.-]+)==([^ \\]+)", re.MULTILINE)


def _locked_versions(filename: str) -> dict[str, str]:
    versions = {}
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        match = LOCK_ENTRY.match(line)
        if match:
            versions[match.group(1).lower()] = match.group(2)
    return versions


def test_dependency_inputs_are_represented_in_exact_locks():
    production = _locked_versions("requirements.lock")
    development = _locked_versions("requirements-dev.lock")

    for line in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines():
        match = LOCK_ENTRY.match(line)
        if match:
            name, version = match.groups()
            assert production[name.lower()] == version
            assert development[name.lower()] == version

    for package in ("pytest", "ruff", "pip-tools"):
        assert package in development
        assert package not in production


def test_lock_entries_include_hashes():
    for filename in ("requirements.lock", "requirements-dev.lock"):
        contents = (ROOT / filename).read_text(encoding="utf-8")
        entries = list(LOCK_ENTRY.finditer(contents))
        assert entries
        assert contents.count("--hash=sha256:") >= len(entries)


def test_workflow_uses_hashed_lock_and_checks_drift():
    workflow = (ROOT / ".github/workflows/instagram_bot.yml").read_text(
        encoding="utf-8"
    )

    assert "pip install --require-hashes -r requirements-dev.lock" in workflow
    assert "./scripts/compile_requirements.sh" in workflow
    assert "git diff --exit-code -- requirements.lock requirements-dev.lock" in workflow
    assert "requirements.txt" not in workflow
    assert "pip install pytest" not in workflow
