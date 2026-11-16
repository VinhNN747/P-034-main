"""Application scoped retained responses, history, and durable deletion tombstones."""
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.shared.models.core import utc_now
from src.shared.models.openai_responses import StoredOpenAIResponse


class OpenAIResponseStore:
    def __init__(self, session):
        self.session = session

    async def read(self, application_id, response_id, *, param=None):
        try:
            row = await self.session.scalar(select(StoredOpenAIResponse).where(
                StoredOpenAIResponse.id == response_id, StoredOpenAIResponse.application_id == application_id,
                StoredOpenAIResponse.deleted_at.is_(None), StoredOpenAIResponse.expires_at > utc_now()))
        except SQLAlchemyError as exc:
            raise OpenAIAPIError(503, 'Response storage is unavailable.', type='server_error',
                code='response_storage_unavailable') from exc
        if row is None or row.response_json is None:
            raise OpenAIAPIError(404, 'Response not found.', code='response_not_found', param=param)
        return row

    async def save(self, application_id, receipt, response, messages):
        # A replay must never resurrect a deleted or expired public object.
        try:
            existing = await self.session.get(StoredOpenAIResponse, response.id)
            if existing is not None:
                return
            dump = response.model_dump(mode='json')
            # Execution may have committed before the public store failed.
            # A retry must recover the resolved history from that execution,
            # even if its previous public response was deleted meanwhile.
            from src.shared.dtos.gateway import ChatMessage
            from src.shared.models.runtime import GatewayRequestRecord
            execution = await self.session.scalar(select(GatewayRequestRecord).where(
                GatewayRequestRecord.request_id == receipt.request_id,
                GatewayRequestRecord.application_id == application_id))
            if execution is not None:
                messages = [ChatMessage.model_validate(message) for message in execution.messages_json]
            history = [message.model_dump(mode='json') for message in messages if message.role not in {'system', 'developer'}]
            assistant = {'role': 'assistant', 'content': None}
            texts, refusals, calls = [], [], []
            for item in response.output:
                if item.type == 'message':
                    texts.extend(part.text for part in item.content if part.type == 'output_text')
                    refusals.extend(part.refusal for part in item.content if part.type == 'refusal')
                elif item.type == 'function_call':
                    calls.append({'id': item.call_id, 'type': 'function',
                        'function': {'name': item.name, 'arguments': item.arguments}})
            if texts:
                assistant['content'] = ''.join(texts)
            if refusals:
                assistant['refusal'] = ''.join(refusals)
            if calls:
                assistant['tool_calls'] = calls
            if texts or refusals or calls:
                history.append(assistant)
            self.session.add(StoredOpenAIResponse(id=response.id, application_id=application_id,
                request_id=receipt.request_id, response_json=dump, history_json=history,
                expires_at=utc_now() + timedelta(days=30)))
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.session.get(StoredOpenAIResponse, response.id)
            if existing is None or existing.application_id != application_id:
                raise OpenAIAPIError(503, 'Response storage is unavailable.', type='server_error', code='response_storage_unavailable')
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise OpenAIAPIError(503, 'Response storage is unavailable. Retry with the same idempotency key.',
                type='server_error', code='response_storage_unavailable') from exc

    async def delete(self, application_id, response_id):
        await self.read(application_id, response_id)
        try:
            await self.session.execute(update(StoredOpenAIResponse).where(
                StoredOpenAIResponse.id == response_id, StoredOpenAIResponse.application_id == application_id)
                .values(deleted_at=utc_now(), response_json=None, history_json=None))
            await self.session.commit()
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise OpenAIAPIError(503, 'Response deletion could not be persisted.', type='server_error',
                code='response_storage_unavailable') from exc

    async def expire(self):
        # Keep a small tombstone while operation receipts can still be replayed.
        result = await self.session.execute(update(StoredOpenAIResponse).where(
            StoredOpenAIResponse.expires_at <= utc_now(), StoredOpenAIResponse.deleted_at.is_(None))
            .values(deleted_at=utc_now(), response_json=None, history_json=None))
        await self.session.commit()
        return result.rowcount
