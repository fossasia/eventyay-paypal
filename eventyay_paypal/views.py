import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus

from django.contrib import messages
from django.core import signing
from django.db.models import Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django_scopes import scopes_disabled
from eventyay.base.models import Event, Order, OrderPayment, OrderRefund, Quota
from eventyay.base.payment import PaymentException
from eventyay.control.permissions import event_permission_required
from eventyay.multidomain.urlreverse import eventreverse

from .models import ReferencedPayPalObject
from .operational_log import OUTCOME_FAILURE, OUTCOME_SUCCESS, log_operation
from .payment import Paypal
from .utils import paypal_merchant_can_receive_payments, safe_get

logger = logging.getLogger(__name__)


def paypal_provider_settings_url(event):
    return reverse(
        "control:event.settings.payment.provider",
        kwargs={
            "organizer": event.organizer.slug,
            "event": event.slug,
            "provider": "paypal",
        },
    )


def redirect_to_paypal_settings(event):
    return redirect(paypal_provider_settings_url(event))


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    signer = signing.Signer(salt="safe-redirect")
    try:
        url = signer.unsign(request.GET.get("url", ""))
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid parameter")

    r = render(
        request,
        "plugins/paypal/redirect.html",
        {
            "url": url,
        },
    )
    r._csp_ignore = True
    return r


def store_paypal_connection(event, merchant_id: str, display_name: str | None = None, *, enable: bool = True):
    """Link a PayPal merchant to an event and finish the onboarding.

    ``enable`` is False while PayPal still reports open items for the account. The
    account is linked either way, but PayPal is not switched on, so customers are
    never offered a payment method that PayPal would refuse.
    """
    event.settings.payment_paypal_connect_user_id = merchant_id
    event.settings.payment_paypal_merchant_id = merchant_id
    event.settings.payment_paypal_connect_user_name = display_name or merchant_id
    if enable:
        event.settings.payment_paypal__enabled = True
    del event.settings.payment_paypal_connect_tracking_id


def warn_about_open_paypal_items(request, merchant_info: dict):
    """Tell the seller what PayPal wants from them before payments can be taken."""
    messages.warning(
        request,
        _("PayPal is not switched on yet, because PayPal does not report your account as ready to receive payments."),
    )
    if not merchant_info.get("payments_receivable", True):
        messages.warning(
            request,
            _("Please check the open items in your PayPal account, then switch PayPal on here."),
        )
    if not merchant_info.get("primary_email_confirmed", True):
        messages.warning(
            request,
            _("Please confirm the email address of your PayPal account, then switch PayPal on here."),
        )


@event_permission_required("can_change_event_settings")
@require_GET
def oauth_start(request, **kwargs):
    """Start PayPal Connect onboarding without calling PayPal during settings render."""
    prov = Paypal(request.event)
    if not prov.connect_configured():
        messages.error(
            request,
            _("PayPal Connect is not yet configured. Please ask your administrator to set up the credentials."),
        )
        return redirect_to_paypal_settings(request.event)
    url = prov.get_connect_url(request)
    if not url:
        return redirect_to_paypal_settings(request.event)
    return redirect(url)


