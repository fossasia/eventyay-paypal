import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.utils.timezone import now

from eventyay_paypal.payment import OrderRefund, PaymentException, Paypal

MERCHANT_ID = "MERCHANT-1"
FAILED_ACTION = "eventyay.event.order.refund.failed"


def make_provider(handler):
    provider = Paypal.__new__(Paypal)
    provider.settings = SimpleNamespace(prefix="", connect_user_id="", merchant_id=MERCHANT_ID)
    provider.event = SimpleNamespace(
        currency="USD",
        settings=SimpleNamespace(payment_paypal_merchant_id=MERCHANT_ID),
    )
    provider.paypal_request_handler = handler
    return provider


def make_refund(amount=Decimal("10.00")):
    order = SimpleNamespace(event=SimpleNamespace(currency="USD"), log_action=MagicMock())
    return SimpleNamespace(
        payment=SimpleNamespace(info_data={}),
        amount=amount,
        order=order,
        local_id=7,
        provider="paypal",
        info="",
        state=None,
        save=MagicMock(),
        done=MagicMock(),
    )


def make_handler(refund_response, detail_response=None):
    handler = MagicMock()
    handler.refund_payment.return_value = refund_response
    handler.get_refund_detail.return_value = detail_response
    return handler


@pytest.fixture
def completed_capture(monkeypatch):
    monkeypatch.setattr("eventyay_paypal.payment.COMPLETED_CAPTURE_STATUSES", ("COMPLETED",))
    monkeypatch.setattr(
        "eventyay_paypal.payment.paypal_captures",
        lambda info_data: [{"id": "CAPTURE-1", "status": "COMPLETED"}],
    )


def test_execute_refund_without_completed_capture_raises(monkeypatch):
    monkeypatch.setattr("eventyay_paypal.payment.COMPLETED_CAPTURE_STATUSES", ("COMPLETED",))
    monkeypatch.setattr(
        "eventyay_paypal.payment.paypal_captures",
        lambda info_data: [{"id": "CAPTURE-1", "status": "PENDING"}],
    )
    handler = make_handler({})
    provider = make_provider(handler)

    with pytest.raises(PaymentException):
        provider.execute_refund(make_refund())

    handler.refund_payment.assert_not_called()


def test_execute_refund_without_any_capture_raises(monkeypatch):
    monkeypatch.setattr("eventyay_paypal.payment.paypal_captures", lambda info_data: [])
    handler = make_handler({})
    provider = make_provider(handler)

    with pytest.raises(PaymentException):
        provider.execute_refund(make_refund())

    handler.refund_payment.assert_not_called()


def test_execute_refund_sends_capture_amount_and_merchant(completed_capture):
    handler = make_handler({"response": {"id": "REFUND-1"}}, {"response": {"status": "COMPLETED"}})
    provider = make_provider(handler)

    provider.execute_refund(make_refund(Decimal("10.00")))

    kwargs = handler.refund_payment.call_args.kwargs
    assert kwargs["capture_id"] == "CAPTURE-1"
    assert kwargs["merchant_id"] == MERCHANT_ID
    assert kwargs["refund_data"]["amount"]["currency_code"] == "USD"
    assert Decimal(kwargs["refund_data"]["amount"]["value"]) == Decimal("10.00")
    handler.get_refund_detail.assert_called_once_with(refund_id="REFUND-1", merchant_id=MERCHANT_ID)


def test_execute_refund_sends_partial_amount(completed_capture):
    handler = make_handler({"response": {"id": "REFUND-1"}}, {"response": {"status": "COMPLETED"}})
    provider = make_provider(handler)

    provider.execute_refund(make_refund(Decimal("2.50")))

    kwargs = handler.refund_payment.call_args.kwargs
    assert Decimal(kwargs["refund_data"]["amount"]["value"]) == Decimal("2.50")


