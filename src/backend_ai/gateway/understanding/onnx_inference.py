"""Run the exported task classifier (eval/task_type/export_onnx.py): tokenize, one ONNX pass, softmax + sigmoid.

Self-contained on purpose (numpy, onnxruntime, tokenizers; nothing from ``src``): the gateway uses it in-process
and src/classifier_service/ copies this file alone into its Cloud Run image, so both run the same inference code.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

REQUIRED_FILES = ("labels.json", "tokenizer.json")


@dataclass(frozen=True)
class Prediction:
    task: str
    probability: float
    difficulty: float
    """Share of the reference 7B pool expected to fail the prompt (0..1)."""
    probabilities: dict[str, float]
    latency_ms: float


class OnnxClassifierModel:
    """model_dir holds labels.json, tokenizer.json and the ONNX file."""

    def __init__(
        self, model_dir: Path, model_file: str = "model.onnx", threads: int = 2, *, mem_arena: bool = True
    ) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        labels = json.loads((model_dir / "labels.json").read_text(encoding="utf-8"))
        self.tasks: list[str] = list(labels["tasks"])
        self.max_len = int(labels["max_len"])
        self.name = f"{model_dir.name}/{model_file}"
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(self.max_len)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        # Off saves ~360 MiB (1.49 -> 1.12 GiB measured) for slightly slower long prompts; fits 2 GiB hosts.
        options.enable_cpu_mem_arena = mem_arena
        self.session = ort.InferenceSession(str(model_dir / model_file), options, providers=["CPUExecutionProvider"])

    @staticmethod
    def missing_files(model_dir: Path, model_file: str = "model.onnx") -> list[str]:
        return [name for name in (*REQUIRED_FILES, model_file) if not (model_dir / name).is_file()]

    def predict(self, text: str) -> Prediction:
        import numpy as np

        started = perf_counter()
        ids = self.tokenizer.encode(text or " ").ids
        input_ids = np.asarray([ids], dtype=np.int64)
        task_logits, difficulty_logit = self.session.run(
            None, {"input_ids": input_ids, "attention_mask": np.ones_like(input_ids)}
        )
        logits = task_logits[0].astype(float)
        exp = np.exp(logits - logits.max())
        probs = exp / exp.sum()
        best = int(probs.argmax())
        return Prediction(
            task=self.tasks[best],
            probability=float(probs[best]),
            difficulty=1 / (1 + math.exp(-float(difficulty_logit[0]))),
            probabilities={task: round(float(p), 4) for task, p in zip(self.tasks, probs)},
            latency_ms=(perf_counter() - started) * 1000,
        )
