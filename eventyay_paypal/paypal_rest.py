import base64
import hashlib
import json
import logging
import time
import urllib.parse
import uuid
from http import HTTPMethod

import requests
from cryptography.fernet import Fernet
from django.core.cache import cache

from .operational_log import logged_request
from .utils import (
    build_paypal_auth_assertion,
    paypal_error_reason,
    resolve_paypal_api_base,
    uses_paypal_connect,
)

logger = logging.getLogger(__name__)


class PaypalRequestHandler:
    def __init__(self, settings):
        self.settings = settings
        if uses_paypal_connect(settings):
            self.connect_client_id = self.settings.connect_client_id
            self.secret_key = self.settings.connect_secret_key
            endpoint_setting = self.settings.connect_endpoint
        else:
            self.connect_client_id = self.settings.get("client_id")
            self.secret_key = self.settings.get("secret")
            endpoint_setting = self.settings.get("endpoint") or "live"

        self.set_cache_token_key()
        # urljoin drops the final path segment unless the base ends with "/".
        self.endpoint = resolve_paypal_api_base(endpoint_setting).rstrip("/") + "/"

        self.oauth_url = urllib.parse.urljoin(self.endpoint, "v1/oauth2/token")
        self.partner_referrals_url = urllib.parse.urljoin(self.endpoint, "v2/customer/partner-referrals")
        self.order_url = urllib.parse.urljoin(self.endpoint, "v2/checkout/orders/{order_id}")
        self.create_order_url = urllib.parse.urljoin(self.endpoint, "v2/checkout/orders")
        self.capture_order_url = urllib.parse.urljoin(self.endpoint, "v2/checkout/orders/{order_id}/capture")
        self.refund_detail_url = urllib.parse.urljoin(self.endpoint, "v2/payments/refunds/{refund_id}")
        self.refund_payment_url = urllib.parse.urljoin(self.endpoint, "v2/payments/captures/{capture_id}/refund")
        self.verify_webhook_url = urllib.parse.urljoin(self.endpoint, "v1/notifications/verify-webhook-signature")
        self.merchant_integrations_url = urllib.parse.urljoin(
            self.endpoint, "v1/customer/partners/{partner_payer_id}/merchant-integrations/{merchant_id}"
        )
        self.merchant_integrations_search_url = urllib.parse.urljoin(
            self.endpoint, "v1/customer/partners/{partner_payer_id}/merchant-integrations"
        )

        self.paypal_request_id = self.get_paypal_request_id()

    def json_auth_headers(self, extra: dict | None = None) -> dict | None:
        token = self.get_access_token()
        if not token:
            return None
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if extra:
            headers.update({key: value for key, value in extra.items() if value})
        return headers

    def credentials_error(self) -> dict:
        return {
            "errors": {
                "type": "ConfigError",
                "reason": "PayPal API credentials are not configured",
                "exception": None,
            }
        }

    def authorized_request(self, url: str, method: HTTPMethod, data=None, extra_headers: dict | None = None) -> dict:
        headers = self.json_auth_headers(extra_headers)
        if headers is None:
            return self.credentials_error()
        return self.request(url=url, method=method, data=data, headers=headers)

    def request(
        self,
        url: str,
        method: HTTPMethod,
        data=None,
        params=None,
        headers=None,
        timeout=15,
    ) -> dict:
        reason = ""
        response_data = {}
        if method not in {HTTPMethod.GET, HTTPMethod.POST, HTTPMethod.PATCH}:
            return {
                "errors": {
                    "type": "UnsupportedMethod",
                    "reason": f"Unsupported HTTP method: {method}",
                    "exception": None,
                }
            }
        try:
            response = logged_request(
                "paypal",
                method.value,
                url,
                data=data,
                params=params,
                headers=headers,
                timeout=timeout,
            )

            reason = paypal_error_reason(response, fallback=response.reason)
            response.raise_for_status()

            if method == HTTPMethod.PATCH:
                return {"response": {}}

            if "application/json" not in response.headers.get("Content-Type", ""):
                response_data["errors"] = {
                    "type": "UnparseableResponse",
                    "reason": reason,
                    "exception": "Response is not json parseable",
                }
                return response_data

            response_data["response"] = response.json()
            return response_data
        except requests.exceptions.ReadTimeout as e:
            response_data["errors"] = {
                "type": "ReadTimeout",
                "reason": reason or "PayPal request timed out",
                "exception": e,
            }
            return response_data
        except requests.exceptions.RequestException as e:
            response_data["errors"] = {
                "type": "Ambiguous",
                "reason": reason or str(e),
                "exception": e,
            }
            return response_data

    @staticmethod
    def check_expired_token(access_token_data: dict, buffer_time: int = 300) -> bool:
        current_time = time.time()
        expiration_time = access_token_data["created_at"] + access_token_data["expires_in"]
        return (current_time + buffer_time) > expiration_time

    @staticmethod
    def encode_b64(connect_client_id: str, connect_secret_key: str) -> str:
        key = f"{connect_client_id}:{connect_secret_key}"
        return base64.b64encode(key.encode("ascii")).decode("ascii")

    def set_cache_token_key(self) -> None:
        if self.connect_client_id and self.secret_key:
            hash_code = hashlib.sha256("".join([self.connect_client_id, self.secret_key]).encode()).hexdigest()
            self.cache_token_key = f"paypal_token_hash_{hash_code}"
            self.fernet = Fernet(base64.urlsafe_b64encode(hash_code[:32].encode()))
        else:
            self.cache_token_key = None
            self.fernet = None

    def get_paypal_request_id(self):
        return str(uuid.uuid4())

    def get_paypal_auth_assertion(self, merchant_id: str) -> str:
        return build_paypal_auth_assertion(self.connect_client_id, merchant_id)

    def get_access_token(self) -> str | None:
        if not self.cache_token_key or not self.connect_client_id or not self.secret_key:
            logger.error("PayPal API credentials are not configured")
            return None

        def request_new_access_token() -> dict:
            access_token_response = self.request(
                url=self.oauth_url,
                method=HTTPMethod.POST,
                headers={
                    "Authorization": f"Basic {self.encode_b64(self.connect_client_id, self.secret_key)}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            )

            if errors := access_token_response.get("errors"):
                logger.error("Error getting access token from Paypal: %s", errors.get("reason", errors))
                return {}

            access_token_data = access_token_response.get("response") or {}
            if not access_token_data.get("access_token"):
                logger.error("PayPal token response did not include an access token")
                return {}
            access_token_data["created_at"] = time.time()
            encrypted_access_token_data = self.fernet.encrypt(json.dumps(access_token_data).encode())
            cache.set(self.cache_token_key, encrypted_access_token_data, 3600 * 2)
            return access_token_data

        encrypted_access_token_data = cache.get(self.cache_token_key)
        if encrypted_access_token_data is None:
            access_token_data = request_new_access_token()
        else:
            access_token_data = json.loads(self.fernet.decrypt(encrypted_access_token_data).decode())
            if self.check_expired_token(access_token_data):
                access_token_data = request_new_access_token()

        if not access_token_data:
            return None
        return access_token_data.get("access_token")

    def create_partner_referrals(self, data: dict) -> dict:
        return self.authorized_request(
            url=self.partner_referrals_url,
            method=HTTPMethod.POST,
            data=json.dumps(data),
        )

    def get_order(self, order_id: str) -> dict:
        return self.authorized_request(
            url=self.order_url.format(order_id=order_id),
            method=HTTPMethod.GET,
        )

    def create_order(self, order_data: dict) -> dict:
        return self.authorized_request(
            url=self.create_order_url,
            method=HTTPMethod.POST,
            extra_headers={"PayPal-Request-Id": self.paypal_request_id},
            data=json.dumps(order_data),
        )

    def capture_order(self, order_id: str) -> dict:
        return self.authorized_request(
            url=self.capture_order_url.format(order_id=order_id),
            method=HTTPMethod.POST,
            extra_headers={"PayPal-Request-Id": self.paypal_request_id},
        )

    def update_order(self, order_id: str, update_data: list[dict]) -> dict:
        return self.authorized_request(
            url=self.order_url.format(order_id=order_id),
            method=HTTPMethod.PATCH,
            data=json.dumps(update_data),
        )

    def get_refund_detail(self, refund_id: str, merchant_id: str) -> dict:
        return self.authorized_request(
            url=self.refund_detail_url.format(refund_id=refund_id),
            method=HTTPMethod.GET,
            extra_headers={"PayPal-Auth-Assertion": self.get_paypal_auth_assertion(merchant_id)},
        )

    def refund_payment(
        self,
        capture_id: str,
        refund_data: dict,
        merchant_id: str = None,
    ) -> dict:
        return self.authorized_request(
            url=self.refund_payment_url.format(capture_id=capture_id),
            method=HTTPMethod.POST,
            extra_headers={
                "PayPal-Auth-Assertion": self.get_paypal_auth_assertion(merchant_id),
                "PayPal-Request-Id": self.paypal_request_id,
            },
            data=json.dumps(refund_data),
        )

    def verify_webhook_signature(self, data: dict) -> dict:
        return self.authorized_request(
            url=self.verify_webhook_url,
            method=HTTPMethod.POST,
            data=json.dumps(data),
        )

    def get_merchant_integrations(self, partner_payer_id: str, merchant_id: str) -> dict:
        """Fetch merchant details (including email) from the PayPal Partner Referrals API.

        Requires the *platform's* PayPal payer ID (``partner_payer_id``), which is the
        merchant ID of the platform's own PayPal account — distinct from the OAuth
        client_id.  This is a one-time value visible in the PayPal Developer Dashboard.

        Returns a dict with ``response`` containing fields like ``email``, ``merchant_id``,
        ``tracking_id``, and ``status`` on success, or ``errors`` on failure.
        """
        if not partner_payer_id or not merchant_id:
            return {
                "errors": {
                    "type": "MissingParams",
                    "reason": "partner_payer_id and merchant_id are required",
                    "exception": None,
                }
            }
        return self.authorized_request(
            url=self.merchant_integrations_url.format(
                partner_payer_id=partner_payer_id,
                merchant_id=merchant_id,
            ),
            method=HTTPMethod.GET,
        )

    def find_merchant_integration(self, partner_payer_id: str, tracking_id: str) -> dict:
        """Look up an onboarding by the tracking ID sent with the partner referral.

        This is how a connection can be completed when PayPal never redirected the
        seller back to us, for example because the installation is not reachable
        under a public HTTPS address. PayPal answers with ``404`` while the seller
        has not finished onboarding.
        """
        if not partner_payer_id or not tracking_id:
            return {
                "errors": {
                    "type": "MissingParams",
                    "reason": "partner_payer_id and tracking_id are required",
                    "exception": None,
                }
            }
        query = urllib.parse.urlencode({"tracking_id": tracking_id})
        return self.authorized_request(
            url=f"{self.merchant_integrations_search_url.format(partner_payer_id=partner_payer_id)}?{query}",
            method=HTTPMethod.GET,
        )
