# .github

The Nirmal Lab's organisation profile (`profile/`) and the CI that every
BioCognia repository shares: SCIMAP Pro (`scimappro`), Plexora (`plexora`),
`biocognia-platform`, `spatialagents`, `biocognia` and `spatialbridge`.

This repository is public on purpose. A public repository cannot call a
reusable workflow kept in a private one, while a private repository can call a
public one, so this is the one place all six can share. It holds no secrets.

## The three tiers

| Tier | When | Where | What | Wall clock |
|---|---|---|---|---|
| 1 Essential (`ci.yml`) | every push to main, every pull request, by hand | hosted Linux, Python 3.13 | generated-file checks, the tests the change needs ([selection](#test-selection)), the wheel installed in a clean environment | 1-5 min |
| 2 Weekly (`weekly.yml`, or `compat.yml` in the small repos) | Monday, and by hand | hosted Linux; the self-hosted Windows box and Mac | the full suite: Linux on 3.12 or 3.14 by turns, sharded; Windows and macOS on one Python a week in rotation; one compiled wheel; parity and the side suites | 15-30 min |
| 3 Full (`monthly.yml`, or `compat.yml`) | the 2nd of the month, and by hand before a big release | as Tier 2 | every combination of {Linux, Windows, macOS} x {3.12, 3.13, 3.14}, every wheel, benchmarks, cross-product conformance | 45-70 min |

Every Python/OS combination runs the full suite at least monthly. 3.13 runs on
every push; 3.12 and 3.14 on Linux every other week; each Python on Windows
and macOS every third week.

Plexora is public, so its hosted minutes are free. It runs Windows and macOS
on hosted runners and never on the self-hosted machines.

## Why it looks like this

In October 2026 the private repositories used up the organisation's 3,000
included Actions minutes, and every private job stopped starting
("recent account payments have failed or your spending limit needs to be
increased"). About 3,100 Linux-equivalent minutes went in two and a half
weeks:

- ~1,700 inside the `snapshots` backup repository. It mirrors every worktree as
  a branch, and spatialagents' test matrix ran on each one.
- ~650 on scimappro's full suite, three times per push, once per Python. It
  failed the same way each time.
- ~590 on spatialagents' six-job, three-OS matrix, on every push to every branch.

None of it came near a GitHub limit: no job ran longer than 77 minutes against
the 6-hour cap. The redesign keeps hosted use near 1,300 Linux-equivalent
minutes a month. If every job ran to its timeout, it would still stay under the
quota (about 2,600).

## Using the pieces

```yaml
# One job of a weekly or monthly run (checkout, uv, the three timeouts, a status artifact)
linux:
  strategy: { matrix: { shard: [1, 2, 3] } }
  uses: nirmallab/.github/.github/workflows/python-tests.yml@v1
  with:
    label: linux shard ${{ matrix.shard }}/3   # unique; shards end in "shard K/N"
    python: alternate                          # or 3.13, or rotate
    shard: ${{ matrix.shard }}/3
    durations-file: ci/durations.json
    durations-run-id: latest
    timeout-minutes: 20
    session-timeout-minutes: 15

# The verdict, in a last job
report:
  needs: [linux, ...]
  if: always()
  runs-on: ubuntu-latest
  timeout-minutes: 5
  steps:
    - uses: nirmallab/.github/actions/ci-report@v1
```

| Piece | What it does |
|---|---|
| `actions/setup-python-uv` | uv, one Python, `uv sync`. `python: alternate` or `rotate` picks this ISO week's turn. The uv cache is restored on hosted runners only. |
| `actions/pytest-run` | pytest under `scripts/run_pytest.py`; see [timeouts](#timeouts). Uploads `ci-status-<label>`, which holds `status.json`, `junit.xml` and `durations.json`. The step that fails is `Result`, after the upload. |
| `actions/select-tests` | Tier 1's change-aware selection, from `ci/selection.toml`. |
| `actions/build-smoke` | Builds the wheel, installs it in a fresh venv, imports it, runs `pip check` and a few commands. |
| `actions/ci-report` | The run's one summary and verdict. |
| `.github/workflows/python-tests.yml` | One pytest job built from the above, for matrices. A job that calls a reusable workflow cannot set its own `timeout-minutes` or `env`, so both are passed in as inputs. |
| `scripts/ci_pytest.py` | The pytest plugin behind sharding and duration records. |

Callers pin `@v1`. The `v1` tag moves only after `selftest.yml` passes and a
canary run in `biocognia` passes.

## Reading a report

The last job of every weekly and monthly run writes one table to the run's
summary page. Each job gets one class:

| Class | Meaning | Do |
|---|---|---|
| `success` | green | nothing |
| `test-failure` | tests failed; their node ids are listed | fix the test or the code |
| `session-timeout` | pytest stopped starting tests at its limit; the rest never ran | look at "slowest tests"; mark slow ones `slow`, or add a shard |
| `hard-timeout` | killed: by the runner script, or by the job's `timeout-minutes` | a hang; the JUnit file shows the last test that finished |
| `setup-failure` | failed before any test ran (install, build, checkout) | usually the lock or an upstream release |
| `failure` | a step that is not a test run failed | read the step |
| `cancelled` | cancelled after it started | rerun |
| `not-started` | never got a runner | billing; a self-hosted machine asleep for 24 hours; check Settings > Billing |
| `skipped` | not wanted this time | nothing |

Under the table, the report:

- checks that sharding lost nothing: what the shards kept adds up to what each
  collected, and what ran adds up to what they kept;
- lists the slowest tests;
- estimates the run's billed minutes, at list price relative to Linux: Windows
  x1.67, macOS x10.3, arm64 x0.83, self-hosted free.

Download any job's JUnit file from the run's artifacts (`ci-status-*`, kept 14
days).

## Running a tier by hand

```bash
gh workflow run ci.yml -f scope=full            # Tier 1 on the whole suite (scimappro, plexora)
gh workflow run weekly.yml                      # both halves
gh workflow run weekly.yml -f self_hosted=false # hosted half only (scimappro)
gh workflow run weekly.yml -f python=3.13       # one Python everywhere: the release gate
gh workflow run monthly.yml                     # scimappro: -f wheels=false -f bench=false to trim
gh workflow run compat.yml -f tier=monthly      # biocognia, spatialbridge, spatialagents
gh run watch                                    # then follow it
```

## Test selection

Tier 1 runs, for each push:

- the repository's fixed `essential` set;
- plus the test files that refer to what changed, from `ci/selection.toml`.

For a changed `pkg/sub/mod.py`, "refers to" means a test file that:

- says `pkg.sub.mod` or `alias.sub.mod`;
- says `from pkg.sub import ... mod` or `from pkg.sub.mod import`;
- is called `test_mod.py`.

The other rules:

- A changed private `_helper.py` is followed to the modules that import it.
- A changed test file runs itself.
- `[map]` covers what the text cannot show: a plugin directory, the client, the
  website.
- Mapped files are added cheapest first, until `budget_seconds` of recorded
  time is used.
- Files past the budget are listed as deferred and run in the weekly suite.
- A change too broad to map, such as the lock file, a conftest or a module no
  test names, also adds the `core` set.
- A change to nothing a test reads runs no tests.

Run it locally:

```bash
python ../.github/scripts/select_tests.py --base origin/main | xargs uv run pytest -m "not slow and not perf"
```

Markers:

- `slow`: over about 30 s on a 2-vCPU runner. Skipped by Tier 1, run weekly.
- `perf`: asserts wall-clock time. Runs only where its repository says, never
  as a gate.

## Timeouts

There are three, innermost first, so that a hang ends as a failed step with its
reports uploaded. A job GitHub cancels uploads nothing.

1. `pytest-timeout`: 600 s per test. This also holds on Windows, where the
   suites run under xdist so a killed worker is replaced.
2. `--session-timeout`: pytest stops starting tests and writes its JUnit file.
   On POSIX, `run_pytest.py` sends SIGINT two minutes later.
3. The hard limit: `run_pytest.py` kills the process tree three minutes before
   the job's `timeout-minutes`, counted from when `setup-python-uv` ran.

Every timeout is 1.5-2x the measured run time; the longest job limit is 60
minutes, against GitHub's 360.

## Sharding and durations

`--ci-shard K/N` assigns whole test files, longest first, to the least-loaded
shard, using `ci/durations.json`. The weekly report merges every Linux shard's
times into a `ci-durations` artifact, kept 90 days. Later runs prefer that
artifact over the committed file (`durations-run-id: latest`), so nothing
commits back.

A test the file does not know counts as the median. Stale durations unbalance
shards but never drop a test, and the report's shard check would show it if
one were dropped.

To refresh the committed seed, for example at release time: download the
newest `ci-durations` artifact and commit it as `ci/durations.json`.

## Self-hosted runners

The M2 Pro Mac (`self-hosted, macOS, ARM64`) and the Windows box
(`self-hosted, Windows, X64`) run the Windows and macOS legs of the private
repositories, at no Actions cost.

One-time setup, on each machine:

1. Organisation Settings > Actions > Runners > New self-hosted runner. Register
   at **organisation** level and keep it in the Default group.
2. Settings > Actions > Runner groups > Default:
   - **untick "Allow public repositories"**;
   - restrict the group to `scimappro`, `spatialagents`, `biocognia`,
     `spatialbridge` and `biocognia-platform`.

   A public repository's pull request must never run on these machines.
3. Install the runner as a service under your own account: `./svc.sh install && ./svc.sh start`
   on macOS, or the Windows service option in `config.cmd`.
4. Toolchains the jobs expect:
   - `uv`, plus `uv python install 3.12 3.13 3.14`;
   - Git for Windows: the jobs use `shell: bash`;
   - Node 22;
   - MSVC Build Tools or Xcode command-line tools;
   - Rust, for the desktop builds. The release workflow's Rust step sets the
     default toolchain to stable.

What a job can touch:

- Every test process gets config, data and cache directories under
  `$RUNNER_TEMP` (`run_pytest.py`): `XDG_*`, `APPDATA`, `LOCALAPPDATA`,
  `BIOCOGNIA_DIR`, `SCIMAPPRO_CONFIG_DIR`, `PLEXORA_DATA_PATH`, `NUMBA_CACHE_DIR`.
- Your real licence, telemetry queue and Plexora data root are out of reach.
- Each runner takes one job at a time, in its own `_work` directory.

The self-hosted halves are scheduled for late morning Boston time, when both
machines are awake. A job that finds no runner waits up to 24 hours. It is then
reported as `not-started`, never as a test failure. Rerun it with
`gh run rerun <id> --failed`.

## The budget

The org is on the Team plan: 3,000 included minutes a month for private
repositories, at list price. That is an $18.00 credit, at Linux $0.006/min,
Windows $0.010, macOS $0.062 and arm64 $0.005. The spending limit is $0, so
running out stops CI rather than charging.

| | Linux-equivalent minutes a month |
|---|---|
| scimappro (Tier 1 ~335, weekly ~360, monthly ~350) | ~1,045 |
| biocognia-platform | ~110 |
| spatialagents | ~110 |
| biocognia + spatialbridge | ~30 |
| **Total, steady state** | **~1,290 (43 % of the quota)** |
| Worst case: every job at its timeout | ~2,600 |

Check usage before the 20th of each billing cycle:

```bash
gh api /organizations/nirmallab/settings/billing/usage   # needs a token with admin:org
```

Also: the `snapshots` repository must keep Actions disabled (Settings > Actions >
Disable actions). Scheduled workflows in a public repository stop after 60 days
without a commit; re-enable them from the Actions tab.

## Releases

scimappro's and Plexora's release workflows build a tag only when a
`weekly.yml` or `monthly.yml` run has passed on the same commit.
`tools/release.py ci` (scimappro) and `scripts/release.py ci` (Plexora) run
the weekly suite on the commit and wait for it before pushing the tag.
`--no-gate` skips the wait; the release then fails its gate unless a run
already passed.

## Developing this repository

```bash
uv run --no-project --with pytest --with pytest-xdist --with pytest-timeout pytest tests
```

`selftest.yml` runs these tests on all three operating systems, runs the
actions against `fixture/`, and lints every workflow with actionlint. Move
`v1` only after it is green:

```bash
git tag -f v1 && git push -f origin v1
```
