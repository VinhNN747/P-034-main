import pytest

from src.backend_ai.gateway.routing.session_affinity import (
    SessionAffinity,
    SessionAffinityStore,
    conversation_key,
)
from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService
from src.shared.dtos.gateway import GatewayRequest
from tests.test_gateway.test_jev_db_routing_and_decomposition import _task
from tests.test_gateway.test_routing_service import routing_context  # noqa: F401 - fixture


def _chat(*messages, metadata=None) -> GatewayRequest:
    return GatewayRequest(request_id="r", application_id="app-1", messages=list(messages), metadata=metadata or {})


SYSTEM = {"role": "system", "content": "You are a payments architect."}
TURN_1 = {"role": "user", "content": "Design a payment system with sagas."}


def test_conversation_key_is_stable_across_turns_and_separates_conversations():
    first = conversation_key(_chat(SYSTEM, TURN_1))
    later = conversation_key(_chat(SYSTEM, TURN_1, {"role": "assistant", "content": "Here is the design..."},
                                   {"role": "user", "content": "ok, continue"}))
    assert first == later
    assert conversation_key(_chat(SYSTEM, {"role": "user", "content": "Write a leave email."})) != first
    # The end user separates two people who open with the same messages.
    alice = conversation_key(_chat(SYSTEM, TURN_1, metadata={"client_metadata": {"end_user": "alice"}}))
    assert alice != first
    # An explicit session id wins over the opening messages.
    named = _chat(SYSTEM, TURN_1, metadata={"client_metadata": {"client_metadata": {"session_id": "s-42"}}})
    other_opening = _chat({"role": "user", "content": "different"},
                          metadata={"client_metadata": {"client_metadata": {"session_id": "s-42"}}})
    assert conversation_key(named) == conversation_key(other_opening) != first
    # A bare prompt is a single request, not a conversation.
    assert conversation_key(GatewayRequest(request_id="r", application_id="app-1", prompt="hi")) is None


def test_store_expires_and_caps_entries():
    store = SessionAffinityStore(ttl_seconds=10, max_entries=2)
    store.put("a", "m1", now=0)
    assert store.get("a", now=5) == "m1"
    assert store.get("a", now=11) is None
    store.put("a", "m1", now=20)
    store.put("b", "m2", now=21)
    store.put("c", "m3", now=22)
    assert store.get("a", now=23) is None and store.get("c", now=23) == "m3"


def test_only_the_first_routing_call_of_a_request_uses_affinity():
    affinity = SessionAffinity(SessionAffinityStore(), "key")
    assert affinity.claim("task-1") is True
    assert affinity.claim("task-2") is False
    assert affinity.task_id == "task-1"


def _result(task_id, status, executed_model):
    from types import SimpleNamespace

    return SimpleNamespace(task_id=task_id, status=status, executed_model=executed_model)


def test_only_a_successful_turn_is_remembered_with_the_model_that_answered():
    store = SessionAffinityStore(ttl_seconds=1800)
    affinity = SessionAffinity(store, "conv")
    # Nothing was routed through affinity: nothing to record.
    assert affinity.record_outcome([_result("task-1", "success", "m-primary")]) is None
    affinity.claim("task-1")

    # A failed turn must not pin the model that just failed.
    assert affinity.record_outcome([_result("task-1", "failed", None)]) is None
    assert store.get("conv") is None

    # Another subtask of a decomposed request does not count.
    assert affinity.record_outcome([_result("task-2", "success", "m-other")]) is None

    # When a fallback served the turn, the fallback is what the conversation keeps (as Azure does).
    assert affinity.record_outcome([_result("task-1", "success", "m-fallback")]) == "m-fallback"
    assert store.get("conv") == "m-fallback"


def test_entry_expires_after_the_last_successful_turn():
    store = SessionAffinityStore(ttl_seconds=1800)
    store.put("conv", "m1", now=0)
    store.put("conv", "m1", now=1500)  # a successful turn refreshes it
    assert store.get("conv", now=3000) == "m1"
    assert store.get("conv", now=3301) is None


async def _label(session, ids_and_tiers):
    from src.shared.models.gateway import Model

    for model_id, label in ids_and_tiers:
        (await session.get(Model, model_id)).quality_tier = label
    await session.commit()


@pytest.mark.asyncio
async def test_same_tier_turn_keeps_the_previous_model(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    # cheap and quality share the small tier, the external model is the large tier.
    await _label(session, [(routing_context["cheap"], "simple"), (routing_context["quality"], "simple"),
                           (routing_context["blocked"], "hard")])
    store = SessionAffinityStore()

    def service():
        return DatabaseRoutingService(session, app, affinity=SessionAffinity(store, "conversation-1"))

    # No history: the usual ranking picks one of the two small models. Routing only reads the
    # store; it is written after a successful execution.
    first = await service().route(_task())
    small = {routing_context["cheap"], routing_context["quality"]}
    assert first.selected_model in small
    assert store.get("conversation-1") is None

    # Pretend an earlier turn ran on the other small model: it is kept while the tier is the same.
    other = (small - {first.selected_model}).pop()
    store.put("conversation-1", other)
    kept = await service().route(_task())
    assert kept.selected_model == other
    assert "session affinity kept" in kept.reason
    # The ranking's own first choice is still the first fallback.
    assert kept.fallback_models[0] == first.selected_model

    # A hard turn still moves up to the large tier: affinity never overrides the tier.
    hard_affinity = SessionAffinity(store, "conversation-1")
    hard = await DatabaseRoutingService(session, app, affinity=hard_affinity).route(_task(reasoning=2.8))
    assert hard.selected_model == routing_context["blocked"]
    assert "session affinity" not in hard.reason
    # ... and once that turn succeeds, the conversation remembers the large model.
    hard_affinity.record_outcome([_result(hard.task_id, "success", hard.selected_model)])
    assert store.get("conversation-1") == routing_context["blocked"]


@pytest.mark.asyncio
async def test_affinity_ignores_a_model_that_is_no_longer_eligible(routing_context):  # noqa: F811
    from src.shared.dtos.gateway import RequestConstraints

    session, app = routing_context["session"], routing_context["application_id"]
    await _label(session, [(routing_context["cheap"], "simple"), (routing_context["quality"], "simple"),
                           (routing_context["blocked"], "hard")])
    store = SessionAffinityStore()
    # The previous model is now blocked for this request: routing falls back to the ranking.
    for previous, other in ((routing_context["quality"], routing_context["cheap"]),
                            (routing_context["cheap"], routing_context["quality"])):
        store.put("conversation-2", previous)
        decision = await DatabaseRoutingService(session, app, affinity=SessionAffinity(store, "conversation-2")).route(
            _task(), RequestConstraints(blocked_models={previous})
        )
        assert decision.selected_model == other
        assert "session affinity" not in decision.reason
