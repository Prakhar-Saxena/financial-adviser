# Contributing

Thanks for your interest. Please open an issue to discuss a change before sending code.

## Licence

This project is licensed under the [GNU Affero General Public License v3.0 or later](LICENSE)
(`AGPL-3.0-or-later`), and it will stay that way. There is no dual licensing and no
commercial-licence option.

- By submitting a contribution, you license it under `AGPL-3.0-or-later`.
- You keep the copyright in your contribution.
- No contributor licence agreement (CLA) or copyright assignment is required.

New source files start with this header, in the file's comment syntax:

```
Copyright (C) <year> <your name>
SPDX-License-Identifier: AGPL-3.0-or-later
```

When you make substantial changes to an existing file, you may add your own copyright line
below the existing one.

## Dependencies

Only add dependencies whose licences are compatible with AGPL-3.0-or-later and are free and
open source (for example MIT, BSD, Apache-2.0, ISC, PSF or MPL-2.0). Record each new dependency
and its licence in `docs/decisions.md`.

## Data

Never commit real financial data. Test fixtures in `tests/fixtures/` must be redacted
(`scripts/redact.py`). The pre-commit hook in `scripts/hooks/pre-commit` (installed by
`fin init`) blocks the most common mistakes.
