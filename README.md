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
  When such a constant is a literal replaced by another literal, nothing read it while
  modules loaded, so a freshly imported patched program differs from the warm worker in that
  one attribute: the worker's forked child is simply given the new value (any class set-up
  it inherited is run again), which saves the import. An expression's `arg_types` table is
  handed over the same way when no instance of the class existed while modules loaded
  (`tools/trace_import_made.py`); the worker then works out `required_args` again for the
  class and the subclasses that inherit the table, as the class hook does.
  Reads that need no instance are indexed separately (`tools/build_reads.py`): a name read off
  a class object (`cls.NAME` in a classmethod, `exp.Select.NAME`) counts for every class that
  read can stand for; a name read while modules load to build something stored under another
  name is not narrowed at all; and what a metaclass or `__init_subclass__` stores from a name
  is followed to the readers of what it stored. A classmethod is taken to create an instance
  of its class only if the whole suite, run once at the pinned commit, never saw it do
  otherwise (`tools/check_instantiating.py`).
- **Executor tables**: the executor tests load the TPC tables from CSV in their class setup.
  The image build stores the loaded tables once (`tools/build_tables.py`), and DuckDB
  connections are opened only when a test first uses one. The build also stores the frame
  DuckDB returns for every SQL text those tests send it: over the same fixture files the same
  text always gets the same answer, so a test that sends an unchanged text is handed the stored
  frame, and any other text goes to DuckDB.
- **Swapped comparison operators** (`br/relations.py`): `a is b` and `a == b` give the same
  answer unless an object equals something it is not; `a < b` and `a <= b` unless the two are
  equal. While the maps are built, every such comparison in sqlglot is evaluated both ways each
  time it runs, and the tests in which the two ever disagree are recorded. A patch that only
  swaps such an operator for its partner can change nothing in any other test: every
  evaluation gives what it gave before, so the run is the same run.
- **Regrouped conditions** (`br/boolrel.py`): for every `and` / `or` condition the maps keep
  each distinct evaluation a test makes: which operands were evaluated and which were true
  (skipped operands are left alone, so the tracer runs nothing the program did not). A patch
  that only swaps `and` for `or` yields a second formula over the same operands; replaying the
  recorded evaluations through both shows in which tests it could ask for an operand the
  original skipped, skip a call the original made, or answer differently. An edit inside one
  operand can likewise only matter to the tests whose evaluations reached that operand. A test
  the tracer never saw evaluate the condition is not vouched for and stays a candidate.
- **The same, per fixture entry** (`tools/trace_rel_slices_one.py`): for the tests that loop
  over a fixture file, both recordings are also taken with one entry at a time. When a swapped
  operator or regrouped condition reaches such a test, only the entries in which it can behave
  differently are run, which is often cheap enough to settle the slowest tests outright.
  The class-usage recording (which classes are instantiated, which constants read, which dict
  entries looked up) is taken per entry as well, so an edited class constant, dict entry or
  default value also reaches only the entries that use it. Whatever the class set-up runs,
  reads or evaluates before the first entry counts for every entry.
- **Unchanged hand-over** (`br/memo.py`): the executor's TPC-DS test is by far the slowest test,
  and nearly all of its time goes into a pure-Python query engine and into comparing the result
  with DuckDB's. For a patch that changes none of the lines that half runs, it is a fixed
  function of what it is handed: the plan, the tables and the SQL text for DuckDB. The maps
  keep a fingerprint of those per fixture entry. Entries that hand over exactly what the
  pinned commit handed over must pass again and skip the slow half; entries that hand over
  something new are run for real while that stays cheap, and otherwise the test stays selected.
  Which lines the slow half runs is recorded twice: during the test itself, and again by
  replaying every hand-over in a process that never ran the quick half, so that nothing the
  quick half had already cached is missed. All quick halves run before the first result is
  looked at; if the entries that are handed something new would together cost more than an
  allowance, none of them is run and the test simply stays selected.
  The replay also notes, per entry, which lines its slow half runs and (run once more with
  the class-usage instrumentation) which classes it instantiates, constants it reads and dict
  entries it looks up. An entry whose slow half runs patched code, or uses a patched class
  constant or dict entry, is always run for real; the others can be recognised by their
  hand-over, so the shortcut also serves patches to class-level values that nothing read
  while modules loaded.
- **A slow fixture-driven test next to real failures**: when other tests already fail and the
  slowest such test is still unchecked, its cheapest entries are run, up to about a third of
  its cost and in a few short parts. On the labelled commits a failing run of that test fails
  on most of its entries, so a failure shows on the first part; if every sampled entry passes
  the test is let go. This is the one place where a test is released on a sample while other
  tests fail, and it is a measured trade: a sample is not the whole test.
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
- per comparison: the tests in which the operator and its partner disagree; per and/or
  condition: the evaluations each test makes,
- per entry of the executor's TPC-DS test: a fingerprint of what its slow half is handed, and
  the lines that half runs,
- per fixture entry coverage for fixture-driven tests,
- the lines that already run while the workers warm up, and each test's measured cost.

They are produced by `tools/trace_all.py` (once plain for coverage, once with
`BR_INSTRUMENT=1` for class and dict usage), `tools/trace_rel_all.py` (comparisons and
conditions, on source rewritten to report them), `tools/trace_slices_all.py`,
`tools/trace_import.py`, `tools/trace_memo.py`, `tools/calibrate.py` and `tools/build_reads.py`, and compiled by
`tools/build_maps.py` and `tools/build_relations.py`. Each test is traced in its own process so that caches warmed by one test
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
