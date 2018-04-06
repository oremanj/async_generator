import sys
from functools import wraps, partial
from types import coroutine, CodeType, FunctionType
import inspect
from inspect import (
    getcoroutinestate, CORO_CREATED, CORO_CLOSED, CORO_SUSPENDED
)
import collections.abc

# An async generator object (whether native in 3.6+ or the pure-Python
# version implemented below) is basically an async function with some
# extra wrapping logic. As an async function, it can call other async
# functions, which will probably at some point call a function that uses
# 'yield' to send traps to the event loop. Async generators also need
# to be able to send values to the context in which the generator is
# being iterated, and it's awfully convenient to be able to do that
# using 'yield' too. To distinguish between these two streams of
# yielded objects, the traps intended for the event loop are yielded
# as-is, and the values intended for the context that's iterating the
# generator are wrapped in some wrapper object (YieldWrapper here, or
# an internal Python type called AsyncGenWrappedValue in the native
# async generator implementation) before being yielded.
# The __anext__(), asend(), and athrow() methods of an async generator
# iterate the underlying async function until a wrapped value is received,
# and any unwrapped values are passed through to the event loop.

# These functions are syntactically valid only on 3.6+, so we conditionally
# exec() the code defining them.
_native_asyncgen_helpers = """
async def _wrapper():
    holder = [None]
    while True:
        # The simpler "value = None; while True: value = yield value"
        # would hold a reference to the most recently wrapped value
        # after it has been yielded out (until the next value to wrap
        # comes in), so we use a one-element list instead.
        holder.append((yield holder.pop()))
_wrapper = _wrapper()

async def _unwrapper():
    @coroutine
    def inner():
        holder = [None]
        while True:
            holder.append((yield holder.pop()))
    await inner()
    yield None
_unwrapper = _unwrapper()
"""

if sys.implementation.name == "cpython" and sys.version_info >= (3, 6):
    # On 3.6, with native async generators, we want to use the same
    # wrapper type that native generators use. This lets @async_generator
    # create a native async generator under most circumstances, which
    # improves performance while still permitting 3.5-compatible syntax.
    # It also lets non-native @async_generators (e.g. those that return
    # non-None values) yield_from_ native ones and vice versa.

    import ctypes
    from types import AsyncGeneratorType, GeneratorType
    exec(_native_asyncgen_helpers)

    # Transmute _wrapper to a regular generator object by modifying the
    # ob_type field. The code object inside _wrapper will still think it's
    # associated with an async generator, so it will yield out
    # AsyncGenWrappedValues when it encounters a 'yield' statement;
    # but the generator object will think it's a normal non-async
    # generator, so it won't unwrap them. This way, we can obtain
    # AsyncGenWrappedValues as normal manipulable Python objects.
    #
    # This sort of object type transmutation is categorically a Sketchy
    # Thing To Do, because the functions associated with the new type
    # (including tp_dealloc and so forth) will be operating on a
    # structure whose in-memory layout matches that of the old type.
    # In this case, it's OK, because async generator objects are just
    # generator objects plus a few extra fields at the end; and these
    # fields are two integers and a NULL-until-first-iteration object
    # pointer ag_finalizer, so they don't hold any resources that need
    # to be cleaned up. (We transmute the async generator to a regular
    # generator before we first iterate it, so ag_finalizer stays NULL
    # for the lifetime of the object, and we never have an object we
    # need to remember to drop our reference to.) We have a unit test
    # that verifies that __sizeof__() for generators and async
    # generators continues to follow this pattern in future Python
    # versions.

    _type_p = ctypes.c_size_t.from_address(
        id(_wrapper) + ctypes.sizeof(ctypes.c_size_t)
    )
    assert _type_p.value == id(AsyncGeneratorType)
    _type_p.value = id(GeneratorType)

    supports_native_asyncgens = True

    # Now _wrapper.send(x) returns an AsyncGenWrappedValue of x.
    # We have to initially send(None) since the generator was just constructed;
    # we look at the type of the return value (which is AsyncGenWrappedValue(None))
    # to help with _is_wrapped.
    YieldWrapper = type(_wrapper.send(None))

    # Advance _unwrapper to its first yield statement, for use by _unwrap().
    _unwrapper.asend(None).send(None)

    # Performance note: compared to the non-native-supporting implementation below,
    # this _wrap() is about the same speed (434 +- 16 nsec here, 456 +- 24 nsec below)
    # but this _unwrap() is much slower (1.17 usec vs 167 nsec). Since _unwrap is only
    # needed on non-native generators, and we plan to have most @async_generators use
    # native generators on 3.6+, this seems acceptable.

    _wrap = _wrapper.send

    def _is_wrapped(box):
        return isinstance(box, YieldWrapper)

    def _unwrap(box):
        try:
            _unwrapper.asend(box).send(None)
        except StopIteration as e:
            return e.value
        else:
            raise TypeError("not wrapped")