def test_execute_refund_completed_marks_refund_done(completed_capture):
    handler = make_handler({"response": {"id": "REFUND-1"}}, {"response": {"status": "COMPLETED"}})
    provider = make_provider(handler)
    refund = make_refund()

    provider.execute_refund(refund)

    refund.done.assert_called_once_with()
    refund.order.log_action.assert_not_called()
    assert json.loads(refund.info) == {"status": "COMPLETED"}


def test_execute_refund_pending_keeps_refund_in_transit(completed_capture):
    handler = make_handler({"response": {"id": "REFUND-1"}}, {"response": {"status": "PENDING"}})
    provider = make_provider(handler)
    refund = make_refund()

    provider.execute_refund(refund)

    assert refund.state == OrderRefund.REFUND_STATE_TRANSIT
    refund.done.assert_not_called()
    refund.order.log_action.assert_not_called()


def test_execute_refund_failed_status_logs_reason_and_raises(completed_capture):
    detail = {"response": {"status": "FAILED", "status_details": {"reason": "ECHECK"}}}
    handler = make_handler({"response": {"id": "REFUND-1"}}, detail)
    provider = make_provider(handler)
    refund = make_refund()

    with pytest.raises(PaymentException) as exc_info:
        provider.execute_refund(refund)

    assert "ECHECK" in str(exc_info.value)
    refund.done.assert_not_called()
    refund.order.log_action.assert_called_once_with(
        FAILED_ACTION,
        {"local_id": 7, "provider": "paypal", "error": "ECHECK"},
    )


def test_execute_refund_unknown_status_without_reason_uses_status(completed_capture):
    handler = make_handler({"response": {"id": "REFUND-1"}}, {"response": {"status": "CANCELLED"}})
    provider = make_provider(handler)
    refund = make_refund()

    with pytest.raises(PaymentException) as exc_info:
        provider.execute_refund(refund)

    assert "CANCELLED" in str(exc_info.value)
    refund.order.log_action.assert_called_once_with(
        FAILED_ACTION,
        {"local_id": 7, "provider": "paypal", "error": "CANCELLED"},
    )


def test_execute_refund_request_error_logs_and_raises(completed_capture):
    errors = {"reason": "INVALID_REQUEST"}
    handler = make_handler({"errors": errors})
    provider = make_provider(handler)
    refund = make_refund()

    with pytest.raises(PaymentException):
        provider.execute_refund(refund)

    handler.get_refund_detail.assert_not_called()
    refund.done.assert_not_called()
    refund.order.log_action.assert_called_once_with(
        FAILED_ACTION,
        {"local_id": 7, "provider": "paypal", "error": str(errors)},
    )


def test_execute_refund_response_without_refund_id_raises(completed_capture):
    handler = make_handler({"response": {}})
    provider = make_provider(handler)
    refund = make_refund()

    with pytest.raises(PaymentException):
        provider.execute_refund(refund)

    handler.get_refund_detail.assert_not_called()
    refund.done.assert_not_called()


def test_execute_refund_detail_error_logs_and_raises(completed_capture):
    errors = {"reason": "NOT_FOUND"}
    handler = make_handler({"response": {"id": "REFUND-1"}}, {"errors": errors})
    provider = make_provider(handler)
    refund = make_refund()

    with pytest.raises(PaymentException):
        provider.execute_refund(refund)

    refund.done.assert_not_called()
    refund.order.log_action.assert_called_once_with(
        FAILED_ACTION,
        {"local_id": 7, "provider": "paypal", "error": str(errors)},
    )


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [(0, True), (179, True), (180, True), (181, False), (400, False)],
)
def test_refund_support_follows_paypal_180_day_window(age_days, expected):
    provider = Paypal.__new__(Paypal)
    payment = SimpleNamespace(payment_date=now() - timedelta(days=age_days))

    assert provider.payment_partial_refund_supported(payment) is expected
    assert provider.payment_refund_supported(payment) is expected


def test_refund_not_supported_without_payment_date():
    provider = Paypal.__new__(Paypal)
    payment = SimpleNamespace(payment_date=None)

    assert provider.payment_partial_refund_supported(payment) is False
    assert provider.payment_refund_supported(payment) is False
