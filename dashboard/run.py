"""Run with: python dashboard/run.py"""
import subprocess
import sys

subprocess.run(
    [sys.executable, "-m", "streamlit", "run", "dashboard/app.py",
     "--server.headless", "false"],
    check=True,
)
