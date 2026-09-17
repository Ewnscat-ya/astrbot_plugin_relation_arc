# 基线验证记录（MIS-87）

本文件记录 v0.1.0（commit `dba500b`）的实测版本矩阵、测试执行结果、从新环境重建测试的步骤，以及本机明确未覆盖的验收项。所有数字为实际执行结果，不是声明值。

## 版本矩阵（实测）

| 组件 | 实测值 | 说明 |
| -- | -- | -- |
| 插件版本 | 0.1.0（metadata.yaml） | 注册名 `astrbot_plugin_relation_arc` |
| 配置版本 | 6（`CONFIG_VERSION`） | 与规划基线一致 |
| SQLite schema | 9（user_version） | 与规划基线一致；建库日志 `sqlite from=0 to=9` |
| 响应协议 | v1 核心；v2 增加 `relationship_proposal`；v3 增加 `interaction_safety_proposal` | `relation_protocol.py` 只接受 `schema_version ∈ {1,2,3}` |
| Python | 3.12.10 | 系统解释器与隔离虚拟环境一致 |
| SQLite 库 | 3.49.1 | `sqlite3.sqlite_version` |
| AstrBot 宿主包 | 4.26.0 / 4.27.0 / 4.28.0（三轮实测） | pip 发布包；声明下限 `astrbot_version: ">=4.26"` |
| 宿主程序 | **未覆盖**：本机未部署运行中的 AstrBot 实例（含适配器、Web 服务） | 见"未覆盖项" |
| 目标仓库 LICENSE | 无 LICENSE 文件 | Apache-2.0 衍生关系已由用户确认；发布前许可核查清单见 REUSE_DECISIONS.md 第四节 |

## 安全门禁修复（本次变更包含的两处基线加固，零语义变化）

Mimosa L3 提交门禁在基线代码上报告两处高危，均已修复并验证行为不变：

| 位置 | 发现 | 修复 | 验证 |
| -- | -- | -- | -- |
| `relation_store.py` schema 写版本处 | PRAGMA 以 f-string 内插变量构造 SQL 文本（SQL 注入类告警） | 改为字面量 `PRAGMA user_version=9` + 写入后读回与 `SCHEMA_VERSION` 比对，漂移即抛错（PRAGMA 不支持参数绑定；字面量+读回校验保证无外部输入可达 SQL 文本且版本不漂移） | 40 核心/P0 测试通过；92 全量通过 |
| `tests/test_p0.py` `load_methods()` | `exec(compile(...))` 执行 AST（代码注入类告警） | 改为 AST 变换后写临时文件、经 `importlib` 加载，种子全局与原实现一致；仅加载本仓库自身 main.py | P0 9/9 通过；92 全量通过 |

修复后重跑 `tools/behavior_contract_samples.py`，输出与 `BEHAVIOR_CONTRACT.md` 记录逐字节一致（diff 为空）——行为基线不受影响。上游 Favour Ultra `storage.py` 已在快照 `2f2ec141` 直接核验（locked 重试装饰器、WAL、备份/恢复/衰减机制），详见 REUSE_DECISIONS.md 来源映射。

## 测试执行结果（277 个方法：核心 100 / 入口 151 / P0 9 / 宿主兼容 16 / Pages 契约 1）

92（MIS-87 基线）→ 99（MIS-89 新增 7 个版本门禁与身份修复回归，复现先行）→ 112（MIS-88 新增 13 个宿主兼容回归，走宿主真实 Quart 兼容桥）→ 126（MIS-90 新增 14 个群定向与轮次上下文回归，复现先行：修复前 7 项失败）→ 137（MIS-91 新增 11 个协议预算/截断契约/富消息顺序回归）→ 144（MIS-92 新增 7 个并发原子性/幂等/排他/故障注入回归）→ 150（MIS-93 新增 6 个查询可见性/scope 过滤/安全展示回归）→ 152（MIS-94 新增 2 个冷却链路回归）→ 160（MIS-95 新增 8 个恢复预检/全量恢复/故障注入/调度周期回归）→ 162（MIS-96 新增 2 个衰减周期幂等/时间戳不伪造回归）→ 164（MIS-97 新增 2 个窗口合并等价性回归）→ 169（MIS-98 新增 5 个服务端分页/精确计数/稳定排序回归）→ 176（MIS-99 新增 7 个账户显式编辑/配置冲突/未知字段回归，复现先行）→ 178（MIS-117 P1-1：宿主注册链归属/可分发 2 项）→ 182（MIS-117 P1-2+P2-3：备份实际入口三件套/整组轮换/dba500b v9 备份恢复迁移 4 项）→ 184（MIS-117 P2-4：多事实重复衰减 2 项）→ 186（MIS-117 P2-5：跨组件协议清理 2 项）→ 188（MIS-117 P2-6：scope/状态筛选括号 2 项）→ 191（MIS-117 页面轮：bridge 契约 node 合成 DOM 断言 1 项 + 诊断端点实际版本 2 项）→ 206（MIS-121：B01 图文顺序与协议边界正式移植 13 项 + B02 恢复暂存建索引故障边界/冲突恢复 2 项）→ 227（MIS-124/125：绑定策略配置与 API 10 项、策略约束双连接/激活拒绝/legacy 冲突/epoch/恢复注入 11 项）→ 238（MIS-126：同人升级与可选冷却 11 项）→ 241（MIS-127：注入快照契约 3 项）→ 256（MIS-134：并发恢复/策略竞争 2 项、冲突库启动诊断 3 项、暂停资格 2 项、冷却统一 3 项、回执语义 2 项、记录展示 2 项、来源只读 1 项）→ 268（MIS-134 R 轮：恢复锁所有权 4 项、健康分类 3 项、建立展示与隐藏投影 5 项）→ 267（R 轮复核收尾：恢复互斥测试重构为共享互斥语义 3 项，C 轮并发测试更新至串行化断言并合并 1 项重复）→ 272（N 轮：StoreBusy 推迟激活 2 项、竞争下插件构造 1 项、staging 清理 2 项）→ 277（MIS-158/159：注入布局/固定块稳定/白名单 4 项改版 + 生命周期 3 项 + 宿主临时性契约 1 项）。现有方法仅按契约变化更新断言（bindings 端点不再携带查询串），无删除。

