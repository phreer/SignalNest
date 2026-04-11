# SignalNest 数据处理流程

本文档描述从信息抓取到最终入库的完整流程，包括各阶段的限制参数与配置项。

---

## 总览

```
Agent 调用 collect_* 工具
  └─ rt.state["raw_items"]          所有抓取的原始条目

summarize_items()
  ├─ Stage A  过滤历史已入选内容       selected dedup_key check
  ├─ Stage 1  AI 批量标题筛选          batch_select_by_titles()
  ├─ Stage B  跨来源去重              ai_dedup_across_candidates()
  └─ Stage 2  并行 AI 评分 + 摘要      score_single_item() × N

generate_digest_summary()
  └─ rt.state["digest_summary"]     今日要点总结

build_indexed_items()
  ├─ store.upsert_raw_items()       写入 raw_items 表
  └─ store.replace_annotations_for_job() 写入 item_annotations 表
```

---

## 一、抓取阶段

### 1.1 RSS（`src/collectors/rss_collector.py`）

**入口：** `collect_rss(config, max_total=None)`

**执行流程：**

1. 遍历 `collectors.rss.feeds` 中的所有 feed
2. 对每个 feed 调用 `_fetch_feed(url, days_back, max_items)`：
   - HTTP GET，超时 15 秒，User-Agent 标识为 `SignalNestBot/1.0`
   - 只保留 `published_at >= now - days_lookback` 的条目
   - 最多取 `max_items_per_feed_initial` 条（可被单个 feed 的 `max_items_initial` 字段覆盖）
   - 内容提取优先取 `entry.content`，回退到 `entry.summary`，去除 HTML 标签后截断为 2000 字符
3. 全局 URL 去重（`seen_urls` set）
4. 若传入 `max_total`，对结果截断

**每条 item 产出的字段：**

| 字段 | 来源 |
|---|---|
| `title` | `entry.title` |
| `url` | `entry.link` |
| `description` | 内容提取结果（前 500 字符） |
| `content_snippet` | 内容提取结果（前 2000 字符，供评分阶段使用） |
| `published_at` | 解析自 `published_parsed` / `updated_parsed` / 字符串字段 |
| `feed_title` | `feed.feed.title` 或 URL |
| `source` | `"rss"` |

**相关配置：**

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `collectors.rss.days_lookback` | `2` | 只抓取最近 N 天的文章 |
| `collectors.rss.max_items_per_feed_initial` | `20` | 每个 feed 初始抓取上限 |
| `collectors.rss.feeds[].max_items_initial` | 继承全局 | 单个 feed 的初始抓取上限覆盖 |

> **注意：** `max_items_per_feed`（默认 `3`）不在此阶段生效，而是在 Stage B 之后执行。

---

### 1.2 GitHub（`src/collectors/github_collector.py`）

**入口：** `collect_github(config, max_repos=None)`

**执行流程：**

1. 对 `trending_languages` 中每种语言（空则抓全语言）调用 `_scrape_trending()`
2. 请求 `https://github.com/trending[/<lang>]?since=<since>`，失败自动重试 3 次（退避 1.2n 秒）
3. 用 BeautifulSoup 解析 `article.Box-row`，提取 repo 名称、描述、stars、今日新增 stars、语言
4. 跨语言 URL 去重

**每条 item 产出的字段：**

| 字段 | 来源 |
|---|---|
| `title` | `owner/repo` |
| `url` | `https://github.com/owner/repo` |
| `description` | repo 描述 |
| `stars` | 总 star 数（int） |
| `stars_gained` | 今日新增（字符串） |
| `language` | 编程语言 |
| `source` | `"github"` |

**相关配置：**

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `collectors.github.max_repos` | `25` | 最多抓取的 repo 数 |
| `collectors.github.trending_since` | `"daily"` | `daily` / `weekly` / `monthly` |
| `collectors.github.trending_languages` | `[]` | 空列表 = 全语言 |

---

### 1.3 YouTube（`src/collectors/youtube_collector.py`）

**入口：** `collect_youtube(config, focus="", max_total=None)`

**两条抓取路径：**

**路径 A — 订阅频道：**
1. 通过 YouTube Data API 获取频道上传播放列表
2. 批量拉取视频列表，过滤 `days_lookback` 天内的视频
3. 批量获取播放量（每批 50 个）
4. 按播放量排序（`sort_by="views"`）或按时间排序，取前 `max_results_per_channel` 条

**路径 B — 关键词搜索（默认关闭）：**
1. 先让 AI 根据 `focus` 生成最多 5 个搜索关键词
2. 对每个关键词调用 YouTube Search API
3. 同样按播放量排序，取前 `max_search_results` 条

> **字幕抓取** 不在此阶段进行，而是在 Stage B 之后、Stage 2 评分之前按需获取（尝试中文 → 英文 → 自动生成字幕，截取前 2000 字符）。

**每条 item 产出的字段：**

