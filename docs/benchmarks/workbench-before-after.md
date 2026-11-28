# WuwaTerm-Windows 重构前后性能对照基准报告

本报告记录 PR #69（工作台架构模块化、Qt Model/View 列表迁移与 Fluent 视觉升级）改造前后的同口径离线量化对比数据。

## 基线与测量环境

| 项目 | 值 |
| --- | --- |
| 改造前基线 SHA | `e6835f38dfcc0ac217bc0cc8c608ad6570668428`（`e6835f3`，tag `v0.9.0`） |
| 改造后对照 SHA | `bd239c741e123a4641f2bffe9f0a580bc3859658`（`bd239c7`，PR #69 合并后 `main`） |
| 运行环境 | WSL Linux (`x86_64`), Python 3.12.3, PySide6 6.12.0, `QT_QPA_PLATFORM=offscreen` |
| 隔离条件 | 独立临时配置目录（`APPDATA`/`LOCALAPPDATA`/`XDG_*`）、内存 `KeyringBackend`、零网络请求（`ApiClient(None)`） |
| 基准脚本 | `scripts/compare_perf_revisions.py`（补充覆盖 `scripts/benchmark_windows_perf.py` 未覆盖的 6 项全口径指标） |

> 注：受限于当前宿主为 WSL Linux `offscreen` 平台，本报告测得的是跨版本真实 Qt widget / controller / 序列化与列表管线对比；Windows 原生 DWM / DirectWrite 合成与真实 GPU 帧率需在 Windows 桌面环境额外复测。

## 6 项核心指标前后实测对照

除特别说明外，耗时与内存均取标准采样下的 **P50（中位数）**，同时列出输入延迟与关键项的 **P95**。

| 指标 | 子项 / 口径 | 改造前 (`e6835f3`) | 改造后 (`bd239c7`) | 变化幅度 | 说明 |
| --- | --- | ---: | ---: | ---: | --- |
| **1. 冷启动时间** | 进程导入 + `MainWindow` 就绪（`cold_start_ms` P50） | 1259.09 ms | 1230.06 ms | -2.31% | 独立子进程冷启，含 Python/Qt 导入、`theme.apply_theme`、`qasync` 与主窗口首次 `show()+processEvents()`；P95 为 1355.19 ms → 1808.33 ms |
| | 其中 `MainWindow` 构造 + 首次显示（`window_construct_ms` P50） | 561.68 ms | 492.57 ms | -12.30% | Qt 与主题就绪后的主工作台创建开销 |
| **2. 空闲常驻内存** | 刚启动空闲状态 RSS（`idle_rss_mib` P50） | 155.91 MiB | 158.33 MiB | +1.55% | 新建空闲子进程稳定后 `/proc/self/status` `VmRSS`（`tracemalloc` 当前 32.42 → 33.43 MiB） |
| **3. 输入延迟** | 500 段大稿编辑框按键派发 P50 / P95 | 189.75 / 209.91 ms | 133.95 / 141.78 ms | -29.41% / -32.46% | 约 24.2 万源字 / 53.4 万译字、`TRACK_SEGMENTS` 开启、`MainWindow` 页脚状态联动，30 次 `QTest.keyClick` |
| | 紧随其后的状态同步响应 P50 / P95 | 105.54 / 116.50 ms | 101.58 / 120.77 ms | -3.75% / +3.66% | `_refresh_derived()` + `_follow_caret()` + `status_summary()` 全量同步 |
| **4. 长稿列表刷新** | 200 段工作台分段列表加载 | 97.04 ms | 72.59 ms | -25.19% | 完整 `ManuscriptView` 装载 + `segment_list` 刷新 |
| | 500 段工作台分段列表加载 | 248.95 ms | 179.49 ms | -27.90% | 同上（500 个独立 `SegmentPair`） |
| | 1000 段工作台分段列表加载 | 632.77 ms | 358.09 ms | -43.41% | 同上（1000 个独立 `SegmentPair`） |
| | 200 / 500 / 1000 段术语报告加载 | 26.86 / 54.19 / 107.22 ms | 24.82 / 50.29 / 191.61 ms | -7.58% / -7.20% / +78.71% | 1000 段改造后 3 次样本为 97.02 / 191.61 / 197.66 ms（`findings` 仍逐行 `setRowHidden` 且含 `term_groups` 全量构建） |
| | 200 / 500 / 1000 段视图过滤切换周期 | 69.36 / 219.20 / 354.97 ms | 67.53 / 220.16 / 322.64 ms | -2.64% / +0.44% / -9.11% | `term_filter_combo` 依次切换 `all → confirmed → pending → unhandled` |
| | 200 / 500 / 1000 行纯列表控件装载与过滤周期 | 装载 3.87 / 9.44 / 22.89 ms；过滤 6.72 / 14.23 / 31.80 ms | 装载 4.26 / 11.88 / 21.73 ms；过滤 8.02 / 17.26 / 29.66 ms | 1000 行装载 -5.06%，过滤 -6.74% | 改造前 `_WrappedListWidget`，改造后 `ModelListView`（`QAbstractListModel` + `ReviewFilterProxyModel`） |
| **5. 滚动性能** | 1000 行视口全遍历滚动 + `viewport().render`（25 步） | 55.66 ms | 45.33 ms | -18.55% | 从顶到底 25 步推进滚动条并在每步执行实际 `viewport().render()` 触发 delegate 绘制 |
| | 1000 行 5 次跨段跳转（`scrollToItem` + 选中） | 3.26 ms | 2.73 ms | -16.21% | 覆盖 `0 / 250 / 500 / 750 / 999` 行 |
| | 1000 行视口宽度变更重排 + 渲染（`320 ↔ 380 px`） | 52.31 ms | 55.33 ms | +5.77% | `CachedRowDelegate` 宽度变更时清空缓存并经 proxy model 重新测高 |
| **6. 恢复草稿开销** | 500 段 v5 大型草稿反序列化（1767.8 KiB `draft.json`） | 20.61 ms | 17.17 ms | -16.66% | `recovery.load_recovery()` 含 JSON 解析与 `manuscript.parse_workfile` 校验 |
| | 恢复至 `ManuscriptView` 工作台（含控件与列表刷新） | 531.09 ms | 336.78 ms | -36.59% | `_restore_recovery_draft(state)` + `processEvents()` |
| | 恢复草稿端到端总耗时 | 551.13 ms | 353.96 ms | -35.78% | `load_recovery` + `_restore_recovery_draft` 合计 |

