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
4. **Run them** (`br/worker.py`, `br/engine.py`): warm worker processes fork one child per
   commit. For a change inside functions the child swaps in the new code objects and runs the
   candidate tests with `unittest`, in collection order. Module/class level changes are run from
   a fresh import instead. Fixture edits are served through a symlink overlay in `/tmp` (the
   image is read-only).
5. **Select** (`selector.py`): tests that fail are selected and packed onto 1–8 workers
   (longest first onto the lightest worker, trying every worker count).

Workers form a chain so nothing is imported twice: *bare* (third-party libraries) → *core*
(sqlglot without dialects) → *full* (all dialects, all test modules, the executor fixtures).
A change is checked from the warmest worker that has not already loaded the changed file.

## Keeping the CPU bill down

CPU time is part of the score, so the selector avoids running what it can decide otherwise:

- **Fixture slicing** (`br/variants.py`): many tests loop over the entries of a `.sql` fixture.
  Per-entry coverage lets the same test method run with only the entries a change can reach.
  For an edited fixture, only the entries that differ are run.
- **Scaled tests**: `test_max_nodes` and `test_generator_memory` are slow only because of a size
  constant. They run with the constant reduced; every assertion and code path is unchanged.
- **Class attributes**: a changed class-level constant can only matter to tests that read that
  name on an instance of the class (or a subclass). Candidates are narrowed with an index of
  where each name is read and a record of which classes each test instantiates. For an edited
  entry of a class-level dict, the tests that look that key up (or invoke the callable stored
  under it) are known too; entries read while classes are being built are never narrowed.
  Plain constants of the dialect, tokenizer, parser, generator and expression classes are
  followed more closely: while the maps are built each one sits behind a descriptor that
  records which tests read it from that very definition.
- **Default values**: a different literal default in a function signature can only be seen by a
  call of that function, so the candidates are the tests that enter it (unless something called
  it while modules were still loading).
- **Import check**: narrowing a module or class level edit assumes the module still imports.
  Unless the edit plainly cannot raise (a constant swapped inside a literal), the patched module
  is first imported on its own; if that fails, every test that imports it is selected.
- **Cost-based staging**: cheap candidates run first, up to the first failure. From then on the
  commit pays for a worker anyway, so a remaining candidate is run only when that is expected
  to cost less than the seconds it adds if simply left selected. When a change reaches so many
  tests that running them would cost more than selecting them, they are selected unseen.
- **Slow tests**: a test too slow to run in full is selected, unless everything else passes
  (or, when something fails, the tests closest to it — same test file or same fixture — all
  pass) and a sample of its own fixture entries passes too.

`br/infect.py` is a diagnostic (off by default): it evaluates the old and new versions of a
changed expression side by side while tests run.

## Maps

`maps/` holds plain files derived from the pinned commit only:

- per source file: line → tests executing it (call time and import time separately),
- files each test opens, modules it imports, classes it instantiates,
- class-level dict entries each test looks up or invokes, class-level constants each test reads,
- where each class/module level name is read,
- per fixture entry coverage for fixture-driven tests,
- the lines that already run while the workers warm up, and each test's measured cost.

They are produced by `tools/trace_all.py` (once plain for coverage, once with
`BR_INSTRUMENT=1` for class and dict usage), `tools/trace_slices_all.py`,
`tools/trace_import.py`, `tools/calibrate.py` and `tools/build_reads.py`, and compiled by
`tools/build_maps.py`. Each test is traced in its own process so that caches warmed by one test
never hide lines from another.

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
