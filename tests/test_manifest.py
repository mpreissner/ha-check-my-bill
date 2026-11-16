"""The manifest and the code agree on the integration's identity."""

import json
from pathlib import Path

from const import DOMAIN

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "check_my_bill"


def test_manifest_domain_matches_const() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert manifest["domain"] == DOMAIN
