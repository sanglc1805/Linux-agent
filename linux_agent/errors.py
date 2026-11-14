"""Project-specific exceptions."""


class LinuxAgentError(Exception):
    """Base class for expected, user-facing errors."""


class PolicyError(LinuxAgentError):
    """The requested operation is not allowed by policy."""


class ValidationError(LinuxAgentError):
    """An input failed syntactic or semantic validation."""


class ToolExecutionError(LinuxAgentError):
    """A tool or helper failed to execute."""
