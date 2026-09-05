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
tests/                       # V2/V3 契约测试 + contracts.py 一致性守护
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
   `mTorrent` 解析器。
2. 在插件管理中启用 `M-Team 成人区番号搜索`。
3. 先配置 MoviePilot 的下载目录；插件的 `AV 下载目录` 选择器只会列出已
   配置的下载根目录，可留空表示使用 MoviePilot 默认目录。
4. 可选配置：M-Team 站点 ID（留空/0 自动识别第一个 `mTorrent` 站点）、
   M-Team API 地址（默认 `https://api.m-team.cc/api`）、请求超时。
5. 保存设置。无效或未配置的目录会在提交时被拒绝，插件详情页也会提示。

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

在 MoviePilot 的 `/docs` 页测试搜索：

```json
{"keyword":"PRED-879","page":1,"page_size":100}
```

选中候选后用返回的 `search_id` / `id` 提交到 `/submit`，插件在
MoviePilot 进程内生成 `genDlToken` 凭据下载 URL 并通过宿主下载链创建任务。

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
