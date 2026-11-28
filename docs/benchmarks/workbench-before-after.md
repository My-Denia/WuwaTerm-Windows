# WuwaTerm-Windows 重构前后性能对照基准报告

本报告记录 PR #69（工作台架构模块化、Qt Model/View 列表迁移与 Fluent 视觉升级）改造前后的同口径离线量化对比数据。

## 基线与测量环境

| 项目 | 值 |
| --- | --- |
| 改造前基线 SHA | `e6835f38dfcc0ac217bc0cc8c608ad6570668428`（`e6835f3`，tag `v0.9.0`） |
| 改造后对照 SHA | `bd239c741e123a4641f2bffe9f0a580bc3859658`（`bd239c7`，PR #69 合并后 `main`） |
| 运行环境 | WSL Linux (`x86_64`), Python 3.12.3, PySide6 6.12.0, `QT_QPA_PLATFORM=offscreen` |
| 隔离条件 | 独立临时配置目录（`APPDATA`/`LOCALAPPDATA`/`XDG_*`）、全流程（含冷启动子进程）内存 `KeyringBackend`、零网络请求（`ApiClient(None)`）；冷启动与空闲 RSS 采样不开启 `tracemalloc` |
| 基准脚本 | `scripts/compare_perf_revisions.py`（补充覆盖 `scripts/benchmark_windows_perf.py` 未覆盖的 6 项全口径指标） |

> 注：受限于当前宿主为 WSL Linux `offscreen` 平台，本报告测得的是跨版本真实 Qt widget / controller / 序列化与列表管线对比；Windows 原生 DWM / DirectWrite 合成与真实 GPU 帧率需在 Windows 桌面环境额外复测。

## 6 项核心指标前后实测对照

除特别说明外，耗时与内存均取标准采样下的 **P50（中位数）**，同时列出输入延迟与关键项的 **P95**。

| 指标 | 子项 / 口径 | 改造前 (`e6835f3`) | 改造后 (`bd239c7`) | 变化幅度 | 说明 |
| --- | --- | ---: | ---: | ---: | --- |
| **1. 冷启动时间** | 进程导入 + `MainWindow` 就绪（`cold_start_ms` P50） | 632.03 ms | 441.37 ms | -30.17% | 独立无 `tracemalloc` 子进程冷启（含内存 `_MemoryKeyring` 隔离、`theme.apply_theme`、`qasync` 与首次 `show()+processEvents()`）；P95 为 726.89 ms → 659.48 ms |
| | 其中 `MainWindow` 构造 + 首次显示（`window_construct_ms` P50） | 295.89 ms | 239.64 ms | -19.01% | Qt 与主题就绪后的主工作台创建开销 |
| **2. 空闲常驻内存** | 刚启动稳定后 `VmRSS`（`idle_rss_mib` P50） | 113.69 MiB | 115.27 MiB | +1.39% | 无 `tracemalloc` 独立子进程（不探测宿主 keyring）稳定后 `/proc/self/status` `VmRSS`；单独 `tracemalloc` 子进程测得 Python 堆 30.77 → 31.75 MiB |
| **3. 输入延迟** | 500 段大稿编辑框按键派发 P50 / P95 | 221.36 / 356.86 ms | 185.24 / 208.43 ms | -16.32% / -41.59% | 约 24.2 万源字 / 53.4 万译字、`TRACK_SEGMENTS` 开启、`MainWindow` 页脚状态联动，30 次 `QTest.keyClick` |
| | 紧随其后的状态同步响应 P50 / P95 | 119.56 / 225.01 ms | 121.81 / 153.22 ms | +1.88% / -31.91% | `_refresh_derived()` + `_follow_caret()` + `status_summary()` 全量同步 |
| **4. 长稿列表刷新** | 200 段工作台分段列表加载 | 104.29 ms | 98.55 ms | -5.50% | 完整 `ManuscriptView` 装载 + `segment_list` 刷新 |
| | 500 段工作台分段列表加载 | 288.16 ms | 190.10 ms | -34.03% | 同上（500 个独立 `SegmentPair`） |
| | 1000 段工作台分段列表加载 | 626.77 ms | 509.95 ms | -18.64% | 同上（1000 个独立 `SegmentPair`） |
| | 200 / 500 / 1000 段逐段术语报告加载 | 72.43 / 185.96 / 476.25 ms | 93.22 / 183.48 / 410.07 ms | +28.70% / -1.33% / -13.90% | 真实逐段报告 `_show_segment_term_report()`（含每段 2 条发现、非空 `term_groups` 与 `findings` 构建） |
| | 200 / 500 / 1000 段视图过滤切换周期 | 291.58 / 948.51 / 2603.47 ms | 268.18 / 720.89 / 1985.05 ms | -8.03% / -24.00% / -23.75% | `term_filter_combo` 依次切换 `all → confirmed → pending → unhandled`（每步均含非空 `term_groups` 重建） |
| | 200 / 500 / 1000 行纯列表控件装载与过滤周期 | 装载 4.25 / 12.03 / 25.08 ms；过滤 8.95 / 19.10 / 28.67 ms | 装载 6.85 / 10.81 / 21.95 ms；过滤 11.73 / 15.80 / 28.03 ms | 1000 行装载 -12.47%，过滤 -2.23% | 改造前 `_WrappedListWidget`，改造后 `ModelListView`（均以 `review_states.for_verdict()` 渲染 4 类真实状态徽章） |
| **5. 滚动性能** | 1000 行视口全遍历滚动 + `viewport().render`（25 步） | 42.74 ms | 45.59 ms | +6.67% | 从顶到底 25 步推进滚动条并在每步执行 `viewport().render()`；两者均渲染 4 类状态图标 |
| | 1000 行 5 次跨段跳转（`scrollToItem` + 选中） | 2.90 ms | 2.63 ms | -9.27% | 覆盖 `0 / 250 / 500 / 750 / 999` 行 |
| | 1000 行视口宽度变更重排 + 渲染（`320 ↔ 380 px`） | 44.10 ms | 50.89 ms | +15.39% | `CachedRowDelegate` 宽度变更时清空缓存并经 proxy model 重新测高 |
| **6. 恢复草稿开销** | 500 段 v5 大型草稿反序列化（1900.44 KiB `draft.json`） | 23.35 ms | 22.34 ms | -4.34% | `recovery.load_recovery()` 含 JSON 解析与 `manuscript.parse_workfile` 校验（每段含 2 条发现） |
| | 恢复至 `ManuscriptView` 工作台（含控件与列表刷新） | 629.27 ms | 415.37 ms | -33.99% | `_restore_recovery_draft(state)` + `processEvents()` |
| | 恢复草稿端到端总耗时 | 652.62 ms | 437.70 ms | -32.93% | `load_recovery` + `_restore_recovery_draft` 合计 |

