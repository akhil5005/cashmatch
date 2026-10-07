"""Alembic environment.

The database URL comes from CashMatch's Settings rather than alembic.ini, so
the app and the migrations can never disagree about which database they mean.

It is handed to Alembic **directly**, never through
``config.set_main_option``. That matters more than it looks: alembic.ini is
parsed by ConfigParser, which treats ``%`` as interpolation syntax. A
URL-encoded password — which is to say any password containing a special
character, which is to say any password worth generating — contains ``%``
escapes, and ConfigParser rejects the whole string with
``invalid interpolation syntax``.

Found the hard way, on the first deploy against RDS with a 32-character
generated password. Routing around ConfigParser is the fix; escaping the
percent signs would also work and would break again the next time somebody
touched it.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import cashmatch.models  # noqa: F401  (populates Base.metadata for autogenerate)
from cashmatch.config import get_settings
from cashmatch.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    """The URL to migrate against.

    An explicit ``-x url=...`` wins, so a one-off migration can target
    somewhere other than the configured database without editing anything.
    """
    return context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # create_engine rather than engine_from_config: the latter reads the URL
    # back out of the ConfigParser section, which is exactly the path that
    # cannot carry a percent sign.
    connectable = create_engine(_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite cannot ALTER most things in place; batch mode rewrites
            # the table instead. Harmless on Postgres.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
