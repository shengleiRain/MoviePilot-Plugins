"""Pure contract tests for the MoviePilot V2 M-Team adult plugin."""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / "plugins.v2"
        / "mteamadultsearch"
    ),
)

from contracts import (  # noqa: E402
    build_credential_enclosure,
    build_search_payload,
    normalize_av_keyword,
)


def test_normalizes_common_av_number_separators():
    assert normalize_av_keyword(" pred 879 ") == "PRED-879"
    assert normalize_av_keyword("PRED_879") == "PRED-879"


def test_builds_mteam_adult_search_payload():
    assert build_search_payload("PRED-879", 1, 100) == {
        "keyword": "PRED-879",
        "mode": "adult",
        "categories": [],
        "pageNumber": 1,
        "pageSize": 100,
        "visible": 1,
    }


def test_builds_moviepilot_credential_enclosure():
    enclosure = build_credential_enclosure(
        api_url="https://api.m-team.cc/api",
        torrent_id="123",
        api_key="secret",
        user_agent="UA",
        use_proxy=False,
    )
    encoded, url = enclosure.split("]", 1)
    descriptor = json.loads(base64.b64decode(encoded[1:]).decode("utf-8"))
    assert url == "https://api.m-team.cc/api/torrent/genDlToken"
    assert descriptor["method"] == "post"
    assert descriptor["params"] == {"id": "123"}
    assert descriptor["header"]["x-api-key"] == "secret"