| 字段 | 来源 |
|---|---|
| `video_id` | API 返回 |
| `title` | snippet.title |
| `url` | `https://www.youtube.com/watch?v={video_id}` |
| `channel` | snippet.channelTitle |
| `published_at` | snippet.publishedAt |
| `view_count` | statistics.viewCount |
| `description` | snippet.description（前 300 字符） |
| `transcript_snippet` | `""`（后续填充） |
| `source` | `"youtube"` |

**相关配置：**

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `collectors.youtube.days_lookback` | `3` | 只抓取最近 N 天的视频 |
| `collectors.youtube.max_results_per_channel` | `5` | 每个频道最多保留的视频数 |
| `collectors.youtube.sort_by` | `"views"` | 排序方式 |
| `collectors.youtube.enable_keyword_search` | `false` | 是否启用关键词搜索路径 |
| `collectors.youtube.max_search_results` | `10` | 关键词搜索每关键词上限 |

---

### 1.4 Agent 工具层（`src/agent/tools.py`）

每个 `collect_*` 工具在执行后，通过 `_merge_items(existing, new)` 将结果追加到 `rt.state["raw_items"]`。

**去重键：**
- YouTube → `youtube::{video_id}`
- GitHub → `github::{owner/repo}`
- 其他优先使用规范化 URL
- 无 URL 时回退到 `source::{normalized_title}`

多次调用不同 collect 工具时，同一条目不会重复入 `raw_items`。

---

## 二、AI 筛选与评分阶段

**入口：** `summarize_items(raw_items, config, ...)` — `src/ai/summarizer.py`

