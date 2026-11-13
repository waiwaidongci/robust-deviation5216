from enum import StrEnum


class MissingPolicy(StrEnum):
    REJECT = "reject"
    IMPUTE_TRAIN_MEDIAN = "impute_train_median"


POLICIES = (MissingPolicy.REJECT.value, MissingPolicy.IMPUTE_TRAIN_MEDIAN.value)
DEFAULT_MISSING_POLICY = MissingPolicy.REJECT.value


def resolve_missing_policy(value):
    """Old runs without a recorded policy (or an unknown stored value) stay strict."""
    return value if value in POLICIES else DEFAULT_MISSING_POLICY
