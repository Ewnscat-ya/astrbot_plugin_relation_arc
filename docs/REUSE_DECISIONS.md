# 上游来源、复用决策与追溯记录（MIS-87）

用户已确认：**Favour Ultra 是 Relation Arc 的灵感与代码来源**，本项目按"源自 Favour Ultra 的衍生项目"规划（Apache-2.0）。独立插件名与独立运行数据不改变代码来源关系。本文件落实项目文档新增的《上游关系与升级策略》：

1. 升级任何模块前，先看上游同类模块、相关修复及变更，再决定直接适配、保留本项目实现或独立修复。
2. 记录每项上游变更为何适用、不适用或已经实现。
3. 没有可验证共同基线时，不做整仓自动 merge 或无依据的 cherry-pick。

## 一、来源映射（上游 → 本项目）

最初采用的上游 commit 不可考（本项目 Git 历史只有一个初始提交 `dba500b`）：**来源版本待确认**，不得将上游当前 main 快照冒充 fork 点。下表"可证实版本"均为快照 `2f2ec141`（2026-08-30 push，main HEAD）；行号来源为 C1–C4 实现卡（本项目内留存的追溯记录），映射随任务推进继续细化。

| 上游模块/文件（Favour Ultra） | 本项目文件/功能 | 继承类型 | 差异理由 | 证据/测试 |
| -- | -- | -- | -- | -- |
| `main.py:2199-2211`（安全状态记录/刷新）、`main.py:1659-1673,2246-2256`（惰性过期检查）、`main.py:2713-2782`（管理员取消/列表） | `relation_store.py` timed_safety 表、`main.py` 互动节奏命令、`_effective_state` | 改造继承 | 上游内存态不持久化；本项目改为 SQLite 持久覆盖 + 代际删除，永不写回基础状态（C1 不变量） | C1 卡；tests：`test_c0_llm_auto_*`、同轮安全样例（BEHAVIOR_CONTRACT §6） |
| `storage.py`（无冷暴力持久化） | 同上 | 拒绝上游实现 | 本项目要求跨重载持久且有界时限 | 同上 |
| `main.py:1644-1650`（允许/阻止会话清单）、`main.py:1350-1374`（超级用户显式判定、普通查询权限分渠道）、`permissions.py:51-102` | `main.py` `_enabled`/`_capability_allowed`/`_query_allowed` | 改造继承 | 只信任宿主 Bot-admin ID；群聊第三方/批量查询先拒绝；拒绝不泄露目标存在性（C2 不变量） | C2 卡；tests：capability/scope 用例 |
| 上游群聊宽表展示 | 本项目群聊回复策略 | 拒绝 | 群聊永不展示第三方 ID/数值/绑定/安全状态 | C2 卡 |
| `main.py:212,215-236`（托管后台任务创建与关闭取消）、`main.py:300-314`（任务错误捕获） | `main.py` `_decay_loop`/`terminate` | 改造继承 | 单调度任务、显式 cancel/await、净化异常日志；衰减只按持久化 `last_interaction`（C3 不变量） | C3 卡；衰减测试 |
| 上游运营管控（黑名单/权限面板） | `relation_store.py` settlement_blacklist | 收窄改造 | 仅结算黑名单、默认关闭、只在合法协议结算后判定；不做普通消息拦截（C4 不变量） | C4 卡；黑名单测试 |
| `storage.py` `_retry_on_locked`（快照核验：SQLite locked 重试装饰器、WAL、busy_timeout=30s、pool_size=1） | `relation_store.py` 同步锁 + `self.lock` | 尚未接入 | 本项目当前为同步单写连接；异步化在 MIS-97 二选一基准后决定（aiosqlite/专用 worker vs 上游 aiosqlite+SQLModel 模式） | 本次 storage.py@2f2ec141 直接核验 |
| `storage.py` `get_decay_candidates`/`apply_decay`（last_interaction 驱动、线性+分级模式、floor） | C3 衰减语义 | 改造继承 | 本项目简化为 `max(floor, value-step)`，按维度独立 floor；禁用默认 | C3 卡 |
| `storage.py` `auto_backup`/`list_backups`/`restore_backup`/`cleanup_old_backups`（JSON 记录备份） | `relation_store.py` `backup_now`（SQLite backup API）+ 配置迁移保护备份 | 改造继承（后端不同） | 本项目备份必须覆盖"配置+SQLite 一致快照"；上游 restore 是先删后插、无预检/回滚，本项目 MIS-95 恢复不得复制该模式 | C1 卡迁移备份；MIS-95 实施时对照 |
| `main.py:615` 区域（备份调度）、`main.py:239` 区域（任务卸载清理） | 本项目当前只有衰减任务；备份配置未接调度 | 尚未接入 | MIS-95 接通自动备份、MIS-96/100 对照任务清理 | 研究文档核验记录 |
| 上游单维好感模型、SQLModel 数据模型、WebUI 完整实现 | 本项目六维模型/独立 SQLite schema/Pages 前端 | 本项目新增/替代 | 产品约束：六维含义与行为合同保留，不引入单维替换 | README、BEHAVIOR_CONTRACT.md |

