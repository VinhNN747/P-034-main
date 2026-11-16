"""Reject oversized Content-Length and chunked bodies before multipart parsing."""
from starlette.types import ASGIApp, Receive, Scope, Send

from src.backend_ai.routers.openai_errors import OpenAIAPIError


class OpenAIBodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or not path.startswith('/v1/'):
            return await self.app(scope, receive, send)
        from src.shared.config import get_settings
        settings = get_settings()
        limit = (settings.gateway_file_multipart_max_body_bytes if path.rstrip('/') == '/v1/files'
            else settings.gateway_embeddings_max_body_bytes if path == '/v1/embeddings'
            else settings.gateway_openai_max_body_bytes)
        headers = dict(scope.get('headers', []))
        declared = headers.get(b'content-length')
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                raise OpenAIAPIError(400, 'Invalid Content-Length.', code='invalid_request') from None
            if size > limit:
                raise OpenAIAPIError(413, 'Request body exceeds the endpoint limit.', code='request_too_large')
        consumed = 0
        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message['type'] == 'http.request':
                consumed += len(message.get('body', b''))
                if consumed > limit:
                    raise OpenAIAPIError(413, 'Request body exceeds the endpoint limit.', code='request_too_large')
            return message
        await self.app(scope, bounded_receive, send)
