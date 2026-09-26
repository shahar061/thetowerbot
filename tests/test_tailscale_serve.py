from tools.tailscale_serve import desired, served


def test_desired_serves_main_and_running_workers_only():
    accounts = [
        {"running": True, "dashboard_url": "http://127.0.0.1:10053/"},
        {"running": False, "dashboard_url": "http://127.0.0.1:10018/"},
        {"running": True, "dashboard_url": None},
        {"running": True, "dashboard_url": "http://127.0.0.1:22/"},
    ]
    assert desired(accounts) == {443: 8765, 10053: 10053}


def test_served_ignores_ports_it_does_not_own():
    status = {"TCP": {"443": {"HTTPS": True}, "10053": {"HTTPS": True}, "8443": {"HTTPS": True}}}
    assert served(status) == {443, 10053}
    assert served({}) == set()
