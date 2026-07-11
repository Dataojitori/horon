"""External review (外审) integration for Horon.

Drives a fresh, throwaway opencode session per audit and captures the
reviewer's JSON verdict after caller-supplied schema validation.
"""

from .opencode_client import (
    AuditSession,
    OpencodeUnavailable,
    Verdict,
    NoVerdict,
)

__all__ = [
    "AuditSession",
    "OpencodeUnavailable",
    "Verdict",
    "NoVerdict",
]
