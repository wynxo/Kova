"""Native wire formats normalized to the same ModelEvent stream as local models.

Each adapter declares the capabilities its wire format carries, so provider
kinds (see :mod:`kova.providers`) and UI layers can ask instead of guessing.
"""
from __future__ import annotations

import json

# ``tools``: the wire format carries tool schemas and tool calls.
# ``reasoning``: it carries a dedicated reasoning stream.
# The OpenAI chat-completions format has no reasoning field: reasoning reaches
# Kova only when a provider sends its own ``reasoning_content``/``reasoning``
# extension, which the provider kind declares.
ADAPTER_CAPABILITIES = {
    'openai': frozenset({'tools'}),
    'anthropic': frozenset({'tools', 'reasoning'}),
    'responses': frozenset({'tools', 'reasoning'}),
}


def adapter_capabilities(adapter: str) -> frozenset[str]:
    """What one adapter's wire format can carry; unknown adapters use the OpenAI format."""
    return ADAPTER_CAPABILITIES.get(adapter, ADAPTER_CAPABILITIES['openai'])


def _tool_input(arguments) -> dict:
    """Best-effort parse of stored tool arguments.

    History can hold arguments a model streamed badly, so one bad record must
    never abort every later request in the chat.
    """
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def anthropic_request(model, messages, tools):
    system, converted = [], []
    for message in messages:
        role = message['role']
        if role == 'system':
            system.append(message.get('content') or '')
            continue
        if role == 'tool':
            blocks = [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': message.get('content') or ''}]
            role = 'user'
        else:
            blocks = []
            if message.get('content'):
                blocks.append({'type': 'text', 'text': message['content']})
            for call in message.get('tool_calls') or []:
                function = call['function']
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': function['name'],
                               'input': _tool_input(function.get('arguments'))})
        if converted and converted[-1]['role'] == role:
            converted[-1]['content'].extend(blocks)
        else:
            converted.append({'role': role, 'content': blocks})
    payload = {'model': model, 'messages': converted, 'max_tokens': 8192, 'stream': True}
    if system:
        payload['system'] = '\n\n'.join(system)
    if tools:
        payload['tools'] = [{'name': t['function']['name'], 'description': t['function'].get('description', ''),
                             'input_schema': t['function']['parameters']} for t in tools]
    return 'messages', payload


def responses_request(model, messages, tools):
    instructions, items = [], []
    for message in messages:
        role = message['role']
        if role == 'system':
            instructions.append(message.get('content') or '')
        elif role == 'tool':
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message.get('content') or ''})
        else:
            if message.get('content'):
                items.append({'role': role, 'content': message['content']})
            for call in message.get('tool_calls') or []:
                items.append({'type': 'function_call', 'call_id': call['id'], 'name': call['function']['name'],
                              'arguments': call['function']['arguments']})
    payload = {'model': model, 'input': items, 'instructions': '\n\n'.join(instructions), 'store': False, 'stream': True}
    if tools:
        payload['tools'] = [{'type': 'function', **t['function']} for t in tools]
    return 'responses', payload


async def stream_native(client, url, payload, adapter):
    from kova.models import ModelError, ModelEvent
    calls = {}
    complete = False
    has_text = False
    async with client.stream('POST', url, json=payload) as response:
        if response.status_code >= 400:
            raise ModelError(f'Provider refused the request: HTTP {response.status_code}')
        async for line in response.aiter_lines():
            if not line.startswith('data:') or line[5:].strip() == '[DONE]':
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if not isinstance(event, dict):
                raise ModelError('Provider returned an invalid stream. Run kova doctor, then retry.')
            kind = event.get('type', '')
            if kind in ('error', 'response.failed', 'response.incomplete'):
                raise ModelError('Provider could not complete the response. Check access and usage limits.')
            if kind in ('message_stop', 'response.completed'):
                complete = True
            if adapter == 'anthropic':
                index = event.get('index', 0)
                block = event.get('content_block') or {}
                delta = event.get('delta') or {}
                if kind == 'content_block_start' and block.get('type') == 'tool_use':
                    calls[index] = {'id': block['id'], 'name': block['name'], 'arguments': '', '_input': block.get('input', {})}
                if delta.get('type') == 'input_json_delta' and index in calls:
                    calls[index]['arguments'] += delta.get('partial_json', '')
                    yield ModelEvent('tool_call_delta', tool_call={'index': index, **calls[index]})
                if delta.get('type') == 'text_delta':
                    has_text = has_text or bool(delta.get('text', '').strip())
                    yield ModelEvent('text', delta.get('text', ''))
                if delta.get('type') == 'thinking_delta':
                    yield ModelEvent('reasoning', delta.get('thinking', ''))
            else:
                index = event.get('output_index', 0)
                item = event.get('item') or {}
                if kind == 'response.output_item.added' and item.get('type') == 'function_call':
                    calls[index] = {'id': item['call_id'], 'name': item['name'], 'arguments': item.get('arguments', '')}
                elif kind == 'response.function_call_arguments.delta' and index in calls:
                    calls[index]['arguments'] += event.get('delta', '')
                    yield ModelEvent('tool_call_delta', tool_call={'index': index, **calls[index]})
                elif kind == 'response.output_item.done' and item.get('type') == 'function_call':
                    calls[index] = {'id': item['call_id'], 'name': item['name'], 'arguments': item.get('arguments', '{}')}
                elif kind == 'response.output_text.delta':
                    has_text = has_text or bool(event.get('delta', '').strip())
                    yield ModelEvent('text', event.get('delta', ''))
                elif kind in ('response.reasoning_text.delta', 'response.reasoning_summary_text.delta'):
                    yield ModelEvent('reasoning', event.get('delta', ''))
    if not complete:
        raise ModelError('Provider stream ended before the response completed. Please retry.')
    if not has_text and not calls:
        raise ModelError('Provider returned an empty reply. Choose a chat model with kova setup, then retry.')
    for call in calls.values():
        initial = call.pop('_input', {})
        if not call['arguments']:
            call['arguments'] = json.dumps(initial)
        yield ModelEvent('tool_call', tool_call=call)
    yield ModelEvent('done')
