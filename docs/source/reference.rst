API documentation
=================

Async generators
----------------

In Python 3.6+, you can write a native async generator like this::

    async def load_json_lines(stream_reader):
        async for line in stream_reader:
            yield json.loads(line)

Here's the same thing written with this library, which works on Python 3.5+::

    from async_generator import async_generator, yield

    @async_generator
    async def load_json_lines(stream_reader):
        async for line in stream_reader:
            await yield_(json.loads(line))

Basically:

* decorate your function with ``@async_generator``
* replace ``yield`` with ``await yield_()``
* replace ``yield X`` with ``await yield_(X)``

That's it!


.. _yieldfrom:

Yield from
~~~~~~~~~~

Native async generators don't support ``yield from``::

    # Doesn't work!
    async def wrap_load_json_lines(stream_reader):
        # This is a SyntaxError
        yield from load_json_lines(stream_reader)

But we do::

    from async_generator import async_generator, yield_from_

    # This works!
    @async_generator
    async def wrap_load_json_lines(stream_reader):
        await yield_from_(load_json_lines(stream_reader))

You can use ``yield_from_`` inside a native async generator or an
``@async_generator`` function, and the argument can be any async
iterable. Remember that a native async generator is only created
when an ``async def`` block contains at least one ``yield``
statement; if you only ``yield_from_`` and never ``yield``,
use the ``@async_generator`` decorator.

Our ``yield_from_`` fully supports the classic ``yield from``
semantics, including forwarding ``asend`` and ``athrow`` calls into
the delegated async generator, and returning values::

    from async_generator import async_generator, yield_, yield_from_

    @async_generator
    async def agen1():
        await yield_(1)
        await yield_(2)
        return "great!"

    @async_generator
    async def agen2():
        value = await yield_from_(agen1())
        assert value == "great!"


Introspection
~~~~~~~~~~~~~

For introspection purposes, we also export the following functions:

.. function:: isasyncgen(agen_obj)

   Returns true if passed either an async generator object created by
   this library, or a native Python 3.6+ async generator object.
   Analogous to :func:`inspect.isasyncgen` in 3.6+.

.. function:: isasyncgenfunction(agen_func)

   Returns true if passed either an async generator function created
   by this library, or a native Python 3.6+ async generator function.
   Analogous to :func:`inspect.isasyncgenfunction` in 3.6+.

Example::

   >>> isasyncgenfunction(load_json_lines)
   True
   >>> gen_object = load_json_lines(asyncio_stream_reader)
   >>> isasyncgen(gen_object)
   True

In addition, this library's async generator objects are registered
with the ``collections.abc.AsyncGenerator`` abstract base class (if
available)::

   >>> isinstance(gen_object, collections.abc.AsyncGenerator)
   True


Semantics
~~~~~~~~~

This library generally tries hard to match the semantics of Python
3.6's native async generators in every detail (`PEP 525
<https://www.python.org/dev/peps/pep-0525/>`__), with additional
support for ``yield from`` and for returning non-None values from
an async generator (under the theory that these may well be added
to native async generators one day).

There are actually two implementations of the ``@async_generator``
decorator internally:

* ``@async_generator_legacy`` (or ``@async_generator(allow_native=False)``)
  always uses the pure-Python version that requires no interpreter
  support beyond basic async/await. It is rather slow for yield-bound
  workloads (see :ref:`perf`), but supports PyPy, CPython 3.5, and
  async generators that return non-None values.

* ``@async_generator_native`` (or ``@async_generator(allow_native=True)``)
  uses some ``co_flags`` trickery to create a *native* async generator
  where possible, with 3.5-compatible syntax but closer to 3.6-native
  performance (and some minor semantic differences, described below).
  Native async generators can only be created on CPython 3.6+ and only
  for functions that never return values other than None; if these
  conditions are not met, ``@async_generator_native`` silently behaves
  like ``@async_generator_legacy``.

Currently, ``@async_generator`` behaves like ``@async_generator_legacy``,
and also emits warnings if the resulting generator is used in a way
that would produce different results under ``@async_generator_native``.
The next release of this library will change the default to
``@async_generator_native``.

There are a couple of known differences between the semantics of the
two implementations:

* Native async generators suffer from a `bug <https://bugs.python.org/issue32526>`__
  where they will not be considered "running" if the generator has
  awaited some async function that is currently suspended in the
  event loop. Thus, you can start a second ``asend()`` call (or similar)
  before the first one completes, resulting in behavior that is extremely
  confusing at best. The legacy implementation does not have this quirk,
  and will throw an exception on any attempt to reenter the generator.

* Native async generators provide a rather unhelpful error message if
  they are garbage-collected with no :ref:`finalizer <finalization>` installed
  and some async operations to perform during stack unwinding: they say
  "async generator ignored GeneratorExit", the same as if the generator
  tried to ``yield`` in a ``finally:`` block. The legacy implementation
  provides a clearer error that describes the possible remedies.

