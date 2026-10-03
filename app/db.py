from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL


pool = ConnectionPool(
    conninfo=DATABASE_URL,
    kwargs={"row_factory": dict_row},
    open=False,
)


@contextmanager
def get_connection():
    with pool.connection() as connection:
        yield connection
