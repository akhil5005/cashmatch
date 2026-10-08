"""The API, end to end over HTTP.

Two things carry the weight here. **Money never crosses the wire as a float**
-- every amount is an integer paise with a display string beside it, and a
test walks the whole payload asserting it. And **review actions move real
money**, so the tests that matter are the ones proving a human cannot
double-post, over-apply, or act on an item someone else already handled.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from cashmatch.api.app import create_app
from cashmatch.config import LLMMode, Settings
from cashmatch.db.base import Base
from cashmatch.db.session import get_db
from cashmatch.extraction import run_extraction
from cashmatch.generator import generate_dataset
from cashmatch.generator.config import VolumeConfig
from cashmatch.models import AuditLog, BankTransaction, CustomerAlias, Invoice, MatchResult
from cashmatch.models.enums import AliasSource, MatchDecision, TransactionStatus
from cashmatch.money import format_inr, rupees_to_paise
from cashmatch.scoring import run_decisions

from .support import load_config
from .test_generator import small_config

CONFIG = load_config()


def api_config():
    """A deliberately small dataset.

    The API does not care how deep the open-item book is, and rebuilding a
    150-payment dataset for each of eighty tests cost three and a half
    minutes for no extra coverage.
    """
    config = small_config()
    config.volume = VolumeConfig(customers=6, invoices=90, payments=25)
    return config


@pytest.fixture
def api(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, Session]]:
    """A fully populated API: generated, extracted, matched and decided."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from cashmatch.config import get_settings

    get_settings.cache_clear()

    # TestClient runs the app in a worker thread, and a SQLite connection
    # belongs to the thread that opened it. One shared connection keeps the
    # in-memory database visible to both.
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()

    generate_dataset(session, api_config(), tmp_path)
    session.flush()
    run_extraction(session, Settings(llm_mode=LLMMode.MOCK, data_dir=tmp_path), CONFIG)
    run_decisions(session, CONFIG)
    session.commit()

    app = create_app()

    def override():
        yield session

    app.dependency_overrides[get_db] = override
    yield TestClient(app), session

    session.close()
    engine.dispose()
    get_settings.cache_clear()


def _first(client: TestClient, decision: str) -> dict:
    page = client.get("/api/results", params={"decision": decision, "limit": 1}).json()
    assert page["items"], f"no {decision} items in the fixture"
    return page["items"][0]


# ===========================================================================
# money never crosses the wire as a float
# ===========================================================================


def _assert_money_shape(value: object, where: str) -> int:
    assert isinstance(value, dict), f"{where} is not a Money object"
    assert set(value) == {"paise", "display"}, f"{where} has unexpected keys"
    assert isinstance(value["paise"], int), f"{where}.paise is {type(value['paise'])}"
    assert isinstance(value["display"], str)
    return 1


