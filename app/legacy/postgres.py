"""PostgreSQL 连接。值统一来自 app.config.settings，这里不再自己读环境变量。

用法：from app.legacy.postgres import PG_CONNECTION_STRING
"""
from app.config import settings

PG_CONNECTION_STRING = settings.pg_dsn


def get_connection():
    """短连接，用完即关。给建表 / 一次性脚本用；常驻场景走连接池"""
    import psycopg

    return psycopg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password,
        dbname=settings.pg_database,
    )
