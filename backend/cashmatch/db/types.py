"""Column types that behave identically on PostgreSQL and SQLite.

Deployment runs on Postgres; the test suite runs on in-memory SQLite so that
``pytest`` needs no Docker and finishes in seconds. That only works if the
schema avoids Postgres-only types, which is what this module is for.
"""

from __future__ import annotations

from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, Enum
from sqlalchemy.dialects.postgresql import JSONB

# JSONB on Postgres (indexable, binary-packed), plain JSON on SQLite.
# Used for match explanations and audit payloads.
PortableJSON = JSON().with_variant(JSONB, "postgresql")

# Every money column in the schema. Paise, never rupees; integer, never float.
# BigInteger because 2^31 paise is only ~Rs.2.1 crore -- a single B2B invoice
# can exceed that.
MoneyColumn = BigInteger


def portable_enum(enum_cls: type[PyEnum], **kwargs: Any) -> Enum:
    """Build an enum column stored as VARCHAR + CHECK on both engines.

    ``native_enum=False`` sidesteps PostgreSQL's native ``CREATE TYPE`` enums,
    which SQLite has no equivalent for and which are awkward to extend in a
    migration (``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction).
    The values are stored as readable strings, which also makes raw SQL
    debugging far easier.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
        **kwargs,
    )
