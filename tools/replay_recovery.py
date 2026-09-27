"""Replay recorded recovery cases with mocked replies; never contacts a device/provider.

Manifest: {"cases": [{"request": RecoveryRequest JSON without image bytes,
"current_context": RecoveryContext JSON, "reply": RecoveryReply JSON,
"observed_postcondition": true|false|null}]}. The postcondition is recorded
evidence, not inferred from a proposal or screenshot text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from recovery_policy import (RecoveryContext, RecoveryReply, RecoveryRequest,
                             proposal_is_current, validate_proposal)


def replay_cases(cases: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Evaluate local proposal eligibility and an independently recorded result."""
    outcomes: list[dict[str, str]] = []
    for case in cases:
        try:
            request = RecoveryRequest.model_validate_json(json.dumps(case["request"]))
            current = RecoveryContext.model_validate_json(json.dumps(case["current_context"]))
            reply = RecoveryReply.model_validate_json(json.dumps(case["reply"]))
        except (KeyError, TypeError, ValueError, ValidationError):
            outcomes.append({"outcome": "rejected", "reason": "invalid_record"})
            continue
        if reply.request_id != request.request_id:
            outcomes.append({"outcome": "rejected", "reason": "reply_mismatch"})
            continue
        if reply.error is not None:
            outcomes.append({"outcome": "rejected", "reason": "provider_failure"})
            continue
        assert reply.proposal is not None
        try:
            validate_proposal(reply.proposal, request.context)
        except ValueError:
            outcomes.append({"outcome": "rejected", "reason": "candidate_mismatch"})
            continue
        if not proposal_is_current(request, current):
            outcomes.append({"outcome": "rejected", "reason": "stale_or_blocked_context"})
            continue
        try:
            validate_proposal(reply.proposal, current)
        except ValueError:
            outcomes.append({"outcome": "rejected", "reason": "candidate_mismatch"})
            continue
        postcondition = case.get("observed_postcondition")
        if postcondition is True:
            outcomes.append({"outcome": "postcondition_confirmed", "reason": "recorded_evidence"})
        elif postcondition is False:
            outcomes.append({"outcome": "postcondition_failed", "reason": "recorded_evidence"})
        else:
            outcomes.append({"outcome": "accepted", "reason": "postcondition_unknown"})
    return outcomes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="local recorded incident JSON")
    args = parser.parse_args(argv)
    manifest: Path = args.manifest
    if not manifest.is_file() or manifest.stat().st_size > 10_000_000:
        parser.error("manifest must be a local file of at most 10 MB")
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        parser.error("manifest requires a cases array")
    print(json.dumps({"outcomes": replay_cases(raw["cases"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
