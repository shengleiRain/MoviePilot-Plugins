# M-Team 成人区番号搜索

这是一个 MoviePilot V2 插件，不覆盖 MoviePilot 内置的 M-Team 正常影片
搜索。它提供独立的成人区番号搜索接口，适合 PrivateFilm 的 Javinizer
候选资源流程。

## 前置条件

在 MoviePilot 中先配置一个 M-Team 站点，并在该站点配置 API Access Token。
插件从 MoviePilot 的 `mTorrent` 站点配置读取 API Key，不在插件配置页重复
保存密钥。

## 安装到 MoviePilot V2

本项目是插件源码仓库，不是需要单独启动的服务。当前项目尚未发布到远程
GitHub，因此本地测试有两种方式：

### 本地插件仓测试

1. 将仓库目录映射到 MoviePilot V2 容器内，例如：
   `D:\projects\flutter\private-film\MoviePilot-Plugins` →
   `/config/local-plugins/MoviePilot-Plugins`。
2. 在 MoviePilot 环境中设置：
   `PLUGIN_LOCAL_REPO_PATHS=/config/local-plugins/MoviePilot-Plugins`。
3. 开发时可设置 `PLUGIN_AUTO_RELOAD=true`，或者重启 MoviePilot。
4. 在插件管理中刷新并安装/启用 `MTeamAdultSearch`。

MoviePilot 原生运行在 Windows 时，`PLUGIN_LOCAL_REPO_PATHS` 直接填写仓库
绝对路径即可。

### 远程插件市场安装

将本仓库地址加入 MoviePilot 的 `PLUGIN_MARKET`：

```text
https://github.com/shengleiRain/MoviePilot-Plugins
```

刷新插件市场，再安装 `M-Team 成人区番号搜索`。V2 使用
`package.v2.json` 和 `plugins.v2/mteamadultsearch/`。

## 插件设置页

安装后打开 MoviePilot 的插件管理页：

1. 打开 `M-Team 成人区番号搜索` 的设置。
2. 打开 `启用插件`。
3. 先在 MoviePilot 的下载目录设置中配置可用下载根目录。
4. 在 `AV 下载目录` 中选择一个已配置目录，或选择使用 MoviePilot 默认目录。
5. 按需开关 `提交成功后推送通知`，保存设置。

插件自动绑定 M-Team `mTorrent` 站点，无需配置站点 ID。插件不会重复保存
M-Team API Key。

目录值使用 MoviePilot 的路径语义，例如：

```text
/downloads/av
rclone:/moviepilot/av
```

插件会使用 MoviePilot 的目录 allowlist 校验保存路径，不接受未配置的任意
路径。插件详情页展示状态、绑定站点、下载目录与最近提交记录。

## API

```text
POST /api/v1/plugin/MTeamAdultSearch/search
POST /api/v1/plugin/MTeamAdultSearch/submit
GET  /api/v1/plugin/MTeamAdultSearch/paths
```

`/search` 支持 `max_pages`（多页聚合去重）、`sort`（`site`/`seeders`/
`size`/`time`/`free_first`）与 `free_only`（只看免费）；`keyword` 兼容标准
番号与自由关键词（响应 `is_av_number` 区分），相同关键词短时缓存。候选带
`is_free` 免费标识。

搜索响应只包含安全候选字段。原始 M-Team 行数据保存在插件进程内的短期
搜索会话中；提交时插件才生成 `genDlToken` 凭据并调用 MoviePilot 下载链，
成功后记录提交历史并按开关推送通知。

错误响应为标准 HTTP 状态码，detail 格式 `[错误码] 说明`
（如 `[session_expired] 搜索会话不存在或已过期`），调用方可按状态码决定
重新搜索或报错。

## 正常搜索隔离

插件不实现 `get_module()`，不修改 `IndexerModule`、`SearchChain` 或
`MTorrentSpider`。MoviePilot 自带的 `/search/media` 和 `/search/title` 仍由
宿主原逻辑处理。
