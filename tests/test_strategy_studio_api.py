from pathlib import Path

from fastapi.testclient import TestClient
from fastapi import FastAPI

from tests.test_strategy_service import target_at
from strategy_service import StrategyService


def test_generic_single_assignment_requires_current_identity(tmp_path: Path) -> None:
    from web.strategy_api import register_strategy_routes
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    app = FastAPI()
    register_strategy_routes(app, service=service, local_target=lambda: target)
    client = TestClient(app)
    assert client.get('/api/strategy-studio/status').json()['state'] == 'legacy'
    labs = client.get('/api/strategy-studio/labs')
    assert labs.status_code == 200
    assert labs.json()['workers'][0]['freshness'] == 'unknown'
    template = client.get('/api/strategy-studio/library').json()['templates'][0]
    payload = {'strategy_id': template['id'], 'strategy_version': 1, 'expected_revision': 0,
        'target_id': 'standalone', 'account_id': target.account_id,
        'generation': 'old', 'epoch': 0}
    assert client.post('/api/strategy-studio/assign', json=payload).status_code == 409
    payload['generation'] = target.scope.generation
    assert client.post('/api/strategy-studio/assign', json=payload).json()['status'] == 'pending'


def test_labs_proxy_uses_full_scoped_api_path(tmp_path: Path) -> None:
    from web.strategy_api import register_strategy_routes
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    seen: list[str] = []
    def remote(request, path, body):
        seen.append(path)
        return {'workers': [], 'automated': [], 'reference': None}
    app = FastAPI()
    register_strategy_routes(app, service=service, local_target=lambda: target, remote=remote)
    assert TestClient(app).get('/api/strategy-studio/labs').status_code == 200
    assert seen == ['/api/strategy-studio/labs']


def test_studio_status_reenters_real_runner_identity_without_hanging() -> None:
    import subprocess
    import sys
    import pytest
    script = """
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from control import Controls
from events import EventBus
from runner import BotRunner
from sinks.state import BotState
from strategy import Strategy
from strategy_service import StrategyService
from web.strategy_api import register_strategy_routes
runner = BotRunner(bus=EventBus(), controls=Controls(Strategy.from_config()),
    state=BotState(), templates=None, device_factory=lambda: None, checks={})
with TemporaryDirectory() as root:
    service = StrategyService(library_root=Path(root), assignment_root=Path(root),
        resolve_target=lambda _: runner.strategy_context())
    app = FastAPI()
    register_strategy_routes(app, service=service, local_target=runner.strategy_context, runner=runner)
    assert TestClient(app).get('/api/strategy-studio/status').json()['state'] == 'blocked'
    assert runner.status()['running'] is False
"""
    try:
        subprocess.run([sys.executable, '-c', script], timeout=8, check=True,
                       capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        pytest.fail('Strategy Studio deadlocked while reading runner identity')
