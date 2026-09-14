# 升级交付文档（MIS-101）

多维关系弧线 · 代码与功能升级项目（Linear，MIS-87～MIS-102）的交付总纲。
本文档回答三件事：**这次升级交付了什么、如何安全升级、如何回滚**。

## 1. 交付范围与结论

- 项目 16 个 issue 中 15 个已完成并置 Done（MIS-87～100、MIS-102）。
- **MIS-101 按缩窄范围交付**：回归确认与升级交付文档本文件完成；
  **真实 AstrBot 宿主端到端验收在本机未覆盖**（本机无部署宿主环境），
  已在 Linear 建后续承接 issue，验收清单见 `docs/HOST_COMPATIBILITY.md`。
- 所有变更已推送上游 master（`2680dc3`）；行为合同零漂移（见 §4）。

## 2. 版本矩阵（交付态）

| 项 | 值 | 说明 |
|---|---|---|
| 插件版本 | 0.1.0（metadata.yaml） | 版本号维持不变，是否随发布 bump 由维护者决定 |
| 配置版本 | CONFIG_VERSION = 7 | `config_manager.py`；v6→v7 迁移绑定策略字段（受保护备份） |
| 数据库版本 | SCHEMA_VERSION = 12（user_version） | `relation_store.py`；v11→v12 增加 binding_policy/binding_conflicts/end_reason，旧通用排他索引由策略化约束替代 |
| 宿主要求 | astrbot >= 4.26（metadata 声明） | 实测 4.26.0 / 4.27.0 / 4.28.0 |
| 运行时 | Python 3.12.10 + SQLite 3.49.1 | 交付验证环境 |
| 代码结构 | main.py + prompts/commands/pages_api 三模块拆分 | MIS-100 |

## 3. 升级路径（从上一发布版升级）

### 3.1 自动迁移（无需手工干预）

- **数据库**：启动时只读探测 `user_version`，在 `BEGIN IMMEDIATE` 事务内逐级迁移至 11，
  迁移后回读版本号校验，不一致即启动失败（快速暴露而非静默漂移）。
  涉及迁移链路上的新增表/索引（调度器状态、窗口索引、分页计数等）全部幂等。
- **配置**：CONFIG_VERSION 6 门禁；未知字段在保存时被点名拒绝；
  管理端编辑引入 config_revision 乐观并发（MIS-99），冲突返回明确错误不覆盖。
- **旧身份拆分修复**：`repair_legacy_identity_splits` 迁移路径保留，
  诊断计数可经 Pages `/migrations` 端点核对。

### 3.2 升级步骤

1. 备份：确认插件 `backups/` 目录内有近期全量快照
   （SQLite 快照 + config 副本 + manifest，含 schema/config 版本、
   integrity_check 与双文件 sha256，见 MIS-95）；无快照可先在 Pages `/backups`
   触发 `backup_now`。
2. 替换插件目录为本版代码，重载/重启宿主。
3. 健康检查（Pages 面板）：
   - `/health`：协议健康正常；
   - `/migrations`：schema 版本 = 11、迁移链无遗留告警；
   - `/overview`、`/accounts`：数据与升级前一致（账户数、审计行数抽查）。
4. 抽查一条群聊 + 一条私聊消息的注入与结算输出，比对
   `docs/BEHAVIOR_CONTRACT.md` 冻结样例。

### 3.3 回滚

- **数据**：从 `backups/` 选最近 manifest 完好的快照整目录恢复
  （先停插件，校验 manifest sha256，再覆盖数据库与 config）。
- **代码**：回退到升级前插件文件即可；**schema 只升不降**——
  降级插件版本必须配合数据回滚，不允许新库配旧码。

## 4. 验收证据汇总

