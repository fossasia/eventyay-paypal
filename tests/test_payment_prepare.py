from types import SimpleNamespace
from unittest.mock import MagicMock

from eventyay_paypal.payment import Paypal


def provider_with_handler(handler):
    provider = Paypal.__new__(Paypal)
    provider.settings = SimpleNamespace(prefix="", connect_user_id="", merchant_id="")
    provider.event = SimpleNamespace(currency="USD", name="Bare Minimum", slug="baremini")
    provider.paypal_request_handler = handler
    provider._connected_merchant_id = lambda: None
    return provider


def _paypal_order_created():
    return {
        "response": {
            "id": "PAYPAL-ORDER-1",
            "status": "CREATED",
            "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/approve"}],
        }
    }


def test_payment_prepare_stores_order_payment_for_return_from_paypal(monkeypatch):
    """Paying an already-placed order (e.g. after approval) must keep the payment pk.

    Without it, the PayPal return view falls through to cart checkout and shows
    "Your cart is empty" even though the buyer finished paying at PayPal.
    """
    handler = MagicMock()
    handler.create_order.return_value = _paypal_order_created()
    provider = provider_with_handler(handler)
    request = SimpleNamespace(
        event=provider.event,
        session={},
        resolver_match=None,
    )
    payment_obj = SimpleNamespace(pk=42, amount=150)

    monkeypatch.setattr(
        "eventyay_paypal.payment.build_absolute_uri",
        lambda *args, **kwargs: "https://example.test/paypal/return/",
    )
    monkeypatch.setattr("eventyay_paypal.payment.messages.error", lambda *args, **kwargs: None)

    result = provider.payment_prepare(request, payment_obj)

    assert result == "https://sandbox.paypal.com/approve"
    assert request.session["payment_paypal_payment"] == 42
    assert request.session["payment_paypal_order_id"] == "PAYPAL-ORDER-1"


def test_checkout_prepare_clears_order_payment_for_cart_checkout(monkeypatch):
    handler = MagicMock()
    handler.create_order.return_value = _paypal_order_created()
    provider = provider_with_handler(handler)
    request = SimpleNamespace(
        event=provider.event,
        session={"payment_paypal_payment": 99},
        resolver_match=None,
    )

    monkeypatch.setattr(
        "eventyay_paypal.payment.build_absolute_uri",
        lambda *args, **kwargs: "https://example.test/paypal/return/",
    )
    monkeypatch.setattr("eventyay_paypal.payment.messages.error", lambda *args, **kwargs: None)

    result = provider.checkout_prepare(request, {"total": 150})

    assert result == "https://sandbox.paypal.com/approve"
    assert request.session["payment_paypal_payment"] is None


def test_order_payload_includes_payee_for_connected_merchant():
    provider = Paypal.__new__(Paypal)
    provider.settings = SimpleNamespace(
        prefix="",
        connect_client_id="CONNECT-CLIENT",
        connect_secret_key="CONNECT-SECRET",
        connect_user_id="CONNECT-USER",
        merchant_id="MERCHANT-1",
    )
    provider.event = SimpleNamespace(currency="USD", name="Bare Minimum", slug="baremini")

    request = SimpleNamespace(event=provider.event)

    payload = provider._order_payload(
        request,
        150,
        return_url="https://example.test/paypal/return/",
        cancel_url="https://example.test/paypal/cancel/",
    )

    purchase_unit = payload["purchase_units"][0]

    assert purchase_unit["payee"] == {"merchant_id": "MERCHANT-1"}
    assert purchase_unit["amount"]["currency_code"] == "USD"
    assert purchase_unit["amount"]["value"] == "150.00"
