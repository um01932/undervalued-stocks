"""Smoke test: dashboard module exists and the file is importable by spec."""
import importlib.util


def test_dashboard_module_importable():
    """Verify dashboard/app.py exists and can be located by importlib."""
    spec = importlib.util.spec_from_file_location("dashboard.app", "dashboard/app.py")
    assert spec is not None
