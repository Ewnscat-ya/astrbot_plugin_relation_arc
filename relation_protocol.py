from __future__ import annotations
import json,re
from dataclasses import dataclass
from typing import Any
from .relation_engine import DIMENSIONS

BLOCK=re.compile(r'<relation_judgment>\s*(.*?)\s*</relation_judgment>',re.I|re.S)
MAX_BLOCK_CHARS=65536
MAX_FACT_ITEMS=64

@dataclass(frozen=True)
class ParsedJudgment:
    effects:list[dict[str,Any]]; proposal:dict[str,Any]|None; safety_proposal:dict[str,str]|None; clean_text:str; error:str|None=None
    # Counts only; never expose evidence/reason or message content in diagnostics.
    stats:dict[str,int]|None=None

def strip_protocol_text(text:str)->str:
    """Display-only cleanup: drop complete control blocks and a truncated
    protocol tail — an opener with no later closer whose remainder starts a
    JSON object and does not end like a completed one. An unclosed opener
    followed by ordinary content, or a complete-looking JSON, is kept:
    cleanup must never swallow ordinary text, code samples or JSON."""
    if not isinstance(text,str): return ''
    cleaned=BLOCK.sub('',text)
    last=None
    for last_match in re.finditer(r'<relation_judgment>',cleaned,re.I): last=last_match
    if last is not None:
        remainder=cleaned[last.end():]
        if not re.search(r'</relation_judgment>',remainder,re.I) and remainder.lstrip().startswith('{'):
            try:
                json.JSONDecoder().raw_decode(remainder.lstrip())
            except (ValueError,RecursionError):
                # Incomplete JSON after the opener: a truncated protocol tail.
                cleaned=cleaned[:last.start()].rstrip()
    return cleaned.strip()

def leading_bare_json_span(text:str):
    """Span of a recoverable Relation Arc v1/v2/v3 JSON object at the very
    start of ``text`` (same conditions as parse_response recovery), else None."""
    if not isinstance(text,str): return None
    leading=text.lstrip()
    if not leading.startswith('{'): return None
    try: candidate,end=json.JSONDecoder().raw_decode(leading)
    except (ValueError,RecursionError): return None
    if (isinstance(candidate,dict) and type(candidate.get('schema_version')) is int
            and candidate.get('schema_version') in {1,2,3} and isinstance(candidate.get('fact_effects'),list)):
        start=len(text)-len(leading)
        return (start,start+end)
    return None

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
    clean=strip_protocol_text(text)
    payload=None
    if matches:
        raw_block=matches[-1].group(1)
        if len(raw_block)>MAX_BLOCK_CHARS:
            return ParsedJudgment([],None,None,clean,'invalid_json',{"blocks":len(matches),"oversize":1})
        try: payload=json.loads(raw_block)
        except (ValueError, RecursionError):return ParsedJudgment([],None,None,clean,'invalid_json',{"blocks":len(matches)})
    else:
        span=leading_bare_json_span(text)
        if span is None:
            return ParsedJudgment([],None,None,clean,stats={"blocks":0,"bare":0})
        leading=text.lstrip()
        payload=json.loads(leading[:span[1]-span[0]])
        bare=True
        clean=leading[span[1]-span[0]:].strip()
    if not isinstance(payload,dict) or type(payload.get('schema_version')) is not int or payload.get('schema_version') not in {1,2,3}:
        return ParsedJudgment([],None,None,clean,'invalid_schema',{"blocks":len(matches),"bare":int(bare)})
    effects=[]
    stats={"blocks":len(matches),"bare":int(bare),"items":0,"non_object":0,"shape_rejected":0,"empty_effects":0,"accepted":0}
    raw_items=payload.get('fact_effects',[])
    if not isinstance(raw_items,list):
        return ParsedJudgment([],None,None,clean,'invalid_effects_container',stats)
    stats["truncated_items"]=max(0,len(raw_items)-MAX_FACT_ITEMS)
    for item in raw_items[:MAX_FACT_ITEMS]:
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
