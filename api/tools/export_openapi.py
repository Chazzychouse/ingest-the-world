"""Writes the API's OpenAPI schema, the single source the web app's types are generated from.

uv run python tools/export_openapi.py ../web/src/generated/openapi.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from ingest_api.app import create_app


class NoSigner:
    """Exporting the schema serves no requests, so it needs no storage credentials."""

    def upload_url(self, container: str, name: str, *, expires_at: datetime) -> str:
        raise NotImplementedError("the schema exporter signs nothing")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    # Builds routes only: no database, IdP or storage account is contacted.
    schema = create_app(signer=NoSigner()).openapi()
    Path(argv[0]).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