def _walk_money(node: object, path: str = "$") -> int:
    """Count Money objects and assert every one of them is exact."""
    found = 0
    if isinstance(node, dict):
        if set(node) == {"paise", "display"}:
            return _assert_money_shape(node, path)
        for key, value in node.items():
            found += _walk_money(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _walk_money(value, f"{path}[{index}]")
    return found


def test_every_amount_in_a_result_is_integer_paise(api) -> None:
    """Rule 1 of the project, asserted at the API boundary. A JSON number
    for money invites a float on the other side, and a balance one paise
    wrong is impossible to explain to a customer."""
    client, _ = api
    item = _first(client, "auto_applied")
    detail = client.get(f"/api/results/{item['id']}").json()

    assert _walk_money(detail) >= 5


def test_every_amount_in_the_metrics_is_integer_paise(api) -> None:
    client, _ = api
    assert _walk_money(client.get("/api/metrics").json()) >= 4


def test_the_display_string_matches_the_paise(api) -> None:
    client, _ = api
    item = _first(client, "auto_applied")
    assert item["amount"]["display"] == format_inr(item["amount"]["paise"])
    assert "₹" in item["amount"]["display"]


# ===========================================================================
# metrics
# ===========================================================================


def test_metrics_reports_the_dashboard_figures(api) -> None:
    client, _ = api
    body = client.get("/api/metrics").json()

    assert body["transactions"] == api_config().volume.payments
    assert body["decided"] == body["transactions"]
    assert 0 <= body["auto_match_rate"] <= 1
    assert body["customers"] == api_config().volume.customers
    assert body["open_invoice_count"] > 0


def test_the_decision_counts_sum_to_the_total(api) -> None:
    client, _ = api
    body = client.get("/api/metrics").json()
    assert sum(row["count"] for row in body["by_decision"]) == body["decided"]


def test_precision_is_reported_only_when_an_answer_key_exists(api) -> None:
    """Production has no ground truth. Inventing a precision figure would be
    the single most misleading thing this API could do."""
    client, _ = api
    body = client.get("/api/metrics").json()

    assert body["precision"] is not None  # the demo dataset has one
    assert "answer key" in body["precision_basis"]


def test_precision_is_null_without_a_ground_truth_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "empty"))
    from cashmatch.config import get_settings

    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = create_app()

    def override():
        yield session

    app.dependency_overrides[get_db] = override

    body = TestClient(app).get("/api/metrics").json()
    assert body["precision"] is None
    assert body["precision_basis"] is None

    session.close()
    get_settings.cache_clear()


# ===========================================================================
# the review queue
# ===========================================================================


def test_results_are_paginated(api) -> None:
    client, _ = api
    page = client.get("/api/results", params={"limit": 10}).json()

    assert len(page["items"]) == 10
    assert page["total"] == api_config().volume.payments
    assert page["offset"] == 0


def test_offset_walks_the_queue_without_repeating(api) -> None:
    client, _ = api
    first = client.get("/api/results", params={"limit": 5, "offset": 0}).json()
    second = client.get("/api/results", params={"limit": 5, "offset": 5}).json()

    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


@pytest.mark.parametrize("decision", ["auto_applied", "needs_review", "unapplied"])
def test_filtering_by_decision(api, decision: str) -> None:
    client, _ = api
    page = client.get("/api/results", params={"decision": decision}).json()
    assert all(item["decision"] == decision for item in page["items"])


def test_filtering_by_confidence_band(api) -> None:
    client, _ = api
    page = client.get("/api/results", params={"min_confidence": 0.9, "max_confidence": 1.0}).json()
    assert all(0.9 <= item["confidence"] <= 1.0 for item in page["items"])


def test_searching_by_bank_reference(api) -> None:
    client, _ = api
    target = _first(client, "auto_applied")["statement_ref"]
    page = client.get("/api/results", params={"search": target}).json()

    assert page["total"] >= 1
    assert any(item["statement_ref"] == target for item in page["items"])


def test_filtering_for_untouched_items(api) -> None:
    client, _ = api
    page = client.get("/api/results", params={"reviewed": False, "limit": 5}).json()
    assert all(item["reviewed_at"] is None for item in page["items"])


def test_an_unknown_result_says_so_usefully(api) -> None:
    client, _ = api
    response = client.get("/api/results/999999")

    assert response.status_code == 404
    assert "999999" in response.json()["error"]["message"]


# ===========================================================================
# the explanation reaches the reviewer
# ===========================================================================


def test_the_detail_carries_the_whole_explanation(api) -> None:
    """A reviewer who cannot see why the engine proposed this has to redo
    the analysis, which erases the saving review was supposed to deliver."""
    client, _ = api
    item = _first(client, "auto_applied")
    detail = client.get(f"/api/results/{item['id']}").json()

    assert detail["signals"]
    assert detail["trail"]
    assert detail["allocations"]
    assert detail["reason_text"]
    assert detail["applicable_weight"] > 0
    assert all(signal["detail"] for signal in detail["signals"])


