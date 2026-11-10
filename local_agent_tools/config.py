"""Explicit machines and workspaces; no implicit execution authority."""

import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from local_agent_tools.foundation.sanitize import Fault

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
DEFAULT_CONFIG = Path.home() / ".config/local-agent-tools/config.toml"
DEFAULT_STATE = Path.home() / ".local/state/local-agent-tools"


class Machine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transport: Literal["local"]
    local_access: Literal["read_only", "full_access"]
    execution_enabled: bool = False
    workspace_write_enabled: bool = False


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    machine: Name
    root: str

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\0" in value:
            raise ValueError("workspace root must be an absolute path")
        return value


class SkillSources(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    roots: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("roots")
    @classmethod
    def absolute_roots(cls, values: list[str]) -> list[str]:
        if any(not Path(value).is_absolute() or Path(value) == Path("/") or "\0" in value for value in values):
            raise ValueError("Skill sources must be explicit absolute non-root directories")
        return list(dict.fromkeys(values))


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    machines: dict[Name, Machine]
    workspaces: dict[Name, Workspace] = Field(default_factory=dict)
    skills: SkillSources = Field(default_factory=SkillSources)

    @model_validator(mode="after")
    def configured_machines(self):
        if not self.machines or any(w.machine not in self.machines for w in self.workspaces.values()):
            raise ValueError("workspaces must refer to configured machines")
        return self

    def machine(self, name: str, *, write: bool = False) -> Machine:
        if name not in self.machines:
            raise Fault("unknown_machine", "Select a machine returned by get_capabilities.")
        machine = self.machines[name]
        if write and (not machine.execution_enabled or machine.local_access != "full_access"):
            raise Fault("execution_disabled", "The operator must explicitly enable this machine in configuration.")
        return machine

    def configuration_sha256(self) -> str:
        value = self.model_dump()
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def workspace_machine(self, name: str) -> Machine:
        machine = self.machine(name)
        if machine.local_access == "read_only":
            raise Fault("workspace_write_disabled", "Whole-device Read Only does not authorize mutations.")
        if not machine.workspace_write_enabled:
            raise Fault("workspace_write_disabled", "Typed workspace writes are disabled for this machine.")
        return machine


def load(path: Path) -> Settings:
    try:
        with path.open("rb") as source:
            return Settings.model_validate(tomllib.load(source))
    except (ValidationError, tomllib.TOMLDecodeError) as error:
        # Validation exceptions contain input values; do not print them.
        raise Fault("invalid_config", "Check the schema, machine grants, absolute roots and machine references.") from error
    except OSError as error:
        raise Fault("config_unavailable", "Cannot read configuration; run init or select an existing --config file.") from error


def initialize(path: Path, workspace: Path | None = None, *, commands: bool = False,
               workspace_writes: bool = False, local_access: str = "read_only") -> dict:
    """The first-run writer; never adopt or overwrite."""
    if any(type(value) is not bool for value in (commands, workspace_writes)):
        raise Fault("invalid_config", "First-run grants must be explicit booleans.")
    if not isinstance(local_access, str) or local_access not in {"read_only", "full_access"}:
        raise Fault("invalid_config", "Select explicit read_only or full_access local access.")
    text = ('schema_version = 1\n\n[machines.local]\ntransport = "local"\n'
            f'local_access = "{local_access}"\n'
            f'execution_enabled = {str(commands).lower()}\n'
            f'workspace_write_enabled = {str(workspace_writes).lower()}\n')
    if workspace is not None:
        if not workspace.is_absolute() or not workspace.is_dir():
            raise Fault("invalid_workspace", "The workspace must be an existing absolute directory.")
        text += '\n[workspaces.project]\nmachine = "local"\nroot = ' + json.dumps(str(workspace.resolve()), ensure_ascii=False) + '\n'
    try:
        Settings.model_validate(tomllib.loads(text))
    except (ValidationError, tomllib.TOMLDecodeError) as error:
        raise Fault("invalid_config", "First-run settings must contain a valid local workspace.") from error
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise Fault("config_exists", f"A configuration already exists at {path}; it is never overwritten.") from error
    with os.fdopen(fd, "w") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    return {"created": str(path), "execution_enabled": commands, "workspace_write_enabled": workspace_writes,
            "configuration_sha256": load(path).configuration_sha256()}
