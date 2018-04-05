from ._version import __version__
from ._impl import (
    async_generator,
    yield_,
    yield_from_,
    isasyncgen,
    isasyncgenfunction,
    get_asyncgen_hooks,
    set_asyncgen_hooks,
    supports_native_asyncgens,
)
from ._util import aclosing, asynccontextmanager
from functools import partial as _partial

async_generator_native = _partial(async_generator, allow_native=True)
async_generator_legacy = _partial(async_generator, allow_native=False)

__all__ = [
    "async_generator",
    "async_generator_native",
    "async_generator_legacy",
    "yield_",
    "yield_from_",
    "aclosing",
    "isasyncgen",
    "isasyncgenfunction",
    "asynccontextmanager",
    "get_asyncgen_hooks",
    "set_asyncgen_hooks",
    "supports_native_asyncgens",
]
