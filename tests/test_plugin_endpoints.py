"""Endpoint-level tests for both plugin generations with stubbed host."""

from __future__ import annotations

import pytest

import _harness as harness


pytestmark = pytest.mark.parametrize("generation", ["v2", "v3"])


@pytest.fixture
def env(generation):
    module = harness.load_plugin(generation)
    harness.reset(module)
    plugin = harness.make_plugin(module)
    return module, plugin


def _queue_search(*bodies):
    for body in bodies:
        harness.RequestUtils.responses.append(
            harness._FakeResponse(200, body)
        )


def test_search_returns_sanitized_candidates_with_free_flag(env):
    module, plugin = env
    free_row = harness.make_row(101, name="FREE-ROW", discount="FREE", seeders=5)
    paid_row = harness.make_row(102, name="PAID-ROW", discount="", seeders=1)
    _queue_search(harness.mteam_body([paid_row, free_row], total=2))

    response = plugin.search(module.SearchRequest(keyword="PRED-879"))

    assert response.is_av_number is True
    assert response.total == 2
    assert [item.title for item in response.items] == ["PAID-ROW", "FREE-ROW"]
    free_item = response.items[1]
    assert free_item.is_free is True
    assert free_item.download_factor == 0.0
    assert response.items[0].is_free is False
    # No credential-bearing fields leak to the caller.
    leaked = response.model_dump()
    assert "enclosure" not in str(leaked) and "api_key" not in str(leaked)
    upstream = harness.RequestUtils.calls[0]
    assert upstream["json"]["keyword"] == "PRED-879"
    assert upstream["json"]["mode"] == "adult"


def test_search_sort_and_free_only(env):
    module, plugin = env
    rows = [
        harness.make_row(1, name="PAID", discount="", seeders=1, size=10),
        harness.make_row(2, name="FREE", discount="FREE", seeders=9, size=20),
    ]
    _queue_search(harness.mteam_body(rows, total=2))

    by_seeders = plugin.search(
        module.SearchRequest(keyword="PRED-879", sort="seeders")
    )
    assert [item.title for item in by_seeders.items] == ["FREE", "PAID"]

    free_only = plugin.search(
        module.SearchRequest(keyword="PRED-879", free_only=True)
    )
    assert [item.title for item in free_only.items] == ["FREE"]


def test_search_free_first_is_stable(env):
    module, plugin = env
    rows = [
        harness.make_row(1, name="PAID-1", discount=""),
        harness.make_row(2, name="FREE-1", discount="_2X_FREE"),
        harness.make_row(3, name="PAID-2", discount=""),
    ]
    _queue_search(harness.mteam_body(rows, total=3))

    response = plugin.search(
        module.SearchRequest(keyword="PRED-879", sort="free_first")
    )
    assert [item.title for item in response.items] == ["FREE-1", "PAID-1", "PAID-2"]


def test_search_merges_pages_and_dedupes(env):
    module, plugin = env
    page_one = [harness.make_row(1), harness.make_row(2)]
    page_two = [harness.make_row(2), harness.make_row(3)]  # row 2 repeats
    _queue_search(
        harness.mteam_body(page_one, total=3),
        harness.mteam_body(page_two, total=3),
    )

    response = plugin.search(
        module.SearchRequest(keyword="PRED-879", page_size=2, max_pages=2)
    )

    assert response.total == 3
    assert len(response.items) == 3
    assert len(harness.RequestUtils.calls) == 2
    second_page = harness.RequestUtils.calls[1]["json"]
    assert second_page["pageNumber"] == 2


def test_search_cache_prevents_repeat_upstream_calls(env):
    module, plugin = env
    _queue_search(harness.mteam_body([harness.make_row(1)], total=1))

    first = plugin.search(module.SearchRequest(keyword="PRED-879"))
    second = plugin.search(
        module.SearchRequest(keyword="PRED-879", sort="seeders")
    )

    assert len(harness.RequestUtils.calls) == 1
    assert first.search_id != second.search_id
    assert [a.id for a in first.items] != [] and len(second.items) == 1


def test_free_keyword_search(env):
    module, plugin = env
    _queue_search(harness.mteam_body([harness.make_row(1)], total=1))

    response = plugin.search(module.SearchRequest(keyword=" Some   Actor "))

    assert response.is_av_number is False
    assert response.keyword == "Some Actor"
    assert harness.RequestUtils.calls[0]["json"]["keyword"] == "Some Actor"


def test_invalid_sort_rejected(env):
    module, plugin = env
    with pytest.raises(harness.HTTPException) as error:
        plugin.search(module.SearchRequest(keyword="PRED-879", sort="bogus"))
    assert error.value.status_code == 400
    assert "[invalid_sort]" in error.value.detail


def test_disabled_plugin_rejected(env):
    module, _ = env
    plugin = harness.make_plugin(module, {"enabled": False})
    with pytest.raises(harness.HTTPException) as error:
        plugin.search(module.SearchRequest(keyword="PRED-879"))
    assert error.value.status_code == 409
    assert "[plugin_disabled]" in error.value.detail


