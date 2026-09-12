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
| 配置版本 | CONFIG_VERSION = 6 | `config_manager.py` |
| 数据库版本 | SCHEMA_VERSION = 11（user_version） | `relation_store.py` |
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
| 全量回归（venv + astrbot 4.28.0） | 176/176 通过（16.3s） | `python -m unittest discover -s tests` |
| 全量回归（venv + astrbot 4.26.0 声明下限） | 176/176 通过 | 见 BASELINE_VALIDATION.md 双版本矩阵 |
| 无宿主回归（系统 Python，无 astrbot 包） | 74/74 通过（core+p0） | `python -m unittest tests.test_core tests.test_p0` |
| 行为合同冻结样例复核 | 六类样例关键值与 `BEHAVIOR_CONTRACT.md` 一致，无漂移 | `tools/behavior_contract_samples.py` 复跑 |
| 存储基准 | 固定种子基线数据 | `docs/PERFORMANCE_BASELINE.md`、`tools/benchmark_store.py` |
| 复用决策 / 评估决策 | 文档化 | `docs/REUSE_DECISIONS.md`、`docs/EVALUATION_DECISIONS.md` |

## 5. 未覆盖项与跟进

| 未覆盖项 | 原因 | 跟进 |
|---|---|---|
| 真实宿主端到端验收（真实浏览器 + 真实消息平台 + 持久部署） | 本机无部署宿主环境，按验收纪律不降标为"已通过" | Linear 后续承接 issue；清单见 `docs/HOST_COMPATIBILITY.md` |

## 6. 已知实现说明（非行为差异）

- MIS-100 拆分后，`_api_bindings` 处理器仍位于 `main.py`（其余 7 个已入
  `pages_api.py` 的 Mixin）。注册经由 Mixin 的 `self._api_bindings` 动态绑定，
  行为与测试覆盖一致，仅文件归属不一致；留作后续小整理，不影响本交付。
