from __future__ import annotations
import json,re
from dataclasses import dataclass
from typing import Any
from .relation_engine import DIMENSIONS

BLOCK=re.compile(r'<relation_judgment>\s*(.*?)\s*</relation_judgment>',re.I|re.S)
@dataclass(frozen=True)
class ParsedJudgment:
    effects:list[dict[str,Any]]; proposal:dict[str,Any]|None; safety_proposal:dict[str,str]|None; clean_text:str; error:str|None=None
    # Counts only; never expose evidence/reason or message content in diagnostics.
    stats:dict[str,int]|None=None

def parse_response(text:str, user_text:str, raw_limit:int=10)->ParsedJudgment:
    """Parse a tagged verdict, with a narrow recovery for a leaked leading verdict JSON.

    Recovery is intentionally limited to a complete JSON object at the very start of
    the final reply whose schema is exactly Relation Arc v1.  Ordinary prose or
    arbitrary JSON is never stripped.
    """
    if not isinstance(text, str):
        return ParsedJudgment([],None,None,'','invalid_text',{"blocks":0,"bare":0})
    matches=list(BLOCK.finditer(text))
    bare=False
    clean=BLOCK.sub('',text).strip()
    payload=None
    if matches:
        try: payload=json.loads(matches[-1].group(1))
        except (ValueError, RecursionError):return ParsedJudgment([],None,None,clean,'invalid_json',{"blocks":len(matches)})
    else:
        leading=text.lstrip()
        if leading.startswith('{'):
            try:
                candidate,end=json.JSONDecoder().raw_decode(leading)
            except (ValueError, RecursionError):
                candidate=None
            if (isinstance(candidate,dict) and type(candidate.get('schema_version')) is int and candidate.get('schema_version') in {1,2,3}
                    and isinstance(candidate.get('fact_effects'),list)):
                payload=candidate
                bare=True
                clean=leading[end:].strip()
        if not bare:
            return ParsedJudgment([],None,None,clean,stats={"blocks":0,"bare":0})
    if not isinstance(payload,dict) or type(payload.get('schema_version')) is not int or payload.get('schema_version') not in {1,2,3}:
        return ParsedJudgment([],None,None,clean,'invalid_schema',{"blocks":len(matches),"bare":int(bare)})
    effects=[]
    stats={"blocks":len(matches),"bare":int(bare),"items":0,"non_object":0,"shape_rejected":0,"empty_effects":0,"accepted":0}
    raw_items=payload.get('fact_effects',[])
    if not isinstance(raw_items,list):
        return ParsedJudgment([],None,None,clean,'invalid_effects_container',stats)
    for item in raw_items:
        stats["items"]+=1
        if not isinstance(item,dict):
            stats["non_object"]+=1
            continue
        raw=item.get('effects',{})
        if not isinstance(raw,dict):
            stats["shape_rejected"]+=1
            continue
        fixed={k:max(-raw_limit,min(raw_limit,int(v))) for k,v in raw.items() if k in DIMENSIONS and type(v) is int and v}
        if not fixed:
            stats["empty_effects"]+=1
            continue
        # Optional model-authored audit summary. It is never used as a semantic gate.
        evidence=item.get('evidence','')
        reason=item.get('reason','')
        effects.append({'evidence': evidence[:300] if isinstance(evidence,str) else '',
                        'reason': reason[:500] if isinstance(reason,str) else '',
                        'effects':fixed})
        stats["accepted"]+=1
    proposal=payload.get('relationship_proposal')
    if payload.get('schema_version') in {2,3} and isinstance(proposal, dict):
        action, type_id = proposal.get('action'), proposal.get('type_id')
        origin, mutuality = proposal.get('origin'), proposal.get('mutuality')
        summary = proposal.get('summary', '')
        if action == 'bind' and isinstance(type_id, str) and type_id and isinstance(origin, str) and origin in {'user_request','character_initiated','mutual_dialogue'} and isinstance(mutuality, str) and mutuality in {'clear','insufficient'}:
            proposal = {'action': action, 'type_id': type_id, 'origin': origin, 'mutuality': mutuality, 'summary': summary[:300] if isinstance(summary, str) else ''}
        else: proposal = None
    else:
        proposal = None
    safety=None
    raw_safety=payload.get('interaction_safety_proposal')
    if payload.get('schema_version') == 3 and isinstance(raw_safety,dict):
        level=raw_safety.get('level'); code=raw_safety.get('reason_code')
        if isinstance(level, str) and level in {'slow_down','pause_intimacy'} and isinstance(code, str) and code in {'boundary_pressure','repeated_escalation','hostility'}:
            safety={'level':level,'reason_code':code}
    return ParsedJudgment(effects,proposal,safety,clean,stats=stats)