@scopes_disabled()
def oauth_return(request, *args, **kwargs):
    """
    https://developer.paypal.com/docs/multiparty/seller-onboarding/before-payment/
    Reference for seller onboarding
    """
    if request.GET.get("error"):
        messages.error(
            request,
            _("PayPal returned an error: {}").format(request.GET.get("error_description") or request.GET.get("error")),
        )
        return redirect(reverse("control:index"))

    required_params = [
        "merchantId",
        "merchantIdInPayPal",
        "permissionsGranted",
    ]
    required_session_params = [
        "payment_paypal_oauth_event",
        "payment_paypal_tracking_id",
    ]
    if any(p not in request.session for p in required_session_params) or any(
        p not in request.GET for p in required_params
    ):
        messages.error(
            request,
            _("An error occurred during connecting with PayPal, please try again."),
        )
        return redirect(reverse("control:index"))

    if request.GET.get("permissionsGranted") != "true":
        messages.error(
            request,
            _("PayPal permissions were not granted. Please try connecting again and approve the requested access."),
        )
        return redirect(reverse("control:index"))

    tracking_id = request.session.get("payment_paypal_tracking_id")
    # Partner Referrals returns merchantId as the tracking_id we issued for this onboarding.
    if request.GET.get("merchantId") != tracking_id:
        messages.error(
            request,
            _("An error occurred during connecting with PayPal, please try again."),
        )
        return redirect(reverse("control:index"))

    event = get_object_or_404(Event, pk=request.session.get("payment_paypal_oauth_event"))
    merchant_id = request.GET.get("merchantIdInPayPal")

    # PayPal reports how ready the account is in the redirect parameters, and in more
    # detail through the Partner Merchant Integrations API, which also gives us a
    # human-readable name. That API needs the platform payer ID to be configured.
    prov = Paypal(event)
    partner_payer_id = event.settings.get("payment_paypal_connect_partner_payer_id")
    display_name = merchant_id  # safe default
    info = {"primary_email_confirmed": request.GET.get("isEmailConfirmed") != "false"}
    if partner_payer_id and merchant_id:
        merchant_info = prov.paypal_request_handler.get_merchant_integrations(
            partner_payer_id=partner_payer_id,
            merchant_id=merchant_id,
        )
        if not merchant_info.get("errors"):
            info = merchant_info.get("response") or {}
            display_name = info.get("primary_email") or info.get("merchant_id") or merchant_id
        else:
            logger.warning(
                "Unable to fetch merchant display name from PayPal: %s",
                merchant_info["errors"].get("reason", merchant_info["errors"]),
            )
    account_is_ready = paypal_merchant_can_receive_payments(info)
    store_paypal_connection(event, merchant_id, display_name, enable=account_is_ready)

    for key in required_session_params:
        request.session.pop(key, None)

    messages.success(
        request,
        _("Your PayPal account is now connected to Eventyay. You can change the settings in detail below."),
    )
    if not account_is_ready:
        warn_about_open_paypal_items(request, info)

    return redirect_to_paypal_settings(event)


@event_permission_required("can_change_event_settings")
@require_POST
def oauth_status(request, **kwargs):
    """Finish an onboarding that PayPal never redirected the seller back from."""
    event = request.event
    prov = Paypal(event)
    if not prov.connect_configured():
        messages.error(
            request,
            _("PayPal Connect is not yet configured. Please ask your administrator to set up the credentials."),
        )
        return redirect_to_paypal_settings(event)

    if event.settings.payment_paypal_connect_user_id:
        messages.info(request, _("Your PayPal account is already connected."))
        return redirect_to_paypal_settings(event)

    tracking_id = event.settings.get("payment_paypal_connect_tracking_id")
    if not tracking_id:
        messages.error(request, _("Please start the connection with PayPal first."))
        return redirect_to_paypal_settings(event)

    partner_payer_id = event.settings.get("payment_paypal_connect_partner_payer_id")
    if not partner_payer_id:
        messages.error(
            request,
            _(
                "Eventyay cannot ask PayPal about your onboarding because the platform payer ID is missing. "
                "Please ask your administrator to add it to the global PayPal Connect settings."
            ),
        )
        return redirect_to_paypal_settings(event)

    integration = prov.paypal_request_handler.find_merchant_integration(
        partner_payer_id=partner_payer_id,
        tracking_id=tracking_id,
    )
    if errors := integration.get("errors"):
        logger.warning(
            "PayPal did not confirm the onboarding for event %s: %s",
            event.pk,
            errors.get("reason", errors),
        )
        messages.warning(
            request,
            _(
                "PayPal has not reported your account as onboarded yet. Please finish all steps at PayPal, "
                "including confirming your email address, and check again."
            ),
        )
        return redirect_to_paypal_settings(event)

    info = integration.get("response") or {}
    merchant_id = info.get("merchant_id")
    if not merchant_id or (info.get("tracking_id") and info["tracking_id"] != tracking_id):
        logger.warning("PayPal onboarding lookup for event %s returned %s", event.pk, info)
        messages.warning(request, _("PayPal did not return a merchant ID for this onboarding yet."))
        return redirect_to_paypal_settings(event)

    account_is_ready = paypal_merchant_can_receive_payments(info)
    store_paypal_connection(event, merchant_id, info.get("primary_email") or merchant_id, enable=account_is_ready)
    messages.success(request, _("Your PayPal account is now connected to Eventyay."))
    if not account_is_ready:
        warn_about_open_paypal_items(request, info)
    return redirect_to_paypal_settings(event)


