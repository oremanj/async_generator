import pytest
from functools import wraps, partial
import inspect
import types


@types.coroutine
def mock_sleep():
    yield "mock_sleep"


# Wrap any 'async def' tests so that they get automatically iterated.
# We used to use pytest-asyncio as a convenient way to do this, but nowadays
# pytest-asyncio uses us! In addition to it being generally bad for our test
# infrastructure to depend on the code-under-test, this totally messes up
# coverage info because depending on pytest's plugin load order, we might get
# imported before pytest-cov can be loaded and start gathering coverage.
@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        fn = pyfuncitem.obj

        @wraps(fn)
        def wrapper(**kwargs):
            coro = fn(**kwargs)
            try:
                while True:
                    value = coro.send(None)
                    if value != "mock_sleep":  # pragma: no cover
                        raise RuntimeError(
                            "coroutine runner confused: {!r}".format(value)
                        )
            except StopIteration:
                pass

        pyfuncitem.obj = wrapper


# On CPython 3.6+, tests that accept an 'async_generator' fixture
# will be called twice, once to test behavior with legacy pure-Python
# async generators (i.e. _impl.AsyncGenerator) and once to test behavior
# with wrapped native async generators. Tests should decorate their
# asyncgens with their local @async_generator, and may inspect
# async_generator.is_native to know which sort is being used.
# On CPython 3.5 or PyPy, where native async generators do not
# exist, async_generator will always call _impl.async_generator()
# and tests will be invoked only once.

from .. import supports_native_asyncgens
maybe_native = pytest.param(
    "native",
    marks=pytest.mark.skipif(
        not supports_native_asyncgens,
        reason="native async generators are not supported on this Python version"
    )
)


@pytest.fixture(params=["legacy", maybe_native])
def async_generator(request):
    from .. import async_generator as real_async_generator

    def wrapper(afn=None, *, uses_return=False):
        if afn is None:
            return partial(wrapper, uses_return=uses_return)
        if request.param == "native":
            return real_async_generator(afn, allow_native=True)
        # make sure the uses_return= argument matches what async_generator detects
        real_async_generator(afn, allow_native=False, uses_return=uses_return)
        # make sure autodetection without uses_return= works too
        return real_async_generator(afn, allow_native=False)

    wrapper.is_native = request.param == "native"
    return wrapper
