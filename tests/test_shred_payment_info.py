import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from eventyay_paypal.payment import Paypal

LOG_ACTION_TYPE = "eventyay.plugins.eventyay_paypal.event"
BUYER_EMAIL = "buyer@example.com"


def make_info():
    return {
        "id": "ORDER-1",
        "status": "COMPLETED",
        "payer": {
            "name": {"given_name": "Asha", "surname": "Nair"},
            "payer_info": {"email": BUYER_EMAIL},
        },
        "update_time": "2026-11-01T10:00:00Z",
        "purchase_units": [
            {
                "reference_id": "default",
                "shipping": {"address": {"address_line_1": "1 Example Street"}},
                "payments": {"captures": [{"id": "CAPTURE-1", "status": "COMPLETED"}]},
            }
        ],
    }


def make_log_entry(parsed_data):
    return SimpleNamespace(parsed_data=parsed_data, data="", shredded=False, save=MagicMock())


def make_object(info_data, log_entries=()):
    order = MagicMock()
    order.all_logentries.return_value.filter.return_value.exclude.return_value = list(log_entries)
    return SimpleNamespace(
        info_data=info_data,
        info=json.dumps(info_data),
        order=order,
        save=MagicMock(),
    )


def shred(obj):
    Paypal.__new__(Paypal).shred_payment_info(obj)


def test_shred_keeps_only_operational_payment_fields():
    obj = make_object(make_info())

    shred(obj)

    result = json.loads(obj.info)
    assert set(result) == {"id", "payer", "update_time", "purchase_units", "_shredded"}
    assert result["id"] == "ORDER-1"
    assert result["update_time"] == "2026-11-01T10:00:00Z"
    assert result["purchase_units"] == [{"payments": {"captures": [{"id": "CAPTURE-1", "status": "COMPLETED"}]}}]
    assert result["_shredded"] is True


def test_shred_removes_payer_identity_and_shipping_data():
    obj = make_object(make_info())

    shred(obj)

    assert BUYER_EMAIL not in obj.info
    assert "Asha" not in obj.info
    assert "Nair" not in obj.info
    assert "1 Example Street" not in obj.info
    assert list(json.loads(obj.info)["payer"]) == ["payer_info"]


def test_shred_saves_only_the_info_field():
    obj = make_object(make_info())

    shred(obj)

    obj.save.assert_called_once_with(update_fields=["info"])


def test_shred_without_purchase_units_stores_empty_list():
    info = make_info()
    del info["purchase_units"]
    obj = make_object(info)

    shred(obj)

    assert json.loads(obj.info)["purchase_units"] == []


def test_shred_with_empty_info_leaves_info_untouched():
    obj = make_object({})

    shred(obj)

    assert obj.info == "{}"
    obj.save.assert_not_called()


def test_shred_queries_paypal_log_entries_with_data():
    obj = make_object(make_info())

    shred(obj)

    obj.order.all_logentries.return_value.filter.assert_called_once_with(action_type=LOG_ACTION_TYPE)
    obj.order.all_logentries.return_value.filter.return_value.exclude.assert_called_once_with(data="")


def test_shred_reduces_log_entry_resource_to_reference_ids():
    entry = make_log_entry(
        {
            "resource": {
                "id": "RESOURCE-1",
                "sale_id": "SALE-1",
                "parent_payment": "PARENT-1",
                "payer_email": BUYER_EMAIL,
                "amount": {"total": "10.00"},
            },
            "event_type": "PAYMENT.SALE.COMPLETED",
        }
    )
    obj = make_object(make_info(), [entry])

    shred(obj)

    stored = json.loads(entry.data)
    assert stored["resource"] == {"id": "RESOURCE-1", "sale_id": "SALE-1", "parent_payment": "PARENT-1"}
    assert stored["event_type"] == "PAYMENT.SALE.COMPLETED"
    assert BUYER_EMAIL not in entry.data
    assert entry.shredded is True
    entry.save.assert_called_once_with(update_fields=["data", "shredded"])


def test_shred_marks_log_entry_without_resource_as_shredded():
    entry = make_log_entry({"event_type": "PAYMENT.SALE.COMPLETED"})
    obj = make_object(make_info(), [entry])

    shred(obj)

    assert json.loads(entry.data) == {"event_type": "PAYMENT.SALE.COMPLETED"}
    assert entry.shredded is True
    entry.save.assert_called_once_with(update_fields=["data", "shredded"])


def test_shred_with_empty_info_still_shreds_log_entries():
    entry = make_log_entry({"resource": {"id": "RESOURCE-1", "payer_email": BUYER_EMAIL}})
    obj = make_object({}, [entry])

    shred(obj)

    obj.save.assert_not_called()
    assert BUYER_EMAIL not in entry.data
    assert entry.shredded is True
