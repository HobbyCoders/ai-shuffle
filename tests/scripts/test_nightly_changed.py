import base64
import importlib.util
import json
from pathlib import Path
import urllib.error

import pytest

spec = importlib.util.spec_from_file_location("nightly_changed", Path(__file__).resolve().parents[2] / "scripts/ci/nightly_changed.py")
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)


@pytest.mark.parametrize("channel", ["edge", "nightly"])
def test_published_revision_skips_only_unchanged_main(channel):
    envelope = json.dumps({"payload": base64.b64encode(json.dumps({"channel": channel, "revision": "a" * 40}).encode()).decode()})
    assert not nightly.changed("a" * 40, lambda: envelope)
    assert nightly.changed("b" * 40, lambda: envelope)


def test_first_publication_and_transport_failure():
    def missing():
        raise urllib.error.HTTPError(nightly.URL, 404, "missing", {}, None)
    assert nightly.changed("a" * 40, missing)
    def unavailable():
        raise urllib.error.HTTPError(nightly.URL, 503, "unavailable", {}, None)
    with pytest.raises(urllib.error.HTTPError):
        nightly.changed("a" * 40, unavailable)
    with pytest.raises(ValueError):
        nightly.changed("a" * 40, lambda: "not json")
