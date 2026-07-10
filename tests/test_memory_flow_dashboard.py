import json
import tempfile
from pathlib import Path

import generate


def test_memory_flow_loader_is_optional_and_schema_checked():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert generate.load_memory_flow(root) is None

        path = root / "memory_flow.json"
        path.write_text("{}", encoding="utf-8")
        assert generate.load_memory_flow(root) is None

        payload = {
            "schemaVersion": 1,
            "decisionGrade": False,
            "symbols": {},
            "hypotheses": [],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert generate.load_memory_flow(root) == payload


def test_memory_flow_route_has_a_renderer_and_real_panel():
    source = generate.HTML_TEMPLATE
    assert "memflow:()=>memoryFlowCard()" in source
    assert "'aiwatch','memflow','qt'" in source
    assert 'data-seg="memflow"' in source
    assert "function memoryFlowCard()" in source
