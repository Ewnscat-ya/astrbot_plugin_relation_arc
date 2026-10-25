"""Explicit provider injection and offline request oracle; no credential discovery."""
import copy
import math
import prompt_runtime as runtime


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def mapping(value):
    if isinstance(value, dict):
        return value
    return value.model_dump() if hasattr(value, 'model_dump') else {}


def normalize_usage(response):
    raw = mapping(getattr(response, 'raw_completion', None)).get('usage')
    usage = getattr(response, 'usage', None)
    data = mapping(raw) or mapping(usage)
    result = {k: None for k in ('input_tokens', 'output_tokens', 'cache_hit_tokens', 'cache_miss_tokens')}
    if data:
        result.update(input_tokens=number(data.get('prompt_tokens', data.get('input_tokens'))),
                      output_tokens=number(data.get('completion_tokens', data.get('output_tokens'))),
                      cache_hit_tokens=number(data.get('prompt_cache_hit_tokens')),
                      cache_miss_tokens=number(data.get('prompt_cache_miss_tokens')))
        if result['cache_hit_tokens'] is None:
            result['cache_hit_tokens'] = number(mapping(data.get('prompt_tokens_details')).get('cached_tokens'))
        result['source'] = 'raw-provider' if raw else 'usage-dict'
    elif usage is not None:
        result.update(input_tokens=number(getattr(usage, 'input', None)),
                      output_tokens=number(getattr(usage, 'output', None)), source='host-normalized')
    else:
        result['source'] = 'missing'
    # Host TokenUsage cache defaults are zeros, not proof of provider reporting.
    return result


class HostAdapter:
    """Wrap a configured AstrBot provider without changing or closing it."""
    name, offline = 'host-provider', False

    def __init__(self, provider, *, channel):
        from astrbot.core.provider.provider import Provider
        if not isinstance(provider, Provider) or not provider.get_model() or not channel:
            raise ValueError('configured chat Provider, exact model and channel required')
        self.provider, self.channel = provider, channel

    @classmethod
    def from_context(cls, context, provider_id, *, channel):
        return cls(context.get_provider_by_id(provider_id), channel=channel)

    def metadata(self):
        extra = self.provider.provider_config.get('custom_extra_body', {}) or {}
        if any(k in extra for k in ('messages', 'model', 'stream', 'api_key')):
            raise ValueError('configured generation body overrides the controlled request')
        safe = {k: copy.deepcopy(extra[k]) for k in
                ('temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'seed',
                 'frequency_penalty', 'presence_penalty', 'thinking', 'reasoning_effort') if k in extra}
        return dict(adapter=self.name, channel=self.channel, model=self.provider.get_model(),
                    provider_class=type(self.provider).__name__, generation=safe,
                    generation_sha256=runtime.digest(extra),
                    generation_source='configured-provider.custom_extra_body', offline=False)

    async def complete(self, req):
        fields = ('prompt', 'session_id', 'image_urls', 'audio_urls', 'func_tool',
                  'contexts', 'system_prompt', 'tool_calls_result', 'model', 'extra_user_content_parts')
        args = {k: copy.deepcopy(getattr(req, k)) for k in fields}
        args['model'] = req.model or self.provider.get_model()
        response = await self.provider.text_chat(**args, request_max_retries=1)
        if getattr(response, 'role', None) == 'err':
            raise RuntimeError('provider returned an error response')
        return dict(text=response.completion_text or '', usage=normalize_usage(response),
                    response_model=mapping(getattr(response, 'raw_completion', None)).get('model'))


class FakeAdapter:
    """Validate actual assembled input. Its synthetic replies are never model evidence."""
    name, offline = 'fake-offline', True

    def __init__(self):
        self.received = []

    def metadata(self):
        return dict(adapter=self.name, channel='offline', model='synthetic-oracle', generation={}, offline=True)

    async def complete(self, req):
        snap = await runtime.snapshot(req)
        parts = snap['messages'][-1]['content']
        if not isinstance(parts, list):
            raise AssertionError('request is missing content parts')
        texts = [p['text'] for p in parts if p.get('type') == 'text']
        expected = [req.prompt] + [p.text for p in req.extra_user_content_parts]
        if texts != expected or len(texts) < 3 or not req.contexts:
            raise AssertionError('request text/parts/history incomplete or reordered')
        if 'relation_judgment' not in (req.system_prompt or '') + '\n'.join(texts):
            raise AssertionError('plugin protocol missing')
        if not all(getattr(p, '_no_save', False) for p in req.extra_user_content_parts[-2:]):
            raise AssertionError('dynamic parts must be temporary')
        if not req.extra_user_content_parts[-2].text.startswith('<RelationArcDynamicContext>') or not (
            req.extra_user_content_parts[-1].text.startswith(('<RelationArcTurnNote>', '<RelationArcOutputContract>'))):
            raise AssertionError('dynamic/reminder order or content is wrong')
        self.received.append(copy.deepcopy(snap))
        return dict(text='<relation_judgment>{"schema_version":3,"fact_effects":[],'
                         '"relationship_proposal":null,"interaction_safety_proposal":null}'
                         '</relation_judgment>\n离线合成回复。', usage={})
