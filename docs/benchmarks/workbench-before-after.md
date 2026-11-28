# WuwaTerm-Windows 重构前后性能对照基准报告

本报告记录 PR #69（工作台架构模块化、Qt Model/View 列表迁移与 Fluent 视觉升级）改造前后的同口径离线量化对比数据。

## 基线与测量环境

| 项目 | 值 |
| --- | --- |
| 改造前基线 SHA | `e6835f38dfcc0ac217bc0cc8c608ad6570668428`（`e6835f3`，tag `v0.9.0`） |
| 改造后对照 SHA | `bd239c741e123a4641f2bffe9f0a580bc3859658`（`bd239c7`，PR #69 合并后 `main`） |
| 运行环境 | WSL Linux (`x86_64`), Python 3.12.3, PySide6 6.12.0, `QT_QPA_PLATFORM=offscreen` |
| 隔离条件 | 独立临时配置目录（`APPDATA`/`LOCALAPPDATA`/`XDG_*`）、全流程（含冷启动子进程）内存 `KeyringBackend`、零网络请求（`ApiClient(None)`） |
| 基准脚本 | `scripts/compare_perf_revisions.py`（补充覆盖 `scripts/benchmark_windows_perf.py` 未覆盖的 6 项全口径指标） |

> 注：受限于当前宿主为 WSL Linux `offscreen` 平台，本报告测得的是跨版本真实 Qt widget / controller / 序列化与列表管线对比；Windows 原生 DWM / DirectWrite 合成与真实 GPU 帧率需在 Windows 桌面环境额外复测。

## 6 项核心指标前后实测对照

除特别说明外，耗时与内存均取标准采样下的 **P50（中位数）**，同时列出输入延迟与关键项的 **P95**。

| 指标 | 子项 / 口径 | 改造前 (`e6835f3`) | 改造后 (`bd239c7`) | 变化幅度 | 说明 |
| --- | --- | ---: | ---: | ---: | --- |
| **1. 冷启动时间** | 进程导入 + `MainWindow` 就绪（`cold_start_ms` P50） | 1347.93 ms | 1350.60 ms | +0.20% | 独立子进程冷启（含内存 `_MemoryKeyring` 隔离、`theme.apply_theme`、`qasync` 与首次 `show()+processEvents()`）；P95 为 2111.23 ms → 1966.74 ms |
| | 其中 `MainWindow` 构造 + 首次显示（`window_construct_ms` P50） | 522.44 ms | 495.34 ms | -5.19% | Qt 与主题就绪后的主工作台创建开销 |
| **2. 空闲常驻内存** | 刚启动稳定后 `VmRSS`（`idle_rss_mib` P50） | 146.40 MiB | 149.29 MiB | +1.97% | 新建空闲子进程（不探测宿主 keyring）稳定后 `/proc/self/status` `VmRSS`（`tracemalloc` 当前 30.76 → 31.75 MiB） |
| **3. 输入延迟** | 500 段大稿编辑框按键派发 P50 / P95 | 219.36 / 260.88 ms | 163.79 / 187.83 ms | -25.33% / -28.00% | 约 24.2 万源字 / 53.4 万译字、`TRACK_SEGMENTS` 开启、`MainWindow` 页脚状态联动，30 次 `QTest.keyClick` |
| | 紧随其后的状态同步响应 P50 / P95 | 123.24 / 152.45 ms | 109.18 / 132.92 ms | -11.40% / -12.81% | `_refresh_derived()` + `_follow_caret()` + `status_summary()` 全量同步 |
| **4. 长稿列表刷新** | 200 段工作台分段列表加载 | 108.65 ms | 77.03 ms | -29.10% | 完整 `ManuscriptView` 装载 + `segment_list` 刷新 |
| | 500 段工作台分段列表加载 | 324.36 ms | 200.45 ms | -38.20% | 同上（500 个独立 `SegmentPair`） |
| | 1000 段工作台分段列表加载 | 680.76 ms | 559.05 ms | -17.88% | 同上（1000 个独立 `SegmentPair`） |
| | 200 / 500 / 1000 段术语报告加载 | 30.15 / 71.72 / 116.02 ms | 26.84 / 53.61 / 101.31 ms | -10.98% / -25.25% / -12.68% | 完整 `ManuscriptView._show_term_report()`（含 `findings` 与 `term_groups` 构建） |
| | 200 / 500 / 1000 段视图过滤切换周期 | 76.62 / 247.97 / 396.59 ms | 71.99 / 228.17 / 339.69 ms | -6.03% / -7.98% / -14.35% | `term_filter_combo` 依次切换 `all → confirmed → pending → unhandled` |
| | 200 / 500 / 1000 行纯列表控件装载与过滤周期 | 装载 4.55 / 12.63 / 23.68 ms；过滤 10.84 / 16.97 / 34.45 ms | 装载 5.15 / 13.92 / 23.97 ms；过滤 10.36 / 21.84 / 34.00 ms | 1000 行装载 +1.22%，过滤 -1.29% | 改造前 `_WrappedListWidget`，改造后 `ModelListView`（均以 `review_states.for_verdict()` 渲染 4 类真实状态徽章） |
| **5. 滚动性能** | 1000 行视口全遍历滚动 + `viewport().render`（25 步） | 56.09 ms | 61.28 ms | +9.26% | 从顶到底 25 步推进滚动条并在每步执行 `viewport().render()`；两者均渲染 4 类状态图标 |
| | 1000 行 5 次跨段跳转（`scrollToItem` + 选中） | 3.21 ms | 3.53 ms | +10.10% | 覆盖 `0 / 250 / 500 / 750 / 999` 行 |
| | 1000 行视口宽度变更重排 + 渲染（`320 ↔ 380 px`） | 49.11 ms | 62.69 ms | +27.66% | `CachedRowDelegate` 宽度变更时清空缓存并经 proxy model 重新测高 |
| **6. 恢复草稿开销** | 500 段 v5 大型草稿反序列化（1767.8 KiB `draft.json`） | 20.45 ms | 19.20 ms | -6.10% | `recovery.load_recovery()` 含 JSON 解析与 `manuscript.parse_workfile` 校验 |
| | 恢复至 `ManuscriptView` 工作台（含控件与列表刷新） | 681.11 ms | 446.29 ms | -34.48% | `_restore_recovery_draft(state)` + `processEvents()` |
| | 恢复草稿端到端总耗时 | 701.56 ms | 470.16 ms | -32.98% | `load_recovery` + `_restore_recovery_draft` 合计 |

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