Both types of ``@async_generator`` behave the same with respect to
:ref:`finalization semantics <finalization>`, :ref:`yield_from interoperability
with native generators <yieldfrom>`, and so on; you don't need to use
``@async_generator_native`` for those if you're on 3.6.

The value ``async_generator.supports_native_asyncgens`` will be True
if we're running on a platform where the above-described tricks are
possible.

.. note::
   Producing a native async generator requires this library to determine
   whether an async function being decorated with ``@async_generator`` might
   return something other than None. (Returns with an argument are
   syntactically forbidden in native async generators, and if through
   trickery we were to manage to execute one, the interpreter would probably
   crash.) Normally the presence of such returns is autodetected through
   some fairly straightforward bytecode inspection, but it's possible to
   verify that the check is behaving as you expect: if you decorate a
   function with ``@async_generator(uses_return=True)`` and it doesn't
   appear to have non-None returns, or with
   ``@async_generator(uses_return=False)`` and it does, the decorator
   will throw a RuntimeError.


.. _perf:

Performance
~~~~~~~~~~~

`PEP 525 <https://www.python.org/dev/peps/pep-0525/#improvements-over-asynchronous-iterators>`__
includes a microbenchmark demonstrating that a yield-bound async
generator (that yields out ten million integers without awaiting
anything) runs 2.3x faster than the equivalent async iterator.
The pure-Python version of ``@async_generator`` cannot claim such
improvements; it's about 10x *slower* than the async iterator on
that workload. On the other hand, the version that produces a native
async generator with ``await yield_(...)`` calls is only about 1.7x
slower than the async iterator (4.3x slower than a backwards-incompatible
fully native async generator); the overhead appears to be entirely
due to the two extra async function calls that must be executed per
``yield_`` compared to a native ``yield``.

When considering these numbers, remember that most async generators
are not yield-bound; if they didn't have to wait for something to
happen in the event loop, they probably wouldn't be async! (Pure
Python ``@async_generator`` functions do add a bit of overhead per
event loop trap as well, but native ones do not.)


.. _finalization:

Garbage collection hooks
~~~~~~~~~~~~~~~~~~~~~~~~

This library fully supports the native async generator
`finalization semantics <https://www.python.org/dev/peps/pep-0525/#finalization>`__,
including the per-thread ``firstiter`` and ``finalizer`` hooks.
You can use ``async_generator.set_asyncgen_hooks()`` exactly
like you would use ``sys.set_asyncgen_hooks()`` with native
generators. On Python 3.6+, the former is an alias for the latter,
so libraries that use the native mechanism should work seamlessly
with ``@async_generator`` functions. On Python 3.5, where there is
no ``sys.set_asyncgen_hooks()``, most libraries probably *won't* know
about ``async_generator.set_asyncgen_hooks()``, so you'll need
to exercise more care with explicit cleanup, or install appropriate
hooks yourself.

While finishing cleanup of an async generator is better than dropping
it on the floor at the first ``await``, it's still not a perfect solution;
in addition to the unpredictability of GC timing, the ``finalizer`` hook
has no practical way to determine the context in which the generator was
being iterated, so an exception thrown from the generator during ``aclose()``
must either crash the program or get discarded. It's much better to close
your generators explicitly when you're done with them, perhaps using the
:ref:`aclosing context manager <contextmanagers>`. See `this discussion
<https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#cleanup-in-generators-and-async-generators>`__
and `PEP 533 <https://www.python.org/dev/peps/pep-0533/>`__ for more
details.


.. _contextmanagers:

Context managers
----------------

As discussed above, you should always explicitly call ``aclose`` on
async generators. To make this more convenient, this library also
includes an ``aclosing`` async context manager. It acts just like the
``closing`` context manager included in the stdlib ``contextlib``
module, but does ``await obj.aclose()`` instead of
``obj.close()``. Use it like this::

   from async_generator import aclosing

   async with aclosing(load_json_lines(asyncio_stream_reader)) as agen:
       async for json_obj in agen:
           ...

Or if you want to write your own async context managers, we've got you
covered:

.. function:: asynccontextmanager
   :decorator:

   This is a backport of :func:`contextlib.asynccontextmanager`, which
   wasn't added to the standard library until Python 3.7.

You can use ``@asynccontextmanager`` with either native async
generators, or the ones from this package. If you use it with the ones
from this package, remember that ``@asynccontextmanager`` goes *on
top* of ``@async_generator``::

   # Correct!
   @asynccontextmanager
   @async_generator
   async def my_async_context_manager():
       ...

   # This won't work :-(
   @async_generator
   @asynccontextmanager
   async def my_async_context_manager():
       ...