def success(request, *args, **kwargs):
    token = request.GET.get("token")
    payer = request.GET.get("PayerID")
    request.session["payment_paypal_token"] = token
    if payer:
        request.session["payment_paypal_payer"] = payer
    if token and not request.session.get("payment_paypal_order_id"):
        request.session["payment_paypal_order_id"] = token

    urlkwargs = {}
    if "cart_namespace" in kwargs:
        urlkwargs["cart_namespace"] = kwargs["cart_namespace"]

    if request.session.get("payment_paypal_payment"):
        payment = OrderPayment.objects.get(pk=request.session.get("payment_paypal_payment"))
    else:
        payment = None

    if request.session.get("payment_paypal_order_id", None):
        if payment:
            prov = Paypal(request.event)
            try:
                resp = prov.execute_payment(request, payment)
            except PaymentException as e:
                messages.error(request, str(e))
                urlkwargs["step"] = "payment"
                return redirect(eventreverse(request.event, "presale:event.checkout", kwargs=urlkwargs))
            if resp:
                return resp
    else:
        messages.error(request, _("Invalid response from PayPal received."))
        logger.error("Session did not contain payment_paypal_order_id")
        urlkwargs["step"] = "payment"
        return redirect(eventreverse(request.event, "presale:event.checkout", kwargs=urlkwargs))

    if payment:
        return redirect(
            eventreverse(
                request.event,
                "presale:event.order",
                kwargs={"order": payment.order.code, "secret": payment.order.secret},
            )
            + ("?paid=yes" if payment.order.status == Order.STATUS_PAID else "")
        )
    urlkwargs["step"] = "confirm"
    return redirect(eventreverse(request.event, "presale:event.checkout", kwargs=urlkwargs))


def abort(request, *args, **kwargs):
    messages.error(request, _("It looks like you canceled the PayPal payment"))

    if request.session.get("payment_paypal_payment"):
        payment = OrderPayment.objects.get(pk=request.session.get("payment_paypal_payment"))
    else:
        payment = None

    if payment:
        return redirect(
            eventreverse(
                request.event,
                "presale:event.order",
                kwargs={"order": payment.order.code, "secret": payment.order.secret},
            )
            + ("?paid=yes" if payment.order.status == Order.STATUS_PAID else "")
        )
    else:
        return redirect(eventreverse(request.event, "presale:event.checkout", kwargs={"step": "payment"}))


def check_webhook_signature(request, event, event_json, prov) -> bool:
    """
    Verifies the signature of a webhook from PayPal.

    :param request: The current request object
    :param event: The event object
    :param event_json: The json payload of the webhook
    :param prov: The payment provider instance
    :return: True if the signature is valid, False otherwise
    """

    required_headers = [
        "PAYPAL-AUTH-ALGO",
        "PAYPAL-CERT-URL",
        "PAYPAL-TRANSMISSION-ID",
        "PAYPAL-TRANSMISSION-SIG",
        "PAYPAL-TRANSMISSION-TIME",
    ]
    if any(header not in request.headers for header in required_headers):
        logger.error("Paypal webhook missing required headers")
        return False

    # Prevent replay attacks: check timestamp
    current_time = datetime.now(UTC)
    try:
        transmission_time = datetime.fromisoformat(request.headers.get("PAYPAL-TRANSMISSION-TIME"))
    except (TypeError, ValueError):
        logger.error("Paypal webhook timestamp is invalid")
        return False
    if transmission_time.tzinfo is None:
        transmission_time = transmission_time.replace(tzinfo=UTC)
    if current_time - transmission_time > timedelta(minutes=7):
        logger.error("Paypal webhook timestamp is too old.")
        return False

    verify_response = prov.paypal_request_handler.verify_webhook_signature(
        data={
            "auth_algo": request.headers.get("PAYPAL-AUTH-ALGO"),
            "transmission_id": request.headers.get("PAYPAL-TRANSMISSION-ID"),
            "cert_url": request.headers.get("PAYPAL-CERT-URL"),
            "transmission_sig": request.headers.get("PAYPAL-TRANSMISSION-SIG"),
            "transmission_time": request.headers.get("PAYPAL-TRANSMISSION-TIME"),
            "webhook_id": event.settings.payment_paypal_webhook_id,
            "webhook_event": event_json,
        }
    )

    if errors := verify_response.get("errors"):
        logger.error("Unable to verify signature of webhook: %s", errors.get("reason", errors))
        return False
    if safe_get(verify_response, ["response", "verification_status"], "") != "SUCCESS":
        logger.error("Unable to verify signature of webhook")
        return False
    return True


