"""Explicit isolated OpenAI-compatible provider factory (also for DeepSeek).

Called ONLY by an authorized run. Required environment variables:
RELATION_AB_API_KEY, RELATION_AB_BASE_URL, RELATION_AB_MODEL, RELATION_AB_CHANNEL.
RELATION_AB_PARAMETERS_JSON optionally supplies the configured generation body.
No live AstrBot configuration is read or changed. No request occurs in make().
"""
import json
import os
from provider_adapters import HostAdapter


class OwnedOpenAIAdapter(HostAdapter):
    async def close(self):
        await self.provider.client.close()


def make():
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    values = {key: os.environ.get('RELATION_AB_' + key, '')
              for key in ('API_KEY', 'BASE_URL', 'MODEL', 'CHANNEL')}
    if not all(values.values()):
        raise ValueError('the four explicitly scoped environment variables are required')
    params = json.loads(os.environ.get('RELATION_AB_PARAMETERS_JSON', '{}'))
    if not isinstance(params, dict) or any(k in params for k in ('messages', 'model', 'stream', 'api_key')):
        raise ValueError('generation parameters must not override the controlled request')
    provider = ProviderOpenAIOfficial(dict(id='relation-isolated-ab', type='openai_chat_completion',
        key=[values['API_KEY']], api_base=values['BASE_URL'], model=values['MODEL'],
        custom_extra_body=params, timeout=120), {})
    return OwnedOpenAIAdapter(provider, channel=values['CHANNEL'])
