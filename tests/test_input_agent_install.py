from pathlib import Path


def test_input_agent_launch_agent_is_current_user_and_never_listens_on_tcp() -> None:
    root = Path(__file__).parents[1] / "host-agent" / "TowerInputAgent"
    manifest = (root / "LaunchAgents" / "com.thetowerbot.input-agent.plist").read_text()

    assert "~/Library/LaunchAgents" not in manifest
    assert "com.thetowerbot.input-agent" in manifest
    assert "--socket" in manifest
    assert "__SOCKET_PATH__" in manifest
    assert "127.0.0.1" not in manifest
    assert "0.0.0.0" not in manifest


def test_input_agent_has_a_bundle_identity_and_doctor_command() -> None:
    root = Path(__file__).parents[1] / "host-agent" / "TowerInputAgent"
    info = (root / "Resources" / "Info.plist").read_text()
    source = (root / "Sources" / "TowerInputAgent" / "main.swift").read_text()

    assert "com.thetowerbot.input-agent" in info
    assert "doctor" in source
    assert "AXIsProcessTrusted" in source
    assert "ScreenCaptureKit" in source
    assert "getpeereid" in source
    assert "prepareRuntimeDirectory" in source


def test_installer_builds_a_stable_app_identity_without_a_tcp_listener() -> None:
    installer = (Path(__file__).parents[1] / "host-agent" / "TowerInputAgent" / "install.sh").read_text()

    assert "swiftc" in installer
    assert "TowerInputAgent.app" in installer
    assert "chmod 700" in installer
    assert "__SOCKET_PATH__" in installer
