"""Wording shared by the research guard and the data-quality verifier (single definition)."""

from __future__ import annotations

import re
from typing import Final

# Wording that describes something other than ownership. A verified quote containing it can never support CONFIRMED.
NEGATIVE_WORDING: Final = re.compile(
    r"\b(partners?|customers?|clients?|vendors?|suppliers?|resellers?|distributors?|competitors?|formerly|former|"
    r"previously|sold to|divested|spun[- ]off|minority (?:stake|interest)|investor in)\b", re.I)
