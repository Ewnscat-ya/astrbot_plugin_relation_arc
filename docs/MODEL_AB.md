# 可复现的模型对照与离线采集（v0.2.1）

这里的工具始终从候选目录运行；`--repo` 决定**实际加载**哪一侧插件，不再通过切换工作目录或 side 标签假装切换源码。需要安装 AstrBot 4.26.0 或 4.28.0 的隔离 Python 环境。所有账户、配置、关系状态均为临时目录内的合成输入，不调用 judge 写入真实账本。

## 1. 固定两侧源码

在候选仓库执行以下命令。`CANDIDATE_SHA` 换成要验收的完整 SHA，`BASELINE_DIR` 是单独检出的 913ca59 目录；无需修改朋友实例。

```text
git worktree add --detach ../relation-baseline 913ca59036267de2bcc481b8910b5f6fc8044f91
python tools/model_ab_test.py run --side baseline --repo ../relation-baseline --expected-commit 913ca59036267de2bcc481b8910b5f6fc8044f91 --adapter fake --out ../ab/baseline.json
python tools/model_ab_test.py run --side candidate --repo . --expected-commit CANDIDATE_SHA --adapter fake --out ../ab/candidate.json
python tools/model_ab_test.py compare ../ab/baseline.json ../ab/candidate.json
```

默认 20 个场景 × 3 次重复，每侧 60 次离线调用。包含普通/规则较多人设、长/短固定历史、隐藏/显示恋爱、资格、安全、私聊/定向群聊、全局/会话与有效排他。假 provider 逐次检查动态块、提醒、原文、预设历史与部件顺序；正式测试还将完整参数送入真实 OpenAI provider 并在 SDK 边界拦截断言。返回的固定合成标签只用于验收工具执行链，不构成模型遵循率、缓存、时延或费用收益。

输出逐条原子更新，含请求开始状态、原始最终文本、完整组装消息、过滤后的历史、来源提交、实际源码哈希、dirty 状态、宿主版本与工具哈希。预热调用也落盘，失败/超时不会伪装成有效测量；错误只存类型，避免异常内容带出凭据。两个检出即使文件夹不叫插件名，也会按显式路径加载，进程内缓存键包含源码哈希。

`--plan-only` 只写计划调用数，返回 0 但 `ran:false`；compare 不接受该结果。非法参数、未获授权、请求失败、零有效协议样本、未完成或不具备可比条件时返回非 0。缺失 usage 是未知而非请求失败；相关指标保持 null。合法率要求首行完整唯一块、schema 3、合法字段/维度/整数幅度共同满足；生产兼容解析器仍保留 v1/v2/裸 JSON 容错，两者用途不同。

## 2. 接入实际配置好的 provider

提供两种明确的接入方式，不再实例化抽象 `Provider()`：

- 运行中、已获授权的隔离宿主，可调用 `HostAdapter.from_context(context, provider_id, channel=...)`，通过宿主真实 `get_provider_by_id` 取得已配置实例。可在 Python 中将它传给 `run_side`；不会关闭或改写该宿主拥有的 provider。
- 独立进程可直接使用已实现的 `tools/provider_factory_openai.py:make`，创建具体的 AstrBot OpenAI 兼容 provider。明确设置 `RELATION_AB_API_KEY`、`RELATION_AB_BASE_URL`、`RELATION_AB_MODEL`、`RELATION_AB_CHANNEL`；可用 `RELATION_AB_PARAMETERS_JSON` 设置相同生成参数，例如温度/输出上限/思考配置。这里不读取生产配置。凭据只放本地环境，不写进仓库、输出或任务记录。

预检仍不导入 factory：

```text
python tools/model_ab_test.py run --side candidate --repo . --expected-commit CANDIDATE_SHA --out ../ab/plan.json --plan-only
```

仅在环境、凭据范围和预算已经获得授权后执行：