## 已有单版本脚本的缺口对照

`scripts/benchmark_windows_perf.py`（合入于 PR #69）原先仅覆盖单版本的 `MainWindow` 构造（`window_construct_ms`）、16.9k 字按键派发（`Pair` 实际折叠为 23 段而非 500 独立段）、1024 行 `ModelListView` 刷新与 5 次跳转、span 定位，以及基准结束后的后置 RSS；缺少：

1. 包含模块导入与 Qt/主题初始化的真实冷启动子进程计时（且需与 `tracemalloc` 分离）；
2. 刚启动未加载大稿件且未开 `tracemalloc` 时的真实空闲 RSS；
3. 按键后的 `_refresh_derived` / `_follow_caret` / `status_summary` 状态同步响应；
4. 200/500/1000 真实独立分段的大稿加载、逐段术语报告（非空 `term_groups`）与过滤器切换；
5. 全视口滚动推进与 `viewport().render()` delegate 绘制、宽度变更重排；
6. 大型 v5 `draft.json` 反序列化与工作台恢复耗时；
7. 兼容 `e6835f3`（`_WrappedListWidget`）与 `bd239c7`（`ModelListView`）的跨版本同口径对照能力。

新增 `scripts/compare_perf_revisions.py` 补齐了上述 6 项同口径基准与跨版本对比能力，并保留 `scripts/benchmark_windows_perf.py` 的原有接口和单测兼容。

## 结果解读

- **明显收益**：冷启动时间下降 `30.2%`（P50 `632.0 ms → 441.4 ms`）、500 段编辑器按键派发 P95 下降 `41.6%`（`356.9 ms → 208.4 ms`）、工作台 500/1000 段列表加载下降 `34.0% / 18.6%`、真实非空 `term_groups` 过滤切换在 500/1000 段分别下降 `24.0% / 23.8%`（1000 段 4 步切换节省 `618 ms`），500 段 v5 草稿恢复至工作台下降 `34.0%`。
- **可观测代价**：模块拆分使纯空闲 `VmRSS` 增加约 `1.6 MiB`（`+1.39%`）。1000 行纯列表控件下，`ModelListView` 批量装载与过滤略快于 `_WrappedListWidget`（`-12.5%` / `-2.2%`），但因 `QSortFilterProxyModel` 代理层与 `CachedRowDelegate` 额外绘制 Fluent 选中竖条、并在宽度变更时清空缓存重测，25 步视口滚动绘制与宽度变更重排分别略慢 `2.9 ms`（`+6.7%`）与 `6.8 ms`（`+15.4%`）。
- **待优化点**：`ManuscriptView._refresh_term_lists()` 中 `self.findings` 仍逐行调用 `setRowHidden(i, not matches)` 并伴随 `term_groups` 全量重建（1000 段 4 步过滤耗时约 `1.99 s`）；`ModelListView` 宽度变更时清空全部 `size_cache` 并经 proxy model 重测全部行高，可作为后续针对性优化项。

## 复现命令

```bash
# 快速烟雾检查（单次采样，退出码 0）
QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py --repo-root . --quick --json

# 完整跨版本对比（分别导出 e6835f3 与 bd239c7 至临时目录对比，结束后自动清理）
QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py \
  --before-ref e6835f3 --after-ref bd239c7 \
  --samples 3 --cold-samples 5 --keystrokes 30 --items 1000 --scroll-steps 25 --json
```
