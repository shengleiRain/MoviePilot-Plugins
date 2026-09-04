# MoviePilot Plugins

This repository contains a MoviePilot V2 plugin for searching M-Team's adult
catalog by AV number without changing MoviePilot's built-in movie/TV search.

## Plugin

`MTeamAdultSearch` is implemented under `plugins.v2/mteamadultsearch/`.

The plugin deliberately does not implement `get_module()` and does not patch
`IndexerModule`, `SearchChain`, or `MTorrentSpider`. It exposes a separate API:

```text
POST /api/v1/plugin/MTeamAdultSearch/search
POST /api/v1/plugin/MTeamAdultSearch/submit
```

The search request uses M-Team's adult-mode payload. The submit endpoint keeps
the raw M-Team result inside MoviePilot, creates the same credential-style
download URL used by the built-in M-Team spider, and submits it through
MoviePilot's `DownloadChain`.

## Configuration

First configure the M-Team site in MoviePilot with an API key. Then enable the
plugin and optionally set:

- the M-Team indexer site ID (blank/0 auto-detects the first M-Team `mTorrent` site);
- the M-Team API base URL, normally `https://api.m-team.cc/api`;
- a MoviePilot download path such as `local:/downloads/av`.

The download path is interpreted inside the MoviePilot runtime/container. It
must be a path already accessible to MoviePilot and visible to the later
PrivateFilm scan.

## Install into MoviePilot V2

This checkout is a plugin repository, not a standalone service. It is currently
local and has not been published to a public GitHub repository.

For local MoviePilot development:

1. Make this repository visible to the MoviePilot process. For Docker, bind
   mount the host directory `D:\projects\flutter\private-film\MoviePilot-Plugins`
   to a container path such as `/config/local-plugins/MoviePilot-Plugins`.
2. Set MoviePilot's `PLUGIN_LOCAL_REPO_PATHS` to that container path (or to the
   Windows path when MoviePilot runs natively). `PLUGIN_AUTO_RELOAD=true` is
   optional during development.
3. Restart MoviePilot or reload its plugin list, then install/enable
   `MTeamAdultSearch` from the plugin manager.

For normal remote installation, add this repository URL to MoviePilot's
`PLUGIN_MARKET` and refresh the plugin market:

```text
https://github.com/shengleiRain/MoviePilot-Plugins
```

Then install `MTeamAdultSearch` from the refreshed plugin list.
The V2 index is `package.v2.json`, and the implementation is under
`plugins.v2/mteamadultsearch/`.

## Use from the MoviePilot plugin page

1. Configure the M-Team site in MoviePilot first, including its API Access
   Token and the `mTorrent` parser.
2. In MoviePilot's plugin manager, open `M-Team 成人区番号搜索` and switch on
   `启用插件`.
3. Configure MoviePilot's download directories first. The plugin's `AV 下载目录`
   selector then lists those configured roots. Choose `使用 MoviePilot 默认下载目录`
   or a specific configured root.
4. Save the plugin settings. An invalid or unconfigured path is rejected at
   submit time; the plugin details page also reports the path problem.
5. Test the API from MoviePilot's `/docs` page with a body such as:

   ```json
   {"keyword":"PRED-879","page":1,"page_size":100}
   ```

   The plugin endpoint is `/api/v1/plugin/MTeamAdultSearch/search`. Submit a
   selected opaque candidate to `/api/v1/plugin/MTeamAdultSearch/submit`.

The current PrivateFilm checkout has not yet been changed to call these plugin
endpoints; that is a separate client-integration step. The plugin can be
verified independently from MoviePilot's API docs first.

## PrivateFilm integration contract

PrivateFilm should call the plugin's search endpoint for Javinizer candidates,
keep only the returned opaque `id` values, and submit the selected
`searchId`/`candidateId`. M-Team API keys and download credentials must never
be sent to Flutter.

## Verification

Run the tests with the MoviePilot V2 host environment, because the plugin uses
the host's `app.plugins`, `app.utils.http`, `app.helper.sites`, and download
chain APIs:

```text
<MoviePilot>/.venv/bin/python -m pytest tests/v2/mteamadultsearch
```
