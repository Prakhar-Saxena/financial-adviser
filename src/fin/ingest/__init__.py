"""Importers. Importing this package registers every parser with fin.ingest.inbox."""


def load_parsers() -> None:
    from fin.ingest import amazon, amazon_payments, bank_csv, costco, statements  # noqa: F401
