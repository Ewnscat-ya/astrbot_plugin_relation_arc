# 行为合同基线（MIS-87）

本文件固定 v0.1.0（commit `dba500b`）的实际行为基线：六维含义、默认值、路线/绑定/安全、scope 与反刷的输入→输出样例。
所有样例由 `tools/behavior_contract_samples.py` 在隔离临时数据库与合成账户上实际运行产生，不是人工推断；
后续任务若需改变以下任何行为，必须满足行为合同的独立决策流程（前后案例 + 迁移回滚说明），并同步更新本文件与脚本。

- 生成环境：Python 3.12.10 + AstrBot 4.28.0（pip 包，仅作依赖与数据类来源）+ SQLite 3.49.1
- 证据测试：`tests/test_core.py`、`tests/test_p0.py`、`tests/test_main.py`（112 个方法，见 BASELINE_VALIDATION.md）
- 复跑方式：见 `docs/BASELINE_VALIDATION.md` 的"从新环境重建"

## 样例输出（脚本实际运行结果）


## 六维、默认值与展示单位

- **DIMENSIONS**: trust, respect, comfort, closeness, resonance, romance_interest
- **DEFAULT_VALUES**: {"trust": 400, "respect": 500, "comfort": 450, "closeness": 150, "resonance": 100, "romance_interest": 0}
- **展示单位**: 整数 0–1000 存储；对外展示 = 原值 / 10（一位小数），如 trust 400 → 40.0
- **edge_saturation**: score≥700 时单轮正向增量上限 2，≥850 时上限 1（apply_delta 700/850 两档）
- **range_clamp**: 0≤score≤1000 硬夹取
- **样例:1000+8**: applied=0 notes=['edge_saturation', 'range_clamp']
- **样例:860+5**: applied=1 notes=['edge_saturation']
- **public_directory 类型数**: 5（friend/close_friend/partner/romantic_partner/spouse，romance 组排他）

## 1. 正常结算（v1 协议，普通正负分）

- **输入**: completion_text='当然。'+<relation_judgment> v1; effects {trust:+4, comfort:+2}
- **输出 clean_text**: 当然。
- **输出 values**: trust 404 (400+4), comfort 452 (450+2)
- **输出 audit**: events 1 行 actor=llm, binding=no_binding
- **输出 notes.trust**: {"requested": 4, "repeat_factor": 1.0, "notes": [], "window_positive": 0}

## 2. 反刷：重复事实衰减与消息幂等

- **输入**: 新 message_id + 同一 evidence/reason/effects 第二轮提交
- **输出**: trust 406（404+2，repeat_factor=0.6）; notes.trust={"requested": 4, "repeat_factor": 0.6, "notes": ["same_fact_decay"], "window_positive": 4}
- **输入**: 同 message_id（message-2）重放
- **输出**: values 不变（{"trust": 406, "respect": 500, "comfort": 453, "closeness": 150, "resonance": 100, "romance_interest": 0}）；最新行 notes.trust 仍为 {"requested": 4, "repeat_factor": 0.6, "notes": ["same_fact_decay"], "window_positive": 4}（duplicate_event 幂等，events 仍 2 行）
- **events 总行数**: 2

## 3. 隐藏恋爱：romance_interest 锁定为 0 变化

- **前置**: romance.default_policy=hidden（默认路线），romance_state 未达 eligible
- **输入**: v1 effects {trust:+2, romance_interest:+5}
- **输出**: trust 402 (400+2), romance_interest 0（不变，applied=0）; notes={"requested": 5, "repeat_factor": 1.0, "notes": ["romance_locked"], "window_positive": 0}
- **纯恋爱被锁轮**: romance_interest 单独被锁且无其他变化 → zero_after_policy，零写入、无审计行（events=0，本轮行为基线事实）
- **inject 注入**: 恋爱路线未启用：本轮不得考虑/输出 romance_interest；合同 effect_keys 不含 romance_interest

## 4. 合法双向明确提案自动绑定（friend）

- **前置**: values 达 friend 阈值 trust=500 comfort=450 respect=500（阈值 500/450/450）
- **输入**: v3 relationship_proposal action=bind type_id=friend origin=mutual_dialogue mutuality=clear（无分数变化轮）
- **输出**: binding_created; active_bindings=['friend']
- **同轮重复提案**: 第二次相同 bind → binding_rejected:duplicate（排他/唯一性保留，账本仍一行）
- **重放输出**: null（duplicate_event 幂等，不产生第二条绑定）
- **反例输入**: mutuality=insufficient 且 trust 400+3<500 阈值
- **反例输出**: no binding; binding note={"notes": ["binding_rejected:mutuality"]}（分数达标≠绑定，双向明确才自动绑定）