**全局上限（优先于各阶段限制）：**

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ai.max_items_per_digest` | `15` | 最终进入 digest 的条目总上限 |
| `ai.min_relevance_score` | `5` | 评分低于此值的条目被丢弃 |
| `ai.max_workers` | `5` | Stage 2 并发线程数 |

---

### Stage A — 过滤历史已入选内容

**目标：** 过滤掉过去任意一次已经入选过 digest 的内容，避免重复推送。

**执行方式：**

1. 从 `raw_items + item_annotations` 读取所有 `selected_for_digest=1` 的历史 `dedup_key`
2. 同时兼容旧数据中的 legacy key 和当前 canonical key
3. 当前轮 `raw_items` 若命中这些 key，则直接跳过

**说明：** `data/history/digest_*.json` 仍会持续写入，但只用于归档与回看，不再参与主筛选决策。

---

### Stage 1 — 批量标题筛选（`src/ai/filter.py`）

**目标：** 单次 AI 调用，从大量候选中快速筛选出值得精读的条目。

**AI 输入：** 编号列表 `[i] [SOURCE] 标题 — 描述[:80字符]`，附带：
- `focus` 关注方向（如有）
- 最多 3 条用户口味示例（来自 `feedback.db`，评分 ≥ 4）

**AI 输出：** `{"selected": [0, 3, 7, ...]}`，最多选 `min(max_output × 2, len(candidates))` 条。

**来源最小保障（选后执行）：** 若某来源（如 GitHub、YouTube）的候选数不足 `ai.min_items_per_source` 配置的最小值，从 `items_after_history` 池中强制补充该来源的条目。

---

### Stage B — 跨来源去重（`src/ai/dedup.py`）

**目标：** 处理同一事件被多个来源报道的情况，只保留一条最优代表。

**执行方式：**

1. 先计算一份确定性 `fallback_result`：规范化 URL 合并同 URL 条目，再用标题相似度 ≥ 0.97 的规则合并明显重复
2. 始终调用 AI 做当前批次跨源去重判断
3. 如果 AI 失败或结果异常，回退到 `fallback_result`
4. 如果 AI 结果比程序规则更宽松（保留条数更多），也回退到 `fallback_result`

**Stage B 之后执行 RSS 每 feed 上限：**

- 按 `feed_title` 分组，每组最多保留 `collectors.rss.max_items_per_feed`（默认 `3`）条
- 超出的条目直接丢弃

**候选补充（如数量不足）：** 若当前候选数 < `max_output`，让 AI 从剩余池中挑选补充条目（`ai_pick_fill_candidates()`），RSS per-feed 上限同样适用。

**YouTube 字幕填充：** 对所有 YouTube 候选调用字幕 API，将结果写入 `item["transcript_snippet"]`（最多 2000 字符），供 Stage 2 评分使用。

---

### Stage 2 — 并行评分与摘要（`src/ai/scorer.py`）

**目标：** 对每条候选进行独立 AI 评分，产出结构化摘要。

**系统提示词包含：**
- 1-10 分评分标准（9-10 极高相关，7-8 较高，5-6 边界，1-4 无关/低质）
- `focus` 关注方向加权（如有）
- 最多 `ai.taste_examples_limit`（默认 8）条口味示例（来自 `feedback.db`）

**每条 item 发给 AI 的内容：**

| 来源 | 提供给 AI 的字段 |
|---|---|
| RSS | feed_title, content_snippet（前 800 字符） |
| GitHub | stars, stars_gained, language, description, readme_snippet（前 800 字符） |
| YouTube | channel, view_count, description, transcript_snippet（前 800 字符） |

**AI 返回：** `{"score": 7, "summary": "...", "reason": "..."}`

**并发：** 所有候选同时提交到 `ThreadPoolExecutor`（`max_workers` 线程），`as_completed()` 收集结果。

**评分后组装（选取最终条目）：**
1. 按评分倒序排列
2. 各来源按等比分配槽位（`max_output / 来源数`），剩余槽位由高分条目填充
3. 再次执行来源最小保障（`enforce_source_minimums()`）：不足时从低分池补充，必要时驱逐超额来源的最低分条目以腾出位置
4. 截断至 `max_output`，按评分排序

**每条 item 新增字段：** `ai_score`、`ai_summary`、`ai_reason`

---

## 三、今日要点生成（`src/ai/digest.py`）

**入口：** `generate_digest_summary(news_items, config, focus)`

单次 AI 调用（`max_tokens=600`），对 Stage 2 输出的所有条目生成整体总结段落。

**提示词要求 AI：**
- 提炼 3-5 条最值得关注的主题或趋势
- 每条以 `•` 开头，1-2 句，覆盖不同领域
- 不使用 Markdown 标题

输出存入 `rt.state["digest_summary"]`，最终写入 `digests.summary_text` 列。

---

## 四、入库阶段（`src/web/content.py` + `src/web/store.py`）

### 4.1 索引构建（`build_indexed_items()`）

将 `raw_items`（全量）和 `news_items`（AI 筛选后）合并为统一格式：

- 以 `(source, url)` 为 key 在 `news_items` 中查找，有匹配则 `selected_for_digest=True`，并将 AI 字段（score/summary/reason）合并进去
- **全量 raw_items 都会入库**，未被选中的条目 `selected_for_digest=0`，AI 字段为 NULL

### 4.2 写入数据库（`store.upsert_raw_items()` + `store.replace_annotations_for_job()`）

原子操作分两步：

1. `upsert_raw_items()` 按 canonical `dedup_key` 写入或更新 `raw_items`
2. `replace_annotations_for_job()` 先删除该 `job_run_id` 的旧 annotation，再插入本轮 annotation

**`raw_items` 表核心字段：**

| 列 | 来源 | 说明 |
|---|---|---|
| `source` | `raw.source` | rss / github / youtube |
| `title` | `raw.title` | |
| `url` | `raw.url` | |
| `dedup_key` | canonical identity | 统一内容身份键 |
| `external_id` | 计算得出 | YouTube: video_id；GitHub: owner/repo；其他: 空 |
| `published_at` | `raw.published_at` | |
| `first_seen_at` | 首次入库时间 | |
| `last_seen_at` | 最近一次再次看到该条目的时间 | |
| `seen_count` | 自动累加 | 该条目被采集到的次数 |

**`item_annotations` 表写入的字段：**

| 列 | 来源 | 说明 |
|---|---|---|
| `raw_item_id` | `raw_items.id` | |
| `job_run_id` | 当前任务 | |
| `selected_for_digest` | bool → 0/1 | |
| `ai_score` | `selected.ai_score` | 未选中为 NULL |
| `ai_summary` | `selected.ai_summary` | 未选中为 NULL |
| `ai_reason` | `selected.ai_reason` | 未选中为 NULL |

---

## 五、数据库文件一览

| 文件 | 内容 |
|---|---|
| `data/app.db` | `job_runs`、`job_logs`、`digests`、`raw_items`、`item_annotations`、`deep_summaries` |
| `data/agent_sessions.db` | Agent 对话历史、tool call 日志、session state |
| `data/feedback.db` | 用户评分（1-5），用于生成 AI 评分口味示例 |
| `data/history/digest_*.json` | 历次运行归档，用于审计与回看 |

---

## 六、配置项速查

### 抓取阶段

| 配置项 | 默认值 | 阶段 |
|---|---|---|
| `collectors.rss.days_lookback` | `2` | RSS 抓取 |
| `collectors.rss.max_items_per_feed_initial` | `20` | RSS 抓取（每 feed 初始上限） |
| `collectors.rss.max_items_per_feed` | `3` | Stage B 后 RSS 每 feed 最终上限 |
| `collectors.github.max_repos` | `25` | GitHub 抓取 |
| `collectors.github.trending_since` | `"daily"` | GitHub 时间范围 |
| `collectors.youtube.days_lookback` | `3` | YouTube 抓取 |
| `collectors.youtube.max_results_per_channel` | `5` | YouTube 每频道上限 |

### 筛选与评分阶段

| 配置项 | 默认值 | 阶段 |
|---|---|---|
| `ai.max_items_per_digest` | `15` | 全局最终条目上限 |
| `ai.min_relevance_score` | `5` | Stage 2 评分过滤阈值 |
| `ai.max_workers` | `5` | Stage 2 并发线程数 |
| `ai.min_items_per_source` | `{github:5, youtube:2}` | 来源最小保障 |
| `ai.taste_examples_limit` | `8` | 口味示例最大条数 |
| `ai.max_tokens` | `512` | 每次 AI 调用的 token 上限 |
