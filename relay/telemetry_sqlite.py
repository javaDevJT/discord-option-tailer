"""Observe SQLite's connection shortcuts, which cursor-only instrumentation misses.

Parameters/iterators are passed through untouched. The official instrumentor
continues handling explicit cursor calls; these wrappers cover genuine execute,
executemany and executescript calls on the public connection API.
"""
import functools
import re
import sqlite3

import wrapt

from .telemetry import operation

VERBS = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP",
    "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN", "REPLACE", "WITH"}


class Connection(wrapt.ObjectProxy):
    def __enter__(self):
        self.__wrapped__.__enter__()
        return self

    def __exit__(self, *args):
        return self.__wrapped__.__exit__(*args)

    def _invoke(self, method, statement, *args, **kwargs):
        match = re.match(r"\s*([A-Za-z]+)", statement) if isinstance(statement, str) else None
        verb = match.group(1).upper() if match else "SQL"
        if verb not in VERBS:
            verb = "SQL"
        attributes = {"db.system": "sqlite", "db.system.name": "sqlite",
            "db.operation.name": verb, "db.operation.method": method}
        if isinstance(statement, str):
            attributes["db.query.text"] = statement
        with operation("sqlite." + verb, kind="client", attributes=attributes):
            return getattr(self.__wrapped__, method)(statement, *args, **kwargs)

    def execute(self, statement, *args, **kwargs):
        return self._invoke("execute", statement, *args, **kwargs)

    def executemany(self, statement, *args, **kwargs):
        return self._invoke("executemany", statement, *args, **kwargs)

    def executescript(self, statement, *args, **kwargs):
        return self._invoke("executescript", statement, *args, **kwargs)


def instrument():
    if getattr(sqlite3.connect, "_relay_shortcuts_instrumented", False):
        return
    original = sqlite3.connect

    @functools.wraps(original)
    def connect(*args, **kwargs):
        return Connection(original(*args, **kwargs))

    connect._relay_shortcuts_instrumented = True
    sqlite3.connect = connect
