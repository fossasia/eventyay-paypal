import contextlib
import json
import logging
import urllib.parse
from collections import OrderedDict
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core import signing
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.utils.timezone import now
from django.utils.translation import gettext as __
from django.utils.translation import gettext_lazy as _
from eventyay.base.decimal import round_decimal
from eventyay.base.models import Event, Order, OrderPayment, OrderRefund, Quota
from eventyay.base.payment import BasePaymentProvider, PaymentException
from eventyay.base.services.mail import SendMailException
from eventyay.base.settings import SettingsSandbox
from eventyay.helpers.urls import build_absolute_uri as build_global_uri
from eventyay.multidomain.urlreverse import build_absolute_uri
from i18nfield.strings import LazyI18nString

from .models import ReferencedPayPalObject
from .paypal_rest import PaypalRequestHandler
from .utils import (
    COMPLETED_CAPTURE_STATUSES,
    CONNECT_STATE_CONNECTED,
    CONNECT_STATE_PENDING,
    canonical_paypal_endpoint,
    is_paypal_sandbox,
    paypal_approval_href,
    paypal_can_return_to,
    paypal_captures,
    paypal_connect_state,
    paypal_is_configured,
    paypal_payee_block,
    safe_get,
    uses_paypal_connect,
)

logger = logging.getLogger(__name__)

SUPPORTED_CURRENCIES = [
    "AUD",
    "BRL",
    "CAD",
    "CZK",
    "DKK",
    "EUR",
    "HKD",
    "HUF",
    "INR",
    "ILS",
    "JPY",
    "MYR",
    "MXN",
    "TWD",
    "NZD",
    "NOK",
    "PHP",
    "PLN",
    "GBP",
    "RUB",
    "SGD",
    "SEK",
    "CHF",
    "THB",
    "USD",
]

LOCAL_ONLY_CURRENCIES = ["INR"]


