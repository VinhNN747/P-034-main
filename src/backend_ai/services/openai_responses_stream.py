from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from src.backend_ai.dtos.openai_responses import ResponseObject


def iter_response_events(response: ResponseObject) -> Iterator[dict[str, Any]]:
    """Replay the complete public protocol, including tools and refusals."""
    initial = response.model_dump(mode='json')
    initial.update(status='in_progress', output=[], usage=None, gateway_usage=None, incomplete_details=None)
    sequence = 0

    def event(kind, **fields):
        nonlocal sequence
        result = {'type': kind, 'sequence_number': sequence, **fields}
        sequence += 1
        return result

    yield event('response.created', response=initial)
    yield event('response.in_progress', response=initial)
    for output_index, output in enumerate(response.output):
        item = output.model_dump(mode='json')
        progress = {**item, 'status': 'in_progress'}
        if output.type == 'message':
            progress['content'] = []
        else:
            progress['arguments'] = ''
        yield event('response.output_item.added', output_index=output_index, item=progress)
        if output.type == 'message':
            for content_index, part in enumerate(output.content):
                final_part = part.model_dump(mode='json')
                field = 'text' if part.type == 'output_text' else 'refusal'
                empty_part = {**final_part, field: ''}
                common = {'output_index': output_index, 'content_index': content_index, 'item_id': output.id}
                yield event('response.content_part.added', **common, part=empty_part)
                if part.type == 'output_text':
                    yield event('response.output_text.delta', **common, delta=part.text, logprobs=[])
                    yield event('response.output_text.done', **common, text=part.text, logprobs=[])
                else:
                    yield event('response.refusal.delta', **common, delta=part.refusal)
                    yield event('response.refusal.done', **common, refusal=part.refusal)
                yield event('response.content_part.done', **common, part=final_part)
        else:
            common = {'output_index': output_index, 'item_id': output.id}
            yield event('response.function_call_arguments.delta', **common, delta=output.arguments)
            yield event('response.function_call_arguments.done', **common, arguments=output.arguments, name=output.name)
        yield event('response.output_item.done', output_index=output_index, item=item)
    yield event('response.completed' if response.status == 'completed' else 'response.incomplete',
                response=response.model_dump(mode='json'))


def encode_response_event(event: dict[str, Any]) -> str:
    event_type = event.get('type')
    if not isinstance(event_type, str) or not (event_type.startswith('response.') or event_type == 'error'):
        raise ValueError('Invalid Responses SSE event type')
    data = json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return f'event: {event_type}\ndata: {data}\n\n'
