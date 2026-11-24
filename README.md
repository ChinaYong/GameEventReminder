# Game Event Reminder (`ger`)

面向 VPS 部署的轻量级游戏活动收集与节点提醒服务。

## 核心特性

- **极简低资源**：纯 `httpx` 对接所有外部接口，不依赖重型 AI SDK 或浏览器引擎；常驻模式内存约 40–60 MB，也支持 `systemd timer / cron` 零常驻模式（闲时 0 MB 内存）。
- **混合可扩展数据源**：
  - `gameg_site`：直连社区开源项目 [gameg.site](https://www.gameg.site/) (`Oven2002/game-event-gantt`) 的标准 YAML 排期，零 Token 消耗、精确到分钟。
  - `ical_feed`：零依赖标准 `.ics` (RFC 5545) 订阅源，可直连 `Trrrrw/hoyo_calendar`、`Astrian/ArkBulletinGPT` 及任何社区游戏日历链接。
  - `miyoushe_news`：直连米游社公开静态公告/活动接口（原神/星铁/绝区零/崩坏3 国服），精准提取官方网页活动与前瞻公告。
  - `ai_collector`：支持 **通用 OpenAI 兼容接口 + Tavily / Exa 联网搜索**（及直抓官方公告 URL），也支持 **Gemini 原生 Google Search 联网搜索**（两阶段检索 + JSON Schema 严格提取）。
  - `rss_ai`：支持官方 RSS/Atom 或 RSSHub 订阅源 + AI 提取活动时间。
- **标准化 5 类活动范畴（默认国服 `cn`）**：`ingame_event`（游戏内道具活动）、`web_event`（官方网页道具活动，严格排除纯抽奖营销）、`checkin_redeem`（限时签到、福利领取、版本前瞻直播与兑换码）、`banner`（卡池更新）、`version`（大版本更新/维护），支持全局/单源启停及按类别配置提醒偏移量。
- **三层谨慎防重与自动清理**：Unicode NFKC 标题规范化 + 规则合并 + 灰色地带疑似重复按需触发**联网 AI 审查**（支持别名/简称核实，权威优先级：**人工锁定 > 结构化源 > AI 源**）；已结束活动硬性拦截不入库，默认保留 3 天便于事后核对后自动清理。
- **可靠的提醒引擎**：SQLite WAL 持久化、幂等提醒重建、活动延期自动重排、离线宽限期防轰炸、人工修改自动锁定（`manual_locked`）防次日同步覆盖。
- **多通道推送**：内置 **ntfy**（JSON UTF-8 完整支持）与 **Bark**（POST `/push`）。
- **无 UI 但易检查修改**：提供完整 **CLI**（SSH 快速查改）和 **轻量 REST API**（Bearer Token 鉴权，自带 `/docs` Swagger 页面，手机或快捷指令可直接操作）。

---

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 复制并编辑配置与环境变量（程序会自动加载项目根目录 .env）
cp config.example.yaml config.yaml
cp .env.example .env
# 按需填写 ntfy/bark、API Token、LLM 与 Tavily/Exa/Gemini 密钥

# 3. 测试通知通道是否连通
uv run ger -c config.yaml test-notify

# 4. 手动执行一次同步
uv run ger -c config.yaml sync

# 5. 查看收集到的活动及提醒节点
uv run ger -c config.yaml list
uv run ger -c config.yaml show 1
```

---

## 常用 CLI 命令

```bash
# 查看近期活动（支持按游戏、状态、待人工核对过滤）
ger list
ger list --game 原神
ger list --review            # 仅看 AI 标记为 needs_review 的事件
ger list --all               # 包含已结束的历史活动

# 查看单个活动详情和关联提醒
ger show 1

# 手动新增 / 修改 / 静音 / 标记完成 / 删除
ger add --game 原神 --title "限时挑战" --start "2026-11-26T10:00:00+08:00" --end "2026-12-08T03:59:00+08:00"
ger edit 1 --end "2026-12-10T03:59:00+08:00" --clear-review
ger mute 1
ger done 1
ger delete 1

# 立即触发同步或提醒检查
ger sync
ger sync --source gameg-site
ger dispatch
```

> 注：通过 `ger edit` 或 `PATCH /events/{id}` 修改过的事件会自动设置 `manual_locked=true`，防止次日自动同步覆盖你的人工修正；如需恢复自动同步，传入 `--unlock` 或 `"manual_locked": false`。

---

## 轻量 REST API

启动 `ger serve` 后，访问 `http://127.0.0.1:8765/docs`（右上角 `Authorize` 输入 `api.auth_token`）即可免写前端直接检查与修改事件：

- `GET /events`：列表查询（支持 `game`, `status`, `needs_review`, `include_past`）
- `GET /events/{id}`：查看事件及所有提醒节点状态
- `POST /events`：手动创建事件
- `PATCH /events/{id}`：修改时间、状态或静音（自动加锁并重算提醒）
- `DELETE /events/{id}`：删除事件
- `POST /sync` / `GET /sync/latest`：手动触发同步 / 查看最近一次同步报告
- `POST /dispatch` / `POST /notify/test`：手动触发到期推送 / 发送测试通知

---

## VPS 两种部署模式

### 模式 A：常驻模式（推荐，支持远程 API + 自动调度，~45 MB 内存）

**方式 1：Docker Compose**
```bash
# 将密钥写入 .env，编辑 config.yaml 后启动
docker compose up -d
```

**方式 2：systemd 单进程**
```bash
sudo cp deploy/ger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ger.service
```

### 模式 B：零常驻模式（闲时 0 MB 内存，仅定时运行 + SSH CLI 查改）

```bash
sudo cp deploy/ger-sync.service deploy/ger-sync.timer /etc/systemd/system/
sudo cp deploy/ger-dispatch.service deploy/ger-dispatch.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ger-sync.timer ger-dispatch.timer
```

需要远程检查时也可临时在 SSH 中执行 `ger serve --no-scheduler`。

---

## 扩展指南

项目通过注册表装饰器解耦外部依赖，扩展新能力无需修改核心调度与存储代码：

- **新增数据源**：在 `src/ger/sources/` 新建模块，继承 `BaseSource` 并添加 `@register_source("my_source")`，在 `sources/__init__.py` 导入后即可在 `config.yaml` 中使用。
- **新增搜索引擎**：在 `src/ger/search/` 继承 `BaseSearchProvider` 并添加 `@register_search_provider("my_search")`。
- **新增通知通道**：在 `src/ger/notifiers/` 继承 `BaseNotifier` 并添加 `@register_notifier("my_notifier")`。
