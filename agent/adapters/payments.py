"""Payment rail adapter — monetization (STUB, soft requirement).

Per payments-research.md: x402 (Coinbase's HTTP 402 micropayment protocol) gated
by a CDP testnet wallet is the realistic same-day pick — free Base Sepolia
testnet, no KYC, no real money. Not wired up yet; this stub documents the
intended gate so cited.md can show the monetization story in the demo before
the real x402 middleware exists.
"""

PAYWALL_NOTICE = (
    "> **Unlock full plan details** — gated via x402 (Base Sepolia testnet, no real funds) "
    "behind a CDP wallet. Not yet wired up — see `payments-research.md` for the integration plan.\n"
)


def is_unlocked(_receipt: str | None = None) -> bool:
    """Stub: no payment rail wired up yet, so nothing is gated in the demo."""
    return True
