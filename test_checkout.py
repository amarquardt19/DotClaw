"""Quick smoke test for the secure_checkout package (Apple Pay flow)."""
from secure_checkout.models import (
    PurchaseRequest, PurchaseState, ApprovalPacket, CardIntent, VirtualCard,
)
from secure_checkout.card_provider import MockCardProvider
from secure_checkout.policy import PolicyEngine
from secure_checkout.audit import AuditLogger
from secure_checkout.agent_api import AgentCheckoutAPI, PURCHASE_REQUEST_SCHEMA
from secure_checkout.telegram_approver import TelegramNotifier, _format_payment_message, _escape
from secure_checkout.stripe_checkout import _encode_stripe_params

print("All imports OK (including new Apple Pay modules)")

# --- Mock card provider ---
provider = MockCardProvider()
intent = CardIntent(spend_limit=500.0, merchant_lock="Expedia")
card = provider.create_card(intent)
print(f"Mock card: {card.masked_pan()}")
assert card.pan.startswith("4") and len(card.cvv) == 3

ok = provider.freeze_card(card.card_id)
assert ok and card.frozen
print("Card freeze OK")

# --- Policy engine ---
policy = PolicyEngine()

good = ApprovalPacket(merchant="Expedia", total_amount=487.32, currency="USD", description="test")
assert policy.check_pre_approval(good) == []
print("Valid packet passes policy")

bad_merchant = ApprovalPacket(merchant="Shady", total_amount=100, currency="USD", description="t")
assert len(policy.check_pre_approval(bad_merchant)) > 0
print("Bad merchant blocked")

over_limit = ApprovalPacket(merchant="Expedia", total_amount=99999, currency="USD", description="t")
assert len(policy.check_pre_approval(over_limit)) > 0
print("Over-limit blocked")

# --- Card limit computation ---
limit = policy.compute_card_limit(487.32)
assert limit > 487.32
print(f"Card limit for $487.32: ${limit:.2f} (buffer applied)")

# --- State machine ---
req = PurchaseRequest(approval_packet=good)
req.transition(PurchaseState.AWAITING_APPROVAL)
assert req.state == PurchaseState.AWAITING_APPROVAL
req.transition(PurchaseState.APPROVED)
assert req.state == PurchaseState.APPROVED
req.transition(PurchaseState.EXECUTING)
req.transition(PurchaseState.COMPLETED)
assert req.state == PurchaseState.COMPLETED
print("State machine transitions OK")

# --- Safe dict never contains PAN ---
req.card = card
safe = req.to_safe_dict()
assert "pan" not in str(safe)
assert "cvv" not in str(safe)
assert "card_masked" in safe
print("Safe dict scrubs card details OK")

# --- Audit logger ---
audit = AuditLogger()
audit.log("test_event", "test-id-1234", {"amount": 500, "merchant": "Expedia"})
print("Audit log write OK")

# --- Tool schema ---
assert PURCHASE_REQUEST_SCHEMA["name"] == "request_purchase"
assert "total_amount" in PURCHASE_REQUEST_SCHEMA["input_schema"]["properties"]
assert "Apple Pay" in PURCHASE_REQUEST_SCHEMA["description"]
print("Agent tool schema OK (Apple Pay description)")

# --- Stripe param encoding ---
encoded = _encode_stripe_params({"mode": "payment", "line_items": [{"quantity": "1"}]})
assert "mode=payment" in encoded
assert "line_items[0][quantity]=1" in encoded
print("Stripe param encoding OK")

# --- Telegram message formatting ---
req2 = PurchaseRequest(
    approval_packet=ApprovalPacket(
        merchant="Expedia", total_amount=487.32, currency="USD",
        description="DFW to JFK round-trip",
        itinerary={"flights": [{"route": "DFW > JFK", "date": "2026-06-15", "airline": "AA"}]},
    ),
    card_intent=CardIntent(spend_limit=507.32, merchant_lock="Expedia"),
)
msg = _format_payment_message(req2, "https://checkout.stripe.com/test123")
assert "PAY TO CONFIRM" in msg
assert "507" in msg
assert "checkout\\.stripe\\.com" in msg or "checkout.stripe.com" in msg
print("Telegram payment message formatting OK")

# --- Orchestrator can init with mock (no Telegram/Stripe needed) ---
from secure_checkout.orchestrator import CheckoutOrchestrator
import os
os.environ["CHECKOUT_CARD_PROVIDER"] = "mock"
orch = CheckoutOrchestrator(headless=True)
assert orch.card_provider is not None
assert orch.policy is not None
print("Orchestrator init OK (lazy Telegram, mock card provider)")

print("\n=== ALL TESTS PASSED ===")