## 5. 禁用 scope：剥离但零写入

- **输入**: llm_judgment_enabled=false + 合法 v1 块
- **输出 clean_text**: 不客气。
- **输出**: events 行数=0（0，无任何结算写入）

## 6. 同轮安全提升：v3 提案生效并压制同轮恋爱正向

- **前置**: interaction_safety.llm_mode=llm_auto（默认）
- **输入**: v3: effects {trust:+2, romance_interest:+4} + safety slow_down/boundary_pressure
- **输出 values**: trust 402 (400+2), romance_interest 0（0，同轮被压）
- **输出 notes**: romance={"requested": 4, "repeat_factor": 1.0, "notes": ["romance_locked"], "window_positive": 0}; safety={"notes": ["timed_safety_applied:slow_down"]}
- **输出 timed_safety**: level=slow_down, source=llm_auto（30 分钟自动时限状态，与管理员基础安全状态分离）

## scope 隔离与群聊定向

- **is_global_relation=true（默认）**: identity=platform:sender，scope=global；session 模式下同一用户在不同会话是独立账户
- **group_require_at_or_reply=true（默认）**: 群聊未被唤醒/@/引用时 settlement=skipped_group_not_directed，不结算
- **无稳定 message_id**: settlement=skipped_no_stable_message_id，先于账户创建，不产生首互动行

## 行为投影（V4 合同，固定文案）

- **中性默认**: 信赖（35–54：中性陌生/正常往来）：正常交流，不预设欺骗；重要事项仍自行判断；认可（35–54：中性陌生/正常往来）：平等认真回应，不轻视也不额外抬高；安心感（35–54：中性陌生/正常往来）：可自然交流，但不主动暴露脆弱或过度私人化；……
- **隐藏/锁定**: 不得把单方表白、模板情话、攻略话术、送礼本身或命令解释为角色同意恋爱。
- **未达资格**: 不得输出恋爱意向正向变化，也不得被强推话术带偏。
- **safety!=normal**: 不得输出恋爱意向正向变化或推进亲密关系。
- **可恋爱高分**: 恋爱意向（55–74：稳定建立）：双向特殊情感较明确，仅可建议确认

## 维度归因合同（注入文本固定要求）

- **六维含义**: 信赖=可靠真诚守约可托付；认可=能力原则判断值得认真看待；安心感=无压节奏边界受尊重；亲近感=共同记忆与自然日常关心；共鸣=情绪价值经历幽默被真正理解
- **不计入**: 普通礼貌、复读、刷屏、群聊起哄通常持平；单方情话/模板攻略不得仅凭自身提升 romance_interest

## 合同要点（由以上样例固定）

1. **计分**：单事实每维 ±10 上限（raw_delta_limit）、同事实重复衰减 `[1.0, 0.6, 0.3, 0.0]`/180 分钟、≥700/≥850 正向饱和、0–1000 夹取；展示值 = 原值/10。
2. **绑定**：数值达标本身不绑定；仅当协议提案 `action=bind`、固定类型、`mutuality=clear` 且通过类型/路线/安全/排他校验后才自动绑定；重复绑定被 `duplicate` 拒绝；禁止发明类型。
3. **恋爱路线**：默认 hidden；未启用/未达资格/safety≠normal 三层锁均使 romance_interest 无法正向变化；纯恋爱被锁且无其他变化时整轮零写入。
4. **同轮安全提升**：v3 `interaction_safety_proposal` 在 `llm_auto` 模式下仅允许提升（或刷新既有自动级），写入 timed_safety（默认 30 分钟，来源 llm_auto）；与管理员基础安全状态分离；同轮恋爱正向被一并压制。
5. **幂等**：`event_id=llm:sha256(identity|scope|message_id)`，同 message_id 重放返回 duplicate_event，不加行不重算。
6. **scope**：默认 `is_global_relation=true`（全局账本）；session 模式按会话隔离；群聊默认需唤醒/@/引用才结算；无稳定 message_id 不创建账户。
7. **禁用**：`llm_judgment_enabled=false` 时协议块仍被剥离（净化无条件），但零结算写入。
8. **反刷**：24 小时滚动窗口正向上限（如 trust 50）；超限部分记 `rolling_window_cap`（见 anti_farm 配置）。

## 已知边界（基线事实，不是缺陷修复承诺）

- 关系记录命令直接展示 applied_json 全维数值与原 reason，未复用恋爱隐藏/群聊展示策略（MIS-93 处理）。
- 结算的重复判定使用"分数变化向量 + 首条 evidence"，可能把不同事实并入同一签名（MIS-102 评估项）。
- 中性有效互动是否更新活跃时间未定义（MIS-102 评估项）。
