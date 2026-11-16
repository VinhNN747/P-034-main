"""Request understanding and decomposition for the AI Agent Gateway."""

__all__ = ["RequestPipeline"]


def __getattr__(name: str):
    # Keep package imports light; load the pipeline only when requested.
    if name == "RequestPipeline":
        from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline

        return RequestPipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