| 环境 | 命令 | 结果 |
| -- | -- | -- |
| 无宿主包（系统 Python） | `python -m unittest tests.test_core tests.test_p0` | 106/106 通过；test_main/test_host_compat 因缺 `astrbot` 包导入失败/跳过 → 记为**缺依赖**，非用例失败 |
| 虚拟环境 + astrbot 4.28.0 | `python -m unittest discover -s tests` | **277/277 通过**（52.1s） |
| 虚拟环境 + astrbot 4.26.0（声明下限） | 同上 | **277/277 通过**（52.1s） |
| 虚拟环境 + astrbot 4.27.0 | 同上（MIS-87 时 92 方法） | **92/92 通过**（4.5s） |
| 虚拟环境 + astrbot 4.28.0（MIS-89 后，99 方法） | 同上 | **99/99 通过**（4.6s） |

- 既有失败：0。新回归：0。缺依赖：已通过安装 pip 发布包消除（仅宿主程序运行仍缺）。
- 入口测试（test_main.py）使用宿主真实数据类（`ProviderRequest`/`Plain`/`MessageChain`）与 Fake 事件对象，属于"包级 API 兼容"证据；不等于真实 HTTP/适配器链路验收。
- 宿主兼容测试（test_host_compat.py）走宿主真实 Quart 兼容桥与路由匹配函数，包级证明 8 个 Pages API 与 400/403/404/409 行为；真实宿主/浏览器层仍未覆盖（见 HOST_COMPATIBILITY.md）。

## 从新环境重建测试

1. 取得仓库并检出基线 commit。
2. 创建独立虚拟环境：`python -m venv .venv`（仓库外），激活后 `pip install astrbot`（当前 4.28.0；验证下限用 `pip install astrbot==4.26.0`）。
3. 在仓库目录运行：`python -m unittest discover -s tests -v` → 应为 `Ran 277 tests ... OK`。
4. 生成行为合同样例：`python tools/behavior_contract_samples.py` → 输出应与 `docs/BEHAVIOR_CONTRACT.md` 记录一致（数字确定性，不含时间戳）。
5. 无 astrbot 包时仅能运行 `python -m unittest tests.test_core tests.test_p0`（40 个），此时不要把其余测试标记为失败。

## 最小真实宿主与 Pages 验证入口（供 MIS-88 / MIS-101 执行）

- **包级兼容层**（已可执行）：`tests/test_host_compat.py`，判定标准 = 两版本 112/112 OK；结论见 `docs/HOST_COMPATIBILITY.md`。
- **真实宿主层**（未覆盖）：需要一个运行中的 AstrBot 实例。验证步骤：安装插件 → 启用 → 分别在私聊、群聊（@/回复/普通消息三类）发送消息 → 核对 `on_llm_request` 注入两段合同文本、`on_llm_response` 结算、`/关系` `/关系记录` 输出、协议块不出现在最终回复。判定标准 = 以上行为与 BEHAVIOR_CONTRACT.md 一致。
- **Pages 层**（未覆盖）：宿主 Web 面板中打开插件 settings 页；验证配置读写、关系列表/审计/绑定接口的 HTTP 状态与鉴权；MIS-98/MIS-99 补分页与保存语义。

## 未覆盖项（本机验收边界）

| 项 | 状态 | 补验位置 |
| -- | -- | -- |
| 真实 AstrBot 宿主进程（真实事件钩子、平台适配器、流式输出） | 未覆盖 | MIS-88 / MIS-101 |
| 真实浏览器 Pages 验收（HTTP 状态、鉴权、重载卸载） | 未覆盖 | MIS-88 / MIS-99 / MIS-101 |
| 真实群聊消息验收 | 未覆盖（按执行合同禁止使用真实群聊数据） | MIS-101 由所有者执行 |
| 性能基准（1k/10k 账户、10万/100万事件） | 未覆盖（M2 前不做性能声明） | MIS-97 |
| Hypothesis 属性测试 | 未安装（按执行合同仅放开发依赖，待引入时再评估） | 后续任务按需 |