else:
    supports_native_asyncgens = False

    class YieldWrapper:
        __slots__ = ("payload",)

        def __init__(self, payload):
            self.payload = payload

    def _wrap(value):
        return YieldWrapper(value)

    def _is_wrapped(box):
        return isinstance(box, YieldWrapper)

    def _unwrap(box):
        return box.payload


# The magic @coroutine decorator is how you write the bottom level of
# coroutine stacks -- 'async def' can only use 'await' = yield from; but
# eventually we must bottom out in a @coroutine that calls plain 'yield'.
@coroutine
def _yield_(value):
    return (yield _wrap(value))


# But we wrap the bare @coroutine version in an async def, because async def
# has the magic feature that users can get warnings messages if they forget to
# use 'await'.
async def yield_(value=None):
    return await _yield_(value)


async def yield_from_(delegate):
    # Transcribed with adaptations from:
    #
    #   https://www.python.org/dev/peps/pep-0380/#formal-semantics
    #
    # This takes advantage of a sneaky trick: if an @async_generator-wrapped
    # function calls another async function (like yield_from_), and that
    # second async function calls yield_, then because of the hack we use to
    # implement yield_, the yield_ will actually propagate through yield_from_
    # back to the @async_generator wrapper. So even though we're a regular
    # function, we can directly yield values out of the calling async
    # generator.
    def unpack_StopAsyncIteration(e):
        if e.args:
            return e.args[0]
        else:
            return None

    _i = type(delegate).__aiter__(delegate)
    if hasattr(_i, "__await__"):
        _i = await _i
    try:
        _y = await type(_i).__anext__(_i)
    except StopAsyncIteration as _e:
        _r = unpack_StopAsyncIteration(_e)
    else:
        while 1:
            try:
                _s = await yield_(_y)
            except GeneratorExit as _e:
                try:
                    _m = _i.aclose
                except AttributeError:
                    pass
                else:
                    await _m()
                raise _e
            except BaseException as _e:
                _x = sys.exc_info()
                try:
                    _m = _i.athrow
                except AttributeError:
                    raise _e
                else:
                    try:
                        _y = await _m(*_x)
                    except StopAsyncIteration as _e:
                        _r = unpack_StopAsyncIteration(_e)
                        break
            else:
                try:
                    if _s is None:
                        _y = await type(_i).__anext__(_i)
                    else:
                        _y = await _i.asend(_s)
                except StopAsyncIteration as _e:
                    _r = unpack_StopAsyncIteration(_e)
                    break
    return _r


# This is the awaitable / iterator that implements asynciter.__anext__() and
# friends.
#
# Note: we can be sloppy about the distinction between
#
#   type(self._it).__next__(self._it)
#
# and
#
#   self._it.__next__()
#
# because we happen to know that self._it is not a general iterator object,
# but specifically a coroutine iterator object where these are equivalent.
class ANextIter:
    def __init__(self, it, first_fn, *first_args):
        self._it = it
        self._first_fn = first_fn
        self._first_args = first_args

    def __await__(self):
        return self

    def __next__(self):
        if self._first_fn is not None:
            first_fn = self._first_fn
            first_args = self._first_args
            self._first_fn = self._first_args = None
            return self._invoke(first_fn, *first_args)
        else:
            return self._invoke(self._it.__next__)

    def send(self, value):
        return self._invoke(self._it.send, value)

    def throw(self, type, value=None, traceback=None):
        return self._invoke(self._it.throw, type, value, traceback)

    def _invoke(self, fn, *args):
        try:
            result = fn(*args)
        except StopIteration as e:
            # The underlying generator returned, so we should signal the end
            # of iteration.
            raise StopAsyncIteration(e.value)
        except StopAsyncIteration as e:
            # PEP 479 says: if a generator raises Stop(Async)Iteration, then
            # it should be wrapped into a RuntimeError. Python automatically
            # enforces this for StopIteration; for StopAsyncIteration we need
            # to it ourselves.
            raise RuntimeError(
                "async_generator raise StopAsyncIteration"
            ) from e
        if _is_wrapped(result):
            raise StopIteration(_unwrap(result))
        else:
            return result


