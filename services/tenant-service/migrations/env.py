import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from tenant_service.config import normalize_db_url
from tenant_service.models import Base

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)

url = normalize_db_url(os.environ["DATABASE_URL"])


def run_offline() -> None:
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_offline() if context.is_offline_mode() else run_online()