def test_signals_that_did_not_fire_are_still_shown(api) -> None:
    client, _ = api
    item = _first(client, "needs_review")
    detail = client.get(f"/api/results/{item['id']}").json()

    assert any(not signal["fired"] for signal in detail["signals"])


def test_the_allocation_shows_the_invoice_a_reviewer_needs(api) -> None:
    client, _ = api
    item = _first(client, "auto_applied")
    allocation = client.get(f"/api/results/{item['id']}").json()["allocations"][0]

    assert allocation["invoice_number"].startswith("INV-")
    assert allocation["customer_name"]
    assert allocation["due_date"]
    assert allocation["open_amount"]["paise"] > 0


# ===========================================================================
# review actions move real money
# ===========================================================================


def test_approving_posts_the_cash_and_stamps_the_reviewer(api) -> None:
    client, session = api
    item = _first(client, "needs_review")
    invoice_ids = [
        a["invoice_id"] for a in client.get(f"/api/results/{item['id']}").json()["allocations"]
    ]
    before = session.scalar(
        select(func.sum(Invoice.open_amount_paise)).where(Invoice.id.in_(invoice_ids))
    )

    body = client.post(f"/api/results/{item['id']}/approve", json={"reviewed_by": "akhil"}).json()

    assert body["cash_posted"] is True
    assert body["result"]["reviewed_by"] == "akhil"
    assert body["result"]["review_action"] == "approved"
    assert body["result"]["decision"] == "manually_applied"

    after = session.scalar(
        select(func.sum(Invoice.open_amount_paise)).where(Invoice.id.in_(invoice_ids))
    )
    assert after < before


def test_approving_in_suggest_only_mode_moves_no_money(api) -> None:
    client, session = api
    item = _first(client, "needs_review")
    before = session.scalar(select(func.sum(Invoice.open_amount_paise)))

    body = client.post(
        f"/api/results/{item['id']}/approve",
        json={"reviewed_by": "akhil", "post_cash": False},
    ).json()

    assert body["cash_posted"] is False
    assert session.scalar(select(func.sum(Invoice.open_amount_paise))) == before
    # The audit trail is written either way.
    assert body["result"]["reviewed_by"] == "akhil"


def test_rejecting_discards_the_suggestion_and_posts_nothing(api) -> None:
    client, session = api
    item = _first(client, "needs_review")
    before = session.scalar(select(func.sum(Invoice.open_amount_paise)))

    body = client.post(
        f"/api/results/{item['id']}/reject",
        json={"reviewed_by": "akhil", "note": "customer confirmed this is for another account"},
    ).json()

    assert body["cash_posted"] is False
    assert body["result"]["decision"] == "unapplied"
    assert body["result"]["allocations"] == []
    assert body["result"]["unapplied_amount"]["paise"] == body["result"]["amount"]["paise"]
    assert session.scalar(select(func.sum(Invoice.open_amount_paise))) == before


def test_reassigning_replaces_the_allocation_with_the_humans(api) -> None:
    client, session = api
    item = _first(client, "needs_review")
    invoice = session.scalars(
        select(Invoice).where(Invoice.open_amount_paise >= item["amount"]["paise"]).limit(1)
    ).first()
    assert invoice is not None

    body = client.post(
        f"/api/results/{item['id']}/reassign",
        json={
            "reviewed_by": "akhil",
            "allocations": [
                {
                    "invoice_id": invoice.id,
                    "allocated_amount": f"{item['amount']['paise'] / 100:.2f}",
                }
            ],
        },
    ).json()

    assert body["result"]["strategy"] == "manual"
    assert body["result"]["review_action"] == "reassigned"
    assert [a["invoice_id"] for a in body["result"]["allocations"]] == [invoice.id]


