"""Local stand-ins match what Terraform deploys, so behaviour tested locally is the cloud's.

Terraform isn't parsed in full; the few values that have a local twin are read by pattern.
"""

import re
from pathlib import Path

from ingest_api.events import COMMIT_APIS
from tests.contracts.servicebus import duration_seconds, queue_config

MESSAGING = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "messaging.tf"


def _block(kind: str, name: str) -> str:
    text = MESSAGING.read_text()
    start = text.index(f'resource "{kind}" "{name}"')
    return text[start : text.index("\n}\n", start)]


def _setting(block: str, key: str) -> str:
    match = re.search(rf"^\s*{key}\s*=\s*(.+?)\s*$", block, re.MULTILINE)
    assert match, f"{key} not set in {MESSAGING.name}"
    return match[1].strip('"')


def test_the_emulator_ingest_queue_matches_the_deployed_one():
    deployed = _block("azurerm_servicebus_queue", "ingest")
    local = queue_config("ingest")

    # The emulator caps message TTL at an hour, so only lock and redelivery are compared.
    assert duration_seconds(_setting(deployed, "lock_duration")) == local.lock_seconds
    assert int(_setting(deployed, "max_delivery_count")) == local.max_delivery_count


def test_the_parser_accepts_exactly_the_apis_event_grid_forwards():
    subscription = _block("azurerm_eventgrid_system_topic_event_subscription", "landing_to_ingest")
    values = re.search(r"values\s*=\s*\[([^\]]*)\]", subscription)

    assert values, "commit API filter missing"
    assert sorted(re.findall(r'"([^"]+)"', values[1])) == sorted(COMMIT_APIS)
