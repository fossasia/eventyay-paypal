"""Privacy and fail-open behavior of plugin operational logs."""

import builtins
import importlib
import json
import logging
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from eventyay.base.models import OrderPayment, OrderRefund
from eventyay.base.payment import PaymentException

from eventyay_paypal import operational_log, views
from eventyay_paypal.operational_log import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    log_operation,
    logged_request,
)
from eventyay_paypal.payment import Paypal


@pytest.fixture
def captured(monkeypatch):
    records = []

    def fake_log(level, action, extra=None):
        records.append((level, action, extra or {}))

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", fake_log)
    return records


def test_drops_email_token_and_unknown_fields(captured):
    log_operation(
        "plugin.loaded",
        OUTCOME_SUCCESS,
        backend="paypal",
        email="person@example.com",
        token="secret-token",
        body="{raw}",
        event_id=12,
    )
    assert len(captured) == 1
    extra = captured[0][2]
    assert extra["backend"] == "paypal"
    assert extra["event_id"] == 12
    assert extra["outcome"] == "success"
    assert "email" not in extra
    assert "token" not in extra
    assert "body" not in extra
    blob = str(extra)
    assert "person@example.com" not in blob
    assert "secret-token" not in blob


def test_rejects_unsafe_action_and_backend(captured):
    log_operation("has space", OUTCOME_SUCCESS, backend="paypal")
    log_operation("connection.request", OUTCOME_FAILURE, backend="pay pal", error_code="not safe")
    assert len(captured) == 1
    level, action, extra = captured[0]
    assert level == logging.WARNING
    assert action == "connection.request"
    assert extra["outcome"] == OUTCOME_FAILURE
    assert "backend" not in extra
    assert "error_code" not in extra


def test_logging_failure_is_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", boom)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", boom)
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="paypal")


def test_host_emitter_failure_falls_back_to_plugin_logger(monkeypatch):
    records = []

    def broken_emitter(*args, **kwargs):
        raise RuntimeError("host emitter failed")

    def fake_log(level, action, extra=None):
        records.append((level, action, extra or {}))

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", broken_emitter)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", fake_log)

    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="paypal")

    assert len(records) == 1
    assert records[0][0] == logging.INFO
    assert records[0][1] == "plugin.loaded"
    assert records[0][2]["backend"] == "paypal"


def test_module_import_swallows_non_import_errors(monkeypatch):
    real_import = builtins.__import__

    def broken_host_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "eventyay.base.operational_logging" or (
            name == "eventyay.base" and fromlist and "operational_logging" in fromlist
        ):
            raise RuntimeError("host init error")
        return real_import(name, globals, locals, fromlist, level)

    try:
        monkeypatch.setattr(builtins, "__import__", broken_host_import)
        importlib.reload(operational_log)
        assert operational_log._HOST_LOG_EVENT is None
    finally:
        monkeypatch.undo()
        importlib.reload(operational_log)


def test_logged_request_records_status_without_url(captured, monkeypatch):
    class Response:
        status_code = 201

    def fake_post(url, **kwargs):
        assert url == "https://example.invalid/secret"
        assert kwargs["json"]["token"] == "raw"
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    logged_request("paypal", "POST", "https://example.invalid/secret", json={"token": "raw"})
    assert captured[-1][1] == "connection.request"
    extra = captured[-1][2]
    assert extra["status"] == 201
    assert extra["outcome"] == "success"
    assert "example.invalid" not in str(extra)
    assert "raw" not in str(extra)


def _make_refund_provider(handler):
    provider = Paypal.__new__(Paypal)
    provider.event = SimpleNamespace(
        pk=12,
        currency="USD",
        settings=SimpleNamespace(payment_paypal_merchant_id="MERCHANT-1"),
    )
    provider.paypal_request_handler = handler
    provider.format_price = lambda _amount: "10.00"
    return provider