def test_an_item_cannot_be_reviewed_twice(api) -> None:
    """Two analysts working the same queue must not both post the same cash."""
    client, _ = api
    item = _first(client, "needs_review")

    first = client.post(f"/api/results/{item['id']}/approve", json={"reviewed_by": "akhil"})
    second = client.post(f"/api/results/{item['id']}/approve", json={"reviewed_by": "priya"})

    assert first.status_code == 200
    assert second.status_code == 422
    assert "already reviewed by akhil" in second.json()["error"]["message"]


def test_a_reassignment_cannot_apply_more_cash_than_arrived(api) -> None:
    """Two separate guards, and this one is about the payment rather than
    the invoice: an allocation that fits inside its invoice can still claim
    more cash than the bank actually sent."""
    client, session = api
    item = _first(client, "needs_review")
    payment_paise = item["amount"]["paise"]

    # An invoice with room for more than the whole payment, so the
    # per-invoice guard passes and the payment-total guard is what fires.
    invoice = session.scalars(
        select(Invoice)
        .where(Invoice.open_amount_paise > payment_paise)
        .order_by(Invoice.open_amount_paise.desc())
        .limit(1)
    ).first()
    assert invoice is not None

    response = client.post(
        f"/api/results/{item['id']}/reassign",
        json={
            "reviewed_by": "akhil",
            "allocations": [
                {
                    "invoice_id": invoice.id,
                    "allocated_amount": f"{invoice.open_amount_paise / 100:.2f}",
                }
            ],
        },
    )

    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert "only" in message and "was received" in message


def test_a_reassignment_cannot_overclear_an_invoice(api) -> None:
    client, session = api
    item = _first(client, "needs_review")
    small = session.scalars(select(Invoice).order_by(Invoice.open_amount_paise).limit(1)).first()
    too_much = f"{(small.open_amount_paise + 100_000) / 100:.2f}"

    response = client.post(
        f"/api/results/{item['id']}/reassign",
        json={
            "reviewed_by": "akhil",
            "allocations": [{"invoice_id": small.id, "allocated_amount": too_much}],
        },
    )

    assert response.status_code == 422
    assert "still open on it" in response.json()["error"]["message"]


def test_a_reassignment_to_an_unknown_invoice_is_refused(api) -> None:
    client, _ = api
    item = _first(client, "needs_review")

    response = client.post(
        f"/api/results/{item['id']}/reassign",
        json={
            "reviewed_by": "akhil",
            "allocations": [{"invoice_id": 999999, "allocated_amount": "100.00"}],
        },
    )

    assert response.status_code == 422
    assert "does not exist" in response.json()["error"]["message"]


def test_the_same_invoice_twice_in_one_reassignment_is_refused(api) -> None:
    client, session = api
    item = _first(client, "needs_review")
    invoice = session.scalars(select(Invoice).limit(1)).first()

    response = client.post(
        f"/api/results/{item['id']}/reassign",
        json={
            "reviewed_by": "akhil",
            "allocations": [
                {"invoice_id": invoice.id, "allocated_amount": "10.00"},
                {"invoice_id": invoice.id, "allocated_amount": "20.00"},
            ],
        },
    )
    assert response.status_code == 422


def test_an_unreadable_amount_is_refused_not_rounded(api) -> None:
    client, session = api
    item = _first(client, "needs_review")
    invoice = session.scalars(select(Invoice).limit(1)).first()

    response = client.post(
        f"/api/results/{item['id']}/reassign",
        json={
            "reviewed_by": "akhil",
            "allocations": [{"invoice_id": invoice.id, "allocated_amount": "about 500"}],
        },
    )
    assert response.status_code == 422


def test_every_review_action_is_audited(api) -> None:
    """A human decision that cannot be traced to a person is worse than no
    decision: it looks like the machine did it."""
    client, session = api
    item = _first(client, "needs_review")
    client.post(f"/api/results/{item['id']}/approve", json={"reviewed_by": "akhil"})

    entry = session.scalar(
        select(AuditLog).where(AuditLog.action == "review_approved").order_by(AuditLog.id.desc())
    )
    assert entry is not None
    assert entry.actor == "user:akhil"