def test_upstream_error_mapped(env):
    module, plugin = env
    harness.RequestUtils.responses.append(
        harness._FakeResponse(200, {"code": 403, "message": "forbidden"})
    )
    with pytest.raises(harness.HTTPException) as error:
        plugin.search(module.SearchRequest(keyword="PRED-879"))
    assert error.value.status_code == 502
    assert "[upstream_error]" in error.value.detail
    assert "forbidden" in error.value.detail


def test_submit_creates_download_history_and_notification(env):
    module, plugin = env
    _queue_search(harness.mteam_body([harness.make_row(7, name="TARGET")], total=1))
    response = plugin.search(module.SearchRequest(keyword="PRED-879"))

    result = plugin.submit(
        module.SubmitRequest(
            search_id=response.search_id, candidate_id=response.items[0].id
        )
    )

    assert result.submitted is True
    assert result.download_id == "dl-hash-1"
    assert result.title == "TARGET"
    assert harness.DownloadChain.calls[0]["save_path"] is None
    assert harness.DownloadChain.calls[0]["source"] == "MTeamAdultSearch"
    history = plugin.get_data("submission_history")
    assert history[0]["title"] == "TARGET"
    assert history[0]["download_id"] == "dl-hash-1"
    assert len(plugin.messages) == 1
    assert plugin.messages[0]["mtype"] == "插件消息"
    assert "TARGET" in plugin.messages[0]["text"]


def test_submit_notification_can_be_disabled(env):
    module, plugin = env
    plugin = harness.make_plugin(module, {"notify": False})
    _queue_search(harness.mteam_body([harness.make_row(7)], total=1))
    response = plugin.search(module.SearchRequest(keyword="PRED-879"))

    plugin.submit(
        module.SubmitRequest(
            search_id=response.search_id, candidate_id=response.items[0].id
        )
    )

    assert plugin.messages == []


def test_submit_candidate_consumed_once(env):
    module, plugin = env
    rows = [harness.make_row(7), harness.make_row(8)]
    _queue_search(harness.mteam_body(rows, total=2))
    response = plugin.search(module.SearchRequest(keyword="PRED-879"))
    request = module.SubmitRequest(
        search_id=response.search_id, candidate_id=response.items[0].id
    )

    plugin.submit(request)
    with pytest.raises(harness.HTTPException) as error:
        plugin.submit(request)

    assert error.value.status_code == 404
    assert "[candidate_not_found]" in error.value.detail
    # Other candidates in the same session stay usable.
    plugin.submit(
        module.SubmitRequest(
            search_id=response.search_id, candidate_id=response.items[1].id
        )
    )


def test_submit_expired_session(env):
    module, plugin = env
    _queue_search(harness.mteam_body([harness.make_row(7)], total=1))
    response = plugin.search(module.SearchRequest(keyword="PRED-879"))
    harness.expire_session(module, response.search_id)

    with pytest.raises(harness.HTTPException) as error:
        plugin.submit(
            module.SubmitRequest(
                search_id=response.search_id, candidate_id=response.items[0].id
            )
        )

    assert error.value.status_code == 410
    assert "[session_expired]" in error.value.detail


def test_submit_rejects_invalid_save_path_without_consuming(env):
    module, plugin = env
    plugin = harness.make_plugin(module, {"save_path": "/not/allowed"})
    _queue_search(harness.mteam_body([harness.make_row(7)], total=1))
    response = plugin.search(module.SearchRequest(keyword="PRED-879"))

    with pytest.raises(harness.HTTPException) as error:
        plugin.submit(
            module.SubmitRequest(
                search_id=response.search_id, candidate_id=response.items[0].id
            )
        )

    assert error.value.status_code == 400
    assert "[invalid_save_path]" in error.value.detail
    # The candidate is not consumed by the rejected submit.
    session = module.MTeamAdultSearch._sessions[response.search_id]
    assert response.items[0].id in session.candidates


def test_submit_with_configured_save_path(env):
    module, _ = env
    plugin = harness.make_plugin(module, {"save_path": "/downloads/av"})
    _queue_search(harness.mteam_body([harness.make_row(7)], total=1))
    response = plugin.search(module.SearchRequest(keyword="PRED-879"))

    result = plugin.submit(
        module.SubmitRequest(
            search_id=response.search_id, candidate_id=response.items[0].id
        )
    )

    assert result.save_path == "/downloads/av"
    assert harness.DownloadChain.calls[0]["save_path"] == "/downloads/av"


def test_paths_lists_configured_dirs(env):
    module, plugin = env
    result = plugin.paths()
    assert len(result) == 1
    assert result[0].save_path == "local:/downloads/av"
    assert result[0].name == "AV 目录"


def test_site_auto_binding_ignores_legacy_site_id(env):
    module, plugin = env
    plugin = harness.make_plugin(module, {"site_id": 999})
    _queue_search(harness.mteam_body([harness.make_row(1)], total=1))

    response = plugin.search(module.SearchRequest(keyword="PRED-879"))

    # The M-Team mTorrent site is bound automatically; site_id is ignored.
    assert response.items[0].site_name == "M-Team"
    upstream = harness.RequestUtils.calls[0]
    assert upstream["json"]["keyword"] == "PRED-879"
