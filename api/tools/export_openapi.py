"""Writes the API's OpenAPI schema, the single source the web app's types are generated from.

uv run python tools/export_openapi.py ../web/src/generated/openapi.json
"""

import json
import sys
from pathlib import Path

from ingest_api.app import create_app


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    schema = create_app().openapi()  # builds routes only: no database or IdP is contacted
    Path(argv[0]).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
