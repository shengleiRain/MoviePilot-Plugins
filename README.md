# MoviePilot Plugins（PrivateFilm 私有插件市场）

本仓库是与 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
结构对齐的第三方插件市场仓库，供 MoviePilot 通过 `PLUGIN_MARKET` 安装。
仓库内有且仅有一个插件：

- **`MTeamAdultSearch`（M-Team 成人区番号搜索）**：同时提供
  **MoviePilot V2（`1.1.0`）** 与 **MoviePilot V3（`2.0.0`）** 两个实现。
  插件通过独立的 API 调用 M-Team 成人区（`mode=adult`）实现 AV 番号搜索，
  不修改 MoviePilot 内置的正常电影/电视剧搜索，供 PrivateFilm 的
  Javinizer 候选资源流程调用。

## 仓库结构

```text
package.v2.json              # V2 市场索引（MTeamAdultSearch v1.1.0，v3: false）
package.v3.json              # V3 市场索引（MTeamAdultSearch v2.0.0，>=3.0.0）
plugins.v2/mteamadultsearch/ # V2 实现（旧实现原样保留）
plugins.v3/mteamadultsearch/ # V3 实现（SDK 导入移植，行为与 V2 一致）
icons/Moviepilot_A.png       # 插件图标（取自官方仓库）
tests/                       # V2/V3 契约测试、宿主桩端点测试、contracts.py 一致性守护
.github/scripts/             # 官方版本门禁脚本 check_plugin_versions.py
```

两个实现的目录按官方市场规则各自自包含（市场按整目录下发），共享的纯逻辑
`contracts.py`（番号归一化、M-Team 请求体、genDlToken 凭据封装）在两个目录
中各存一份，由 `tests/test_contracts_parity.py` 守护字节级一致。

MoviePilot 安装时会按宿主版本自动选择：V2 宿主读 `package.v2.json` +
`plugins.v2/`；V3 宿主读 `package.v3.json` + `plugins.v3/`。V3 版本号按
官方规则相对 V2 版本做大版本跃迁（`1.1.0` → `2.0.0`）。

## 安装

### 远程插件市场（推荐）

将本仓库地址加入 MoviePilot 的 `PLUGIN_MARKET`（逗号分隔可多市场共存）：

```text
https://github.com/shengleiRain/MoviePilot-Plugins
```

刷新插件市场后安装 `M-Team 成人区番号搜索`。

### 本地插件仓（开发）

1. 将仓库目录映射到 MoviePilot 容器内，例如：
   `D:\projects\flutter\private-film\MoviePilot-Plugins` →
   `/config/local-plugins/MoviePilot-Plugins`。
2. 设置 `PLUGIN_LOCAL_REPO_PATHS=/config/local-plugins/MoviePilot-Plugins`。
3. 开发时可设置 `PLUGIN_AUTO_RELOAD=true`，或者重启 MoviePilot。
4. 在插件管理中刷新并安装/启用 `MTeamAdultSearch`。

MoviePilot 原生运行在 Windows 时，`PLUGIN_LOCAL_REPO_PATHS` 直接填写仓库
绝对路径即可。

## 配置与使用

1. 先在 MoviePilot 中配置 M-Team 站点，包含 API Access Token 和
   `mTorrent` 解析器。插件自动绑定该站点，无需也不显示站点 ID。
2. 在插件管理中启用 `M-Team 成人区番号搜索`。
3. 先配置 MoviePilot 的下载目录；插件的 `AV 下载目录` 选择器只会列出已
   配置的下载根目录，可留空表示使用 MoviePilot 默认目录。
4. 可选配置：M-Team API 地址（默认官方地址）、请求超时、
   `提交成功后推送通知` 开关（通过 MoviePilot 消息渠道推送）。
5. 保存设置。无效或未配置的目录会在提交时被拒绝，插件详情页也会提示，
   并展示最近提交记录。

插件不会重复保存 M-Team API Key，一切凭据留在 MoviePilot 侧。

## 插件 API

插件不实现 `get_module()`，不修改宿主的 `IndexerModule`、`SearchChain` 或
M-Team 爬虫；只暴露独立接口（`apikey` 鉴权，返回裸业务模型，V2/V3 路径与
契约一致）：

```text
POST /api/v1/plugin/MTeamAdultSearch/search
POST /api/v1/plugin/MTeamAdultSearch/submit
GET  /api/v1/plugin/MTeamAdultSearch/paths
```

`/search` 请求体：

```json
{"keyword":"PRED-879","page":1,"page_size":100,
 "max_pages":1,"sort":"site","free_only":false}
```

- `keyword` 支持标准番号（自动归一化）和自由关键词（演员名等，响应中
  `is_av_number` 区分）
- `max_pages`（1–5）：一次请求聚合多页并按资源 ID 去重
- `sort`：`site`（站点原始顺序）/ `seeders` / `size` / `time` / `free_first`
  （免费优先，其余保持站点顺序）
- `free_only`：只返回免费（下载系数为 0）的资源，候选含 `is_free` 标识
- 相同关键词短时结果缓存 120 秒，对站点请求保持最小间隔，避免频繁触发

`/submit` 用 `/search` 返回的 `search_id` / `id` 提交，插件在 MoviePilot
进程内生成 `genDlToken` 凭据下载 URL 并通过宿主下载链创建任务，成功后
记录提交历史并可推送通知。

错误响应使用标准 HTTP 状态码，格式为 `[错误码] 说明`：

| 状态码 | 错误码 | 含义 |
| --- | --- | --- |
| 400 | `invalid_keyword` / `invalid_sort` / `invalid_api_url` / `invalid_save_path` / `site_not_configured` | 请求参数或宿主配置问题 |
| 404 | `candidate_not_found` | 候选不存在或已被提交 |
| 409 | `plugin_disabled` | 插件未启用 |
| 410 | `session_expired` | 搜索会话过期，需重新搜索 |
| 502 | `upstream_error` / `download_failed` | 站点或下载链路失败 |

## PrivateFilm 集成约定

PrivateFilm 调用 `/search` 获取 Javinizer 候选，只保存返回的不透明
`search_id` / `id`，提交时回传给 `/submit`。M-Team API Key 与下载凭据
绝不下发给 Flutter 端。

## 验证

本地（无需 MoviePilot 宿主）：

```bash
python -m compileall -q plugins.v2 plugins.v3
python -m pytest tests/ -q
python .github/scripts/check_plugin_versions.py package.v2.json package.v3.json
```

真机冒烟（V2 与 V3 各一次）：安装启用插件后，在 `/docs` 调 `/search` 与
`/submit` 走通「搜索 → 候选提交 → 下载任务创建」链路；V3 首次加载时观察
日志无 `[兼容导入]` 旧路径告警。
