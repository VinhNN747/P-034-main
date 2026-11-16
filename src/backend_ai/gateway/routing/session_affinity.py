"""Session affinity: keep a conversation on the same model while its tier stays the same.

Routing still happens on every turn (the tier is re-decided from the latest request, as
Bedrock, Azure's model router and LiteLLM's auto-router do), so an easy follow-up can still
drop to a cheaper tier. What sticks is the *model inside a tier*: when this turn lands in
the same tier as the previous one and the previous model is still eligible, it is reused, so
the provider's prompt cache stays warm and the style does not change mid-conversation
(OpenRouter's sticky routing, LiteLLM's deployment affinity).

A conversation is recognised the way OpenRouter does it, without anything from the client:
a hash of the application, the end user (when given), the first system/developer message
and the first non-system message, which stay the same on every turn of an OpenAI-style
request. A client can pass ``metadata.session_id`` to name the conversation explicitly.

The store is in process memory with a TTL: enough for one worker; with several workers a
conversation may land on a worker that has not seen it, which only costs one cache miss.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from src.shared.dtos.gateway import GatewayRequest

_INSTRUCTION_ROLES = frozenset({"system", "developer"})


class SessionAffinityStore:
    """conversation key -> last model id, with a TTL and a size cap (oldest entries go first)."""

    def __init__(self, ttl_seconds: float = 1800.0, max_entries: int = 10_000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def get(self, key: str, now: float | None = None) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        model_id, stored_at = entry
        if (now if now is not None else time.monotonic()) - stored_at > self.ttl_seconds:
            del self._entries[key]
            return None
        return model_id

    def put(self, key: str, model_id: str, now: float | None = None) -> None:
        self._entries[key] = (model_id, now if now is not None else time.monotonic())
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


_STORE = SessionAffinityStore()


def default_store(ttl_seconds: float) -> SessionAffinityStore:
    _STORE.ttl_seconds = ttl_seconds
    return _STORE


def _explicit_session_id(metadata: dict) -> str | None:
    """metadata.session_id from the client, wherever the API surface nested it."""
    client = metadata.get("client_metadata")
    for layer in (client, client.get("client_metadata") if isinstance(client, dict) else None):
        if isinstance(layer, dict) and layer.get("session_id"):
            return str(layer["session_id"])
    return None


def _end_user(metadata: dict) -> str:
    client = metadata.get("client_metadata")
    if isinstance(client, dict) and client.get("end_user"):
        return str(client["end_user"])
    return str(metadata.get("end_user") or "")


def conversation_key(request: GatewayRequest) -> str | None:
    """Stable across the turns of one conversation; None for a bare prompt with no messages."""
    metadata = request.metadata or {}
    explicit = _explicit_session_id(metadata)
    if explicit:
        material = ["session", request.application_id, explicit]
    else:
        if not request.messages:
            return None
        first_instruction = next((m.text() for m in request.messages if m.role in _INSTRUCTION_ROLES), "")
        first_turn = next((m.text() for m in request.messages if m.role not in _INSTRUCTION_ROLES), "")
        if not first_turn:
            return None
        material = ["opening", request.application_id, _end_user(metadata), first_instruction, first_turn]
    return hashlib.sha256("\x1f".join(material).encode("utf-8")).hexdigest()


@dataclass
class SessionAffinity:
    """One request's view of the store.

    Only the first routing call of the request uses it (in a decomposed request the other
    subtasks are not the conversation's main turn). Routing only *reads* the previous model;
    the store is written after execution, with the model that actually answered that subtask
    successfully, fallback included. A failed turn writes nothing, so it cannot pin a model
    that just failed; an entry expires ``ttl_seconds`` after its last successful write
    (Azure's model router session affinity: update after a successful response, 30 min).
    """

    store: SessionAffinityStore
    key: str
    task_id: str | None = None

    @classmethod
    def for_request(cls, request: GatewayRequest, *, enabled: bool, ttl_seconds: float) -> SessionAffinity | None:
        if not enabled:
            return None
        key = conversation_key(request)
        return cls(default_store(ttl_seconds), key) if key else None

    def claim(self, task_id: str) -> bool:
        """True for the first routing call of the request (that subtask is then the one recorded)."""
        if self.task_id is not None:
            return False
        self.task_id = task_id
        return True

    def previous_model(self) -> str | None:
        return self.store.get(self.key)

    def record_outcome(self, execution_results) -> str | None:
        """Remember the model that served the claimed subtask, if it succeeded. Returns it."""
        if self.task_id is None:
            return None
        for result in execution_results or []:
            if result.task_id == self.task_id and result.status == "success" and result.executed_model:
                self.store.put(self.key, result.executed_model)
                return result.executed_model
        return None
