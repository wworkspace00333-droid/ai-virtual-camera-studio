"""Print the environment report used to plan native work. Run: .venv/bin/python scripts/inspect_env.py"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vcstudio.core.system_info import inspect_environment  # noqa: E402

print(json.dumps(inspect_environment(), indent=2))
