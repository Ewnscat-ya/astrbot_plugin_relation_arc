# 宿主接口与 Pages 兼容性核验（MIS-88）

本文件记录 AstrBot pip 发布版（4.26.0 / 4.28.0）下 Relation Arc Pages 与宿主接口的实测兼容结论、可执行回归的位置，以及本机无法覆盖、需真实宿主补验的项目。核验日期：2026-09-12。

## 实测结论（pip 包级，4.26.0 与 4.28.0 两版对照）

| 核验点 | 结论 | 证据 |
| -- | -- | -- |
| `Context.register_web_api` 签名 | 两版完全一致（route, view_handler, methods, desc）；相同 route+methods 重复注册为**替换**，重载不产生重复路由 | `tests/test_host_compat.py::test_register_web_api_replaces_same_route_and_methods` |
| 宿主面板框架 | Starlette/FastAPI；插件 API 经 `/api/plug/{path}`（legacy，`require_dashboard_user`）与 `/plugins/extensions/{path}`（`require_plugin_scope`→4.28 改为 `ScopeDependency(scope='plugin')`）两条路由进入，均强制鉴权 | `test_host_plugin_routes_require_authentication`（断言两版形态都被接受） |
| Quart 兼容桥 | 宿主内置（`dashboard/asgi_runtime.py`）：`bind_quart_request_context` 用 Quart `test_request_context` 包裹插件 handler，`_quart_response_to_starlette`/`_coerce_view_result` 转换 `jsonify(...)` 与 `(body, status)` 元组。插件现有 `from quart import request, jsonify` 写法是发布版明确支持的契约，**无需改写 handler** | `test_host_compat.py` 全部用例经宿主真实 `call_request_view` 驱动 |
| 8 个 Pages API 可达性 | config/accounts/audit/backups/overview/health/migrations/bindings 全部 200；POST backup_now 成功 | `test_read_only_gets_all_reachable`、`test_backups_post_manual_backup_roundtrip` |
| 错误码语义 | 400（非对象配置/非法字段/错误 action/部分维度）、403（scope 未开放/绑定 scope 不可证实）、404（账户不存在拒绝隐式创建）、409（revision CAS 冲突）全部经宿主层正确透传 | `test_config_post_*`、`test_accounts_error_ladder_400_403_404_409`、`test_bindings_*` |
| Pages 静态契约 | 宿主按 `pages/<name>/index.html` 发现页面；插件 `pages/settings/` 布局与 `metadata.yaml` 声明匹配；bridge SDK 由宿主路由供给 | `test_pages_layout_matches_host_discovery_contract` |
| 账户编辑单位 | Pages API 的 `values` 为**人类展示单位**（0–100.0，内部 ×10 存储）；本次测试曾误用存储单位得到 400，属预期行为 | handler `_numeric_tenths`（×10）+ `update_account_admin`（0–1000 整数校验） |

### 对规划假设的修正

研究文档曾以"上游 master 直接依赖 Quart，公共 Web API 已改 FastAPI/Starlette"为潜在风险。实测结论：**发布版 4.26.0/4.28.0 均已是 Starlette 面板 + Quart 兼容桥**，插件无需封装额外兼容层即可工作；该风险在声明支持的版本区间内不存在。已在 REUSE_DECISIONS.md 筛选表同步此修正。

## 回归位置

- `tests/test_host_compat.py`（13 个用例）：用宿主真实的 `FastAPIAppAdapter` + `call_request_view` + `_match_registered_web_api` 驱动插件真实 handler，不镜像宿主逻辑；缺 `astrbot` 包时整类跳过（记为缺依赖，不算失败）。
- 两版本各跑一轮：4.26.0 → 112/112；4.28.0 → 112/112（99 基线 + 13 兼容）。

## 未覆盖项（本机无真实宿主部署）

| 项 | 状态 | 补验路径（归 MIS-101 收口） |
| -- | -- | -- |
| 真实宿主进程端到端（两个明确版本安装→面板访问→8 API 真实 HTTP 调用） | 未覆盖 | 部署 AstrBot（先 4.26.x、再 4.28.x）→ 安装插件 → 浏览器登录面板 → 逐 API 调用并记录版本号与响应 |
| 浏览器 Pages 验收（`bridge.ready()` promise、页面渲染、超时与错误提示） | 未覆盖（JS 行为需真实浏览器） | 打开 settings 页 → 确认加载、保存、错误路径 |
| 未授权访问的真实 401 拒绝 | 宿主层鉴权依赖存在已断言，但未执行真实 401 | 无凭据 curl 面板 API → 应 401/403 |
| 真实重载后的路由表与数据路径 | 去重逻辑已在包级断言；真实进程重启未验 | 宿主面板禁用/启用插件 → 复查 API 可用性与数据目录未漂移 |
| 老数据经真实升级链路可读 | 迁移门禁有测试覆盖；真实旧版本库文件未验 | 用 v4 时代的真实备份文件在新宿主上启动 → 核对迁移日志与备份产物 |

以上任一项发现问题时，先在本文件登记复现，再按行为合同决定修复或独立决策任务。
