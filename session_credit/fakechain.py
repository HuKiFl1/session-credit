"""In-memory ledger for one pinned session escrow.

``credit`` is a test faucet. Liquid balances and per-key escrows are separate:
a draw moves fee units out of liquid and into an escrow, and only pay or
expire moves that escrow back out.
"""

from __future__ import annotations

import re

_AMOUNT = re.compile(r"^0$|^[1-9][0-9]*$")


class FakeChain:
    def __init__(self) -> None:
        self._liquid: dict[str, int] = {}
        self._escrow: dict[str, int] = {}

    def credit(self, did: str, amount: str) -> None:
        """Add faucet funds to a liquid balance. Does not touch escrow."""
        if not isinstance(did, str) or did == "":
            raise ValueError("did is required")
        self._liquid[did] = self.balance(did) + _parse_amount(amount)

    def balance(self, did: str) -> int:
        """Liquid balance. Unknown accounts are 0. Escrow is not included."""
        return self._liquid.get(did, 0)

    def escrow_of(self, key_id: str) -> int:
        """Escrow bound to a session key. Unknown keys are 0."""
        return self._escrow.get(key_id, 0)

    def lock_escrow(self, did: str, key_id: str, amount: int) -> None:
        """Move ``amount`` from liquid into an empty escrow. All-or-nothing."""
        if type(amount) is not int or amount <= 0:
            raise ValueError("amount must be a positive integer")
        if not isinstance(did, str) or did == "" or not isinstance(key_id, str) or key_id == "":
            raise ValueError("did and key_id are required")
        if self._escrow.get(key_id, 0) != 0:
            raise ValueError("escrow occupied")
        liquid = self._liquid.get(did, 0)
        if liquid < amount:
            raise ValueError("insufficient liquid")
        self._liquid[did] = liquid - amount
        self._escrow[key_id] = amount

    def release_escrow(self, key_id: str, did: str) -> int:
        """Move the whole escrow to a liquid account. Empty escrow moves nothing."""
        if not isinstance(did, str) or did == "" or not isinstance(key_id, str) or key_id == "":
            raise ValueError("did and key_id are required")
        amount = self._escrow.get(key_id, 0)
        if amount <= 0:
            raise ValueError("empty escrow")
        self._escrow[key_id] = 0
        self._liquid[did] = self._liquid.get(did, 0) + amount
        return amount


def _parse_amount(amount: str) -> int:
    if not isinstance(amount, str) or _AMOUNT.match(amount) is None:
        raise ValueError("amount must be a decimal integer string")
    return int(amount)
