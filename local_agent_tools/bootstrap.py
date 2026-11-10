"""Explicit component assembly; constructing an application never probes or writes."""

from pathlib import Path

from local_agent_tools import codex
from local_agent_tools.app import Application
from local_agent_tools.app.machines import Machines
from local_agent_tools.config import Settings


def create_application(config: Settings, state: Path) -> Application:
    return Application(config, state, Machines(config, state), codex=codex)