| 证据 | 结果 | 位置 |
|---|---|---|
| 全量回归（venv + astrbot 4.28.0） | 206/206 通过（32.2s） | `python -m unittest discover -s tests` |
| 全量回归（venv + astrbot 4.26.0 声明下限） | 191/191 通过 | 见 BASELINE_VALIDATION.md 双版本矩阵 |
| 无宿主回归（系统 Python，无 astrbot 包） | 80/80 通过（core+p0） | `python -m unittest tests.test_core tests.test_p0` |
| 行为合同冻结样例复核 | 六类样例关键值与 `BEHAVIOR_CONTRACT.md` 一致，无漂移 | `tools/behavior_contract_samples.py` 复跑 |
| 外部复核缺陷修复轮（MIS-117） | 复核提出的 2 P1 + 7 P2 及补充 P1（bridge 参数契约）全部修复；复核协议脚本 4/4、存储脚本 2/2 转正通过；宿主注册链在 4.28.0 与 4.26.0 下限均为 16 处理器归属 / 14 命令可分发 | 提交 bf7ca20 / 8069867 / 92b74d5 / 42f8acb / 20be4d3 / 4bbf44b |
| 复验待修 B01/B02（MIS-121） | B01 图文顺序回归修复（合并回退仅对内容差异触发）+ 复核边界脚本 13/13 正式移植；B02 排他索引移入暂存副本，复制后零结构写入，容量受限场景恢复完整成功、三个复制前故障点活库零变化 | 提交 88a6009 / 0eb5859 |
| Pages 前端契约 | node 合成 DOM 驱动真实 `app.js`：纯 endpoint + 独立 params、去重含参数、翻页点击、配置冲突重建（12 断言） | `tests/pages_app_harness.mjs`、`tests/test_pages_js.py` |
| N 轮收尾（MIS-134 N01/N02） | 互斥竞争转为可推迟激活（StoreBusy→保留生效策略+可重试错误+正常注册）；staging 三退出路径全清理 | 提交本轮 |
| R 轮复核收尾（MIS-134 R01/R02） | 激活与恢复共享同一跨实例互斥（OS 字节范围锁+进程内 RLock，持续持有全程，进程退出 OS 释放）；全零增量不再误判 applied；激活串行化于恢复之后、epoch 严格递增，两场景真实交错回归 | 提交本轮 |
| 复验 R 轮（MIS-134 R01-R05） | 恢复互斥改库外文件锁+所有权贯穿+精确回滚快照；健康按整轮实际结果分类（binding_rejected 入库、暂停收据零变化）；五类型建立展示+隐藏投影作用于事件含义 | 提交 afe52b6 |
| 复核修复轮（MIS-134） | C01-C09 全部修复：并发恢复租约+只读复制源+事后验证回滚、冲突库启动诊断（谓词级）、诊断键含类型、暂停实际值资格、冷却下沉共享阶梯、llm_receipt 回执不计黑名单、记录展示建立/升级/拒绝、来源只读；复核方探针复跑：升级 12/12、冲突库 3×loaded、收据/来源旧缺陷断言不再成立 | 提交 a877e4d |
| 关系规则调整轮（MIS-123～128） | 可选排他/冷却、同人原子升级、精简注入全部交付；策略四组合、双连接并发、在途 epoch、legacy 冲突、恢复策略注入与 staging 故障均有自动测试 | 提交 9a1b370 / eb44163 / c64d57f / 7e5d09f / 84f945f |
| 存储基准 | 固定种子基线数据 | `docs/PERFORMANCE_BASELINE.md`、`tools/benchmark_store.py` |
| 复用决策 / 评估决策 | 文档化 | `docs/REUSE_DECISIONS.md`、`docs/EVALUATION_DECISIONS.md` |

## 5. 未覆盖项与跟进

| 未覆盖项 | 原因 | 跟进 |
|---|---|---|
| 真实宿主端到端验收（真实浏览器 + 真实消息平台 + 持久部署） | 本机无部署宿主环境，按验收纪律不降标为"已通过" | Linear `MIS-116` 承接（含三项列表页真实浏览器首次加载/翻页/筛选复核）；清单见 `docs/HOST_COMPATIBILITY.md` |

## 6. 关系规则调整轮（MIS-123～MIS-128）交付说明

本轮为**产品行为调整**（授权合同见 Linear《关系规则调整 · GLM 执行方案》）：

- **新装默认**：跨用户恋爱排他关闭（exclusivity=none）、结束后同类型重绑冷却关闭（rebind_cooldown=off）。
- **旧版升级**：v6 配置缺字段显式迁移为 scope/type_default（即上一版行为），来源记 legacy；已有显式选择、全部关系与历史保留。
- **生效语义**：两项策略保存到 JSON 为 desired，重载插件后在数据库事务内激活为 effective（epoch 单调，不复用）；API/页面区分 desired/effective/pending/激活失败/来源；激活遇多人占用拒绝并保持原策略，普通聊天不受影响。
- **同人升级**：恋人→此生挚爱为后端原子升级（旧记录 end_reason=upgraded，前后 ID 可回溯），不算分手、不触发恋人冷却；目标类型真实结束冷却仍约束升级；无恋人直接绑定 spouse 保留。
- **恢复边界**：数据库恢复沿用当前有效策略+全新 epoch（旧备份索引/epoch 作废）；scope 策略下含多人冲突的备份在复制前拒绝；配置副本随备份保存但**不是**自动恢复能力——手工替换配置后按方案重载激活。
- **回滚演练**：升级前成对备份（配置受保护备份 + migration 数据库快照，均自动生成且校验 manifest）；回滚=成对还原后运行 0eb5859 代码；旧版代码会拒绝 schema 12（预期守卫，SchemaVersionGuardTests 覆盖）。恢复到旧备份即回到该时点，不保留之后的新互动。
- **自动验收**：见 §4 证据表（最终提交 241/241 双版本、96/96 无宿主）；真实宿主验收保留待办（MIS-128 实机部分、MIS-116）。

## 7. 已知实现说明（非行为差异）

- MIS-100 拆分后，`_api_bindings` 处理器仍位于 `main.py`（其余 7 个已入
  `pages_api.py` 的 Mixin）。注册经由 Mixin 的 `self._api_bindings` 动态绑定，
  行为与测试覆盖一致，仅文件归属不一致；留作后续小整理，不影响本交付。
- MIS-117 修复轮将 14 条带装饰器聊天命令入口移回 `main.py` 类体（宿主按
  函数 `__module__` 在装饰时登记并按插件模块归属）；`commands.py` 仅保留
  非装饰器助手。这是宿主注册契约要求，非行为变更。
- 复核报告提示的环境事实：本机 C 盘曾满载导致临时目录偶发 `disk is full`
  测试抖动；测试代码保持可移植（默认临时目录），本机复验时以 `TMPDIR`
  指向 D 盘运行。
