"""The base of the order input models, and pydantic errors as invalid requests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import BaseModel, ConfigDict, ValidationError

from ib_gateway_mcp.errors import InvalidRequestError

__all__ = ["invalid_request_on"]


class _Spec(BaseModel):
    """Base for inputs: unknown fields are errors."""

    model_config = ConfigDict(extra="forbid")


def _validation_message(name: str, exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        message = str(error["msg"]).removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return f"Invalid {name}: " + "; ".join(parts)


@contextmanager
def invalid_request_on(model: type[BaseModel]) -> Iterator[None]:
    """Raise a failed validation of ``model`` in the block as :class:`InvalidRequestError`.

    Tools with flat parameters build their spec inside it, so a rule that spans several
    parameters reaches the caller as an ``invalid_request`` error, not an internal one,
    while the constructor call stays type-checked::

        with invalid_request_on(BracketSpec):
            spec = BracketSpec(contract=contract, action=action, ...)
    """
    try:
        yield
    except ValidationError as exc:
        raise InvalidRequestError(_validation_message(model.__name__, exc)) from None
