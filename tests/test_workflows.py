"""Every GitHub workflow parses. A YAML error there is a workflow GitHub never
loads, and the only place that shows is a run that never happens."""

import pytest
import yaml

import helpers

WORKFLOWS = sorted((helpers.REPO / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["jobs"], path
