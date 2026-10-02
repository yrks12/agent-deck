"""Detector for the company-grade repository operating contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_company_repository_files_exist():
    required = {
        ".editorconfig",
        ".github/dependabot.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/pull_request_template.md",
        ".github/workflows/ci.yml",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "NOTICE",
        "Makefile",
        "SECURITY.md",
        "requirements-dev.txt",
        "requirements.txt",
    }
    missing = sorted(path for path in required if not (ROOT / path).is_file())
    assert not missing, f"repository operating controls missing: {missing}"


def test_ci_runs_both_products_with_read_only_permissions():
    workflow = read(".github/workflows/ci.yml")

    assert "permissions:\n  contents: read" in workflow
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "backend:" in workflow
    assert "macos:" in workflow
    assert "python -m pytest -q" in workflow
    assert "playwright install --with-deps chromium" in workflow
    assert "swift test --package-path macos" in workflow


def test_pull_requests_require_detector_and_gap_evidence():
    template = read(".github/pull_request_template.md").lower()

    for phrase in ("detector", "red witness", "class sweep", "premise", "untested"):
        assert phrase in template


def test_security_and_ownership_have_actionable_routes():
    security = read("SECURITY.md")

    assert "Security Advisories" in security
    assert "Do not open a public issue" in security


def test_one_command_surface_covers_focused_and_full_suites():
    makefile = read("Makefile")

    for target in ("test-backend:", "test-macos:", "test:", "check:"):
        assert target in makefile


def test_dependabot_scans_the_directory_that_owns_the_dockerfile():
    dependabot = read(".github/dependabot.yml")

    assert "directory: /docker/desk-computer" in dependabot