## 已有单版本脚本的缺口对照

`scripts/benchmark_windows_perf.py`（合入于 PR #69）原先仅覆盖单版本的 `MainWindow` 构造（`window_construct_ms`）、16.9k 字按键派发（`Pair` 实际折叠为 23 段而非 500 独立段）、1024 行 `ModelListView` 刷新与 5 次跳转、span 定位，以及基准结束后的后置 RSS；缺少：

1. 包含模块导入与 Qt/主题初始化的真实冷启动子进程计时；
2. 刚启动未加载大稿件时的真实空闲 RSS；
3. 按键后的 `_refresh_derived` / `_follow_caret` / `status_summary` 状态同步响应；
4. 200/500/1000 真实独立分段的大稿加载与过滤器切换；
5. 全视口滚动推进与 `viewport().render()` delegate 绘制、宽度变更重排；
6. 大型 v5 `draft.json` 反序列化与工作台恢复耗时；
7. 兼容 `e6835f3`（`_WrappedListWidget`）与 `bd239c7`（`ModelListView`）的跨版本同口径对照能力。

新增 `scripts/compare_perf_revisions.py` 补齐了上述 6 项同口径基准与跨版本对比能力，并保留 `scripts/benchmark_windows_perf.py` 的原有接口和单测兼容。

## 结果解读

- **明显收益**：编辑器按键派发（`-29.4%` P50 / `-32.5%` P95）、1000 段工作台分段列表加载（`-43.4%`）、500 段 v5 草稿恢复至工作台（`-36.6%`）与 1000 行全视口滚动绘制（`-18.6%`）均有显著改善；主窗口构建耗时下降 `12.3%`。
- **微小代价**：模块拆分后冷启动总导入文件数增加，空闲 RSS 增加约 `2.4 MiB`（`+1.55%`），`CachedRowDelegate` 在宽度变化清缓存重排时略慢约 `3 ms`（`+5.77%`）；200/500 行小规模裸控件插入略慢 1–2 ms，在 1000 行以上反超。
- **待优化点**：`ManuscriptView._refresh_term_lists()` 中 `self.findings` 仍逐行调用 `setRowHidden(i, not matches)` 并伴随 `term_groups` 全量重建，是 1000 段术语报告加载与过滤切换的主要耗时点。

## 复现命令

```bash
# 快速烟雾检查（单次采样，退出码 0）
QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py --repo-root . --quick --json

# 完整跨版本对比（自动将 e6835f3 导出至临时目录并对比当前 HEAD，结束后自动清理）
QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py \
  --before-ref e6835f3 --after-ref HEAD \
  --samples 3 --cold-samples 5 --keystrokes 30 --items 1000 --scroll-steps 25 --json
```
