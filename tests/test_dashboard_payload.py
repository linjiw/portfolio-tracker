import json

import pytest

from scripts.dashboard_payload import decode_dashboard_payload


def test_decoder_handles_javascript_like_text_inside_json_strings():
    payload = {"text": "};\nconst fmt=malicious-looking-but-data", "nested": {"ok": True}}
    html = "<script>\nconst DATA = " + json.dumps(payload) + ";\nconst fmt=()=>0;\n</script>"

    assert decode_dashboard_payload(html) == payload


def test_decoder_rejects_missing_terminator_and_non_object():
    with pytest.raises(ValueError, match="terminator"):
        decode_dashboard_payload("const DATA = {} const next = 1")
    with pytest.raises(ValueError, match="not an object"):
        decode_dashboard_payload("const DATA = [];\n")


def test_decoder_rejects_non_finite_json_constants():
    with pytest.raises(ValueError, match="non-finite"):
        decode_dashboard_payload('const DATA = {"value": NaN};')