def _make_refund():
    order = SimpleNamespace(
        event_id=12,
        event=SimpleNamespace(currency="USD"),
        log_action=MagicMock(),
    )
    payment = SimpleNamespace(
        info_data={
            "purchase_units": [
                {"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}},
            ]
        }
    )
    return SimpleNamespace(
        payment=payment,
        order=order,
        order_id=99,
        amount="10.00",
        local_id=1,
        provider="paypal",
        info="",
        state=OrderRefund.REFUND_STATE_CREATED,
        save=MagicMock(),
        done=MagicMock(),
    )


def test_execute_refund_logs_success_only_on_completed(captured):
    handler = MagicMock()
    handler.refund_payment.return_value = {"response": {"id": "REF-1"}}
    handler.get_refund_detail.return_value = {"response": {"id": "REF-1", "status": "COMPLETED"}}
    provider = _make_refund_provider(handler)
    refund = _make_refund()

    provider.execute_refund(refund)

    refund.done.assert_called_once()
    assert len(captured) == 1
    assert captured[0][1] == "payment.refund"
    assert captured[0][2]["outcome"] == OUTCOME_SUCCESS
    assert captured[0][2]["event_id"] == 12
    assert captured[0][2]["order_id"] == 99


def test_execute_refund_pending_emits_no_terminal_outcome(captured):
    handler = MagicMock()
    handler.refund_payment.return_value = {"response": {"id": "REF-1"}}
    handler.get_refund_detail.return_value = {"response": {"id": "REF-1", "status": "PENDING"}}
    provider = _make_refund_provider(handler)
    refund = _make_refund()

    result = provider.execute_refund(refund)

    assert result is None
    assert refund.state == OrderRefund.REFUND_STATE_TRANSIT
    refund.save.assert_called_with(update_fields=["state"])
    assert captured == []


def test_execute_refund_detail_error_logs_failure_without_success(captured):
    handler = MagicMock()
    handler.refund_payment.return_value = {"response": {"id": "REF-1"}}
    handler.get_refund_detail.return_value = {"errors": {"reason": "Not found"}}
    provider = _make_refund_provider(handler)
    refund = _make_refund()

    with pytest.raises(PaymentException):
        provider.execute_refund(refund)

    assert len(captured) == 1
    assert captured[0][1] == "payment.refund"
    assert captured[0][2]["outcome"] == OUTCOME_FAILURE
    assert captured[0][2]["error_code"] == "refund_detail_failed"


def test_execute_refund_not_completed_logs_failure_without_success(captured):
    handler = MagicMock()
    handler.refund_payment.return_value = {"response": {"id": "REF-1"}}
    handler.get_refund_detail.return_value = {
        "response": {"id": "REF-1", "status": "CANCELLED", "status_details": {"reason": "ECHECK"}}
    }
    provider = _make_refund_provider(handler)
    refund = _make_refund()

    with pytest.raises(PaymentException):
        provider.execute_refund(refund)

    assert len(captured) == 1
    assert captured[0][1] == "payment.refund"
    assert captured[0][2]["outcome"] == OUTCOME_FAILURE
    assert captured[0][2]["error_code"] == "refund_not_completed"


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"{not-json", "invalid_payload"),
        (b'{"resource": "not-a-dict"}', "invalid_payload"),
        (b'{"resource_type": "dispute", "resource": {"id": "1"}}', "wrong_resource_type"),
    ],
)
def test_webhook_rejections_before_event_lookup_log_failure(captured, body, expected_code):
    request = SimpleNamespace(method="POST", body=body)
    response = views.webhook(request)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert len(captured) == 1
    assert captured[0][1] == "webhook.inbound"
    assert captured[0][2]["outcome"] == OUTCOME_FAILURE
    assert captured[0][2]["error_code"] == expected_code
    assert captured[0][2]["status"] == 400


def test_webhook_event_not_found_logs_failure(captured, monkeypatch):
    monkeypatch.setattr(views, "parse_webhook_event", lambda _req, _json: (None, None, None))
    request = SimpleNamespace(
        method="POST",
        body=json.dumps({"resource_type": "capture", "resource": {"id": "CAP-1"}}).encode(),
    )

    response = views.webhook(request)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert len(captured) == 1
    assert captured[0][1] == "webhook.inbound"
    assert captured[0][2]["outcome"] == OUTCOME_FAILURE
    assert captured[0][2]["error_code"] == "event_not_found"
    assert captured[0][2]["status"] == 400


def test_webhook_signature_and_order_rejections_log_failure(captured, monkeypatch):
    fake_event = SimpleNamespace(pk=12)
    monkeypatch.setattr(views, "parse_webhook_event", lambda _req, _json: (fake_event, "PAY-1", None))
    monkeypatch.setattr(views, "Paypal", lambda _ev: SimpleNamespace())
    request = SimpleNamespace(
        method="POST",
        body=json.dumps({"resource_type": "capture", "resource": {"id": "CAP-1"}}).encode(),
    )

    monkeypatch.setattr(views, "check_webhook_signature", lambda *_args: False)
    resp_sig = views.webhook(request)
    assert resp_sig.status_code == HTTPStatus.BAD_REQUEST

    monkeypatch.setattr(views, "check_webhook_signature", lambda *_args: True)
    monkeypatch.setattr(views, "extract_order_and_payment", lambda *_args: (None, None))
    resp_order = views.webhook(request)
    assert resp_order.status_code == HTTPStatus.BAD_REQUEST

    assert len(captured) == 2
    assert captured[0][2]["error_code"] == "signature_invalid"
    assert captured[0][2]["event_id"] == 12
    assert captured[1][2]["error_code"] == "order_not_found"
    assert captured[1][2]["event_id"] == 12


