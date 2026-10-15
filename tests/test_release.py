"""The release checklist, as tests.

Releasing is merging a pull request that carries docs/releases/v<version>.md for the version in
src/safety_explorer/__init__.py; .github/workflows/release.yml then builds the app, creates the
tag and the GitHub Release. These tests keep the pieces that workflow reads in agreement.
"""

import re
import subprocess
from pathlib import Path

import pytest

from safety_explorer import __version__

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "src" / "safety_explorer" / "__init__.py"
NOTES = ROOT / "docs" / "releases" / f"v{__version__}.md"


def test_the_release_workflow_reads_the_version_the_package_carries():
    # release.yml extracts the version with this sed line; a reformatted __init__.py
    # (single quotes, a type annotation) would silently stop every release.
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "sed -n 's/^__version__ = \"\\(.*\\)\"/\\1/p' src/safety_explorer/__init__.py" in workflow
    try:
        out = subprocess.run(
            ["sed", "-n", r's/^__version__ = "\(.*\)"/\1/p', str(INIT)],
            capture_output=True, text=True, check=True).stdout.strip()
    except FileNotFoundError:
        pytest.skip("no sed on this platform")
    assert out == __version__


def test_the_app_workflow_accepts_the_tag_the_release_workflow_passes():
    app = (ROOT / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
    assert "workflow_call:" in app and re.search(r"inputs:\s+tag:", app)
    assert "TAG: ${{ inputs.tag || github.ref_name }}" in app


@pytest.mark.skipif(not NOTES.exists(), reason="this version has no release notes yet")
def test_a_version_with_release_notes_is_cited_as_that_version():
    cff = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    assert re.search(r"^version:\s*\"?([^\s\"]+)", cff, re.M).group(1) == __version__
    assert "Elliot Telford" in NOTES.read_text(encoding="utf-8")


def test_every_download_link_in_the_docs_is_a_file_the_release_carries():
    # releases/latest/download/<file> always serves the newest release's <file>, so a link to
    # a file the workflow stopped uploading would 404 on the README and in every paper.
    app = (ROOT / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
    upload = app[app.index("gh release upload"):]
    uploaded = set(re.findall(r"dist/([\w.-]+)", upload[:upload.index("--clobber")]))
    assert {"AI-Safety-Explorer-macOS.dmg", "AI-Safety-Explorer-macOS.zip"} <= uploaded
    docs = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    linked = {}
    for doc in docs:
        for name in re.findall(r"releases/latest/download/([\w.-]+)", doc.read_text("utf-8")):
            linked.setdefault(name, doc.relative_to(ROOT).as_posix())
    assert "AI-Safety-Explorer-macOS.dmg" in linked       # the README's download button
    missing = {n: d for n, d in linked.items() if n not in uploaded}
    assert not missing, f"linked but never uploaded: {missing}"


def test_the_app_updater_reads_files_the_release_carries_and_the_manifest_goes_last():
    from explorer_loader import appupdate
    app = (ROOT / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
    uploads = re.findall(r"dist/([\w.-]+)", app[app.index("gh release upload"):])
    assert appupdate.ZIP in uploads and appupdate.MANIFEST in uploads
    # An installed app acts on app.json at once, so what it names must already be there.
    assert uploads.index(appupdate.MANIFEST) > uploads.index(appupdate.ZIP)
