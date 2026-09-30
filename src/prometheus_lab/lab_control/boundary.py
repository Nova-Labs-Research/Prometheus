"""There is deliberately no VM adapter, command construction, or feature flag."""

from typing import NoReturn


class VMOperationsUnavailable(RuntimeError):
    pass


def request_vm_operation(*args: object, **kwargs: object) -> NoReturn:
    """Unconditionally refuse, without inspecting or acting on supplied objects."""
    raise VMOperationsUnavailable("VM operations are unavailable in the code-only Phase 2A milestone")
