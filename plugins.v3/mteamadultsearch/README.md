# M-Team 成人区番号搜索（MoviePilot V3）

这是 `MTeamAdultSearch` 的 MoviePilot V3 版本（`2.0.0`），与
`plugins.v2/mteamadultsearch` 的 V2 版本（`1.1.0`）功能一致：不覆盖
MoviePilot 内置的 M-Team 正常影片搜索，只提供独立的成人区番号搜索接口，
适合 PrivateFilm 的 Javinizer 候选资源流程。

V3 版本按官方 `V3_Plugin_Adaptation.md` 要求使用 SDK 命名空间
（`app.sdk.media` / `app.sdk.network` / `app.application.directory`），
插件 API 路径与 V2 版完全相同。

## 前置条件

在 MoviePilot 中先配置一个 M-Team 站点，并在该站点配置 API Access Token。
插件从 MoviePilot 的 `mTorrent` 站点配置读取 API Key，不在插件配置页重复
保存密钥。

## 安装到 MoviePilot V3

### 本地插件仓测试

1. 将仓库目录映射到 MoviePilot V3 容器内，例如：
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

V3 宿主会优先读取 `package.v3.json` 并安装
`plugins.v3/mteamadultsearch/`；V2 宿主读取 `package.v2.json`。

## 插件设置页

安装后打开 MoviePilot 的插件管理页：

1. 打开 `M-Team 成人区番号搜索` 的设置。
2. 打开 `启用插件`。
3. 先在 MoviePilot 的下载目录设置中配置可用下载根目录。
4. 在 `AV 下载目录` 中选择一个已配置目录，或选择使用 MoviePilot 默认目录。
5. 保存设置。

插件不会重复保存 M-Team API Key。`M-Team 站点 ID` 留空或填 `0` 时，会自动
选择第一个 M-Team `mTorrent` 站点；有多个 M-Team 站点时填写对应 ID。

目录值使用 MoviePilot 的路径语义（本地 `/downloads/av` 或远端
`rclone:/moviepilot/av`）。插件使用 MoviePilot 的目录 allowlist 校验保存
路径，不接受未配置的任意路径。

## API

```text
POST /api/v1/plugin/MTeamAdultSearch/search
POST /api/v1/plugin/MTeamAdultSearch/submit
GET  /api/v1/plugin/MTeamAdultSearch/paths
```

搜索响应只包含安全候选字段。原始 M-Team 行数据保存在插件进程内的短期
搜索会话中；提交时插件才生成 `genDlToken` 凭据并调用 MoviePilot 下载链。
V3 下插件路由返回裸业务模型（宿主不额外包装顶层字段），与 V2 版对调用方
的线上契约一致。

## 正常搜索隔离

插件不实现 `get_module()`，不修改宿主的索引器模块或搜索链。MoviePilot
自带的搜索仍由宿主原逻辑处理。
