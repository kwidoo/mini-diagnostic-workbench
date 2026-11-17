"""P1-5: the documented first-hardware checklist only uses read-only, allowlisted requests."""

import re
from pathlib import Path

import pytest

from app.obd.allowlist import AllowedCommand, ForbiddenRequestError
from app.obd.client import INIT_SEQUENCE

DOC = Path(__file__).resolve().parents[2] / "docs" / "HARDWARE_SETUP_MAC.md"


def _checklist_requests() -> list[str]:
    text = DOC.read_text()
    block = re.search(
        r"<!-- checklist-requests -->\s*```\s*(.*?)```\s*<!-- /checklist-requests -->", text, re.S
    )
    assert block, "checklist request block missing from HARDWARE_SETUP_MAC.md"
    return block.group(1).split()


def test_every_checklist_request_passes_the_read_only_allowlist():
    reqs = _checklist_requests()
    assert {"0100", "010C", "0105", "0110", "010B", "03", "07", "0A", "020200"} <= set(reqs)
    for r in reqs:
        assert AllowedCommand.parse(r).text == r
    assert set(INIT_SEQUENCE) <= set(reqs)


@pytest.mark.parametrize(
    "forbidden", ["04", "14", "0800", "2E", "31", "2701", "1003", "3B", "ATMA", "ATBRD10"]
)
def test_checklist_contains_nothing_that_changes_ecu_state(forbidden):
    assert forbidden not in _checklist_requests()
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand.parse(forbidden)


def test_checklist_is_marked_not_hardware_validated():
    text = DOC.read_text()
    assert "NOT hardware-validated" in text and "Do **not** drive the vehicle" in text
