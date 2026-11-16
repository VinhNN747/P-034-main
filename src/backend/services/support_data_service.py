"""Read-only, bounded SQL analytics. No LLM, executable code or cross-account queries."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Numeric, case, cast, func, literal, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.dtos.support_chat import SupportAnalyticsData, SupportAnalyticsQuery, SupportDataRow
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord


class SupportDataUnavailableError(RuntimeError):
    pass


class SupportDataQueryError(ValueError):
    pass


METRICS = {
    "cost_usd": (
        "Chi phí execution",
        "USD",
        "Tổng usage_events.cost_usd, có thể gồm giá trị estimated; không phải billing charge.",
    ),
    "requests": (
        "Request",
        "request",
        "Số gateway_requests đã ghi nhận, không gồm HTTP request bị từ chối trước persistence.",
    ),
    "input_tokens": ("Input tokens", "token", "Tổng input_tokens trong usage_events."),
    "output_tokens": ("Output tokens", "token", "Tổng output_tokens trong usage_events."),
    "tokens": ("Tokens", "token", "Tổng input_tokens + output_tokens trong usage_events."),
    "success_rate": (
        "Tỷ lệ hoàn thành",
        "%",
        "gateway_requests.status = completed / tất cả gateway_requests đã ghi nhận.",
    ),
    "latency_ms": (
        "Độ trễ trung bình",
        "ms",
        "Trung bình gateway_requests.total_latency_ms, chỉ request completed/failed.",
    ),
}
_REQUEST_METRICS = {"requests", "success_rate", "latency_ms"}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


class SupportDataService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.offset = get_settings().support_chat_utc_offset_minutes
        self.tz = timezone(timedelta(minutes=self.offset))

    def range(self, period: str, query: SupportAnalyticsQuery, now: datetime) -> tuple[datetime, datetime]:
        if bool(query.start_date) != bool(query.end_date):
            raise SupportDataQueryError("Hãy cung cấp đủ ngày bắt đầu và ngày kết thúc (YYYY-MM-DD).")
        if query.start_date:
            try:
                start = datetime.combine(date.fromisoformat(query.start_date), datetime.min.time(), self.tz)
                end = datetime.combine(date.fromisoformat(query.end_date), datetime.min.time(), self.tz)
            except ValueError as exc:
                raise SupportDataQueryError("Ngày không hợp lệ. Hãy dùng YYYY-MM-DD.") from exc
            if end > now.astimezone(self.tz) + timedelta(days=1):
                raise SupportDataQueryError("Không thể truy vấn khoảng ngày trong tương lai.")
            end = min(end, now.astimezone(self.tz))
            start, end = utc(start), utc(end)
        else:
            local = now.astimezone(self.tz)
            day = local.replace(hour=0, minute=0, second=0, microsecond=0)
            if period == "today":
                start, end = utc(day), now
            elif period == "yesterday":
                start, end = utc(day - timedelta(days=1)), utc(day)
            elif period == "this_week":
                start, end = utc(day - timedelta(days=day.weekday())), now
            elif period == "this_month":
                start, end = utc(day.replace(day=1)), now
            else:
                start, end = now - timedelta(days=30 if period == "last_30_days" else 7), now
        if end <= start or end - start > timedelta(days=90):
            raise SupportDataQueryError(
                "Khoảng thời gian phải lớn hơn 0 và không quá 90 ngày; ngày kết thúc không bao gồm trong khoảng."
            )
        return start, end

    def _bucket(self, column, dimension: str):
        if self.session.get_bind().dialect.name == "postgresql":
            shifted = column + literal(timedelta(minutes=self.offset))
            return func.to_char(func.date_trunc(dimension, shifted), "YYYY-MM-DD")
        shifted = func.datetime(column, f"{self.offset:+d} minutes")
        return func.date(shifted, "weekday 0", "-6 days") if dimension == "week" else func.date(shifted)

    @staticmethod
    def _value(metric: str, count: int, completed: int, summed: Decimal | float | int) -> float | None:
        if metric == "success_rate":
            return 100 * completed / count if count else None
        if metric == "latency_ms":
            return float(summed) / count if count else None
        number = float(summed if metric != "requests" else count)
        if not math.isfinite(number):
            raise SupportDataQueryError("Dữ liệu thống kê chứa số không hợp lệ.")
        return number

    async def query(
        self, *, user_id: str, application: Application | None, period: str, query: SupportAnalyticsQuery
    ) -> SupportAnalyticsData:
        try:
            timeout = get_settings().support_chat_query_timeout_seconds
            async with asyncio.timeout(timeout):
                if self.session.get_bind().dialect.name == "postgresql":
                    await self.session.execute(
                        select(func.set_config("statement_timeout", str(int(timeout * 1000)), True))
                    )
                return await self._query(user_id=user_id, application=application, period=period, query=query)
        except (TimeoutError, SQLAlchemyError) as exc:
            raise SupportDataUnavailableError("Analytics query unavailable") from exc

    async def _query(
        self, *, user_id: str, application: Application | None, period: str, query: SupportAnalyticsQuery
    ) -> SupportAnalyticsData:
        now = datetime.now(UTC)
        start, end = self.range(period, query, now)
        if query.operation == "timeseries" and query.group_by not in {"day", "week", "request"}:
            raise SupportDataQueryError("Biểu đồ theo thời gian hỗ trợ nhóm theo ngày, tuần hoặc từng request.")
        if query.operation == "breakdown" and query.group_by not in {"application", "model", "source"}:
            raise SupportDataQueryError("Phân nhóm hỗ trợ application, model hoặc nguồn sử dụng.")
        if (
            query.operation in {"timeseries", "breakdown"}
            and query.group_by == "model"
            and query.metric in _REQUEST_METRICS
        ):
            raise SupportDataQueryError(
                "Theo model, hãy chọn chi phí hoặc tokens; một request có thể chạy nhiều model."
            )
        if query.chart_type == "line" and query.operation == "breakdown":
            raise SupportDataQueryError("Phân nhóm theo application/model/source phù hợp biểu đồ cột.")
        if query.operation == "failures" and query.chart_type:
            raise SupportDataQueryError("Danh sách request lỗi được hiển thị bằng bảng.")
        if query.group_by == "request" and query.operation not in {"timeseries", "failures"}:
            raise SupportDataQueryError("Phân tích theo từng request chỉ hỗ trợ dạng biến động theo thời gian.")
        if query.operation == "timeseries" and query.group_by == "request" and query.metric not in {
            "cost_usd", "latency_ms", "tokens", "input_tokens", "output_tokens"
        }:
            raise SupportDataQueryError(
                "Theo từng request hỗ trợ chi phí, latency hoặc tokens; request count và success rate không có ý nghĩa ở cấp này."
            )

        scope_stmt = select(Application.id).where(Application.owner_user_id == user_id)
        if application:
            scope_stmt = scope_stmt.where(Application.id == application.id)
        scope_ids = list(await self.session.scalars(scope_stmt))
        name, unit, definition = METRICS[query.metric]
        scope = f"application {application.name}" if application else "các application của bạn"
        base = dict(
            operation=query.operation,
            metric=query.metric,
            dimension=query.group_by,
            unit=unit,
            title=f"{name} — {scope}",
            scope=scope,
            start=iso(start),
            end=iso(end),
            timezone=f"UTC{self.offset / 60:+g}",
            as_of=iso(now),
            metric_definition=definition,
            chart_type=query.chart_type,
            application_ids=scope_ids,
        )
        if query.group_by == "request":
            base.update(
                title=f"{name} theo từng request — {scope}",
                metric_definition=(
                    definition
                    + " Mỗi điểm là một request theo thứ tự thời gian; tooltip hiển thị ID và chênh lệch với request liền trước."
                ),
            )

        if query.operation == "failures":
            base.update(
                unit="ms",
                dimension="request",
                title=f"Request lỗi — {scope}",
                metric_definition="Request thất bại đã ghi nhận; giá trị là độ trễ đầu cuối (ms).",
            )
            rows = list(
                (
                    await self.session.execute(
                        select(
                            GatewayRequestRecord.request_id,
                            GatewayRequestRecord.status,
                            GatewayRequestRecord.total_latency_ms,
                            GatewayRequestRecord.started_at,
                        )
                        .join(Application, Application.id == GatewayRequestRecord.application_id)
                        .where(
                            Application.owner_user_id == user_id,
                            Application.id.in_(scope_ids),
                            GatewayRequestRecord.started_at >= start,
                            GatewayRequestRecord.started_at < end,
                            GatewayRequestRecord.status == "failed",
                        )
                        .order_by(GatewayRequestRecord.started_at.desc(), GatewayRequestRecord.request_id)
                        .limit(51)
                    )
                ).all()
            )
            return SupportAnalyticsData(
                **base,
                rows=[
                    SupportDataRow(
                        label=row.request_id,
                        value=row.total_latency_ms,
                        requests=1,
                        detail=f"{row.status} · {iso(row.started_at)}",
                    )
                    for row in rows[:50]
                ],
                total=None,
                truncated=len(rows) > 50,
            )

        request_metric = query.metric in _REQUEST_METRICS
        model = GatewayRequestRecord if request_metric else UsageEventRecord
        column = model.started_at if request_metric else model.created_at
        count = func.count() if request_metric else func.count(func.distinct(model.request_id))
        completed = func.sum(case((model.status == "completed", 1), else_=0)) if request_metric else literal(0)
        if query.metric == "cost_usd":
            summed = func.sum(cast(model.cost_usd, Numeric(24, 9)))
        elif query.metric == "tokens":
            summed = func.sum(model.input_tokens + model.output_tokens)
        elif query.metric in {"input_tokens", "output_tokens"}:
            summed = func.sum(getattr(model, query.metric))
        elif query.metric == "latency_ms":
            summed = func.sum(model.total_latency_ms)
        else:
            summed = literal(0)

        def statement(low: datetime, high: datetime):
            stmt = (
                select(
                    count.label("requests"),
                    func.coalesce(completed, 0).label("completed"),
                    func.coalesce(summed, 0).label("amount"),
                )
                .select_from(model)
                .join(Application, Application.id == model.application_id)
                .where(
                    Application.owner_user_id == user_id, Application.id.in_(scope_ids), column >= low, column < high
                )
            )
            if query.metric == "latency_ms":
                stmt = stmt.where(model.status.in_(["completed", "failed"]))
            return stmt

        overall = (await self.session.execute(statement(start, end))).one()
        total = self._value(query.metric, *overall)
        if query.operation == "compare":
            duration = end - start
            previous_start = start - duration
            if not query.start_date and period == "this_week":
                previous_start = start - timedelta(days=7)
            elif not query.start_date and period == "this_month":
                previous_start = utc((start.astimezone(self.tz) - timedelta(days=1)).replace(day=1))
            previous_end = previous_start + duration
            if previous_end > start:
                raise SupportDataQueryError(
                    "Kỳ tháng trước ngắn hơn khoảng hiện tại. Hãy so sánh 7/30 ngày hoặc chọn khoảng ngày cụ thể."
                )
            previous = (await self.session.execute(statement(previous_start, previous_end))).one()
            baseline = self._value(query.metric, *previous)
            delta = total - baseline if total is not None and baseline is not None else None
            change = 100 * delta / baseline if delta is not None and baseline else None
            # Rows show N/A for intervals with no observations, preserving the distinction from measured zero.
            return SupportAnalyticsData(
                **base,
                rows=[
                    SupportDataRow(
                        label="Kỳ trước cùng độ dài",
                        value=baseline if previous.requests else None,
                        requests=previous.requests,
                        detail=f"{iso(previous_start)} → {iso(previous_end)}",
                    ),
                    SupportDataRow(
                        label="Kỳ hiện tại",
                        value=total if overall.requests else None,
                        requests=overall.requests,
                        detail=f"{iso(start)} → {iso(end)}",
                    ),
                ],
                total=total if overall.requests else None,
                baseline=baseline if previous.requests else None,
                delta=delta if overall.requests and previous.requests else None,
                percent_change=change if overall.requests and previous.requests else None,
            )

        if query.group_by == "request":
            occurred_at = func.min(column).label("occurred_at")
            application_name = func.min(Application.name).label("application_name")
            request_id = model.request_id
            metadata = (
                func.min(GatewayRequestRecord.status).label("metadata")
                if request_metric
                else func.count(UsageEventRecord.id).label("metadata")
            )
            grouped = (
                statement(start, end)
                .add_columns(
                    request_id.label("label"),
                    occurred_at,
                    application_name,
                    metadata,
                )
                .group_by(request_id)
                .order_by(occurred_at, request_id)
            )
            rows = list((await self.session.execute(grouped.limit(101))).all())
            points = []
            for row in rows[:100]:
                context = f"{iso(row.occurred_at)} · {row.application_name}"
                context += (
                    f" · {row.metadata}"
                    if request_metric
                    else f" · {int(row.metadata)} usage event{'s' if int(row.metadata) != 1 else ''}"
                )
                points.append(
                    SupportDataRow(
                        label=str(row.label),
                        value=self._value(query.metric, row.requests, row.completed, row.amount),
                        requests=1,
                        detail=context,
                    )
                )
            return SupportAnalyticsData(
                **base,
                rows=points,
                total=total if overall.requests else None,
                truncated=len(rows) > 100,
            )

        if query.group_by in {"day", "week"}:
            label = self._bucket(column, query.group_by)
            key = label
        elif query.group_by == "application":
            label, key = Application.name, Application.id
        else:
            label = func.coalesce(getattr(model, query.group_by), "unknown")
            key = label
        grouped = statement(start, end).add_columns(label.label("label"), key.label("group_id")).group_by(key, label)
        ranking = (
            count
            if query.metric == "requests"
            else completed * 100.0 / func.nullif(count, 0)
            if query.metric == "success_rate"
            else summed / func.nullif(count, 0)
            if query.metric == "latency_ms"
            else summed
        )
        grouped = grouped.order_by(label) if query.operation == "timeseries" else grouped.order_by(ranking.desc(), key)
        rows = list((await self.session.execute(grouped.limit(101))).all())
        points = [
            SupportDataRow(
                label=str(row.label),
                value=self._value(query.metric, row.requests, row.completed, row.amount),
                requests=row.requests,
                detail=str(row.group_id) if query.group_by == "application" else None,
            )
            for row in rows[:100]
        ]
        if query.operation == "timeseries" and points:
            values = {p.label: p for p in points}
            cursor = start.astimezone(self.tz).date()
            if query.group_by == "week":
                cursor -= timedelta(days=cursor.weekday())
            last = (end - timedelta(microseconds=1)).astimezone(self.tz).date()
            dense = []
            while cursor <= last:
                label_value = cursor.isoformat()
                dense.append(
                    values.get(label_value)
                    or SupportDataRow(
                        label=label_value, value=None if query.metric in {"success_rate", "latency_ms"} else 0
                    )
                )
                cursor += timedelta(days=7 if query.group_by == "week" else 1)
            points = dense
        return SupportAnalyticsData(
            **base, rows=points, total=total if overall.requests else None, truncated=len(rows) > 100
        )

    @staticmethod
    def summary(data: SupportAnalyticsData) -> str:
        if not data.rows or all(row.value is None for row in data.rows):
            return f"Không có dữ liệu được ghi nhận cho {data.scope} trong khoảng đã chọn."
        if data.operation == "failures":
            message = f"Có {len(data.rows)} request lỗi gần nhất của {data.scope}."
        else:
            value = (
                "N/A" if data.total is None else f"{data.total:,.6f}" if data.unit == "USD" else f"{data.total:,.2f}"
            )
            message = f"{data.title}: {value} {data.unit}."
            if data.operation == "compare":
                previous = "N/A" if data.baseline is None else f"{data.baseline:,.6f}"
                change = (
                    "N/A (kỳ trước bằng 0 hoặc thiếu dữ liệu)"
                    if data.percent_change is None
                    else f"{data.percent_change:+.2f}%"
                )
                message += f" Kỳ trước cùng độ dài: {previous} {data.unit}; thay đổi: {change}."
        if data.truncated:
            message += " Bảng đã giới hạn số kết quả; tổng vẫn tính trên toàn bộ phạm vi."
        return message + f" Khoảng [{data.start}, {data.end}), múi giờ {data.timezone}. Đọc lúc {data.as_of}."
