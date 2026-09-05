"""Guard that the V2 and V3 plugin copies keep contracts.py in sync.

The plugin market ships each plugin directory as a self-contained unit, so
``contracts.py`` intentionally exists twice. When you change one copy, copy the
file to the other directory in the same commit; this test fails otherwise.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
V2_CONTRACTS = (
    REPO_ROOT / "plugins.v2" / "mteamadultsearch" / "contracts.py"
)
V3_CONTRACTS = (
    REPO_ROOT / "plugins.v3" / "mteamadultsearch" / "contracts.py"
)


def test_v2_and_v3_contracts_are_identical():
    v2_bytes = V2_CONTRACTS.read_bytes()
    v3_bytes = V3_CONTRACTS.read_bytes()
    assert v2_bytes == v3_bytes, (
        "plugins.v2/mteamadultsearch/contracts.py and "
        "plugins.v3/mteamadultsearch/contracts.py have drifted; "
        "copy the changed file over the other one in the same commit"
    )
