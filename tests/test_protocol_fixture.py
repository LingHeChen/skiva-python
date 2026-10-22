import json
from pathlib import Path

from skiva_sdk.protocol import Limits, Manifest, canonical_json


def test_actual_go_protocol_canonicalization():
    fixture = json.loads((Path(__file__).parent / "fixtures/go-manifest.json").read_text())
    manifest = Manifest.parse(fixture["manifest"], Limits())
    assert canonical_json([f.wire() for f in manifest.files]).decode() == fixture["canonical"]
