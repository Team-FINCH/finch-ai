"""Exercise cron-facing exits in isolated processes with fake HTTP and DB only."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = r"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock
import httpx
from app.rag import dart
from ingest import events, financials

module_name, scenario = sys.argv[1:]
statuses = {
    "systemic": ["rejected", "rejected", "rejected"],
    "quiet": ["013", "013", "013"],
    "partial": ["rejected", "013", "013"],
    "empty": [],
    "missing_key": [],
    "different": ["rejected", "other", "013"],
}[scenario]
targets = [(str(i).zfill(6), str(i)) for i in range(len(statuses))]
responses = iter(statuses)
calls = []
def handler(request):
    calls.append(request)
    return httpx.Response(200, json={"status": next(responses)})

sync_client = httpx.Client
async_client = httpx.AsyncClient
httpx.Client = lambda **kwargs: sync_client(transport=httpx.MockTransport(handler))
httpx.AsyncClient = lambda **kwargs: async_client(transport=httpx.MockTransport(handler))
session = AsyncMock()
session.execute.return_value = []
factory = MagicMock()
factory.return_value.__aenter__ = AsyncMock(return_value=session)
factory.return_value.__aexit__ = AsyncMock(return_value=None)
for module in (dart, events, financials):
    module.settings.dart_api_key = "" if scenario == "missing_key" else "stub-secret-never-log"
    module.SessionFactory = factory
    module.REQUEST_DELAY_S = 0
    module.engine = MagicMock(dispose=AsyncMock())
dart.load_targets = AsyncMock(return_value=targets)
events.load_targets = AsyncMock(return_value=targets)
dart.existing_rcept_nos = AsyncMock(return_value=set())
financials._targets = AsyncMock(return_value=targets)
financials._upsert = AsyncMock(return_value=0)
financials.DELAY_S = 0
sys.argv = [module_name]
code = 0
try:
    if module_name == "dart":
        code = dart.main([])
    elif module_name == "events":
        asyncio.run(events._main())
    else:
        asyncio.run(financials._main())
except SystemExit as exc:
    code = exc.code
assert len(calls) == len(targets), (len(calls), len(targets))
if scenario == "systemic":
    assert not session.commit.called
raise SystemExit(code)
"""


@pytest.mark.parametrize("module", ["dart", "events", "financials"])
@pytest.mark.parametrize(
    ("scenario", "exit_code", "message"),
    [
        ("systemic", 1, "status=rejected"),
        ("quiet", 0, "0"),
        ("partial", 0, "status=rejected"),
        ("empty", 0, "corp_code"),
        ("missing_key", 1, "DART_API_KEY"),
        ("different", 0, "status=other"),
    ],
)
def test_cron_exit_and_logs(module, scenario, exit_code, message):
    ai_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, module, scenario],
        cwd=ai_root,
        env={**os.environ, "PYTHONPATH": str(ai_root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    # 대상 0건은 조용한 날이 아니라 instruments 가 비었다는 뜻이다. dart 는 실패로 본다.
    # events·financials 는 아직 경고에 머물러 있다 — 맞추는 것은 별건이다.
    loud_empty = scenario == "empty" and module == "dart"
    if loud_empty:
        exit_code = 1

    output = result.stdout + result.stderr
    assert result.returncode == exit_code, output
    assert message in output
    assert "stub-secret-never-log" not in output
    assert "Traceback" not in output
    if scenario == "systemic":
        assert "check API credentials or quota" in output
        assert "ERROR" in output
    elif loud_empty:
        assert "ERROR" in output
    elif scenario != "missing_key":
        assert "ERROR" not in output
    print(f"\n{module}/{scenario}: exit={result.returncode}\n{output}")
