"""Shared symtable name-binding check for the two architecture gates."""
import builtins
from pathlib import Path
import symtable


_MODULE_ATTRIBUTES = {
    "__annotations__", "__builtins__", "__cached__", "__doc__", "__file__",
    "__loader__", "__name__", "__package__", "__spec__",
}


def _walk(table):
    yield table
    for child in table.get_children():
        yield from _walk(child)


def undefined_globals(source, path):
    """Find referenced globals lacking a binding, not runtime assignment ordering.

    Lexical free names are already resolved by symtable (including __class__
    cells); an invalid nonlocal declaration causes symtable to raise SyntaxError.
    """
    table = symtable.symtable(source, str(path), "exec")
    scopes = [(scope, scope.get_symbols()) for scope in _walk(table)]
    bound = set(vars(builtins)) | _MODULE_ATTRIBUTES
    if Path(path).name == "__init__.py":
        bound.add("__path__")
    for _, symbols in scopes:
        bound.update(symbol.get_name() for symbol in symbols
                     if symbol.is_global() and (symbol.is_assigned() or symbol.is_imported()))
    return [
        f"{path}:{scope.get_lineno() or 1}: {scope.get_name()} references undefined global {symbol.get_name()!r}"
        for scope, symbols in scopes for symbol in symbols
        if symbol.is_global() and symbol.is_referenced() and symbol.get_name() not in bound
    ]
