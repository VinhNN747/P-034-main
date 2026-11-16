"""Translate final producer deltas to the Responses event protocol."""
import re
import time

from src.backend_ai.dtos.openai_responses import ResponseObject
from src.backend_ai.services.openai_responses_stream import encode_response_event


class ResponseStreamEncoder:
    def __init__(self, stream, payload):
        self.stream, self.payload = stream, payload
        self.normalized = re.sub(r'[^a-zA-Z0-9]', '', stream.request_id)
        self.sequence = 0
        self.text_kind = None
        self.items = []
        self.tool_items = {}

    def event(self, kind, **fields):
        event = {'type': kind, 'sequence_number': self.sequence, **fields}
        self.sequence += 1
        return encode_response_event(event)

    def initial(self):
        response = ResponseObject(id='resp_' + self.normalized, created_at=time.time(),
            model=self.stream.model or self.payload.model, output=[], status='completed',
            store=bool(self.payload.store), text=self.payload.text).model_dump(mode='json')
        response.update(status='in_progress')
        return [self.event('response.created', response=response), self.event('response.in_progress', response=response)]

    def failure(self, code, message):
        response = ResponseObject(id='resp_' + self.normalized, created_at=time.time(),
            model=self.stream.model or self.payload.model, output=[], status='completed',
            store=bool(self.payload.store), text=self.payload.text).model_dump(mode='json')
        response.update(status='failed', error={'code':'server_error', 'message':message}, gateway_error_code=code)
        return self.event('response.failed', response=response)

    def delta(self, delta):
        events = []
        if delta.type in {'text_delta', 'refusal_delta'}:
            kind = 'output_text' if delta.type == 'text_delta' else 'refusal'
            if self.text_kind is None:
                self.text_kind = kind
                item_id = 'msg_' + self.normalized + '_0'
                self.items.append(item_id)
                item = {'type': 'message', 'id': item_id, 'role': 'assistant', 'status': 'in_progress', 'content': []}
                events.append(self.event('response.output_item.added', output_index=len(self.items)-1, item=item))
                part = {'type': kind, **({'text': '', 'annotations': [], 'logprobs': []} if kind == 'output_text' else {'refusal': ''})}
                events.append(self.event('response.content_part.added', output_index=len(self.items)-1,
                    content_index=0, item_id=item_id, part=part))
            item_id = 'msg_' + self.normalized + '_0'
            fields = {'output_index': self.items.index(item_id), 'content_index': 0, 'item_id': item_id, 'delta': delta.text}
            if kind == 'output_text':
                fields['logprobs'] = []
            events.append(self.event('response.' + kind + '.delta', **fields))
        elif delta.type == 'tool_delta':
            tool = delta.tool_delta
            index = tool['index']
            saved = self.tool_items.setdefault(index, {'id': '', 'name': '', 'arguments': '', 'added': False})
            saved['id'] += tool.get('id') or ''
            function = tool.get('function') or {}
            saved['name'] += function.get('name') or ''
            saved['arguments'] += function.get('arguments') or ''
            if not saved['added'] and saved['id'] and saved['name']:
                saved['added'] = True
                item_id = f'fc_{self.normalized}_{index}'
                self.items.append(item_id)
                events.append(self.event('response.output_item.added', output_index=len(self.items)-1,
                    item={'type': 'function_call', 'id': item_id, 'call_id': saved['id'], 'name': saved['name'],
                          'arguments': '', 'status': 'in_progress'}))
                argument_delta = saved['arguments']
            else:
                argument_delta = function.get('arguments') or ''
            if saved['added'] and argument_delta:
                item_id = f'fc_{self.normalized}_{index}'
                events.append(self.event('response.function_call_arguments.delta', output_index=self.items.index(item_id),
                    item_id=item_id, delta=argument_delta))
        return events

    def finish(self, response):
        # Final item order follows the order in which items were announced.
        by_id = {item.id: item for item in response.output}
        if set(by_id) != set(self.items):
            raise ValueError('Final response items do not match streamed output')
        response.output = [by_id[item_id] for item_id in self.items]
        for index, output in enumerate(response.output):
            if output.type == 'message':
                for content_index, part in enumerate(output.content):
                    common = {'output_index': index, 'content_index': content_index, 'item_id': output.id}
                    if part.type == 'output_text':
                        yield self.event('response.output_text.done', **common, text=part.text, logprobs=[])
                    else:
                        yield self.event('response.refusal.done', **common, refusal=part.refusal)
                    yield self.event('response.content_part.done', **common, part=part.model_dump(mode='json'))
            else:
                yield self.event('response.function_call_arguments.done', output_index=index,
                    item_id=output.id, arguments=output.arguments, name=output.name)
            yield self.event('response.output_item.done', output_index=index, item=output.model_dump(mode='json'))
        yield self.event('response.completed' if response.status == 'completed' else 'response.incomplete',
                         response=response.model_dump(mode='json'))
