"""Summarize older conversation turns without destroying the transcript."""
import json

from kova.models import ModelError


async def compact(store, chat_id, client, *, context_tokens, reserved_chars=0, threshold=0.75, force=False):
    summary, previous_cutoff, rows = store.context_snapshot(chat_id)
    messages = [m for _, m in rows]
    size = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) + len(summary or '') + reserved_chars
    threshold = max(0.1, min(float(threshold), 1.0))
    if not force and (size <= context_tokens * 4 * threshold or reserved_chars >= context_tokens * 4 * threshold):
        return None
    # Only split at a user-turn boundary. Always retain at least the latest turn.
    starts = [i for i, m in enumerate(messages) if m['role'] == 'user']
    if len(starts) < 2:
        return None
    cut = starts[-2] if len(starts) >= 3 else starts[-1]
    older = rows[:cut]
    if not older:
        return None
    # Keep tool output useful but bounded; never silently omit conversation turns.
    budget = max(1000, int(context_tokens * 4 * 0.5))
    entries = []
    for _, msg in older:
        data = dict(msg)
        if data['role'] == 'tool':
            data['content'] = (data.get('content') or '')[:500]
        entries.append(json.dumps(data, ensure_ascii=False))
    source = '\n'.join(entries)
    instruction = ('Summarize the earlier conversation faithfully and concisely. Preserve decisions, '
                   'file paths, open tasks, user preferences and important tool findings. '
                   'Do not invent facts. Integrate any previous summary without repeating it.')
    request = [{'role': 'system', 'content': instruction}, {'role': 'user', 'content':
               'Previous summary:\n' + (summary or '(none)') + '\nEarlier messages:\n' + source}]
    try:
        parts = []
        async for event in client.stream(request):
            if event.type == 'text':
                parts.append(event.text)
        result = ''.join(parts).strip()
        # Reject empty or expanding summaries; never recursively compact in this call.
        if not result or len(result) >= len(source) or len(result) > budget:
            return None
        if not store.save_compaction(chat_id, previous_cutoff, older[-1][0], result):
            return None
        return {'type': 'compacted', 'count': len(older), 'summary': result}
    except ModelError:
        return None
