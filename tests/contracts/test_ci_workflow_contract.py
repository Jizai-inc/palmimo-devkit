"""Every standalone example project must be checked by the CI `examples` job.

The job's matrix lists example directories by hand, while the lockfile and
notice contracts discover them from the tree; an example added without a
matrix entry would ship with no lock, lint, type, or test check.
"""

from pathlib import Path

import yaml

from tests.contracts.test_self_containment import EXAMPLE_PROJECT_DIRS


REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_examples_job_covers_every_example_project() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    matrix_dirs = {entry["dir"] for entry in workflow["jobs"]["examples"]["strategy"]["matrix"]["example"]}
    project_dirs = {path.relative_to(REPO_ROOT).as_posix() for path in EXAMPLE_PROJECT_DIRS}

    assert matrix_dirs == project_dirs
