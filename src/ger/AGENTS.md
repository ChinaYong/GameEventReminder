# `src/ger` 核心设计约束与维护指引

在修改本目录及子模块代码时，必须遵守以下核心不变量：

## 1. 顶层全局监测名单 `watched_games`（主开关）

- `AppConfig.watched_games` 是全系统监测游戏列表的**唯一主开关**，保存**规范游戏显示名**（如 `["原神", "崩坏：星穹铁道", "绝区零", "鸣潮", "明日方舟"]`）。
- 语义约定：
  - **非空列表**：所有数据源采集、事件入库、提醒分发均严格受此名单约束。
  - **空列表 `[]`**：不启用全局白名单过滤，退化为各数据源自主配置。
- `AppConfig.is_game_watched(canonical_game)` 是判断某规范游戏是否受监测的统一入口。

## 2. 撤出监测时的“静默与清理职责分离”原则

当某款游戏被移出 `watched_games` 时，**严禁立即物理删除该游戏在 SQLite 中的已有活动记录**，必须遵循以下状态机：

1. **自动静默（`Database.reconcile_watched_games`）**：
   - 将不在 `watched_games` 中且 `status = 'active'` 的活动更新为 `status = 'muted', unwatched_muted = 1`；
   - 同步将其所有 `pending` 提醒标记为 `muted`，阻止后续通知。
   - 该对账必须在 **API 启动 (`api.py`)**、**同步前 (`sync.py`)** 和 **分发前 (`dispatcher.py`)** 三个入口均执行，确保修改配置后立即静默、绝不漏发。
2. **过期自动清理（`Database.cleanup_ended_events`）**：
   - 所有活动（含因撤出监测而被静默的活动）的物理删除，统一等待其自然结束超过 `cleanup_ended_after_days` 后由清理逻辑自动完成。
3. **回坑恢复与人工静音隔离**：
   - 当某游戏被重新加回 `watched_games` 时，仅恢复 `status = 'muted' AND unwatched_muted = 1` 的活动并调用 `rebuild_reminders_for_event` 重建未来提醒。
   - 用户手动单独静音的活动其 `unwatched_muted = 0`（在 `update_event` 显式变更 `status` 时清零），因此绝不会被自动恢复。

## 3. 权威优先级与三层去重

- 数据写入权威优先级固定为：**人工锁定 (`manual_locked`) > 结构化源 (`is_structured_source=True`) > AI 源**。
- 修改或新增入库路径时，不得绕过 `sync.py` 的过期硬性过滤（`_is_expired_event`）、标准 5 类事件归一化（`normalize_event_type`）与去重流程。

## 4. 通知汇总队列与总开关原则

- `AppConfig.notifications.enabled` 是全部活动外发通知（含同步异常摘要）的**全局总开关**。
- 默认采用 `mode = "digest"`：到期提醒先由 `enqueue_due_reminders()` 进入待发送队列（`status = 'queued'`），再在 `notifications.digest_cron`（默认每天 `12:15`）经 `flush_digest_queue()` 合并为 **1 条汇总通知**发出；队列为空时静默不发。
- 任何静默操作（`reconcile_watched_games`、手动 `muted`/`done`）必须同时清理 `pending` **与 `queued`** 状态的提醒，确保静默后不会在当天的 12:15 被发出。
- 纯前瞻直播预告属于 `livestream` 类型，默认不在 `enabled_event_types` 中；`checkin_redeem` 仅收录直播后已公布的具体兑换码与领取/签到截止时间。