def parse_webhook_event(request, event_json):
    """
    Parse the given webhook event and return the corresponding event, payment ID and RPO.

    :param request: The current request object
    :param event_json: The json payload of the webhook
    :return: A tuple of (event, payment_id, referenced_paypal_object)
    """
    event = None
    payment_id = None
    if event_json["resource_type"] == "refund":
        for link in event_json["resource"]["links"]:
            if link["rel"] == "up":
                refund_url = link["href"]
                payment_id = refund_url.split("/")[-1]
                break
    else:
        payment_id = event_json["resource"]["id"]

    references = [payment_id]

    # For filtering reference, there are a lot of ids appear within json__event
    if ref_order_id := (safe_get(event_json, ["resource", "supplementary_data", "related_ids", "order_id"])):
        references.append(ref_order_id)

    # Grasp the corresponding RPO
    rpo = (
        ReferencedPayPalObject.objects.select_related("order", "order__event").filter(reference__in=references).first()
    )

    if rpo:
        event = rpo.order.event
        if "id" in rpo.payment.info_data:
            payment_id = rpo.payment.info_data["id"]
    elif hasattr(request, "event"):
        event = request.event

    return event, payment_id, rpo


def extract_order_and_payment(payment_id, event, event_json, prov, rpo=None):
    """
    Extracts order details and associated payment information from PayPal webhook data.

    :param payment_id: The ID of the payment to be extracted.
    :param event: The event object associated with the payment.
    :param event_json: The JSON payload of the webhook event.
    :param prov: The payment provider instance.
    :param rpo: Optional. The referenced PayPal object containing order and payment information.

    :returns: A tuple containing the order details and the payment object.
              Returns (None, None) if an error occurs while retrieving order details.
    """
    order_detail = None
    payment = None

    order_response = prov.paypal_request_handler.get_order(order_id=payment_id)
    if errors := order_response.get("errors"):
        logger.error("Paypal error on webhook: %s event=%s", errors.get("reason", errors), event_json)
        return order_detail, payment

    order_detail = order_response.get("response")
    if not order_detail:
        logger.error("Paypal webhook returned an empty order for %s", payment_id)
        return None, None

    if rpo and rpo.payment:
        payment = rpo.payment
    else:
        payments = OrderPayment.objects.filter(
            order__event=event, provider="paypal", info__icontains=order_detail.get("id")
        )
        payment = None
        for p in payments:
            # Match by stored order ID, not capture ID — the Order ID is not a
            # capture ID, so paypal_payment_matches_capture would always return False.
            if p.info_data.get("id") == order_detail.get("id"):
                payment = p
                break

    return order_detail, payment


