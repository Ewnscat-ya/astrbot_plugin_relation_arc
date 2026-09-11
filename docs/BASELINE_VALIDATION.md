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

## 测试执行结果（99 个方法：核心 38 / 入口 52 / P0 9）

MIS-87 时为 92 个方法；MIS-89 新增 7 个版本门禁与身份修复回归测试（复现先行：修复前 6 项失败，修复后全绿），现有 92 个方法零改动。

| 环境 | 命令 | 结果 |
| -- | -- | -- |
| 无宿主包（系统 Python） | `python -m unittest tests.test_core tests.test_p0` | 40/40 通过（1.8s）；test_main.py 因缺 `astrbot` 包导入失败 → 记为**缺依赖**，非用例失败 |
| 虚拟环境 + astrbot 4.28.0 | `python -m unittest discover -s tests` | **92/92 通过**（4.6s） |
| 虚拟环境 + astrbot 4.26.0（声明下限） | 同上 | **92/92 通过**（4.5s） |
| 虚拟环境 + astrbot 4.27.0 | 同上 | **92/92 通过**（4.5s） |
| 虚拟环境 + astrbot 4.28.0（MIS-89 后，99 方法） | 同上 | **99/99 通过**（4.6s） |

- 既有失败：0。新回归：0。缺依赖：已通过安装 pip 发布包消除（仅宿主程序运行仍缺）。
- 入口测试（test_main.py）使用宿主真实数据类（`ProviderRequest`/`Plain`/`MessageChain`）与 Fake 事件对象，属于"包级 API 兼容"证据；不等于真实 HTTP/适配器链路验收。

## 从新环境重建测试

1. 取得仓库并检出 `dba500b`（或当前基线 commit）。
2. 创建独立虚拟环境：`python -m venv .venv`（仓库外），激活后 `pip install astrbot`（当前 4.28.0；验证下限用 `pip install astrbot==4.26.0`）。
3. 在仓库目录运行：`python -m unittest discover -s tests -v` → 应为 `Ran 92 tests ... OK`。
4. 生成行为合同样例：`python tools/behavior_contract_samples.py` → 输出应与 `docs/BEHAVIOR_CONTRACT.md` 记录一致（数字确定性，不含时间戳）。
5. 无 astrbot 包时仅能运行 `python -m unittest tests.test_core tests.test_p0`（40 个），此时不要把入口测试标记为失败。

## 最小真实宿主与 Pages 验证入口（供 MIS-88 / MIS-101 执行）

- **入口测试层**（已可执行）：`tests/test_main.py` 覆盖 inject/judge/管理命令的行为，使用宿主数据类 + Fake 事件；判定标准 = 92/92 OK。
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