- **明显收益**：编辑器按键派发（`-25.3%` P50 / `-28.0%` P95）与紧随其后的状态同步（`-11.4%` P50 / `-12.8%` P95）、工作台 200/500/1000 段列表加载与术语报告/过滤刷新（`-6.0% ~ -38.2%`），以及 500 段 v5 草稿恢复至工作台（`-34.5%`）均取得实质提升；主窗口构建耗时下降 `5.2%`。
- **可观测代价**：模块拆分使空闲 RSS 增加约 `2.9 MiB`（`+1.97%`），冷启动总耗时持平（`+0.20%`）。裸列表控件在同口径渲染 4 类状态图标时，由于 `QSortFilterProxyModel` 映射与 `CachedRowDelegate` 额外绘制 Fluent 选中竖条与 Python 侧 `sizeHint` 代理开销，1000 行视口滚动遍历与宽度重排分别慢 `5.2 ms`（`+9.3%`）和 `13.6 ms`（`+27.7%`）；而完整 `ManuscriptView` 工作台下的列表加载与过滤切换则整体更快。
- **待优化点**：`ManuscriptView._refresh_term_lists()` 中 `self.findings` 仍逐行调用 `setRowHidden(i, not matches)` 并伴随 `term_groups` 全量重建；`ModelListView` 宽度变更时清空全部 `size_cache` 并经 proxy model 重测全部行高，可作为后续针对性优化项。

## 复现命令

```bash
# 快速烟雾检查（单次采样，退出码 0）
QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py --repo-root . --quick --json

# 完整跨版本对比（分别导出 e6835f3 与 bd239c7 至临时目录对比，结束后自动清理）
QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py \
  --before-ref e6835f3 --after-ref bd239c7 \
  --samples 3 --cold-samples 5 --keystrokes 30 --items 1000 --scroll-steps 25 --json
```