def test_webhook_success_logged_only_after_processing(captured, monkeypatch):
    fake_event = SimpleNamespace(pk=12)
    fake_order = SimpleNamespace(log_action=MagicMock())
    fake_payment = SimpleNamespace(
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        order_id=99,
        order=fake_order,
    )
    monkeypatch.setattr(views, "parse_webhook_event", lambda _req, _json: (fake_event, "PAY-1", None))
    monkeypatch.setattr(views, "Paypal", lambda _ev: SimpleNamespace())
    monkeypatch.setattr(views, "check_webhook_signature", lambda *_args: True)
    monkeypatch.setattr(views, "extract_order_and_payment", lambda *_args: ({"status": "COMPLETED"}, fake_payment))

    body = json.dumps(
        {
            "resource_type": "capture",
            "resource": {"id": "CAP-1"},
            "payer_email": "buyer@example.com",
        }
    ).encode()
    response = views.webhook(SimpleNamespace(method="POST", body=body))

    assert response.status_code == HTTPStatus.OK
    fake_order.log_action.assert_called_once()
    assert len(captured) == 1
    assert captured[0][1] == "webhook.inbound"
    assert captured[0][2]["outcome"] == OUTCOME_SUCCESS
    assert captured[0][2]["status"] == 200
    assert captured[0][2]["event_id"] == 12
    assert captured[0][2]["order_id"] == 99
    assert "buyer@example.com" not in str(captured)


def test_webhook_processing_error_logs_failure_not_success(captured, monkeypatch):
    fake_event = SimpleNamespace(pk=12)
    fake_order = SimpleNamespace(log_action=MagicMock(side_effect=RuntimeError("db down")))
    fake_payment = SimpleNamespace(
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        order_id=99,
        order=fake_order,
    )
    monkeypatch.setattr(views, "parse_webhook_event", lambda _req, _json: (fake_event, "PAY-1", None))
    monkeypatch.setattr(views, "Paypal", lambda _ev: SimpleNamespace())
    monkeypatch.setattr(views, "check_webhook_signature", lambda *_args: True)
    monkeypatch.setattr(views, "extract_order_and_payment", lambda *_args: ({"status": "COMPLETED"}, fake_payment))

    body = json.dumps({"resource_type": "capture", "resource": {"id": "CAP-1"}}).encode()
    with pytest.raises(RuntimeError, match="db down"):
        views.webhook(SimpleNamespace(method="POST", body=body))

    assert len(captured) == 1
    assert captured[0][1] == "webhook.inbound"
    assert captured[0][2]["outcome"] == OUTCOME_FAILURE
    assert captured[0][2]["error_code"] == "processing_error"
    assert captured[0][2]["status"] == 500
    assert captured[0][2]["event_id"] == 12
    assert captured[0][2]["order_id"] == 99


def test_oauth_return_logs_without_pii(captured, monkeypatch):
    fake_event = SimpleNamespace(
        pk=12,
        settings=SimpleNamespace(get=lambda _k: "PARTNER-1"),
    )
    fake_provider = SimpleNamespace(
        paypal_request_handler=SimpleNamespace(
            get_merchant_integrations=lambda **_kw: {
                "response": {
                    "merchant_id": "M-1",
                    "primary_email": "merchant@example.com",
                    "payments_receivable": True,
                    "primary_email_confirmed": True,
                }
            }
        )
    )
    monkeypatch.setattr(views, "get_object_or_404", lambda *_args, **_kw: fake_event)
    monkeypatch.setattr(views, "Paypal", lambda _ev: fake_provider)
    monkeypatch.setattr(views, "store_paypal_connection", MagicMock())
    monkeypatch.setattr(views.messages, "success", MagicMock())
    monkeypatch.setattr(views, "redirect_to_paypal_settings", lambda _ev: SimpleNamespace(status_code=302))

    request = SimpleNamespace(
        GET={
            "merchantId": "TRACK-1",
            "merchantIdInPayPal": "M-1",
            "permissionsGranted": "true",
        },
        session={
            "payment_paypal_oauth_event": 12,
            "payment_paypal_tracking_id": "TRACK-1",
        },
    )
    resp = views.oauth_return(request)

    assert resp.status_code == 302
    assert len(captured) == 1
    assert captured[0][1] == "oauth.callback"
    assert captured[0][2]["outcome"] == OUTCOME_SUCCESS
    assert captured[0][2]["event_id"] == 12
    assert "merchant@example.com" not in str(captured)
    assert "TRACK-1" not in str(captured)
