"""sretk — shared helpers for the SRE toolkit.

Scripts live in sibling directories and bootstrap this package with::

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
    from sretk import out, timewin
    from sretk.aws import Aws

Keeping the helpers here means a new diagnostic is mostly domain logic: find the
resource, read the signal, emit findings.
"""

from . import out, timewin  # noqa: F401
from .aws import Aws, AwsError, arn_tail, error_message, tag_value, utc_window  # noqa: F401
from .findings import CRIT, INFO, OK, WARN, Event, Finding, Report, worst  # noqa: F401

__all__ = [
    "Aws", "AwsError", "arn_tail", "error_message", "tag_value", "utc_window",
    "Finding", "Report", "Event", "worst", "CRIT", "WARN", "INFO", "OK",
    "out", "timewin",
]

__version__ = "0.1.0"
