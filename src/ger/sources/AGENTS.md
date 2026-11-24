# `src/ger/sources` 数据源插件开发与映射规范

本目录承载所有外部数据源适配器（`BaseSource` 子类）。新增或修改任何数据源插件时，**必须显式处理顶层主开关 `AppConfig.watched_games` 与数据源内部标识符之间的映射层**。

## 为什么需要独立的映射层

全局 `watched_games` 使用**规范中文游戏名**（例如 `"原神"`、`"绝区零"`、`"明日方舟"`），而不同外部数据源使用各自独立的标识符：

| 数据源 | 外部标识符形式 | 映射实现位置 |
| :--- | :--- | :--- |
| `gameg_site` | GitHub 仓库 `data/<slug>/` 目录名（如 `genshin-impact`、`zenless-zone-zero`、`wuthering-waves`） | `DEFAULT_GAMEG_NAME_MAP` + `options.game_name_map` + `resolve_gameg_target_slugs()`（[gameg_site.py](file:///q:/1Coding/GameEventReminder/src/ger/sources/gameg_site.py)） |
| `miyoushe_news` | 米游社板块数字 ID `gids`（如 `2`=原神、`6`=星铁、`8`=绝区零、`1`=崩坏3） | `MIYOUSHE_GAME_META` + `options.game_gids_map`（[miyoushe.py](file:///q:/1Coding/GameEventReminder/src/ger/sources/miyoushe.py)） |
| `ai_collector` | 直接使用规范游戏名（支持单游戏 `options.game` 或省略后自动遍历 `watched_games`） | [ai_collector.py](file:///q:/1Coding/GameEventReminder/src/ger/sources/ai_collector.py) |
| `ical_feed` / `rss_ai` | 配置项 `options.game` 显式声明对应的规范游戏名 | [ical_feed.py](file:///q:/1Coding/GameEventReminder/src/ger/sources/ical_feed.py) / [rss.py](file:///q:/1Coding/GameEventReminder/src/ger/sources/rss.py) |

## 新增/修改数据源的强制检查清单

1. **拉取前先反查/拦截主开关**：
   - 在发起任何外部 HTTP 请求、搜索引擎调用或 LLM 调用**之前**，必须通过源级映射把 `app_config.watched_games` 转换为该源自己的目标标识列表（或通过 `app_config.is_game_watched(canonical_game)` 判断）。
   - 如果某游戏不在 `watched_games` 中，必须直接跳过，**不得浪费网络请求、GitHub API 额度或 LLM/搜索 Token**。
2. **双向映射一致性**：
   - 如果外部源同时配置了自身子集选项（如 `gameg_site` 的 `options.games`）和顶层 `watched_games`，必须取**交集**——顶层 `watched_games` 永远具有最高否决权。
   - 所有产出的 `RawEvent.game` 必须输出为与 `watched_games` 一致的**规范游戏名**，否则不仅会被 `sync.py` 入口守门过滤，还会破坏跨源去重。
3. **结构化标记与标准 5 类归一化**：
   - 权威结构化排期源（YAML / ICS / 官方结构化 API）必须设置 `RawEvent.is_structured_source = True`。
   - 所有 `event_type` 必须经 `normalize_event_type()` 归一化为 5 类标准枚举（`ingame_event`、`web_event`、`checkin_redeem`、`banner`、`version`）。
4. **复用已有活动上下文**：
   - 依赖 LLM 抽取的数据源必须在 `fetch(client, existing_events=...)` 中调用 `format_existing_events_for_ai()` 将同游戏未结束活动传给 LLM 作为第一层防重上下文。