```text
python tools/model_ab_test.py run --side baseline --repo ../relation-baseline --expected-commit 913ca59036267de2bcc481b8910b5f6fc8044f91 --provider-factory tools/provider_factory_openai.py:make --allow-paid --max-calls 60 --out ../ab/real-baseline.json
python tools/model_ab_test.py run --side candidate --repo . --expected-commit CANDIDATE_SHA --provider-factory tools/provider_factory_openai.py:make --allow-paid --max-calls 60 --out ../ab/real-candidate.json
python tools/model_ab_test.py compare ../ab/real-baseline.json ../ab/real-candidate.json
```

这两个命令每侧最多计划 60 次工具级调用。`--warmup 2` 会变成每侧 180 次，必须提高已授权的调用数预算。工具给 provider 传 `request_max_retries=1`；供应商内部重试/最终计费仍受实际渠道影响，因此调用次数不是绝对金额上限。精确价格、可承受金额、渠道限额应先在隔离环境中确定。本轮没有执行上述真实调用。

两侧同一精确模型、渠道、生成参数、宿主版本、场景、预设历史、重复和预热次数才允许 compare。正文不回灌历史；第一次调用也不称为“冷缓存”，供应商缓存是否已有不能从进程状态推断。预热与测量分开保存、汇总。记录请求模型与可取得的响应模型；宿主标准化 TokenUsage 能给出 input/output，但其默认 cache=0 不能当成供应商报告。原始 usage 的命中/未命中字段分别采集，完整覆盖且分母大于零时才计算汇总命中率。

`--price-json` 可读取附 source/currency 的价格表，mode 为 `flat`（input_per_1k/output_per_1k）或 `cache`（cache_hit_per_1k/cache_miss_per_1k/output_per_1k）。所有用量完整才计算，包括预热费用；这是按价格估算，不是实际账单。未提供价格、输出用量缺失或缓存计费字段不完整时 cost/total_cost 为 null，不报告节费百分比。

## 3. 完整请求、历史与字符评估

```text
python tools/prompt_samples_dump_full.py --repo ../relation-baseline --expected-commit 913ca59036267de2bcc481b8910b5f6fc8044f91 --side baseline --out ../samples
python tools/prompt_samples_dump_full.py --repo . --expected-commit CANDIDATE_SHA --side candidate --out ../samples
python tools/prompt_eval.py --baseline-dir ../samples --candidate-dir ../samples --out ../samples/eval.json
```

七场景共十三个生命周期快照，双方使用同一合成输入、数据 URI 图片、外部部件、历史和状态变更。实际调用 `inject → ProviderRequest.assemble_context → dump_messages_with_checkpoints`；不会只拼 builder 字符串。比较器拒绝输入、有效状态、宿主、生成器和同侧源码版本混杂。总开销包括固定块分隔换行与全部动态块；序列化完整请求的共同前缀按相同 persona 分组，禁用阶段单列。字符数不解释成 token、缓存命中或费用。

## 4. 聊天反馈采集

```text
python tools/compatibility_probe.py --repo . --expected-commit CANDIDATE_SHA --reference-source REVIEWED_FAVOUR_MAIN.py --out ../compat/candidate.json
```

同样对 baseline 运行一遍。探针不执行上游文件，只读取受限 f-string 和正则，代入合成数据，再走关系弧线真实请求/响应 hook。覆盖两种人设、两插件各自提示及前后两种组合。它验证原文保留和合成标签清理，不冒充上游插件完整生命周期、实际 hook 调度或真实模型效果，也不把双评分插件共存当成必须支持的产品场景。

要定位现场问题，最少需要：双方插件名和精确版本/配置、是否同时启用、准确模型 ID/渠道/思考配置、是否重置上下文；然后在获授权的合成复现环境记录最终模型请求、最终文本/富消息链、两侧 hook 顺序及标签在输出/清理/结算哪个环节消失。优先用合成规则人设；不要把生产会话和人设直接上传外部任务。
