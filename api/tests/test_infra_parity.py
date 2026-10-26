"""Local stand-ins match what Terraform deploys, so behaviour tested locally is the cloud's,
and the test suite's settings match what Compose runs.

Terraform isn't parsed in full; the few values that have a local twin are read by pattern.
"""

import re
from pathlib import Path

import pytest

from ingest_api.events import COMMIT_APIS
from tests.conftest import TEST_ACCOUNT, TEST_ACCOUNT_KEY
from tests.contracts.servicebus import duration_seconds, queue_config

ROOT = Path(__file__).resolve().parents[2]
MESSAGING = ROOT / "infra" / "terraform" / "messaging.tf"
COMPOSE = ROOT / "compose.yaml"
TEST_COMPOSE = ROOT / "compose.test.yaml"


def _block(kind: str, name: str) -> str:
    text = MESSAGING.read_text()
    start = text.index(f'resource "{kind}" "{name}"')
    return text[start : text.index("\n}\n", start)]


def _setting(block: str, key: str) -> str:
    match = re.search(rf"^\s*{key}\s*=\s*(.+?)\s*$", block, re.MULTILINE)
    assert match, f"{key} not set in {MESSAGING.name}"
    return match[1].strip('"')


@pytest.mark.parametrize("queue", ["ingest", "derive"])
def test_the_emulator_queues_match_the_deployed_ones(queue):
    deployed = _block("azurerm_servicebus_queue", queue)
    local = queue_config(queue)

    # The emulator caps message TTL at an hour, so only lock and redelivery are compared.
    assert duration_seconds(_setting(deployed, "lock_duration")) == local.lock_seconds
    assert int(_setting(deployed, "max_delivery_count")) == local.max_delivery_count


def test_the_parser_accepts_exactly_the_apis_event_grid_forwards():
    subscription = _block("azurerm_eventgrid_system_topic_event_subscription", "landing_to_ingest")
    values = re.search(r"values\s*=\s*\[([^\]]*)\]", subscription)

    assert values, "commit API filter missing"
    assert sorted(re.findall(r'"([^"]+)"', values[1])) == sorted(COMMIT_APIS)


def _accounts(compose: str) -> dict[str, str]:
    line = re.search(r"AZURITE_ACCOUNTS:\s*(\S+)", compose)
    assert line, "AZURITE_ACCOUNTS not set"
    return dict(entry.split(":", 1) for entry in line[1].split(";"))


def test_compose_registers_the_accounts_the_stack_and_tests_use():
    compose = COMPOSE.read_text()
    stack_key = re.search(r"INGEST_BLOB_ACCOUNT_KEY:\s*(\S+)", compose)
    stack_url = re.search(r"INGEST_BLOB_ACCOUNT_URL:\s*\S+/(\w+)\s", compose)

    assert stack_key and stack_url
    assert _accounts(compose)[stack_url[1]] == stack_key[1]
    assert _accounts(TEST_COMPOSE.read_text()) == {TEST_ACCOUNT: TEST_ACCOUNT_KEY}
    assert stack_url[1] != TEST_ACCOUNT
