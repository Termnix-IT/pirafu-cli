from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Notice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    field: str | None = None
    hint: str | None = None


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiVersion: Literal[1] = 1
    command: str
    outcome: Literal["success", "no_change", "planned", "needs_action", "error"] = "success"
    dryRun: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[Notice] = Field(default_factory=list)
    errors: list[Notice] = Field(default_factory=list)
    nextActions: list[Notice] = Field(default_factory=list)
    exit_code: int = Field(default=0, exclude=True)


class PirafuError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2, field: str | None = None):
        super().__init__(message)
        self.notice = Notice(code=code, message=message, field=field)
        self.exit_code = exit_code