# ===========================================================================
# the feedback loop designed into the schema in Phase 1
# ===========================================================================


def test_approving_teaches_the_system_an_unknown_payer_spelling(api) -> None:
    """The loop the alias table exists for: a human confirms a spelling the
    matcher could not resolve, and the next payment from it never reaches
    the queue."""
    client, session = api

    learned_before = session.scalar(
        select(func.count())
        .select_from(CustomerAlias)
        .where(CustomerAlias.source == AliasSource.LEARNED)
    )

    taught = None
    for item in client.get("/api/results", params={"decision": "needs_review"}).json()["items"]:
        body = client.post(
            f"/api/results/{item['id']}/approve", json={"reviewed_by": "akhil"}
        ).json()
        if body["alias_learned"]:
            taught = body["alias_learned"]
            break

    if taught is None:
        pytest.skip("no unrecognised payer spelling in this sample")

    learned_after = session.scalar(
        select(func.count())
        .select_from(CustomerAlias)
        .where(CustomerAlias.source == AliasSource.LEARNED)
    )
    assert learned_after == learned_before + 1


def test_a_payer_already_on_file_teaches_nothing(api) -> None:
    """No point recording a spelling the matcher already resolved exactly."""
    client, session = api
    exact = session.scalars(
        select(MatchResult)
        .join(BankTransaction)
        .where(MatchResult.decision == MatchDecision.NEEDS_REVIEW)
        .limit(5)
    ).all()

    for result in exact:
        body = client.post(
            f"/api/results/{result.id}/approve", json={"reviewed_by": "akhil"}
        ).json()
        if body["alias_learned"] is None:
            assert "Learned" not in body["message"]
            return
    pytest.skip("every sampled payer was unrecognised")


# ===========================================================================
# uploads
# ===========================================================================


CSV = """statement_ref,value_date,amount,payer_name,narration
UTR-UPLOAD-001,2026-07-01,41850.00,ABC Traders Pvt Ltd,NEFT INV-00042
UTR-UPLOAD-002,01/07/2026,"1,00,000.50",Konark Agencies,RTGS CREDIT
"""


