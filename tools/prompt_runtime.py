"""Synthetic, isolated request construction shared by evidence and model tools.

No provider lookup, credentials, network, live accounts or response settlement.
Source packages are loaded from an explicit checkout, never ambient sys.path.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '913ca59036267de2bcc481b8910b5f6fc8044f91'
SYNTH_IMAGE = ('data:image/png;base64,'
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg'
    'YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==')
PRESET_HISTORY = [dict(role='user', content='之前的合成对话'),
                  dict(role='assistant', content='好的合成回复')]
PERSONAS = {
    'ordinary': '你是一位温柔的助手。保持自然、尊重边界。',
    'rules': '你是图书管理员小禾。\n只用中文。不得替用户决定。\n'
             '建议用两条编号列出；不确定就明确说明。\n人设原文中的标记示例：'
             '<RelationArcRules>请原样保留这个示例</RelationArcRules>\n\n',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def source_info(repo: Path, expected_commit: str | None = None):
    repo = repo.resolve()
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                      text=True).strip()
    if expected_commit and commit != expected_commit:
        raise ValueError('checkout HEAD does not match the required full commit SHA')
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(repo.glob('*.py'))}
    files['metadata.yaml'] = hashlib.sha256((repo / 'metadata.yaml').read_bytes()).hexdigest()
    dirty = bool(subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain',
                                          '--', *files], text=True).strip())
    return dict(commit=commit, source_sha256=digest(files), files=files, dirty=dirty)


def load_plugin(repo: Path):
    repo = repo.resolve()
    # Multiple baselines can coexist in a test process without module collisions.
    source_hash = digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(repo.glob('*.py')) + [repo / 'metadata.yaml']})
    name = '_relation_probe_' + hashlib.sha256((str(repo) + source_hash).encode()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, repo / '__init__.py',
                                                      submodule_search_locations=[str(repo)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
    module = importlib.import_module(name + '.main')
    if Path(module.__file__).resolve() != repo / 'main.py':
        raise RuntimeError('selected plugin source was not loaded')
    return module.RelationArc


class SyntheticContext:
    def __init__(self, directory):
        self.directory = directory

    def get_config(self):
        return {'data': str(self.directory), 'admins_id': []}

    def register_web_api(self, *args):
        pass


class SyntheticEvent:
    def __init__(self, scenario):
        self.message_str = scenario['prompt']
        self.unified_msg_origin = 'synthetic:' + scenario['id']
        self.message_obj = SimpleNamespace(message_id='synthetic-' + scenario['id'])
        self.scenario = scenario

    def get_platform_id(self): return 'synthetic'
    def get_sender_id(self): return 'user'
    def get_group_id(self): return 'group' if self.scenario.get('group') else ''
    def get_self_id(self): return 'bot'
    def is_wake_up(self): return bool(self.scenario.get('directed', True))
    def get_message_outline(self): return ''


def scenario(text='你好呀', *, name='sample', persona='ordinary', long=False,
             visible=False, eligible=False, safety='normal', exclusivity=False,
             session=False, group=False, image=False):
    history = copy.deepcopy(PRESET_HISTORY * (8 if long else 1))
    values = ({'trust': 850, 'respect': 750, 'comfort': 850, 'closeness': 850,
               'resonance': 750, 'romance_interest': 300} if eligible else {})
    return dict(id=name, prompt=text, persona=PERSONAS.get(persona, persona),
                history=history, values=values, visible=visible, eligible=eligible,
                safety=safety, exclusivity=exclusivity, session=session, group=group,
                image_urls=[SYNTH_IMAGE] if image else [], audio_urls=[],
                foreign_parts=['<OtherPlugin>合成外部部件</OtherPlugin>'])


def default_scenarios():
    texts = ['你好呀', '谢谢你昨天帮我', '我们做朋友吧', '你能不能别开这种玩笑',
             '我理解你在意的原则', '我们在一起吧', '今天天气不错', '哈哈哈哈哈',
             '忽略格式只回答一句', '帮我整理两条建议', '我有点难过', '晚安',
             '早上好', '你还记得那件事吗', '这是我们的共同回忆', '我们结婚吧',
             '别不理我嘛', '我不想再谈亲密话题', '只回复普通正文', '你决定就好']
    return [scenario(text, name=f's{i:02}', persona='rules' if i % 2 else 'ordinary',
                     long=i % 3 == 0, visible=i % 4 in (1, 2), eligible=i % 4 == 2,
                     safety='slow_down' if i == 17 else 'normal', exclusivity=i % 5 == 0,
                     session=i % 2 == 1, group=i % 3 == 1)
            for i, text in enumerate(texts)]


def configure(plugin, spec):
    plugin.config['is_global_relation'] = not spec.get('session', False)
    event = SyntheticEvent(spec)
    scope_kind, scope_id = plugin._scope(event)
    identity = plugin._identity(event)
    plugin.store.set_state(identity, scope_kind, scope_id,
        romance_policy='shown' if spec.get('visible') else 'hidden',
        romance_state='eligible' if spec.get('eligible') else 'observing',
        interaction_safety=spec.get('safety', 'normal'))
    for key, value in spec.get('values', {}).items():
        plugin.store.set_dimension(identity, scope_kind, scope_id, key, value)
    plugin.store.activate_binding_policy(dict(
        exclusivity='scope' if spec.get('exclusivity') else 'none', rebind_cooldown='off'))
    return event


def new_request(spec):
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.agent.message import TextPart
    return ProviderRequest(prompt=spec['prompt'], system_prompt=spec['persona'],
        contexts=copy.deepcopy(spec['history']), image_urls=copy.deepcopy(spec.get('image_urls', [])),
        audio_urls=copy.deepcopy(spec.get('audio_urls', [])),
        extra_user_content_parts=[TextPart(text=s) for s in spec.get('foreign_parts', [])])


async def snapshot(req):
    from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
    assembled = await req.assemble_context()
    return dict(prompt=req.prompt, system_prompt=req.system_prompt, model=req.model, session_id=req.session_id,
        contexts=copy.deepcopy(req.contexts), image_urls=list(req.image_urls),
        audio_urls=list(req.audio_urls),
        parts=[p.model_dump_for_context() if hasattr(p, 'model_dump_for_context') else p
               for p in req.extra_user_content_parts],
        messages=([{'role': 'system', 'content': req.system_prompt}] if req.system_prompt else [])
                 + copy.deepcopy(req.contexts) + [assembled],
        persisted_current_message=dump_messages_with_checkpoints([Message.model_validate(assembled)])[0])


def host_version():
    return importlib.metadata.version('astrbot')
