"""Conservative local intent fallback when OpenAI is unavailable."""

import re

from src.backend.services.support_knowledge_service import is_assistant_model_question, normalize_support_text
from src.shared.dtos.support_chat import (
    SupportAnalyticsQuery,
    SupportConversationState,
    SupportIntentPlan,
    SupportPeriod,
)


def explicit_support_period(question: str) -> SupportPeriod | None:
    text = normalize_support_text(question)
    for period, phrases in (
        ("yesterday", ("hom qua", "yesterday")),
        ("today", ("hom nay", "today")),
        ("this_week", ("tuan nay", "this week")),
        ("this_month", ("thang nay", "this month")),
        ("last_30_days", ("30 ngay", "30 days")),
        ("last_7_days", ("7 ngay", "7 days")),
    ):
        if any(phrase in text for phrase in phrases):
            return period
    return None


_CONTEXTUAL_INTENTS = {
    "usage",
    "analytics",
    "application_statistics",
    "application_detail",
    "billing",
    "models",
}


def _is_follow_up(question: str, state: SupportConversationState) -> bool:
    """Recognize short replies to a clarification without guessing a new topic."""
    text = normalize_support_text(question)
    explicit = bool(
        re.match(
            r"^(?:con\b|the con\b|vay\b|vay thi\b|thoi thi\b|duoc\b|ok(?:ay)?\b|"
            r"and\b|what about\b|how about\b)",
            text,
        )
    ) or any(phrase in text for phrase in ("app do", "ung dung do", "application do"))
    conversational_selection = any(
        phrase in text
        for phrase in ("nhung ma", "ma toi muon", "muon xem", "chon", "lay", "thoi", "just", "instead")
    )
    period_choice = explicit_support_period(question) is not None and state.intent in _CONTEXTUAL_INTENTS and (
        len(text.split()) <= 8 or conversational_selection
    )
    return explicit or period_choice


def fallback_support_intent(question: str, state: SupportConversationState) -> SupportIntentPlan:
    text = normalize_support_text(question)

    def has(*phrases: str) -> bool:
        return any(phrase in text for phrase in phrases)

    period = explicit_support_period(question)
    follow_up = _is_follow_up(question, state)
    explanatory = has("la gi", "dung de", "huong dan", "cach ", "how to", "what is", "explain", "giai thich")
    analytics = local_analytics_query(question, state)
    if analytics is not None:
        return SupportIntentPlan(
            intent="analytics",
            confidence=1,
            application_name=None,
            period=period,
            follow_up=follow_up
            or (
                state.intent in {"usage", "application_statistics", "analytics"}
                and any(word in text for word in ("bieu do", "chart", "plot"))
            ),
            analytics=analytics,
        )
    if has("tai khoan cua toi", "account cua toi", "my account", "email cua toi", "thong tin account"):
        return SupportIntentPlan(intent="account", confidence=1, application_name=None, period=None, follow_up=False)
    if re.search(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b(?:\d{1,2})[-/]\d{1,2}\b", text) or has(
        "tuan truoc", "thang truoc", "last week", "last month"
    ):
        intent = "clarify"
    elif (
        follow_up
        and (period or has("app do", "ung dung do", "application do"))
        and state.intent
        in {
            "usage",
            "analytics",
            "application_statistics",
            "application_detail",
            "billing",
            "models",
        }
    ):
        intent = state.intent
    elif is_assistant_model_question(question):
        intent = "faq"
    elif explanatory or has(
        "cau hinh sdk",
        "xac thuc api",
        "playground",
        "api key",
        "api-key",
        "tao key",
        "khoa truy cap",
        "tao application",
        "tao ung dung",
        "create application",
        "new application",
    ):
        intent = (
            "routing"
            if has("routing", "dinh tuyen") and not has("playground", "cach ", "huong dan", "api key")
            else "faq"
        )
    elif has(
        "thong ke",
        "so request",
        "bao nhieu request",
        "luot goi",
        "ti le thanh cong",
        "ty le thanh cong",
        "do tre",
        "hieu suat",
    ) or (has("request") and (has("gan day", "recent") or re.search(r"\b\d+\s*(?:ngay|days)\b", text))):
        intent = "application_statistics"
    elif has("billing", "credit", "so du", "balance"):
        intent = "clarify" if has("usage", "thong ke") else "billing"
    elif has("usage", "token", "chi phi", "cost", "tieu thu"):
        intent = "usage"
    elif has("available model", "model nao", "danh sach model", "cac model"):
        intent = "models"
    elif has(
        "danh sach application",
        "danh sach ung dung",
        "cac application",
        "my apps",
        "application cua toi",
        "ung dung cua toi",
        "app cua toi",
        "app toi da tao",
        "application toi da tao",
        "ung dung toi da tao",
        "cac ung dung",
        "liet ke application",
    ):
        intent = "application_list"
    elif has(
        "application nay",
        "ung dung nay",
        "app nay",
        "app do",
        "cau hinh",
        "status",
        "trang thai",
        "chi tiet",
        "thong tin",
    ):
        intent = "application_detail"
    elif has("routing", "dinh tuyen", "route"):
        intent = "routing"
    else:
        intent = "faq"
    return SupportIntentPlan(intent=intent, confidence=1, application_name=None, period=period, follow_up=follow_up)


