"""Pure contract tests for the MoviePilot V2 M-Team adult plugin."""

import base64
import importlib.util
import json
from pathlib import Path

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[3] / "plugins.v2" / "mteamadultsearch"
)


def _load_contracts():
    spec = importlib.util.spec_from_file_location(
        "mteamadultsearch_contracts_v2", _PLUGIN_DIR / "contracts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contracts = _load_contracts()


def test_normalizes_common_av_number_separators():
    assert contracts.normalize_av_keyword(" pred 879 ") == "PRED-879"
    assert contracts.normalize_av_keyword("PRED_879") == "PRED-879"


def test_builds_mteam_adult_search_payload():
    assert contracts.build_search_payload("PRED-879", 1, 100) == {
        "keyword": "PRED-879",
        "mode": "adult",
        "categories": [],
        "pageNumber": 1,
        "pageSize": 100,
        "visible": 1,
    }


def test_builds_moviepilot_credential_enclosure():
    enclosure = contracts.build_credential_enclosure(
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