class Paypal(BasePaymentProvider):
    identifier = "paypal"
    verbose_name = _("PayPal")
    payment_form_fields = OrderedDict([])

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "paypal", event)
        self.paypal_request_handler = PaypalRequestHandler(self.settings)

    @property
    def test_mode_message(self):
        if uses_paypal_connect(self.settings):
            is_sandbox = is_paypal_sandbox(self.settings.connect_endpoint)
        else:
            is_sandbox = is_paypal_sandbox(self.settings.get("endpoint"))
        if is_sandbox:
            return _(
                "The PayPal sandbox is being used, you can test without actually sending money but you will need a "
                "PayPal sandbox user to log in."
            )
        return None

    def connect_configured(self) -> bool:
        return uses_paypal_connect(self.settings)

    @property
    def is_enabled(self) -> bool:
        # Do not report PayPal as enabled while it cannot process a payment, e.g. when
        # onboarding was never finished or the platform credentials were removed again.
        return super().is_enabled and paypal_is_configured(self.settings)

    def _connected_merchant_id(self) -> str | None:
        if self.connect_configured() and self.settings.connect_user_id:
            return self.settings.merchant_id or None
        return None

    @property
    def settings_form_fields(self):
        connect_state = paypal_connect_state(self.settings)
        if connect_state == CONNECT_STATE_PENDING:
            # Hide every option until the PayPal account is linked, so that the connect
            # panel rendered by settings_content_render is the only thing to act on.
            return {}

        if connect_state == CONNECT_STATE_CONNECTED:
            fields = [
                (
                    "connect_user_name",
                    forms.CharField(label=_("PayPal account"), disabled=True, required=False),
                ),
                (
                    "connect_user_id",
                    forms.CharField(label=_("PayPal merchant ID"), disabled=True, required=False),
                ),
            ]
        else:
            fields = [
                (
                    "client_id",
                    forms.CharField(
                        label=_("Client ID"),
                        max_length=80,
                        min_length=80,
                        help_text=_('<a target="_blank" rel="noopener" href="{docs_url}">{text}</a>').format(
                            text=_("Click here for a tutorial on how to obtain the required keys"),
                            docs_url="https://docs.eventyay.com/en/latest/user/payments/paypal.html",
                        ),
                    ),
                ),
                (
                    "secret",
                    forms.CharField(
                        label=_("Secret"),
                        max_length=80,
                        min_length=80,
                    ),
                ),
                (
                    "endpoint",
                    forms.ChoiceField(
                        label=_("Endpoint"),
                        initial=canonical_paypal_endpoint(self.settings.get("endpoint")),
                        choices=(
                            ("live", "Live"),
                            ("sandbox", "Sandbox"),
                        ),
                        help_text=_("Use the sandbox to test the checkout without moving real money."),
                    ),
                ),
                (
                    "webhook_id",
                    forms.CharField(
                        label=_("Webhook ID"),
                        initial="test_webhook_id",
                        max_length=20,
                        min_length=10,
                        help_text=_(
                            "The ID of the webhook you created in your PayPal app for the endpoint shown below."
                        ),
                    ),
                ),
            ]

        extra_fields = [
            (
                "prefix",
                forms.CharField(
                    label=_("Reference prefix"),
                    help_text=_(
                        "Any value entered here will be added in front of the regular booking reference "
                        "containing the order number."
                    ),
                    required=False,
                ),
            )
        ]

        d = OrderedDict(fields + extra_fields + list(super().settings_form_fields.items()))

        d.move_to_end("prefix")
        d.move_to_end("_enabled", False)
        return d

    def get_connect_url(self, request):
        """Create a PayPal Partner Referrals action URL for OAuth onboarding."""
        tracking_id = get_random_string(32)
        request.session["payment_paypal_oauth_event"] = request.event.pk
        request.session["payment_paypal_tracking_id"] = tracking_id
        # Also stored on the event so that the onboarding can be completed later on
        # if PayPal never redirects the seller back to us.
        self.settings.connect_tracking_id = tracking_id

        response_data = self.paypal_request_handler.create_partner_referrals(
            data={
                "operations": [
                    {
                        "operation": "API_INTEGRATION",
                        "api_integration_preference": {
                            "rest_api_integration": {
                                "integration_method": "PAYPAL",
                                "integration_type": "THIRD_PARTY",
                                "third_party_details": {
                                    "features": [
                                        "PAYMENT",
                                        "REFUND",
                                        "ACCESS_MERCHANT_INFORMATION",
                                    ],
                                },
                            }
                        },
                    }
                ],
                "products": ["EXPRESS_CHECKOUT"],
                "partner_config_override": {
                    "return_url": build_global_uri("plugins:eventyay_paypal:oauth.return"),
                    "return_url_description": str(_("Return to Eventyay to finish setting up PayPal.")),
                },
                "legal_consents": [{"type": "SHARE_DATA_CONSENT", "granted": True}],
                "tracking_id": tracking_id,
            },
        )

        if errors := response_data.get("errors"):
            messages.error(
                request,
                _("An error occurred during connecting with PayPal: {}").format(errors.get("reason", errors)),
            )
            return

        response = response_data.get("response") or {}
        for link in response.get("links", []):
            if link.get("rel") == "action_url":
                return link.get("href")
        messages.error(
            request,
            _("An error occurred during connecting with PayPal, please try again."),
        )
        return None

    def settings_content_render(self, request):
        connect_state = paypal_connect_state(self.settings)
        if connect_state == CONNECT_STATE_CONNECTED:
            sandbox = is_paypal_sandbox(self.settings.connect_endpoint)
        else:
            sandbox = is_paypal_sandbox(self.settings.get("endpoint"))
        url_kwargs = {"organizer": self.event.organizer.slug, "event": self.event.slug}
        return_url = build_global_uri("plugins:eventyay_paypal:oauth.return")
        template = get_template("plugins/paypal/settings_connect.html")
        return template.render(
            {
                "connect_state": connect_state,
                "connect_url": reverse("plugins:eventyay_paypal:oauth.start", kwargs=url_kwargs),
                "disconnect_url": reverse("plugins:eventyay_paypal:oauth.disconnect", kwargs=url_kwargs),
                "status_url": reverse("plugins:eventyay_paypal:oauth.status", kwargs=url_kwargs),
                "onboarding_started": bool(self.settings.connect_tracking_id),
                "return_url": return_url,
                "return_url_is_reachable": paypal_can_return_to(return_url),
                "account_name": self.settings.connect_user_name or self.settings.connect_user_id,
                "sandbox": sandbox,
                "webhook_url": build_global_uri("plugins:eventyay_paypal:webhook"),
                "docs_url": "https://docs.eventyay.com/en/latest/user/payments/paypal.html",
                "switched_on_but_unusable": (
                    self.settings.get("_enabled", as_type=bool) and not paypal_is_configured(self.settings)
                ),
                "currency": self.event.currency,
                "currency_supported": self.event.currency in SUPPORTED_CURRENCIES,
                "currency_local_only": self.event.currency in LOCAL_ONLY_CURRENCIES,
            },
            request=request,
        )

    def is_allowed(self, request: HttpRequest, total: Decimal = None) -> bool:
        return super().is_allowed(request, total) and self.event.currency in SUPPORTED_CURRENCIES

    def payment_is_valid_session(self, request):
        return bool(request.session.get("payment_paypal_order_id"))

    def payment_form_render(self, request) -> str:
        template = get_template("plugins/paypal/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)

    def checkout_prepare(self, request, cart):
        kwargs = {}
        if request.resolver_match and "cart_namespace" in request.resolver_match.kwargs:
            kwargs["cart_namespace"] = request.resolver_match.kwargs["cart_namespace"]
        return self.start_paypal_order(
            request,
            cart["total"],
            return_url=build_absolute_uri(
                request.event,
                "plugins:eventyay_paypal:return",
                kwargs=kwargs,
            ),
            cancel_url=build_absolute_uri(
                request.event,
                "plugins:eventyay_paypal:abort",
                kwargs=kwargs,
            ),
        )

    def payment_prepare(self, request, payment_obj):
        # Paying an existing order (retry / after approval) must remember the
        # OrderPayment so the PayPal return view can capture it. Without this,
        # success() treats the return as cart checkout and redirects to an empty
        # cart ("Your cart is empty") without recording the payment.
        return self.start_paypal_order(
            request,
            payment_obj.amount,
            return_url=build_absolute_uri(request.event, "plugins:eventyay_paypal:return"),
            cancel_url=build_absolute_uri(request.event, "plugins:eventyay_paypal:abort"),
            payment=payment_obj,
        )

    def start_paypal_order(self, request, amount, *, return_url: str, cancel_url: str, payment=None):
        order_response = self.paypal_request_handler.create_order(
            order_data=self._order_payload(request, amount, return_url=return_url, cancel_url=cancel_url)
        )
        if errors := order_response.get("errors"):
            messages.error(
                request,
                _("An error occurred during connecting with PayPal: {}").format(errors.get("reason", errors)),
            )
            return None
        request.session["payment_paypal_payment"] = payment.pk if payment is not None else None
        return self._create_order(request, order_response.get("response"))

    def _order_payload(self, request, amount, *, return_url: str, cancel_url: str) -> dict:
        formatted_amount = self.format_price(amount)
        purchase_unit = {
            "items": [
                {
                    "name": (f"{self.settings.prefix} " if self.settings.prefix else "")
                    + __("Order for %s") % str(request.event),
                    "quantity": "1",
                    "unit_amount": {
                        "currency_code": request.event.currency,
                        "value": formatted_amount,
                    },
                }
            ],
            "amount": {
                "currency_code": request.event.currency,
                "value": formatted_amount,
                "breakdown": {
                    "item_total": {
                        "currency_code": request.event.currency,
                        "value": formatted_amount,
                    }
                },
            },
            "description": __("Event tickets for {event}").format(event=request.event.name),
        }
        merchant_id = self._connected_merchant_id()
        payee = paypal_payee_block(merchant_id)
        if payee:
            purchase_unit["payee"] = payee
        return {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "payment_method_preference": "UNRESTRICTED",
                        "landing_page": "LOGIN",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }

    def format_price(self, value):
        return str(
            round_decimal(
                value,
                self.event.currency,
                {
                    # PayPal behaves differently than Stripe in deciding what currencies have decimal places
                    # Source https://developer.paypal.com/docs/classic/api/currency_codes/
                    "HUF": 0,
                    "JPY": 0,
                    "MYR": 0,
                    "TWD": 0,
                    # However, CLPs are not listed there while PayPal requires us not to send decimal places there. WTF.
                    "CLP": 0,
                    # Let's just guess that the ones listed here are 0-based as well
                    # https://developers.braintreepayments.com/reference/general/currencies
                    "BIF": 0,
                    "DJF": 0,
                    "GNF": 0,
                    "KMF": 0,
                    "KRW": 0,
                    "LAK": 0,
                    "PYG": 0,
                    "RWF": 0,
                    "UGX": 0,
                    "VND": 0,
                    "VUV": 0,
                    "XAF": 0,
                    "XOF": 0,
                    "XPF": 0,
                },
            )
        )

    @property
    def abort_pending_allowed(self):
        return False

    def _create_order(self, request, order):
        if not order or order.get("status") not in ("CREATED", "PAYER_ACTION_REQUIRED"):
            messages.error(request, _("We had trouble communicating with PayPal"))
            logger.error("Invalid order state: %s", order)
            return

        request.session["payment_paypal_order_id"] = order["id"]
        href = paypal_approval_href(order)
        if href:
            if request.session.get("iframe_session", False):
                signer = signing.Signer(salt="safe-redirect")
                return (
                    build_absolute_uri(request.event, "plugins:eventyay_paypal:redirect")
                    + "?url="
                    + urllib.parse.quote(signer.sign(href))
                )
            return str(href)
        messages.error(request, _("We had trouble communicating with PayPal"))
        logger.error("PayPal order %s did not include an approval URL: %s", order.get("id"), order)
        return None

    def checkout_confirm_render(self, request) -> str:
        """
        Returns the HTML that should be displayed when the user selected this provider
        on the 'confirm order' page.
        """
        template = get_template("plugins/paypal/checkout_payment_confirm.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        def handle_paypal_error(errors, order_id, payment, message):
            exception = errors.get("exception")
            logger.error(message, order_id, errors.get("reason", errors))
            payment.fail(
                info={
                    "error": {
                        "name": errors.get("type"),
                        "message": errors.get("reason"),
                        "exception": None if exception is None else f"{exception}",
                        "order_id": order_id,
                    }
                }
            )
            raise PaymentException(_("Unable to process your payment with Paypal"))

        order_id = request.session.get("payment_paypal_order_id", "")
        if not order_id:
            raise PaymentException(
                _("We were unable to process your payment. See below for details on how to proceed.")
            )

        order_response = self.paypal_request_handler.get_order(order_id=order_id)
        if errors := order_response.get("errors"):
            handle_paypal_error(
                errors,
                order_id,
                payment,
                "Unable to retrieve order %s from Paypal: %s",
            )

        order_detail = order_response.get("response") or {}
        if not order_detail:
            handle_paypal_error(
                {"type": "EmptyResponse", "reason": "Empty PayPal order response", "exception": None},
                order_id,
                payment,
                "Unable to retrieve order %s from Paypal: %s",
            )
        with contextlib.suppress(ReferencedPayPalObject.MultipleObjectsReturned):
            ReferencedPayPalObject.objects.get_or_create(order=payment.order, payment=payment, reference=order_id)

        _unit = order_detail.get("purchase_units", [{}])[0]
        _paypal_amount_str = safe_get(_unit, ["amount", "value"]) or "0"
        try:
            # Compare as Decimal so zero-decimal currencies (JPY, HUF, …) are not
            # falsely rejected: format_price returns "1000" but PayPal may return
            # "1000.00". String equality would fail; Decimal equality is correct.
            _amounts_match = Decimal(self.format_price(payment.amount)) == Decimal(str(_paypal_amount_str))
        except Exception:
            _amounts_match = False
        if not _amounts_match or safe_get(_unit, ["amount", "currency_code"]) != self.event.currency:
            logger.error(
                "Value mismatch: Payment %s vs paypal trans %s",
                payment.id,
                str(order_detail),
            )
            payment.fail(
                info={
                    "error": {
                        "name": "ValidationError",
                        "message": "Value mismatch",
                    }
                }
            )
            raise PaymentException(
                _("We were unable to process your payment. See below for details on how to proceed.")
            )

        captured_order = None
        order_status = order_detail.get("status")
        if order_status == "COMPLETED":
            captured_order = order_detail
        elif order_status == "APPROVED":
            description = (f"{self.settings.prefix} " if self.settings.prefix else "") + __(
                "Order {order} for {event}"
            ).format(event=request.event.name, order=payment.order.code)

            update_response = self.paypal_request_handler.update_order(
                order_id=order_id,
                update_data=[
                    {
                        "op": "replace",
                        "path": "/purchase_units/@reference_id=='default'/description",
                        "value": description,
                    }
                ],
            )
            if errors := update_response.get("errors"):
                handle_paypal_error(
                    errors,
                    order_id,
                    payment,
                    "Unable to patch order %s in Paypal: %s",
                )

            capture_response = self.paypal_request_handler.capture_order(order_id=order_id)
            if errors := capture_response.get("errors"):
                handle_paypal_error(
                    errors,
                    order_id,
                    payment,
                    "Unable to capture order %s in Paypal: %s",
                )

            captured_order = capture_response.get("response") or {}
            for purchase_unit in captured_order.get("purchase_units", []):
                for capture in safe_get(purchase_unit, ["payments", "captures"], []):
                    with contextlib.suppress(ReferencedPayPalObject.MultipleObjectsReturned):
                        capture_id = capture.get("id")
                        if not capture_id:
                            continue
                        ReferencedPayPalObject.objects.get_or_create(
                            order=payment.order,
                            payment=payment,
                            reference=capture_id,
                        )
                    if capture.get("status") != "COMPLETED":
                        messages.warning(
                            request,
                            _(
                                "PayPal has not yet approved the payment. We will inform you as soon as the payment completed."
                            ),
                        )
                        payment.info = json.dumps(captured_order)
                        payment.state = OrderPayment.PAYMENT_STATE_PENDING
                        payment.save()
                        return
        else:
            payment.fail(info=order_detail)
            logger.error("Invalid PayPal order state: %s", order_detail)
            raise PaymentException(
                _("We were unable to process your payment. See below for details on how to proceed.")
            )

        payment.refresh_from_db()
        if not captured_order or captured_order.get("status") != "COMPLETED":
            payment.fail(info=captured_order)
            logger.error("Invalid state: %s", repr(captured_order))
            raise PaymentException(
                _("We were unable to process your payment. See below for details on how to proceed.")
            )

        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            logger.warning("PayPal success event even though order is already marked as paid")
            return

        try:
            payment.info = json.dumps(captured_order)
            payment.save(update_fields=["info"])
            payment.confirm()
        except Quota.QuotaExceededException as e:
            raise PaymentException(str(e)) from e
        except SendMailException:
            messages.warning(request, _("There was an error sending the confirmation mail."))
        return None

    def payment_pending_render(self, request, payment) -> str:
        retry = True
        with contextlib.suppress(KeyError):
            if (
                payment.info
                and safe_get(
                    payment.info_data.get("purchase_units", [{}])[0],
                    ["payments", "captures", "status"],
                )
                == "pending"
            ):
                retry = False
        template = get_template("plugins/paypal/pending.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "retry": retry,
            "order": payment.order,
        }
        return template.render(ctx)

    def matching_id(self, payment: OrderPayment):
        for capture in paypal_captures(payment.info_data):
            if capture.get("id"):
                return capture.get("id")
        return payment.info_data.get("id", None)

    def api_payment_details(self, payment: OrderPayment):
        order_id = self.matching_id(payment)
        return {
            "payer_email": safe_get(payment.info_data, ["payer", "payer_info", "email"]),
            "payer_id": safe_get(payment.info_data, ["payer", "payer_info", "payer_id"]),
            "cart_id": payment.info_data.get("cart", None),
            "payment_id": payment.info_data.get("id", None),
            "sale_id": order_id,
        }

    def payment_control_render(self, request: HttpRequest, payment: OrderPayment):
        template = get_template("plugins/paypal/control.html")
        order_id = self.matching_id(payment)
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": payment.info_data,
            "order": payment.order,
            "sale_id": order_id,
        }
        return template.render(ctx)

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        return safe_get(payment.info_data, ["payer", "payer_info", "email"], "")

    def payment_partial_refund_supported(self, payment: OrderPayment):
        # Paypal refunds are possible for 180 days after purchase:
        # https://www.paypal.com/lc/smarthelp/article/how-do-i-issue-a-refund-faq780#:~:text=Refund%20after%20180%20days%20of,PayPal%20balance%20of%20the%20recipient.
        if not payment.payment_date:
            return False
        return (now() - payment.payment_date).days <= 180

    def payment_refund_supported(self, payment: OrderPayment):
        return self.payment_partial_refund_supported(payment)

    def execute_refund(self, refund: OrderRefund):
        payment_info_data = refund.payment.info_data

        capture_id = next(
            (
                capture.get("id")
                for capture in paypal_captures(payment_info_data)
                if capture.get("status") in COMPLETED_CAPTURE_STATUSES and capture.get("id")
            ),
            None,
        )
        if not capture_id:
            raise PaymentException(_("An error occurred while communicating with PayPal, please try again."))
        refund_payment = self.paypal_request_handler.refund_payment(
            capture_id=capture_id,
            refund_data={
                "amount": {
                    "value": self.format_price(refund.amount),
                    "currency_code": refund.order.event.currency,
                }
            },
            merchant_id=self.event.settings.payment_paypal_merchant_id,
        )
        if errors := refund_payment.get("errors"):
            logger.error("execute_refund: %s", errors.get("reason", errors))
            refund.order.log_action(
                "eventyay.event.order.refund.failed",
                {
                    "local_id": refund.local_id,
                    "provider": refund.provider,
                    "error": str(errors),
                },
            )
            raise PaymentException(_("An error occurred while communicating with PayPal, please try again."))

        refund_payment_response = refund_payment.get("response") or {}
        refund_id = refund_payment_response.get("id")
        if not refund_id:
            raise PaymentException(_("An error occurred while communicating with PayPal, please try again."))
        refund.info = json.dumps(refund_payment_response)
        refund.save(update_fields=["info"])

        refund_detail = self.paypal_request_handler.get_refund_detail(
            refund_id=refund_id,
            merchant_id=self.event.settings.payment_paypal_merchant_id,
        )

        if errors := refund_detail.get("errors"):
            refund.order.log_action(
                "eventyay.event.order.refund.failed",
                {
                    "local_id": refund.local_id,
                    "provider": refund.provider,
                    "error": str(errors),
                },
            )
            raise PaymentException(_("An error occurred while communicating with PayPal, please try again."))

        refund_detail_response = refund_detail.get("response") or {}
        refund.info = json.dumps(refund_detail_response)
        refund.save(update_fields=["info"])

        refund_status = refund_detail_response.get("status")
        if refund_status == "COMPLETED":
            refund.done()
        elif refund_status == "PENDING":
            refund.state = OrderRefund.REFUND_STATE_TRANSIT
            refund.save(update_fields=["state"])
        else:
            refund_reason = safe_get(refund_detail_response, ["status_details", "reason"], "") or refund_status
            refund.order.log_action(
                "eventyay.event.order.refund.failed",
                {
                    "local_id": refund.local_id,
                    "provider": refund.provider,
                    "error": refund_reason,
                },
            )
            raise PaymentException(_("Refunding the amount via PayPal failed: {}").format(refund_reason))

    def shred_payment_info(self, obj: OrderPayment | OrderRefund):
        if obj.info_data:
            d = obj.info_data
            purchase_units = d.get("purchase_units", [])
            if purchase_units:
                purchase_units[0] = {"payments": purchase_units[0].get("payments", {})}
            new = {
                "id": d.get("id"),
                "payer": {"payer_info": {"email": "█"}},
                "update_time": d.get("update_time"),
                "purchase_units": purchase_units,
                "_shredded": True,
            }
            obj.info = json.dumps(new)
            obj.save(update_fields=["info"])

        for le in (
            obj.order.all_logentries().filter(action_type="eventyay.plugins.eventyay_paypal.event").exclude(data="")
        ):
            d = le.parsed_data
            if "resource" in d:
                d["resource"] = {
                    "id": d["resource"].get("id"),
                    "sale_id": d["resource"].get("sale_id"),
                    "parent_payment": d["resource"].get("parent_payment"),
                }
            le.data = json.dumps(d)
            le.shredded = True
            le.save(update_fields=["data", "shredded"])

    def render_invoice_text(self, order: Order, payment: OrderPayment) -> str:
        if order.status == Order.STATUS_PAID:
            payment_id = payment.info_data.get("id")
            if not payment_id:
                return super().render_invoice_text(order, payment)

            try:
                paypal_sale_id = payment.info_data["transactions"][0]["related_resources"][0]["sale"]["id"]
                return (
                    f"{_('The payment for this invoice has already been received.')}\r\n"
                    f"{_('PayPal payment ID')}: {payment_id}\r\n"
                    f"{_('PayPal sale ID')}: {paypal_sale_id}"
                )
            except (KeyError, IndexError):
                return (
                    f"{_('The payment for this invoice has already been received.')}\r\n"
                    f"{_('PayPal payment ID')}: {payment_id}"
                )
        return self.settings.get("_invoice_text", as_type=LazyI18nString, default="")