UNSPECIFIED = object()
try:
    from sys import get_asyncgen_hooks, set_asyncgen_hooks

except ImportError:
    import threading

    asyncgen_hooks = collections.namedtuple(
        "asyncgen_hooks", ("firstiter", "finalizer")
    )

    class _hooks_storage(threading.local):
        def __init__(self):
            self.firstiter = None
            self.finalizer = None

    _hooks = _hooks_storage()

    def get_asyncgen_hooks():
        return asyncgen_hooks(
            firstiter=_hooks.firstiter, finalizer=_hooks.finalizer
        )

    def set_asyncgen_hooks(firstiter=UNSPECIFIED, finalizer=UNSPECIFIED):
        if firstiter is not UNSPECIFIED:
            if firstiter is None or callable(firstiter):
                _hooks.firstiter = firstiter
            else:
                raise TypeError(
                    "callable firstiter expected, got {}".format(
                        type(firstiter).__name__
                    )
                )

        if finalizer is not UNSPECIFIED:
            if finalizer is None or callable(finalizer):
                _hooks.finalizer = finalizer
            else:
                raise TypeError(
                    "callable finalizer expected, got {}".format(
                        type(finalizer).__name__
                    )
                )


class AsyncGenerator:
    def __init__(self, coroutine, *, warn_on_native_differences=False):
        self._coroutine = coroutine
        self._warn_on_native_differences = warn_on_native_differences
        self._it = coroutine.__await__()
        self._running = False
        self._finalizer = None
        self._closed = False
        self._hooks_inited = False

    # On python 3.5.0 and 3.5.1, __aiter__ must be awaitable.
    # Starting in 3.5.2, it should not be awaitable, and if it is, then it
    #   raises a PendingDeprecationWarning.
    # See:
    #   https://www.python.org/dev/peps/pep-0492/#api-design-and-implementation-revisions
    #   https://docs.python.org/3/reference/datamodel.html#async-iterators
    #   https://bugs.python.org/issue27243
    if sys.version_info < (3, 5, 2):

        async def __aiter__(self):
            return self
    else:

        def __aiter__(self):
            return self

    ################################################################
    # Introspection attributes
    ################################################################

    @property
    def ag_code(self):
        return self._coroutine.cr_code

    @property
    def ag_frame(self):
        return self._coroutine.cr_frame

    @property
    def ag_await(self):
        return self._coroutine.cr_await

    @property
    def ag_running(self):
        if self._running != self._coroutine.cr_running and self._warn_on_native_differences:
            import warnings
            warnings.warn(
                "Native async generators incorrectly set ag_running = False "
                "when the generator is awaiting a trap to the event loop and "
                "not suspended via a yield to its caller. Your code examines "
                "ag_running under such conditions, and will change behavior "
                "when async_generator starts using native generators by default "
                "(where available) in the next release. "
                "Use @async_generator_legacy to keep the current behavior, or "
                "@async_generator_native if you're OK with the change.",
                category=FutureWarning,
                stacklevel=2
            )
        return self._running

    ################################################################
    # Core functionality
    ################################################################

    # These need to return awaitables, rather than being async functions,
    # to match the native behavior where the firstiter hook is called
    # immediately on asend()/etc, even if the coroutine that asend()
    # produces isn't awaited for a bit.

    def __anext__(self):
        return self._do_it(self._it.__next__)

    def asend(self, value):
        return self._do_it(self._it.send, value)

    def athrow(self, type, value=None, traceback=None):
        return self._do_it(self._it.throw, type, value, traceback)

    def _do_it(self, start_fn, *args):
        if not self._hooks_inited:
            self._hooks_inited = True
            (firstiter, self._finalizer) = get_asyncgen_hooks()
            if firstiter is not None:
                firstiter(self)

        # On CPython 3.5.2 (but not 3.5.0), coroutines get cranky if you try
        # to iterate them after they're exhausted. Generators OTOH just raise
        # StopIteration. We want to convert the one into the other, so we need
        # to avoid iterating stopped coroutines.
        if getcoroutinestate(self._coroutine) is CORO_CLOSED:
            raise StopAsyncIteration()

        async def step():
            if self.ag_running:
                raise ValueError("async generator already executing")
            try:
                self._running = True
                return await ANextIter(self._it, start_fn, *args)
            finally:
                self._running = False

        return step()

    ################################################################
    # Cleanup
    ################################################################

    async def aclose(self):
        state = getcoroutinestate(self._coroutine)
        if state is CORO_CLOSED or self._closed:
            return
        # Make sure that even if we raise "async_generator ignored
        # GeneratorExit", and thus fail to exhaust the coroutine,
        # __del__ doesn't complain again.
        self._closed = True
        if state is CORO_CREATED:
            # Make sure that aclose() on an unstarted generator returns
            # successfully and prevents future iteration.
            self._it.close()
            return
        try:
            await self.athrow(GeneratorExit)
        except (GeneratorExit, StopAsyncIteration):
            pass
        else:
            raise RuntimeError("async_generator ignored GeneratorExit")

    def __del__(self):
        if getcoroutinestate(self._coroutine) is CORO_CREATED:
            # Never started, nothing to clean up, just suppress the "coroutine
            # never awaited" message.
            self._coroutine.close()
        if getcoroutinestate(self._coroutine
                             ) is CORO_SUSPENDED and not self._closed:
            if sys.implementation.name == "pypy":
                # pypy segfaults if we resume the coroutine from our __del__
                # and it executes any more 'await' statements, so we use the
                # old async_generator behavior of "don't even try to finalize
                # correctly". https://bitbucket.org/pypy/pypy/issues/2786/
                raise RuntimeError(
                    "partially-exhausted async_generator {!r} garbage collected"
                    .format(self.ag_code.co_name)
                )
            elif self._finalizer is not None:
                self._finalizer(self)
            else:
                # Mimic the behavior of native generators on GC with no finalizer:
                # throw in GeneratorExit, run for one turn, and complain if it didn't
                # finish.
                thrower = self.athrow(GeneratorExit)
                try:
                    thrower.send(None)
                except (GeneratorExit, StopAsyncIteration):
                    pass
                except StopIteration:
                    raise RuntimeError("async_generator ignored GeneratorExit")
                else:
                    raise RuntimeError(
                        "async_generator {!r} awaited during finalization; install "
                        "a finalization hook to support this, or wrap it in "
                        "'async with aclosing(...):'"
                        .format(self.ag_code.co_name)
                    )
                finally:
                    thrower.close()


