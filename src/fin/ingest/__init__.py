# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Importers. Importing this package registers every parser with fin.ingest.inbox."""


def load_parsers() -> None:
    from fin.cards import offers  # noqa: F401  (registers the `offers` parsers)
    from fin.ingest import amazon, amazon_payments, bank_csv, costco, statements  # noqa: F401
    from fin.ingest.bank import register_bank_sites

    register_bank_sites()  # one per site in bank_accounts (docs/SPEC-checking.md §8)
