from __future__ import annotations

from collections import defaultdict
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.dtos.comparison import (
    ComparisonBranchRead,
    ComparisonDelta,
    ComparisonPage,
    ComparisonRead,
)
from src.shared.models.comparison import ComparisonBranch, ComparisonEvaluation, ComparisonRun


class ComparisonNotFoundError(LookupError):
    pass


class ComparisonQueryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_owned(self, comparison_id: str, owner_user_id: str) -> ComparisonRead:
        run = await self.session.scalar(
            select(ComparisonRun).where(
                ComparisonRun.id == comparison_id,
                ComparisonRun.owner_user_id == owner_user_id,
            )
        )
        if run is None:
            raise ComparisonNotFoundError("Comparison not found")
        return await self._read(run)

    async def get_application(self, comparison_id: str, application_id: str) -> ComparisonRead:
        run = await self.session.scalar(
            select(ComparisonRun).where(
                ComparisonRun.id == comparison_id,
                ComparisonRun.application_id == application_id,
            )
        )
        if run is None:
            raise ComparisonNotFoundError("Comparison not found")
        return await self._read(run)

    async def list_owned(
        self,
        owner_user_id: str,
        *,
        application_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> ComparisonPage:
        conditions = [ComparisonRun.owner_user_id == owner_user_id]
        if application_id:
            conditions.append(ComparisonRun.application_id == application_id)
        total = int(await self.session.scalar(select(func.count()).select_from(ComparisonRun).where(*conditions)) or 0)
        runs = list(
            await self.session.scalars(
                select(ComparisonRun)
                .where(*conditions)
                .order_by(ComparisonRun.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return ComparisonPage(
            items=[await self._read(run) for run in runs],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def report_owned(self, owner_user_id: str, application_id: str | None = None) -> dict:
        conditions = [ComparisonRun.owner_user_id == owner_user_id, ComparisonRun.execution_mode == "live"]
        if application_id:
            conditions.append(ComparisonRun.application_id == application_id)
        runs = list(await self.session.scalars(select(ComparisonRun).where(*conditions)))
        groups: dict[str, list[tuple[ComparisonBranch, ComparisonBranch, dict]]] = defaultdict(list)
        evaluation_cost = 0.0
        for run in runs:
            branches = list(
                await self.session.scalars(select(ComparisonBranch).where(ComparisonBranch.comparison_id == run.id))
            )
            evaluation = await self.session.scalar(
                select(ComparisonEvaluation).where(ComparisonEvaluation.comparison_id == run.id)
            )
            evaluation_cost += evaluation.cost_usd if evaluation else 0.0
            scores = evaluation.scores_json if evaluation else {}
            gateway = next((item for item in branches if item.branch_type == "gateway"), None)
            if gateway is None:
                continue
            for reference in (item for item in branches if item.branch_type == "reference"):
                groups[reference.model_id or reference.model_name or "unknown"].append((gateway, reference, scores))
        cohorts = []
        for model_id, pairs in groups.items():
            latencies_gateway = [
                g.latency_ms
                for g, r, _ in pairs
                if g.status == "completed" and r.status == "completed" and g.latency_ms is not None
            ]
            latencies_reference = [
                r.latency_ms
                for g, r, _ in pairs
                if g.status == "completed" and r.status == "completed" and r.latency_ms is not None
            ]
            cost_pairs = [(g, r) for g, r, _ in pairs if g.usage_complete and r.usage_complete and r.cost_usd > 0]
            gateway_cost = sum(g.cost_usd for g, _ in cost_pairs)
            reference_cost = sum(r.cost_usd for _, r in cost_pairs)
            quality_pairs = [(self._score(scores, g.id), self._score(scores, r.id)) for g, r, scores in pairs]
            quality_pairs = [(g, r) for g, r in quality_pairs if g is not None and r is not None]
            cohorts.append(
                {
                    "reference_model_id": model_id,
                    "samples": len(pairs),
                    "completed_pairs": sum(g.status == "completed" and r.status == "completed" for g, r, _ in pairs),
                    "comparable_cost_pairs": len(cost_pairs),
                    "gateway_cost_usd": round(gateway_cost, 10),
                    "reference_cost_usd": round(reference_cost, 10),
                    "cost_delta_usd": round(reference_cost - gateway_cost, 10) if cost_pairs else None,
                    "cost_delta_percent": round(100 * (reference_cost - gateway_cost) / reference_cost, 4)
                    if reference_cost > 0
                    else None,
                    "gateway_latency_p50_ms": self._percentile(latencies_gateway, 50),
                    "gateway_latency_p95_ms": self._percentile(latencies_gateway, 95),
                    "reference_latency_p50_ms": self._percentile(latencies_reference, 50),
                    "reference_latency_p95_ms": self._percentile(latencies_reference, 95),
                    "gateway_quality_mean": round(sum(g for g, _ in quality_pairs) / len(quality_pairs), 3)
                    if quality_pairs
                    else None,
                    "reference_quality_mean": round(sum(r for _, r in quality_pairs) / len(quality_pairs), 3)
                    if quality_pairs
                    else None,
                }
            )
        return {
            "comparisons": len(runs),
            "evaluation_cost_usd": round(evaluation_cost, 10),
            "cohorts": sorted(cohorts, key=lambda item: item["reference_model_id"]),
        }

    async def export_owned(self, owner_user_id: str, application_id: str | None = None) -> list[dict]:
        conditions = [ComparisonRun.owner_user_id == owner_user_id]
        if application_id:
            conditions.append(ComparisonRun.application_id == application_id)
        runs = list(
            await self.session.scalars(
                select(ComparisonRun).where(*conditions).order_by(ComparisonRun.created_at.desc())
            )
        )
        rows: list[dict] = []
        for run in runs:
            report = await self._read(run)
            gateway = next((branch for branch in report.branches if branch.branch_type == "gateway"), None)
            deltas = {delta.reference_branch_id: delta for delta in report.deltas}
            for reference in (branch for branch in report.branches if branch.branch_type == "reference"):
                delta = deltas[reference.id]
                rows.append(
                    {
                        "comparison_id": report.id,
                        "created_at": report.created_at.isoformat(),
                        "application_id": report.application_id,
                        "source": report.source,
                        "execution_mode": report.execution_mode,
                        "status": report.status,
                        "reference_model_id": reference.model_id,
                        "reference_model_name": reference.model_name,
                        "gateway_cost_usd": gateway.cost_usd if gateway else None,
                        "reference_cost_usd": reference.cost_usd,
                        "cost_delta_usd": delta.cost_delta_usd,
                        "cost_delta_percent": delta.cost_delta_percent,
                        "gateway_latency_ms": gateway.latency_ms if gateway else None,
                        "reference_latency_ms": reference.latency_ms,
                        "latency_delta_ms": delta.latency_delta_ms,
                        "latency_delta_percent": delta.latency_delta_percent,
                        "gateway_quality": gateway.quality_score if gateway else None,
                        "reference_quality": reference.quality_score,
                        "quality_delta_points": delta.quality_delta_points,
                        "cost_comparable": delta.comparable_cost,
                        "latency_comparable": delta.comparable_latency,
                        "quality_comparable": delta.comparable_quality,
                        "comparison_spend_usd": report.comparison_spend_usd,
                    }
                )
        return rows

    @staticmethod
    def _percentile(values: list[float | None], percentile: int) -> float | None:
        ordered = sorted(float(value) for value in values if value is not None)
        if not ordered:
            return None
        if percentile == 50:
            return round(float(median(ordered)), 3)
        index = max(0, min(len(ordered) - 1, int((percentile / 100) * len(ordered) + 0.999999) - 1))
        return round(ordered[index], 3)

    async def _read(self, run: ComparisonRun) -> ComparisonRead:
        branches = list(
            await self.session.scalars(
                select(ComparisonBranch)
                .where(ComparisonBranch.comparison_id == run.id)
                .order_by(ComparisonBranch.branch_type, ComparisonBranch.branch_key)
            )
        )
        evaluation = await self.session.scalar(
            select(ComparisonEvaluation).where(ComparisonEvaluation.comparison_id == run.id)
        )
        score_map = evaluation.scores_json if evaluation and evaluation.status == "completed" else {}
        status = run.status
        # Older runs counted answer generation alone as completed.
        if status == "completed" and run.quality_evaluation and evaluation and evaluation.status != "completed":
            status = "partial"
        branch_reads = [self._branch_read(branch, score_map) for branch in branches]
        gateway = next((branch for branch in branches if branch.branch_type == "gateway"), None)
        deltas = [self._delta(gateway, branch, score_map) for branch in branches if branch.branch_type == "reference"]
        evaluation_error = evaluation.error_message if evaluation else None
        if evaluation_error == "The comparison branch could not be completed.":
            evaluation_error = (
                "Quality evaluation failed. This historical run did not record the provider error reason. "
                "Run a new comparison to obtain detailed diagnostics."
            )
        return ComparisonRead(
            id=run.id,
            application_id=run.application_id,
            gateway_request_id=run.gateway_request_id,
            source=run.source,
            status=status,
            execution_mode=run.execution_mode,
            prompt=run.prompt,
            quality_evaluation=run.quality_evaluation,
            execution_strategy=run.strategies_json.get("execution_strategy", "parallel"),
            evaluator_model_id=run.evaluator_model_id,
            evaluation_status=evaluation.status if evaluation else None,
            evaluator_model_name=evaluation.evaluator_model_name if evaluation else None,
            evaluation_error_code=(evaluation.rationale_json or {}).get("_error", {}).get("code") if evaluation else None,
            evaluation_total_tokens=evaluation.input_tokens + evaluation.output_tokens if evaluation else 0,
            evaluation_error_message=evaluation_error,
            evaluation_cost_usd=round(evaluation.cost_usd, 10) if evaluation else 0.0,
            evaluation_usage_complete=evaluation.usage_complete if evaluation else False,
            comparison_spend_usd=round(
                sum(branch.cost_usd for branch in branches) + (evaluation.cost_usd if evaluation else 0.0), 10
            ),
            branches=branch_reads,
            deltas=deltas,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error_code=run.error_code,
            error_message=run.error_message,
        )

    @staticmethod
    def _score(score_map: dict, branch_id: str) -> float | None:
        value = score_map.get(branch_id)
        if not isinstance(value, dict):
            return None
        total = value.get("total")
        return float(total) if isinstance(total, (int, float)) else None

    @classmethod
    def _branch_read(cls, branch: ComparisonBranch, score_map: dict) -> ComparisonBranchRead:
        return ComparisonBranchRead(
            id=branch.id,
            branch_key=branch.branch_key,
            branch_type=branch.branch_type,
            model_id=branch.model_id,
            model_name=branch.model_name,
            provider_name=branch.provider_name,
            status=branch.status,
            output=branch.output,
            input_tokens=branch.input_tokens,
            output_tokens=branch.output_tokens,
            total_tokens=branch.input_tokens + branch.output_tokens,
            cost_usd=branch.cost_usd,
            usage_complete=branch.usage_complete,
            pricing_estimated=branch.pricing_estimated,
            latency_ms=branch.latency_ms,
            queue_delay_ms=branch.queue_delay_ms,
            quality_score=cls._score(score_map, branch.id),
            error_code=branch.error_code,
            error_message=branch.error_message,
        )

    @classmethod
    def _delta(
        cls,
        gateway: ComparisonBranch | None,
        reference: ComparisonBranch,
        score_map: dict,
    ) -> ComparisonDelta:
        cost_ok = bool(
            gateway
            and gateway.status == "completed"
            and reference.status == "completed"
            and gateway.usage_complete
            and reference.usage_complete
            and reference.cost_usd > 0
        )
        latency_ok = bool(
            gateway
            and gateway.status == "completed"
            and reference.status == "completed"
            and gateway.latency_ms is not None
            and reference.latency_ms is not None
            and reference.latency_ms > 0
        )
        gateway_score = cls._score(score_map, gateway.id) if gateway else None
        reference_score = cls._score(score_map, reference.id)
        quality_ok = gateway_score is not None and reference_score is not None
        cost_delta = reference.cost_usd - gateway.cost_usd if cost_ok and gateway else None
        latency_delta = reference.latency_ms - gateway.latency_ms if latency_ok and gateway else None
        return ComparisonDelta(
            reference_branch_id=reference.id,
            reference_model_id=reference.model_id,
            cost_delta_usd=round(cost_delta, 10) if cost_delta is not None else None,
            cost_delta_percent=round(100 * cost_delta / reference.cost_usd, 4) if cost_delta is not None else None,
            latency_delta_ms=round(latency_delta, 3) if latency_delta is not None else None,
            latency_delta_percent=(
                round(100 * latency_delta / reference.latency_ms, 4)
                if latency_delta is not None and reference.latency_ms
                else None
            ),
            quality_delta_points=(round(gateway_score - reference_score, 3) if quality_ok else None),
            comparable_cost=cost_ok,
            comparable_latency=latency_ok,
            comparable_quality=quality_ok,
        )


__all__ = ["ComparisonNotFoundError", "ComparisonQueryService"]
