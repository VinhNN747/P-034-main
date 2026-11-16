from __future__ import annotations

import subprocess
import sys


def test_request_and_management_imports_do_not_load_numpy_or_trainer():
    code = (
        "import sys; import src.shared.models; import src.backend_ai.routers.runtime; "
        "import src.backend_ai.latency.predictor; import src.scripts.latency_learning_report; "
        "assert 'numpy' not in sys.modules; "
        "assert 'src.backend_ai.latency.trainer' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
