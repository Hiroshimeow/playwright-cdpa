from __future__ import annotations

import pytest

from playwright_gpt_core.config import CoreConfig
from playwright_gpt_core.errors import InvalidInputError


def test_cdp_endpoint_must_be_loopback() -> None:
    with pytest.raises(InvalidInputError):
        CoreConfig(cdp_endpoint="http://example.com:9222").validated()
    assert CoreConfig(cdp_endpoint="http://127.0.0.1:9222").validated()