def local_analytics_query(question: str, state: SupportConversationState) -> SupportAnalyticsQuery | None:
    """Deterministic fallback; the model remains responsible for general natural language."""
    text = normalize_support_text(question)

    def has(*parts):
        return any(part in text for part in parts)

    chart = has("bieu do", "chart", "plot", "graph", "bien dong", "trend")
    compare = has("so sanh", "so voi", "compare", "versus")
    breakdown = has(
        "theo model",
        "theo nguon",
        "theo source",
        "theo application",
        "theo app",
        "by model",
        "by source",
        "by app",
        "chiem nhieu",
        "top model",
    )
    failures = has("request loi", "request bi loi", "failed request", "loi gan nhat")
    request_series = has(
        "theo tung request",
        "theo moi request",
        "theo request",
        "tung request",
        "moi request",
        "per request",
        "by request",
        "request by request",
    )
    continuation = state.intent == "analytics" and _is_follow_up(question, state)
    custom_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    dated_data = len(custom_dates) == 2 and has("usage", "chi phi", "cost", "token", "request")
    if not (
        chart
        or compare
        or breakdown
        or failures
        or request_series
        or continuation
        or dated_data
        or has("theo ngay", "theo tuan", "by day", "daily")
    ):
        return None
    if (
        chart
        and not has("usage", "chi phi", "cost", "token", "request", "thanh cong", "do tre")
        and state.intent not in {"usage", "application_statistics", "analytics"}
    ):
        return None
    previous = state.analytics
    metric = previous.metric if continuation and previous else "cost_usd"
    if has("chi phi", "cost", "gia"):
        metric = "cost_usd"
    elif has("input token"):
        metric = "input_tokens"
    elif has("output token"):
        metric = "output_tokens"
    elif has("token"):
        metric = "tokens"
    elif has("thanh cong", "success rate"):
        metric = "success_rate"
    elif has("do tre", "latency"):
        metric = "latency_ms"
    elif has("request", "luot goi"):
        metric = "requests"
    operation = (
        "failures"
        if failures
        else "timeseries"
        if request_series
        else "compare"
        if compare
        else "breakdown"
        if breakdown
        else previous.operation
        if continuation and previous
        else "timeseries"
    )
    dimension = previous.group_by if continuation and previous else "day"
    if request_series:
        dimension = "request"
    elif has("theo model", "by model", "top model"):
        dimension = "model"
    elif has("theo nguon", "theo source", "by source"):
        dimension = "source"
    elif breakdown:
        dimension = "application"
    elif has("theo tuan", "by week", "weekly"):
        dimension = "week"
    requested_type = "bar" if has("bieu do cot", "bar chart") or operation == "breakdown" else "line"
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if len(dates) not in {0, 2}:
        return None
    custom = (dates[0], dates[1]) if dates else (None, None)
    if continuation and previous and not explicit_support_period(question) and not dates:
        custom = (previous.start_date, previous.end_date)
    return SupportAnalyticsQuery(
        operation=operation,
        metric=metric,
        group_by=dimension,
        chart_type=requested_type if chart else previous.chart_type if continuation and previous else None,
        start_date=custom[0],
        end_date=custom[1],
    )
