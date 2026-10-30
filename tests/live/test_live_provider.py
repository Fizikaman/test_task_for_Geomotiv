"""P22: never enabled by the normal suite, --e2e, --neo4j or CI."""

import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.live.p22_fixture import Sample, samples


@pytest.mark.live_provider
@pytest.mark.parametrize("sample", samples(), ids=lambda sample: sample.name)
def test_live_pipeline(sample: Sample, request: pytest.FixtureRequest) -> None:
    # Defense in depth when invoked outside normal collection hooks.
    if os.environ.get("CI") or not request.config.getoption("--live-provider"):
        pytest.skip("P22 live smoke disabled")
    if os.environ.get("P22_LIVE_CONSENT") != "separate-key-provided-for-p22":
        pytest.fail(
            "P22 needs explicit separate-key consent; see docs/live-smoke.md",
            pytrace=False,
        )
    from tests.live.p22_live import run_sample

    # No retained checkpoints, response payloads, HTTP debug logs or raw errors.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with TemporaryDirectory(prefix="geomotiv-p22-") as directory:
            run_sample(sample, Path(directory))
    except Exception:
        pytest.fail(
            f"P22 {sample.name} failed (details suppressed to protect credentials); "
            "check explicit config, isolated DB, provider capabilities and quality",
            pytrace=False,
        )
    finally:
        logging.disable(previous_logging)