def test_uploading_a_bank_statement_creates_payments(api) -> None:
    client, session = api
    response = client.post(
        "/api/uploads/bank-statement",
        files={"file": ("statement.csv", CSV.encode(), "text/csv")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert body["errors"] == []

    loaded = session.scalar(
        select(BankTransaction).where(BankTransaction.statement_ref == "UTR-UPLOAD-002")
    )
    assert loaded.amount_paise == rupees_to_paise("100000.50")
    assert loaded.status is TransactionStatus.UNMATCHED


def test_re_uploading_the_same_statement_is_idempotent(api) -> None:
    """The bank reference is unique precisely so this is safe."""
    client, _ = api
    files = {"file": ("statement.csv", CSV.encode(), "text/csv")}
    client.post("/api/uploads/bank-statement", files=files)
    body = client.post(
        "/api/uploads/bank-statement",
        files={"file": ("statement.csv", CSV.encode(), "text/csv")},
    ).json()

    assert body["created"] == 0
    assert body["skipped"] == 2


def test_alternative_column_names_are_accepted(api) -> None:
    """Banks and ERPs disagree about what to call a date."""
    client, _ = api
    alt = "utr,txn_date,credit,remitter,particulars\nUTR-ALT-1,2026-07-02,500.00,ABC,ref\n"
    body = client.post(
        "/api/uploads/bank-statement", files={"file": ("alt.csv", alt.encode(), "text/csv")}
    ).json()

    assert body["created"] == 1


def test_a_missing_required_column_says_which_one(api) -> None:
    client, _ = api
    response = client.post(
        "/api/uploads/bank-statement",
        files={"file": ("bad.csv", b"date,amount\n2026-07-01,100.00\n", "text/csv")},
    )

    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert "statement_ref" in message and "payer_name" in message


def test_a_bad_row_is_reported_without_losing_the_good_ones(api) -> None:
    """One malformed line should not cost someone their upload."""
    client, _ = api
    mixed = (
        "statement_ref,value_date,amount,payer_name\n"
        "UTR-GOOD-1,2026-07-01,500.00,ABC Traders\n"
        "UTR-BAD-1,2026-07-01,not a number,ABC Traders\n"
        "UTR-BAD-2,nonsense,500.00,ABC Traders\n"
    )
    body = client.post(
        "/api/uploads/bank-statement", files={"file": ("mixed.csv", mixed.encode(), "text/csv")}
    ).json()

    assert body["created"] == 1
    assert body["skipped"] == 2
    assert len(body["errors"]) == 2
    assert "Row 3" in body["errors"][0]


def test_an_empty_upload_is_refused(api) -> None:
    client, _ = api
    response = client.post(
        "/api/uploads/bank-statement", files={"file": ("empty.csv", b"", "text/csv")}
    )
    assert response.status_code == 422


def test_uploading_remittance_text_stores_it_unlinked(api) -> None:
    """Advice arrives on a different channel from the money. Pairing it is
    matching work, not an upload detail."""
    client, _ = api
    advice = b"Subject: payment\n\nWe have paid Rs. 41,850.00 against INV-00042."
    body = client.post(
        "/api/uploads/remittance", files={"file": ("advice.txt", advice, "text/plain")}
    ).json()

    assert body["created"] == 1
    assert "extraction" in body["message"]


# ===========================================================================
# pipeline
# ===========================================================================


def test_running_matching_from_the_api_records_decisions(api) -> None:
    client, _ = api
    body = client.post("/api/pipeline/match").json()

    assert body["transactions"] > 0
    assert sum(body["by_decision"].values()) == body["transactions"]
    assert body["posted"] is False
    assert "No cash was posted" in body["message"]


def test_running_extraction_from_the_api(api) -> None:
    client, _ = api
    body = client.post("/api/pipeline/extract", params={"force": True}).json()
    assert body["transactions"] > 0


# ===========================================================================
# reference data for the reassign picker
# ===========================================================================


def test_customers_carry_their_open_balance(api) -> None:
    client, _ = api
    customers = client.get("/api/customers", params={"limit": 5}).json()

    assert customers
    assert all(c["open_amount"]["paise"] >= 0 for c in customers)


def test_invoices_can_be_narrowed_to_one_customer(api) -> None:
    client, _ = api
    customer = client.get("/api/customers", params={"limit": 1}).json()[0]
    invoices = client.get("/api/invoices", params={"customer_id": customer["id"]}).json()

    assert all(inv["customer_id"] == customer["id"] for inv in invoices)
    assert all(inv["open_amount"]["paise"] > 0 for inv in invoices)


def test_only_open_invoices_are_offered_for_reassignment(api) -> None:
    """A paid invoice is not somewhere cash can go."""
    client, _ = api
    assert all(
        inv["open_amount"]["paise"] > 0
        for inv in client.get("/api/invoices", params={"limit": 50}).json()
    )


# ===========================================================================
# errors are uniform and useful
# ===========================================================================


def test_every_error_uses_the_same_envelope(api) -> None:
    client, _ = api
    for response in (
        client.get("/api/results/999999"),
        client.get("/api/results", params={"limit": 9999}),
    ):
        assert response.status_code >= 400
        assert set(response.json()["error"]) >= {"code", "message"}


def test_a_bad_query_parameter_explains_itself(api) -> None:
    client, _ = api
    response = client.get("/api/results", params={"min_confidence": 5})

    assert response.status_code == 422
    assert "min_confidence" in response.json()["error"]["message"]
