# Engineering handbook

## Branching
Work branches are `work/<item-id>`; no direct pushes to the default branch.

## Review
Every merge needs the two-approver rule: the facilitator plus one uninvolved
reviewer. Review comments state the requested change as an imperative.

## Testing
A change without a test is a draft, not a candidate. Fast suite must stay
under five minutes; anything slower moves to the nightly lane.

## Style
Python: ruff defaults, 100-column lines, docstrings say WHY not what.
Error messages name the failing thing and the remedy, in one sentence.
