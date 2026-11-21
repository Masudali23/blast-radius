# Blast Radius entry

For every patch against sqlglot `eb11aec`, pick the tests that fail and nothing else.

The selector does not guess from the shape of an edit. It works out which tests *can* be
affected, then **runs those tests against the patched code** inside a warm interpreter and
selects the ones that actually fail. Anything it could not check cheaply stays selected.

## How a commit is handled

1. **Read the patch** (`br/patch.py`): a small unified-diff applier, checked against `git apply`.
2. **Locate the change** (`br/analyze.py`): the old and new files are parsed and compared as
   syntax trees. The result is a list of changed statements, each either inside a function
   (runs at call time) or in a module/class body (runs at import time). Comment and formatting
   edits compare equal and need no tests, unless a test reads that file as data.
3. **Find candidates** (`br/plan.py`, `br/maps.py`): coverage maps built from the pinned commit
   say, for every source line, which tests execute it. Only tests that execute a changed
   statement (or import the changed module, or open the changed fixture) are candidates.
4. **Run them** (`br/worker.py`, `br/engine.py`): a worker process imports sqlglot and all test
   modules once. For each commit it forks; the child swaps in the new code objects of the
   changed functions and runs the candidate tests with `unittest`, in collection order.
   Module/class level changes are run from a fresh import instead. Fixture edits are served
   through a symlink overlay in `/tmp` (the image is read-only).
5. **Select** (`selector.py`): tests that fail are selected and packed onto 1–8 workers
   (longest first onto the lightest worker, trying every worker count).

## Keeping the CPU bill down

CPU time is part of the score, so the selector avoids running what it can decide otherwise:

- **Fixture slicing** (`br/variants.py`): many tests loop over the entries of a `.sql` fixture.
  Per-entry coverage lets the same test method run with only the entries a change can reach.
  For an edited fixture, only the entries that differ are run.
- **Scaled tests**: `test_max_nodes` and `test_generator_memory` are slow only because of a size
  constant. They run with the constant reduced; every assertion and code path is unchanged.
- **Old-versus-new evaluation** (`br/infect.py`): when a patch changes a single expression, the
  patched function also evaluates the old expression from the same operand values and counts
  disagreements. Tests in which the two never differ behave exactly as before.
- **Cost-based staging**: cheap candidates run first; a slower candidate is run only when that is
  expected to cost less than simply selecting it.

## Maps

`maps/` holds plain files derived from the pinned commit only:

- per source file: line → tests executing it (call time and import time separately),
- files each test opens, modules it imports, classes it instantiates,
- per fixture entry coverage for fixture-driven tests,
- the lines that already run while the worker warms up.

They are produced by `tools/trace_all.py`, `tools/trace_slices_all.py`, `tools/trace_import.py`
and compiled by `tools/build_maps.py`. Each test is traced in its own process so that caches
warmed by one test never hide lines from another.

There is no table from patches, or from kinds of edit, to failing tests anywhere in this
repository.

## Layout

```
Dockerfile        image the scorer builds
selector.py       entry point
br/               patch reader, change analysis, planner, worker, variants
maps/             coverage maps from the pinned commit
tools/            map builders (tracing, slicing, calibration)
tools/validation/ local-only checks: synthetic patches and a full-suite labeller used to
                  test the selector's decisions; nothing they produce is shipped
sample/           the kit's 30 sample commits and answers
score.py          the kit's scorer
```

## Local check

```sh
make sample
```
