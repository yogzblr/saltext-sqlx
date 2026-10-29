"""
Run SQL on PostgreSQL, MySQL/MariaDB, Oracle and SQL Server and return the real database error.

Stub created by T0. The public functions are added by T7.
"""

__virtualname__ = "sqlx"


def __virtual__():
    return __virtualname__


def ping(profile):  # pylint: disable=unused-argument
    """
    Placeholder so that ``sys.list_functions sqlx`` lists the module. Implemented by T7.

    CLI Example:

    .. code-block:: bash

        salt '*' sqlx.ping profile=orders_pg
    """
    raise NotImplementedError("sqlx.ping is implemented by task T7")