if hasattr(collections.abc, "AsyncGenerator"):
    collections.abc.AsyncGenerator.register(AsyncGenerator)


def _find_return_of_not_none(co):
    """Inspect the code object *co* for the presence of return statements that
    might return a value other than None. If any such statement is found,
    return the source line number (in file ``co.co_filename``) on which it occurs.
    If all return statements definitely return the value None, return None.
    """

    # 'return X' for simple/constant X seems to always compile to
    # LOAD_CONST + RETURN_VALUE immediately following one another,
    # and LOAD_CONST(None) + RETURN_VALUE definitely does mean return None,
    # so we'll search for RETURN_VALUE not preceded by LOAD_CONST or preceded
    # by a LOAD_CONST that does not load None.
    import dis
    current_line = co.co_firstlineno
    prev_inst = None
    for inst in dis.Bytecode(co):
        if inst.starts_line is not None:
            current_line = inst.starts_line
        if inst.opname == "RETURN_VALUE" and (prev_inst is None or
                                              prev_inst.opname != "LOAD_CONST"
                                              or prev_inst.argval is not None):
            return current_line
        prev_inst = inst
    return None


def _as_native_asyncgen_function(afn):
    """Given a non-generator async function *afn*, which contains some ``await yield_(...)``
    and/or ``await yield_from_(...)`` calls, create the analogous async generator function.
    *afn* must not return values other than None; doing so is likely to crash the interpreter.
    Use :func:`_find_return_of_not_none` to check this.
    """

    # An async function that contains 'await yield_()' statements is a perfectly
    # cromulent async generator, except that it doesn't get marked with CO_ASYNC_GENERATOR
    # because it doesn't contain any 'yield' statements. Create a new code object that
    # does have CO_ASYNC_GENERATOR set. This is preferable to using a wrapper because
    # it makes ag_code and ag_frame work the same way they would for a native async
    # generator with 'yield' statements, and the same as for an async_generator
    # asyncgen with allow_native=False.

    from inspect import CO_COROUTINE, CO_ASYNC_GENERATOR
    co = afn.__code__
    new_code = CodeType(
        co.co_argcount, co.co_kwonlyargcount, co.co_nlocals, co.co_stacksize,
        (co.co_flags & ~CO_COROUTINE) | CO_ASYNC_GENERATOR, co.co_code,
        co.co_consts, co.co_names, co.co_varnames, co.co_filename, co.co_name,
        co.co_firstlineno, co.co_lnotab, co.co_freevars, co.co_cellvars
    )
    asyncgen_fn = FunctionType(
        new_code, afn.__globals__, afn.__name__, afn.__defaults__,
        afn.__closure__
    )
    asyncgen_fn.__kwdefaults__ = afn.__kwdefaults__
    return wraps(afn)(asyncgen_fn)


