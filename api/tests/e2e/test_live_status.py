"""Live upload status through the running stack's nginx, as the browser reads it: the stream's
first bytes arrive at once, not when a proxy's buffer fills or the stream ends.

    make up && make e2e
"""

from collections.abc import Iterator

import httpx
import pytest

from tools.stack import WEB_URL, sign_in

pytestmark = pytest.mark.e2e

# Far below a stream's lifetime (minutes), and above a poll or two of the database.
FIRST_BYTES_SECONDS = 10.0


def _lines(chunks: Iterator[str]) -> Iterator[str]:
    pending = ""
    for chunk in chunks:
        *lines, pending = (pending + chunk).split("\n")
        yield from lines


def test_the_upload_stream_is_not_buffered_by_the_proxy():
    timeout = httpx.Timeout(FIRST_BYTES_SECONDS)
    with (
        httpx.Client(base_url=WEB_URL, timeout=timeout) as http,
        http.stream("GET", "/api/uploads/events", headers=sign_in("carol")) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # A read timeout here means a proxy held the stream back.
        first = next(_lines(response.iter_text()))
    assert first.startswith("retry: ")
