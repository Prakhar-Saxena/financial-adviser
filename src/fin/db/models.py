"""SQLite schema (SPEC §8). Alembic-managed; see migrations/.

Conventions: money is integer cents; positive = money out, negative = money in.
Dates are 'YYYY-MM-DD' strings; timestamps are UTC ISO-8601 strings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in(col: str, values: tuple[str, ...], name: str | None = None) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{col} IN ({allowed})", name=name or col)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


TXN_TYPES = (
    "purchase", "payment", "refund", "credit", "reward", "fee", "interest", "adjustment",
)
CATEGORY_SOURCES = ("rule", "ai", "user", "split")
REVIEW_KINDS = (
    "unmatched_charge", "unmatched_order_charge", "unexpected_merchant", "statement_mismatch",
    "parse_failure", "low_confidence", "uncategorized", "unknown_person", "sync_gap",
)
IMPORT_KINDS = ("transactions", "statement", "orders", "receipts", "emails")


# ------------------------------------------------------------- reference


class Person(Base):
    __tablename__ = "people"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str]
    full_name: Mapped[str]
    relationship: Mapped[str]


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    issuer: Mapped[str]
    product: Mapped[str]
    network: Mapped[str]
    card_type: Mapped[str]


class CardHolder(Base):
    __tablename__ = "card_holders"
    __table_args__ = (
        UniqueConstraint("card_id", "person_id"),
        _in("role", ("primary", "authorized_user")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    card_ending: Mapped[str | None]
    role: Mapped[str]


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))
    name: Mapped[str]


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    merchant_group: Mapped[str | None] = mapped_column(index=True)
    default_category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))


class Rule(Base):
    __tablename__ = "rules"
    __table_args__ = (
        _in("match_field", ("description_clean", "description_raw", "title_clean", "title_raw")),
        _in("created_by", ("seed", "user")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    priority: Mapped[int] = mapped_column(default=100)
    match_field: Mapped[str] = mapped_column(default="description_clean")
    pattern: Mapped[str]
    merchant_name: Mapped[str | None]
    merchant_group: Mapped[str | None]
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))
    txn_type: Mapped[str | None]
    created_by: Mapped[str]
    created_at: Mapped[str] = mapped_column(default=utcnow)


# ------------------------------------------------------------- ingestion


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        _in("mode", ("approve", "trusted")),
        _in("status", ("running", "ok", "partial", "failed", "aborted")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site: Mapped[str] = mapped_column(index=True)
    started_at: Mapped[str] = mapped_column(default=utcnow)
    ended_at: Mapped[str | None]
    since_date: Mapped[str | None]
    until_date: Mapped[str | None]
    run_dir: Mapped[str]
    model: Mapped[str]
    mode: Mapped[str]
    status: Mapped[str] = mapped_column(default="running")
    files_imported: Mapped[int] = mapped_column(default=0)
    guard_blocks: Mapped[int] = mapped_column(default=0)
    report_json: Mapped[str | None] = mapped_column(Text)


class Import(Base):
    __tablename__ = "imports"
    __table_args__ = (
        _in("kind", IMPORT_KINDS),
        _in("status", ("ok", "partial", "failed", "skipped")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"))
    source: Mapped[str]
    kind: Mapped[str]
    # The manifest kind (order_invoice, payment_transactions, ...): `fin rebuild` re-runs
    # the parser registered for (source, file_kind).
    file_kind: Mapped[str | None]
    card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id"))
    file_path: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    period_start: Mapped[str | None]
    period_end: Mapped[str | None]
    row_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str]
    error: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[str] = mapped_column(default=utcnow)


class Statement(Base):
    __tablename__ = "statements"
    __table_args__ = (
        UniqueConstraint("card_id", "period_end"),
        _in("parse_method", ("regex", "ai", "manual")),
        CheckConstraint(
            "ledger_status IS NULL OR ledger_status IN ('match', 'mismatch', 'incomplete')",
            name="ledger_status",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"))
    import_id: Mapped[int | None] = mapped_column(ForeignKey("imports.id"))
    period_start: Mapped[str]
    period_end: Mapped[str]
    due_date: Mapped[str | None]
    previous_balance: Mapped[int]
    payments: Mapped[int] = mapped_column(default=0)
    credits: Mapped[int] = mapped_column(default=0)
    purchases: Mapped[int] = mapped_column(default=0)
    cash_advances: Mapped[int] = mapped_column(default=0)
    balance_transfers: Mapped[int] = mapped_column(default=0)
    fees: Mapped[int] = mapped_column(default=0)
    interest: Mapped[int] = mapped_column(default=0)
    new_balance: Mapped[int]
    minimum_due: Mapped[int | None]
    pdf_path: Mapped[str]
    parse_method: Mapped[str]
    math_ok: Mapped[bool] = mapped_column(default=False)
    ledger_status: Mapped[str | None]
    ledger_delta_cents: Mapped[int | None]


class StatementLine(Base):
    """One activity line printed on a statement. Used to enrich and check CSV transactions
    (order numbers, cardholder sections), not as a second copy of the ledger."""

    __tablename__ = "statement_lines"
    __table_args__ = (
        Index("ix_statement_lines_match", "statement_id", "txn_date", "amount_cents"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"))
    line_no: Mapped[int]
    section: Mapped[str]  # e.g. "PAYMENTS AND OTHER CREDITS", "PURCHASE", "FEES CHARGED"
    txn_date: Mapped[str]
    description: Mapped[str]
    amount_cents: Mapped[int]  # SPEC §8 sign: positive = money out
    order_ref: Mapped[str | None]
    cardholder_name: Mapped[str | None]
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        _in("type", TXN_TYPES),
        CheckConstraint(
            "category_source IS NULL OR category_source IN ('rule', 'ai', 'user', 'split')",
            name="category_source",
        ),
        _in("review_status", ("ok", "needs_review", "reviewed")),
        Index("ix_transactions_card_post", "card_id", "post_date"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"))
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"))
    # name | statement | default_self | user  (how person_id was decided)
    person_source: Mapped[str | None]
    card_ending: Mapped[str | None]
    txn_date: Mapped[str | None]
    post_date: Mapped[str]
    amount_cents: Mapped[int]
    description_raw: Mapped[str]
    description_clean: Mapped[str]
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"))
    type: Mapped[str]
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))
    category_source: Mapped[str | None]
    external_id: Mapped[str | None]
    # csv = the issuer's activity export; statement = a statement line with no CSV row
    source: Mapped[str] = mapped_column(default="csv")
    # Merchant order number linked from the statement (Chase prints Amazon order numbers).
    order_ref: Mapped[str | None] = mapped_column(index=True)
    dedupe_key: Mapped[str] = mapped_column(String, unique=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("imports.id"))
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("statements.id"))
    review_status: Mapped[str] = mapped_column(default="ok")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("merchant", "external_id"),
        _in("channel", ("online", "warehouse", "digital")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant: Mapped[str]
    channel: Mapped[str]
    external_id: Mapped[str]
    import_id: Mapped[int | None] = mapped_column(ForeignKey("imports.id"))
    order_date: Mapped[str]
    location: Mapped[str | None]
    subtotal: Mapped[int | None]
    shipping: Mapped[int] = mapped_column(default=0)
    tax: Mapped[int] = mapped_column(default=0)
    discounts: Mapped[int] = mapped_column(default=0)
    gift_card_applied: Mapped[int] = mapped_column(default=0)
    total: Mapped[int]
    payment_method: Mapped[str | None]  # e.g. "Prime Visa", as the invoice shows it
    payment_card_ending: Mapped[str | None]
    raw_path: Mapped[str]


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    line_no: Mapped[int]
    sku: Mapped[str | None] = mapped_column(index=True)
    seller: Mapped[str | None]
    title_raw: Mapped[str]
    title_clean: Mapped[str | None]
    quantity: Mapped[int] = mapped_column(default=1)
    unit_price_cents: Mapped[int | None]
    line_total_cents: Mapped[int]
    discount_cents: Mapped[int] = mapped_column(default=0)
    taxable: Mapped[bool | None]
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))
    category_source: Mapped[str | None]


class OrderCharge(Base):
    """A charge or refund as the merchant lists it (Amazon: Your Payments → Transactions).

    Stands on its own: `order_ref` is the merchant's order number, and `order_id` links to
    the order once its invoice is imported (digital orders may never have one)."""

    __tablename__ = "order_charges"
    __table_args__ = (_in("kind", ("charge", "refund")),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant: Mapped[str] = mapped_column(default="amazon")
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    order_ref: Mapped[str | None] = mapped_column(index=True)
    import_id: Mapped[int | None] = mapped_column(ForeignKey("imports.id"))
    charge_date: Mapped[str]
    amount_cents: Mapped[int]  # charges positive, refunds negative
    card_name: Mapped[str | None]
    card_ending: Mapped[str | None]
    kind: Mapped[str]
    dedupe_key: Mapped[str | None] = mapped_column(String, unique=True)
    raw_text: Mapped[str | None]


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (_in("method", ("exact", "window", "ai", "user")),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True)
    order_charge_id: Mapped[int | None] = mapped_column(ForeignKey("order_charges.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    method: Mapped[str]
    confidence: Mapped[float] = mapped_column(Float)


class Allocation(Base):
    __tablename__ = "allocations"
    __table_args__ = (_in("source", ("order_items", "user")),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"))
    amount_cents: Mapped[int]
    source: Mapped[str]


# ---------------------------------------------------------- user + AI state


class UserOverride(Base):
    """Survives `fin rebuild`: keyed by stable keys, not row ids."""

    __tablename__ = "user_overrides"
    __table_args__ = (
        UniqueConstraint("target_type", "stable_key", "field"),
        _in("target_type", ("transaction", "order_item", "merchant")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str]
    stable_key: Mapped[str]
    field: Mapped[str]
    value: Mapped[str]
    created_at: Mapped[str] = mapped_column(default=utcnow)


class AICache(Base):
    __tablename__ = "ai_cache"
    task: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    prompt_version: Mapped[str] = mapped_column(String, primary_key=True)
    output_json: Mapped[str] = mapped_column(Text)
    model: Mapped[str]
    created_at: Mapped[str] = mapped_column(default=utcnow)


class AIRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (_in("status", ("ok", "partial", "failed", "limited")),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task: Mapped[str]
    run_dir: Mapped[str]
    n_items: Mapped[int]
    status: Mapped[str]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(default=utcnow)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        _in("kind", REVIEW_KINDS),
        _in("status", ("open", "resolved", "ignored")),
        Index("ix_review_items_ref", "kind", "ref_table", "ref_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str]
    ref_table: Mapped[str | None]
    ref_id: Mapped[str | None]
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(default="open")
    created_at: Mapped[str] = mapped_column(default=utcnow)
    resolved_at: Mapped[str | None]


class Email(Base):
    """Phase 4 (Gmail)."""

    __tablename__ = "emails"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_id: Mapped[str] = mapped_column(String, unique=True)
    from_addr: Mapped[str]
    subject: Mapped[str | None]
    sent_at: Mapped[str]
    merchant: Mapped[str | None]
    parsed_total_cents: Mapped[int | None]
    parsed_json: Mapped[str | None] = mapped_column(Text)
    raw_path: Mapped[str]
    linked_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