@csrf_exempt
@require_POST
@scopes_disabled()
def webhook(request, *args, **kwargs):
    """
    https://developer.paypal.com/api/rest/webhooks/event-names/
    Webhook reference
    """
    event_body = request.body.decode("utf-8").strip()
    try:
        event_json = json.loads(event_body)
    except json.JSONDecodeError:
        log_operation("webhook.inbound", OUTCOME_FAILURE, backend="paypal", payment_provider="paypal", error_code="invalid_payload", status=400)
        return HttpResponse("Invalid JSON", status=HTTPStatus.BAD_REQUEST)

    if not isinstance(event_json, dict) or not isinstance(event_json.get("resource"), dict):
        log_operation("webhook.inbound", OUTCOME_FAILURE, backend="paypal", payment_provider="paypal", error_code="invalid_payload", status=400)
        return HttpResponse("Invalid webhook payload", status=HTTPStatus.BAD_REQUEST)

    if event_json.get("resource_type") not in ("checkout-order", "refund", "capture"):
        return HttpResponse("Wrong resource type", status=HTTPStatus.BAD_REQUEST)

    event, payment_id, rpo = parse_webhook_event(request, event_json)
    if event is None:
        return HttpResponse("Unable to get event from webhook", status=HTTPStatus.BAD_REQUEST)

    prov = Paypal(event)

    # Verify signature
    if not check_webhook_signature(request, event, event_json, prov):
        log_operation("webhook.inbound", OUTCOME_FAILURE, backend="paypal", payment_provider="paypal", error_code="signature_invalid", status=400, event_id=event.pk)
        return HttpResponse("Unable to verify signature of webhook", status=HTTPStatus.BAD_REQUEST)

    order_detail, payment = extract_order_and_payment(payment_id, event, event_json, prov, rpo)
    if order_detail is None or payment is None:
        return HttpResponse("Order or payment not found", status=HTTPStatus.BAD_REQUEST)

    log_operation("webhook.inbound", OUTCOME_SUCCESS, backend="paypal", payment_provider="paypal", status=200, event_id=event.pk, order_id=payment.order_id)
    payment.order.log_action("eventyay.plugins.eventyay_paypal.event", data=event_json)

    def handle_refund():
        refund_id_in_event = safe_get(event_json, ["resource", "id"])
        refund_response = prov.paypal_request_handler.get_refund_detail(
            refund_id=refund_id_in_event,
            merchant_id=event.settings.payment_paypal_merchant_id,
        )
        if errors := refund_response.get("errors"):
            logger.error("Paypal error on webhook: %s event=%s", errors.get("reason", errors), event_json)
            return HttpResponse(f"Refund {refund_id_in_event} not found", status=HTTPStatus.BAD_REQUEST)

        refund_detail = refund_response.get("response")
        if refund_id := refund_detail.get("id"):
            known_refunds = {refund.info_data.get("id"): refund for refund in payment.refunds.all()}
            if refund_id not in known_refunds:
                payment.create_external_refund(
                    amount=abs(Decimal(safe_get(refund_detail, ["amount", "value"], "0.00"))),
                    info=json.dumps(refund_detail),
                )
            elif know_refund := known_refunds.get(refund_id):
                if (
                    know_refund.state
                    in (
                        OrderRefund.REFUND_STATE_CREATED,
                        OrderRefund.REFUND_STATE_TRANSIT,
                    )
                    and refund_detail.get("status", "") == "COMPLETED"
                ):
                    know_refund.done()

            seller_payable_breakdown_value = safe_get(
                refund_detail,
                ["seller_payable_breakdown", "total_refunded_amount", "value"],
                "0.00",
            )
            known_sum = payment.refunds.filter(
                state__in=(
                    OrderRefund.REFUND_STATE_DONE,
                    OrderRefund.REFUND_STATE_TRANSIT,
                    OrderRefund.REFUND_STATE_CREATED,
                )
            ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            total_refunded_amount = Decimal(seller_payable_breakdown_value)
            if known_sum < total_refunded_amount:
                payment.create_external_refund(amount=total_refunded_amount - known_sum)

    def handle_payment_state_confirmed():
        if event_json.get("resource_type") == "refund":
            handle_refund()
        elif order_detail.get("status") == "REFUNDED":
            known_sum = payment.refunds.filter(
                state__in=(
                    OrderRefund.REFUND_STATE_DONE,
                    OrderRefund.REFUND_STATE_TRANSIT,
                    OrderRefund.REFUND_STATE_CREATED,
                )
            ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            if known_sum < payment.amount:
                payment.create_external_refund(amount=payment.amount - known_sum)

    def handle_payment_state_pending():
        if order_detail.get("status") == "APPROVED":
            try:
                request.session["payment_paypal_order_id"] = payment.info_data.get("id")
                payment.payment_provider.execute_payment(request, payment)
            except PaymentException:
                logger.exception("Unable to execute payment in webhook")
        elif order_detail.get("status") == "COMPLETED":
            captured = False
            captures_completed = True
            for purchase_unit in order_detail.get("purchase_units", []):
                for capture in safe_get(purchase_unit, ["payments", "captures"], []):
                    capture_id = capture.get("id")
                    if not capture_id:
                        continue
                    with contextlib.suppress(ReferencedPayPalObject.MultipleObjectsReturned):
                        ReferencedPayPalObject.objects.get_or_create(
                            order=payment.order,
                            payment=payment,
                            reference=capture_id,
                        )
                    if capture.get("status") in (
                        "COMPLETED",
                        "REFUNDED",
                        "PARTIALLY_REFUNDED",
                    ):
                        captured = True
                    else:
                        captures_completed = False
            if captured and captures_completed:
                with contextlib.suppress(Quota.QuotaExceededException):
                    payment.info = json.dumps(order_detail)
                    payment.save(update_fields=["info"])
                    payment.confirm()

    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED and order_detail["status"] in (
        "PARTIALLY_REFUNDED",
        "REFUNDED",
        "COMPLETED",
    ):
        handle_payment_state_confirmed()
    elif payment.state in (
        OrderPayment.PAYMENT_STATE_PENDING,
        OrderPayment.PAYMENT_STATE_CREATED,
        OrderPayment.PAYMENT_STATE_CANCELED,
        OrderPayment.PAYMENT_STATE_FAILED,
    ):
        handle_payment_state_pending()

    return HttpResponse(status=HTTPStatus.OK)


@event_permission_required("can_change_event_settings")
def oauth_disconnect(request, **kwargs):
    if request.method != "POST":
        return render(
            request,
            "plugins/paypal/oauth_disconnect.html",
            {"cancel_url": paypal_provider_settings_url(request.event)},
        )

    del request.event.settings.payment_paypal_connect_user_id
    del request.event.settings.payment_paypal_connect_user_name
    del request.event.settings.payment_paypal_merchant_id
    request.event.settings.payment_paypal__enabled = False
    messages.success(request, _("Your PayPal account has been disconnected."))

    return redirect_to_paypal_settings(request.event)