def async_generator(afn=None, *, allow_native=None, uses_return=None):
    if afn is None:
        return partial(
            async_generator,
            uses_return=uses_return,
            allow_native=allow_native
        )

    uses_wrapper = False
    if not inspect.iscoroutinefunction(afn):
        underlying = afn
        while hasattr(underlying, "__wrapped__"):
            underlying = getattr(underlying, "__wrapped__")
            if inspect.iscoroutinefunction(underlying):
                break
        else:
            raise TypeError(
                "expected an async function, not {!r}".format(
                    type(afn).__name__
                )
            )
        # A sync wrapper around an async function is fine, in the sense
        # that we can call it to get a coroutine just like we could for
        # an async function; but it's a bit suboptimal, in the sense that
        # we can't turn it into an async generator. One way to get here
        # is to put trio's @enable_ki_protection decorator below
        # @async_generator rather than above it.
        uses_wrapper = True

    if sys.implementation.name == "cpython" and not uses_wrapper:
        # 'return' statements with non-None arguments are syntactically forbidden when
        # compiling a true async generator, but the flags mutation in
        # _convert_to_native_asyncgen_function sidesteps that check, which could raise
        # an assertion in genobject.c when a non-None return is executed.
        # To avoid crashing the interpreter due to a user error, we need to examine the
        # code of the function we're converting.

        co = afn.__code__
        nontrivial_return_line = _find_return_of_not_none(co)
        seems_to_use_return = nontrivial_return_line is not None
        if uses_return is None:
            uses_return = seems_to_use_return
        elif uses_return != seems_to_use_return:
            prefix = "{} declared using @async_generator(uses_return={}) but ".format(
                afn.__qualname__, uses_return
            )
            if seems_to_use_return:
                raise RuntimeError(
                    prefix + "might return a value other than None at {}:{}".
                    format(co.co_filename, nontrivial_return_line)
                )
            else:
                raise RuntimeError(
                    prefix + "never returns a value other than None"
                )

        if allow_native and not uses_return and supports_native_asyncgens:
            return _as_native_asyncgen_function(afn)

    @wraps(afn)
    def async_generator_maker(*args, **kwargs):
        return AsyncGenerator(
            afn(*args, **kwargs),
            warn_on_native_differences=(allow_native is not False)
        )

    async_generator_maker._async_gen_function = id(async_generator_maker)
    return async_generator_maker


def isasyncgen(obj):
    if hasattr(inspect, "isasyncgen"):
        if inspect.isasyncgen(obj):
            return True
    return isinstance(obj, AsyncGenerator)


def isasyncgenfunction(obj):
    if hasattr(inspect, "isasyncgenfunction"):
        if inspect.isasyncgenfunction(obj):
            return True
    return getattr(obj, "_async_gen_function", -1) == id(obj)
