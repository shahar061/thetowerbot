"""R00 staging transaction tests; frames here are simulated, not live proof."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bluestacks import BlueStacksAdapter, HostInstance
from fleet.account_creation import AccountFrame, StagingClone, StagingQuarantined, create_staging_account
from fleet.identity import Attempt
from fleet.runtime import WorkerRuntime
from runner import BotRunner, RunnerError


class Host:
    def __init__(self, endpoint: str = "127.0.0.1:5555", lease: str = "lease-a") -> None:
        self.instance = HostInstance("clone-a", endpoint, lease, "running", "seed-clone")

    def inventory(self) -> list[HostInstance]:
        return [self.instance]


def frame(screen: str, account: str | None = None, *, control: str | None = None,
          dialog: str | None = None, stamp: float = 101.) -> AccountFrame:
    return AccountFrame(screen, account, "v29.0.2", f"digest-{screen}-{stamp}", stamp,
                        f"capture://{screen}/{stamp}",
                        {control: (350, 825)} if control else {}, dialog,
                        "ACCOUNT" if screen == "account" else
                        "Warning" if screen == "new_account_warning" else None,
                        "ID:" if screen in {"account", "new_account_warning"} else None)


def setup(tmp_path: Path, *, worker: str = "worker-a", endpoint: str = "127.0.0.1:5555",
          lease: str = "lease-a") -> tuple[WorkerRuntime, Attempt, BlueStacksAdapter, Host]:
    runtime = WorkerRuntime.for_worker(tmp_path / "fleet", worker, 8765 if worker == "worker-a" else 8766)
    attempt = replace(Attempt.new(worker, endpoint, lease, "attempt-1"), created_at=100.)
    host = Host(endpoint, lease)
    adapter = BlueStacksAdapter(host, staging_root=tmp_path / "staging")
    return runtime, attempt, adapter, host


def run(tmp_path: Path, frames: list[AccountFrame], *, source: str = "source-1",
        protected: tuple[str, ...] = (), enabled: bool = True,
        worker: str = "worker-a", endpoint: str = "127.0.0.1:5555",
        start_from_account: bool = False) -> tuple[dict, list[tuple[int, int]]]:
    runtime, attempt, adapter, _ = setup(tmp_path, worker=worker, endpoint=endpoint)
    taps: list[tuple[int, int]] = []
    observations = iter(frames)
    device = SimpleNamespace(serial=endpoint, click=lambda x, y: taps.append((x, y)))
    policy = StagingClone("clone-a", "seed-clone", source, frozenset(protected), enabled)
    result = create_staging_account(
        runtime=runtime, attempt=attempt, adapter=adapter, policy=policy,
        connect=lambda: device, observe=lambda _: next(observations),
        registry=tmp_path / "registry.json", clock=lambda: 102.,
        sleep=lambda _: None, start_from_account=start_from_account,
    )
    return result, taps


def sequence(new_id: str | None = "new-1") -> list[AccountFrame]:
    return [frame("home", control="settings"),
            frame("settings", control="account", stamp=101.1),
            frame("account", "source-1", control="new_account", stamp=101.2),
            frame("new_account_warning", "source-1", control="confirm_new_account", stamp=101.3),
            frame("unknown", stamp=101.4),
            frame("home", control="settings", stamp=101.5),
            frame("settings", control="account", stamp=101.6),
            frame("account", new_id, stamp=101.7)]


def google_play_profile(*, stamp: float = 100.5) -> AccountFrame:
    return AccountFrame(
        "google_play_profile", None, "v29.0.2", f"digest-google-{stamp}", stamp,
        f"capture://google/{stamp}", {"dismiss_google_play_profile": (176, 2289)},
    )


def test_first_run_google_play_profile_is_cancelled_before_tower_navigation(tmp_path: Path) -> None:
    result, taps = run(tmp_path, [google_play_profile(), *sequence()])
    assert result["account_id"] == "new-1"
    assert taps[0] == (176, 2289)
    assert len(taps) == 7


def test_creates_distinct_id_and_audits_source_lineage(tmp_path: Path) -> None:
    result, taps = run(tmp_path, sequence())
    assert result["source_account_id"] == "source-1"
    assert result["account_id"] == "new-1"
    assert result["source_lineage"] == "seed-clone"
    assert result["instance"] == "clone-a"
    assert result["app_version"] == "v29.0.2"
    assert len(result["evidence"]) == 7
    assert taps == [(350, 825)] * 6
    binding = next(p for p in (tmp_path / "fleet" / "worker-a" / "checkpoints").glob("*.json")
                   if p.name != ".r00-account-creation.json")
    assert json.loads(binding.read_text())["account_id"] == "new-1"
    assert stat.S_IMODE(binding.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "registry.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "fleet" / "worker-a" / "checkpoints" /
                        ".r00-account-creation.json").stat().st_mode) == 0o600


def test_preverified_account_screen_can_start_second_clone_flow(tmp_path: Path) -> None:
    frames = [sequence()[2], sequence()[3],
              frame("game_over", control="home_from_game_over", stamp=101.4),
              *sequence()[5:]]
    result, taps = run(tmp_path, frames, start_from_account=True,
                       protected=("source-1",))
    assert result["state"] == "verified" and result["started_from_account"] is True
    assert len(taps) == 5
    assert [row["screen"] for row in result["evidence"]] == [
        "account", "new_account_warning", "game_over", "home", "settings", "account",
    ]


def test_animation_frames_wait_without_extra_taps(tmp_path: Path) -> None:
    baseline = sequence()
    frames = [baseline[0], frame("home", control="settings", stamp=101.05),
              baseline[1], frame("settings", control="account", stamp=101.15),
              baseline[2], frame("account", "source-1", control="new_account", stamp=101.25),
              *baseline[3:]]
    result, taps = run(tmp_path, frames)
    assert result["account_id"] == "new-1"
    assert len(taps) == 6


@pytest.mark.parametrize("new_id,reason", [(None, "unreadable"), ("source-1", "unchanged"),
                                             ("protected-1", "protected")])
def test_bad_post_id_quarantines(tmp_path: Path, new_id: str | None, reason: str) -> None:
    frames = sequence(new_id)
    if new_id == "source-1":
        frames.extend(frame("account", "source-1", stamp=101.7 + index / 100)
                      for index in range(1, 6))
    with pytest.raises(StagingQuarantined, match=reason):
        run(tmp_path, frames, protected=("protected-1",))
    assert not [p for p in (tmp_path / "fleet" / "worker-a" / "checkpoints").glob("*.json")
                if p.name != ".r00-account-creation.json"]


def test_account_id_needs_popup_title_and_label(tmp_path: Path) -> None:
    from dataclasses import replace
    frames = sequence()
    frames[-1] = replace(frames[-1], id_label=None)
    with pytest.raises(StagingQuarantined, match="corroboration"):
        run(tmp_path, frames)


def test_duplicate_active_id_quarantines(tmp_path: Path) -> None:
    run(tmp_path, sequence())
    with pytest.raises(StagingQuarantined, match="duplicate"):
        run(tmp_path, sequence(), worker="worker-b", endpoint="127.0.0.1:5557")


def test_existing_non_r00_worker_binding_is_protected(tmp_path: Path) -> None:
    other = tmp_path / "fleet" / "normal-worker" / "checkpoints"
    other.mkdir(parents=True)
    (other / ("a" * 32 + ".json")).write_text(json.dumps({"account_id": "new-1"}))
    with pytest.raises(StagingQuarantined, match="duplicate"):
        run(tmp_path, sequence())


def test_wrong_lease_or_endpoint_has_no_observation_or_tap(tmp_path: Path) -> None:
    runtime, attempt, adapter, host = setup(tmp_path)
    host.instance = HostInstance("clone-a", "127.0.0.1:5557", "lease-b", "running", "seed-clone")
    called: list[str] = []
    with pytest.raises(StagingQuarantined, match="identity"):
        create_staging_account(runtime=runtime, attempt=attempt, adapter=adapter,
            policy=StagingClone("clone-a", "seed-clone", "source-1", frozenset(), True),
            connect=lambda: called.append("connect"), observe=lambda _: called.append("observe"),
            registry=tmp_path / "registry.json")
    assert called == []
    journal = runtime.checkpoint_root / ".r00-account-creation.json"
    assert json.loads(journal.read_text())["state"] == "quarantined"


def test_unapproved_host_source_lineage_blocks_before_observation(tmp_path: Path) -> None:
    runtime, attempt, adapter, host = setup(tmp_path)
    host.instance = HostInstance("clone-a", attempt.endpoint, attempt.lease_id,
                                 "running", "other-source")
    with pytest.raises(StagingQuarantined, match="lineage"):
        create_staging_account(runtime=runtime, attempt=attempt, adapter=adapter,
            policy=StagingClone("clone-a", "seed-clone", "source-1", frozenset(), True),
            connect=lambda: pytest.fail("connected before lineage check"),
            observe=lambda _: pytest.fail("observed before lineage check"),
            registry=tmp_path / "registry.json")


@pytest.mark.parametrize("dialog", ["New session detected", "Cloud Session Different Than Local Session"])
def test_conflict_dialog_quarantines_without_confirming(tmp_path: Path, dialog: str) -> None:
    frames = sequence()
    frames[2] = frame("account", "source-1", control="new_account", dialog=dialog, stamp=101.2)
    with pytest.raises(StagingQuarantined, match="session_conflict"):
        run(tmp_path, frames)


@pytest.mark.parametrize("control", ["Yes", "No", "Change Account", "Force Cloud Save",
                                           "Email", "Change Email", "Change Password"])
def test_destructive_controls_never_tapped(tmp_path: Path, control: str) -> None:
    frames = sequence()
    frames[2] = frame("account", "source-1", control=control, stamp=101.2)
    with pytest.raises(StagingQuarantined, match="new_account"):
        run(tmp_path, frames)


def test_restart_after_uncertain_new_account_action_stays_quarantined(tmp_path: Path) -> None:
    frames = sequence()[:3]
    with pytest.raises(StagingQuarantined):
        run(tmp_path, frames)
    with pytest.raises(StagingQuarantined, match="previous"):
        run(tmp_path, sequence())


def test_exact_quarantined_warning_can_resume_once_without_repeating_new_account(tmp_path: Path) -> None:
    runtime, attempt, adapter, _ = setup(tmp_path)
    policy = StagingClone("clone-a", "seed-clone", "source-1", frozenset({"source-1"}), True)
    taps: list[tuple[int, int]] = []
    device = SimpleNamespace(serial=attempt.endpoint,
                             click=lambda x, y: taps.append((x, y)))
    first = iter(sequence()[:3] + [frame("unknown", dialog="ambiguous confirmation", stamp=101.3)])
    arguments = dict(runtime=runtime, attempt=attempt, adapter=adapter, policy=policy,
                     connect=lambda: device, registry=tmp_path / "registry.json",
                     clock=lambda: 102., sleep=lambda _: None)
    with pytest.raises(StagingQuarantined, match="ambiguous dialog"):
        create_staging_account(**arguments, observe=lambda _: next(first))
    assert len(taps) == 3
    second = iter(sequence()[3:])
    result = create_staging_account(**arguments, observe=lambda _: next(second),
                                    resume_confirmation=True)
    assert result["state"] == "verified" and result["account_id"] == "new-1"
    assert len(taps) == 6
    assert [row["screen"] for row in result["evidence"]] == [
        "home", "settings", "account", "new_account_warning", "home", "settings", "account",
    ]
    with pytest.raises(StagingQuarantined, match="cannot resume"):
        create_staging_account(**arguments, observe=lambda _: pytest.fail("observed twice"),
                               resume_confirmation=True)


def test_quarantined_warning_resume_requires_exact_attempt_and_source(tmp_path: Path) -> None:
    runtime, attempt, adapter, _ = setup(tmp_path)
    policy = StagingClone("clone-a", "seed-clone", "source-1", frozenset(), True)
    device = SimpleNamespace(serial=attempt.endpoint, click=lambda *_: None)
    first = iter(sequence()[:3] + [frame("unknown", dialog="ambiguous confirmation", stamp=101.3)])
    with pytest.raises(StagingQuarantined):
        create_staging_account(runtime=runtime, attempt=attempt, adapter=adapter,
            policy=policy, connect=lambda: device, observe=lambda _: next(first),
            registry=tmp_path / "registry.json", clock=lambda: 102., sleep=lambda _: None)
    wrong = replace(attempt, generation="a" * 32)
    with pytest.raises(StagingQuarantined, match="cannot resume"):
        create_staging_account(runtime=runtime, attempt=wrong, adapter=adapter,
            policy=policy, connect=lambda: pytest.fail("connected"),
            observe=lambda _: pytest.fail("observed"), registry=tmp_path / "registry.json",
            resume_confirmation=True, clock=lambda: 102., sleep=lambda _: None)


def test_post_confirmation_resume_uses_game_stats_home_without_reconfirming(tmp_path: Path) -> None:
    runtime, attempt, adapter, _ = setup(tmp_path)
    policy = StagingClone("clone-a", "seed-clone", "source-1", frozenset({"source-1"}), True)
    taps: list[tuple[int, int]] = []
    device = SimpleNamespace(serial=attempt.endpoint,
                             click=lambda x, y: taps.append((x, y)))
    arguments = dict(runtime=runtime, attempt=attempt, adapter=adapter, policy=policy,
                     connect=lambda: device, registry=tmp_path / "registry.json",
                     clock=lambda: 102., sleep=lambda _: None)
    first = iter(sequence()[:4] + [frame("unexpected", stamp=101.35)])
    with pytest.raises(StagingQuarantined, match="unexpected screen"):
        create_staging_account(**arguments, observe=lambda _: next(first))
    assert len(taps) == 4
    second = iter([frame("game_over", control="home_from_game_over", stamp=101.4),
                   frame("game_over", control="home_from_game_over", stamp=101.45),
                   *sequence()[5:]])
    result = create_staging_account(**arguments, observe=lambda _: next(second),
                                    resume_after_confirmation=True)
    assert result["state"] == "verified" and result["account_id"] == "new-1"
    assert len(taps) == 7
    assert [row["screen"] for row in result["evidence"]] == [
        "home", "settings", "account", "new_account_warning", "game_over",
        "home", "settings", "account",
    ]


def test_two_workers_have_distinct_persisted_ids(tmp_path: Path) -> None:
    first, _ = run(tmp_path, sequence("new-1"))
    second, _ = run(tmp_path, sequence("new-2"), worker="worker-b", endpoint="127.0.0.1:5557")
    assert first["account_id"] != second["account_id"]


def test_default_gate_blocks_all_io(tmp_path: Path) -> None:
    with pytest.raises(StagingQuarantined, match="disabled"):
        run(tmp_path, sequence(), enabled=False)


def test_runner_refuses_incomplete_or_mismatched_r00_journal(tmp_path: Path) -> None:
    runtime, attempt, _, _ = setup(tmp_path)
    runtime.ensure_directories()
    binding = runtime.checkpoint_root / f"{attempt.generation}.json"
    from fleet.identity import IdentityEvidence
    attempt.persist(binding, IdentityEvidence("new-1", 101.2, "capture://after"))
    journal = runtime.checkpoint_root / ".r00-account-creation.json"
    journal.write_text(json.dumps({"state": "quarantined", "account_id": "new-1"}))
    runner = BotRunner.__new__(BotRunner)
    runner._binding_path = binding
    runner._attempt = attempt
    with pytest.raises(RunnerError, match="R00"):
        runner._verified_account()
    journal.write_text(json.dumps({"state": "verified", "account_id": "other"}))
    with pytest.raises(RunnerError, match="R00"):
        runner._verified_account()


def test_staging_clone_cannot_enter_normal_runner_before_r00(tmp_path: Path) -> None:
    runtime, attempt, adapter, _ = setup(tmp_path)
    runtime.ensure_directories()
    runner = BotRunner.__new__(BotRunner)
    runner._binding_path = runtime.checkpoint_root / f"{attempt.generation}.json"
    runner._attempt = attempt
    runner._host_adapter = adapter
    runner._host_instance = "clone-a"
    with pytest.raises(RunnerError, match="R00"):
        runner._verified_account()


def test_verified_worker_identity_survives_new_attempt_generation(tmp_path: Path) -> None:
    result, _ = run(tmp_path, sequence())
    runtime, attempt, _, _ = setup(tmp_path)
    runner = BotRunner.__new__(BotRunner)
    runner._binding_path = runtime.checkpoint_root / f"{attempt.generation}.json"
    runner._attempt = attempt
    assert runner._verified_account() == result["account_id"]
