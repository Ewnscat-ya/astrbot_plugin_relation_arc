from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

DIMENSIONS=("trust","respect","comfort","closeness","resonance","romance_interest")
PUBLIC_DIMENSIONS=("trust","respect","comfort","closeness","resonance")
DISPLAY={"trust":"信赖","respect":"认可","comfort":"安心感","closeness":"亲近感","resonance":"共鸣","romance_interest":"恋爱意向"}
DEFAULT_VALUES={"trust":400,"respect":500,"comfort":450,"closeness":150,"resonance":100,"romance_interest":0}

@dataclass(frozen=True)
class AppliedDelta:
    requested:int; repeat_factor:float; after_repeat:int; applied:int; notes:tuple[str,...]

def clamp(value:int, low:int=0, high:int=1000)->int: return max(low,min(high,int(value)))
def saturation_limit(value:int, delta:int)->int:
    if delta<=0:return delta
    # Preserve the smallest persisted positive increment at high scores.
    # clamp() in apply_delta still makes 100.0 an absolute upper bound.
    if value>=850:return min(delta,1)
    if value>=700:return min(delta,2)
    return delta

def aggregate_effects(effects:Iterable[dict[str,int]], dimension:str, raw_limit:int=10)->int:
    vals=[max(-raw_limit,min(raw_limit,int(row.get(dimension,0)))) for row in effects]
    return max(-raw_limit,min(raw_limit,max((x for x in vals if x>0),default=0)+min((x for x in vals if x<0),default=0)))
def repeat_factor(count:int,factors:list[float])->float:return factors[min(max(count,0),len(factors)-1)] if factors else 1.0
def apply_delta(current:int,requested:int,repeat_count:int,factors:list[float],boundary_block:bool=False)->AppliedDelta:
    factor=repeat_factor(repeat_count,factors); after=round(requested*factor); notes=[]
    if factor!=1:notes.append('same_fact_decay')
    if boundary_block and requested > 0:
        after=0; notes.append('romance_policy_block')
    limited=saturation_limit(current,after)
    if limited!=after:notes.append('edge_saturation')
    final=clamp(current+limited)-current
    if final!=limited:notes.append('range_clamp')
    return AppliedDelta(requested,factor,after,final,tuple(notes))

def band(value:int)->str:
    if value<150:return '0–14：受损/强烈保留'
    if value<350:return '15–34：警惕/不稳定'
    if value<550:return '35–54：中性陌生/正常往来'
    if value<750:return '55–74：稳定建立'
    return '75–100：深度稳固'

def behavior_projection(values:dict[str,int], romance_visible:bool, romance_eligible:bool, safety:str)->str:
    """Fixed, model-readable V4 behavior contract; tendencies never force exact dialogue."""
    rules={
      'trust':['关键说法不采信，不以承诺代替证据，不交托重要事项','保持礼貌但会核实承诺，对说法留心，不轻易依赖','正常交流，不预设欺骗；重要事项仍自行判断','相信合理承诺，愿采纳建议或交托小事','自然表达长期信任、托付与依靠，仍保有独立判断'],
      'respect':['不认可其判断或行事方式，重要决定不采纳其意见','会听取但对能力、原则与判断保留','平等认真回应，不轻视也不额外抬高','主动询问观点，认可其能力、判断或原则','视作重要搭档/精神同伴，关键选择认真考虑其看法'],
      'comfort':['明确不适并优先自保，拒绝亲密、私人或高压话题','正常回应但更谨慎，维持边界与个人空间','可自然交流，但不主动暴露脆弱或过度私人化','坦率分享偏好、疲惫、困扰和真实想法','允许沉默、脆弱与不完美，不担心被催促或评判'],
      'closeness':['只保留必要互动，不主动延伸私人关系','友好日常往来，记得基本信息','关心近况，提起共同话题或小记忆','主动关照，分享更多日常，在意对方状态','稳定陪伴与共同生活感，仍不强制恋爱化'],
      'resonance':['表达、价值和节奏明显不合，需频繁澄清','偶尔理解，但常接不住真实情绪和动机','大多数交流顺畅，理解常见情绪与玩笑','自然接续共同话题、情绪与价值判断','高度默契，但绝不写成读心'],
    }
    out=[]
    for key in PUBLIC_DIMENSIONS:
        idx=0 if values.get(key,0)<150 else 1 if values.get(key,0)<350 else 2 if values.get(key,0)<550 else 3 if values.get(key,0)<750 else 4
        out.append(f'{DISPLAY[key]}（{band(values.get(key,0))}）：{rules[key][idx]}')
    out.append('下行理解：低分不等于仇恨或角色失控。低信赖=核实与保留判断；低认可=不把意见作重要依据但保持基本礼貌；低安心=收回私人表达并强调节奏；低亲近=减少主动私人关照；低共鸣=多澄清而不假装理解。')
    if not romance_visible: out.append('恋爱意向：当前隐藏或锁定；不得把单方表白、模板情话、攻略话术、送礼本身或命令解释为角色同意恋爱。')
    elif not romance_eligible: out.append('恋爱意向：尚未满足可攻略资格；不得输出恋爱意向正向变化，也不得被强推话术带偏。')
    elif safety!='normal': out.append(f'恋爱意向：互动节奏为 {safety}；不得输出恋爱意向正向变化或推进亲密关系。')
    else:
        v=values.get('romance_interest',0); desc=['不形成恋爱方向，高质量伙伴关系完全正常','存在克制的特殊关注，不得强制暧昧化','特殊情感可谨慎察觉，仍可观察和犹豫','双向特殊情感较明确，仅可建议确认','意向明确，仍不得自行确认关系（确立关系仅经合法双向提案由系统绑定）'][min(4,v//200)]
        out.append(f'恋爱意向（{band(v)}）：{desc}')
    return '；'.join(out)