## 二、上游修复筛选表（手动一次，本表随任务升级前更新）

范围：上游 main 近期提交（截至快照 `2f2ec141`）。无 fork 点基线，故只做逐项影响评估，不做自动 merge/cherry-pick。

| 上游提交/发布 | 解决的问题 | 本项目是否受影响 | 采用方式 | 回归范围 |
| -- | -- | -- | -- | -- |
| `6e2c1b1c`、`ce730d45`（fix: 配置原子写入 + 损坏/误删自动恢复） | 插件更新后配置可能清空/损坏 | 部分受影响：本项目 `config_manager.py` 已有原子写入（tmp+replace）且损坏/未来版本**拒绝加载不覆盖**，强于上游修复 | 已实现（自有实现，无需吸收）；对照确认无遗漏 | 配置迁移测试（现有）；MIS-95 备份任务复核 |
| `0cb85983`（v4.4.5 WebUI 深度重构） | WebUI 体验升级 | 无直接影响（前端为两套实现）；MIS-98/99 做 Pages 体验时可对照其交互模式 | 仅借鉴，不复制（AGPL/Apache 边界见下） | MIS-98/99 验收项 |
| `e707f52a`、`4a38517a`、`41263d21`（prompt 模板重构：轻量规则、强制页脚、scoring mode、拒绝示例） | 提示词结构与服从性 | 间接受影响：本项目协议合同注入（`RelationArcOutputContract`）与上游 prompt 无共享文本；其"拒绝示例"思路与本项目反强推条款同向 | 保留本项目实现；MIS-94 统一模型/用户说明时可对照措辞，不自动采用 | MIS-91/94 协议回归 |
| 上游 `init_db`（无 schema 版本校验，仅按列补齐） | 本项目 schema user_version 门禁 | 本项目受影响面不同：上游没有版本门禁可借鉴；MIS-89 独立实现"未来版本拒绝 + 损坏库拒绝 + 单事务迁移" | 独立修复（无上游对应物）；不在筛选表标记为"已实现"，标记为"本项目新增" | tests：`SchemaVersionGuardTests`（未来版本/损坏库/保留原文件）、迁移原子性 |
| `4813daf9`、`2fa253d4` 等（v4.4.3 beta 合流） | 常规发布 | 无独立影响 | 跟随上表逐项评估 | — |

## 三、补充技术参考（4 个，非上游）

| 来源 | 可证实版本 | 许可 | 采用决策 |
| -- | -- | -- | -- |
| [AstrBot 官方](https://github.com/AstrBotDevs/AstrBot) | pip 4.26.0/4.27.0/4.28.0 已实测；master `8b5f3888` | 仓库 AGPL-3.0 | 采用 pip 发布版作为依赖与版本矩阵对象；只调用公共 API/Pages 契约，不复制宿主源码 |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | `9b127cec` | MIT | MIS-97 二选一基准后决定；完整事务须独立串行化设计 |
| [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | `cd434f23` | MPL-2.0（LICENSE.txt） | 仅开发测试依赖，引入时单列并记录版本 |
| [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | `9e1c2d71` | AGPL-3.0 | 仅行为借鉴（GET 去重/失败释放/POST 不重试），独立实现，不复制源码 |

搜索发现的其他群体陪伴/记忆插件（如 group_companion，MIT）方向不同，不列为复用来源。

## 四、许可义务（发布前核查清单，MIS-100/101 承接）

1. **本仓库当前无 LICENSE 文件**：作为 Apache-2.0 衍生项目，对外分发需由仓库所有者确认许可落位（添加 LICENSE/NOTICE 或等效声明）。zcode 不替仓库选择许可证；在此之前**不进行文件级源码复制**（含上游），只做行为借鉴 + 独立实现。
2. 若确认 Apache-2.0：按其要求保留上游版权与许可声明、说明本项目对继承代码的重大修改（对照第一节的继承类型列）、保留 NOTICES；上游 Apache-2.0 不要求衍生项目同许可证，但须保留声明并标注修改。
3. 与 AGPL 组件（AstrBot 宿主、LivingMemory）保持网络/公共 API 边界，不合并源码。
4. 每次吸收或对照上游变更，在本文件第二节追加一行记录；拆分文件（MIS-100）时同步移动来源标注。
