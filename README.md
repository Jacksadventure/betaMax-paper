# betaMax

Benchmark runners and experiment scripts for betaMax / eRepair-style repair experiments.

**betaMax** is a structured-data repair project for real-world settings where large volumes of data lack explicit format specifications such as regular expressions or grammar files. Unlike existing approaches that depend on format specifications or precise parser error messages, betaMax uses only a set of valid data samples and a black-box validator that can decide whether an input is valid. It automatically infers data formats, builds approximate grammars, and iteratively optimizes repair candidates to fix corrupted data. Experiments on real formats including dates, times, ISBNs, IP addresses, and URLs show that betaMax repairs **86.9%** of corrupted records, achieving a **1.44x** higher repair success rate than the state-of-the-art **epsilonRepair** system while reducing validator calls during repair by **4.6x**. This makes betaMax an efficient and general repair approach for practical data cleaning and data recovery scenarios where no formal format specification is available.

## What This Checkout Supports

This repository snapshot is organized around the **C++ betaMax backend** in [betamax_cpp](./betamax_cpp).

The easiest path after cloning is:

1. Build the Docker image from [Dockerfile](./Dockerfile).
2. Run a small `date` smoke test with a mounted result directory.
3. Run the full regex benchmark suite after the smoke test succeeds.

What works cleanly in this checkout:

- Main regex benchmarks: `date`, `time`, `isbn`, `ipv4`, `ipv6`, `url`

This README focuses on the regex benchmark workflow.

Important:

- This checkout supports the bundled C++ betaMax backend only.

## Docker Quick Start

Docker is the recommended way to run this checkout because it packages the native compiler, CMake, RE2, Python runtime, C++ betaMax backend, and validators into one reproducible image.

Use the published Docker Hub image:

```bash
docker pull idk233333/betamax:latest
docker tag idk233333/betamax:latest betamax:latest
```

The published image includes the bundled C++ backend, validators, and precomputed DFA caches under `cache/`.

Alternatively, build the image locally from this checkout:

```bash
docker build -t betamax:latest .
```

Run a small `date` smoke test first and keep the output database on the host:

```bash
mkdir -p docker-results
docker run --rm \
  -v "$PWD/docker-results:/results" \
  betamax:latest quick --formats date --db /results/smoke_date.db
```

This should write:

- `docker-results/smoke_date.db`

Quick mode creates a fresh smoke-test database by default, so this output database contains only the small set of smoke-test cases selected by `--limit`.

Print the smoke-test result summary:

```bash
python report_docker.py --db docker-results/smoke_date.db
```

After the smoke test succeeds, run the full regex benchmark suite:

```bash
docker run --rm \
  -v "$PWD/docker-results:/results" \
  betamax:latest regex
```

The full suite runs:

```bash
./run_bms.sh regex
```

and writes:

- `docker-results/betamax_single.db`
- `docker-results/betamax_double.db`
- `docker-results/betamax_triple.db`

Print the Docker benchmark result summary:

```bash
python report_docker.py
```

During full benchmark runs, a small number of per-case repair timeouts can be normal. A timeout means that the case was not repaired successfully within the configured time budget; it is recorded as an unfinished/unsuccessful repair in the result database rather than as an infrastructure failure.

To run the default smoke test directly:

```bash
docker run --rm \
  -v "$PWD/docker-results:/results" \
  betamax:latest quick --db /results/smoke_single.db
```

Pass any [run_bms.sh](./run_bms.sh) mode and arguments after the image name:

```bash
docker run --rm -v "$PWD/docker-results:/results" betamax:latest single --formats date url --limit 10
docker run --rm -v "$PWD/docker-results:/results" -e DB_PREFIX=/results/trial1 betamax:latest regex
```

The Docker image packages the main regex benchmark workflow. Subject/truncation workflows still require their additional subject artifacts to be built separately.

Export the image for offline distribution if needed:

```bash
docker save betamax:latest | gzip > betamax_latest.tar.gz
docker load < betamax_latest.tar.gz
```

The image entrypoint is [run_bms.sh](./run_bms.sh). To run DDMax instead, override the entrypoint:

```bash
docker run --rm --entrypoint ./run_ddmax.sh betamax:latest regex
```

The Docker build installs the native system dependencies, installs [requirements.txt](./requirements.txt), builds [betamax_cpp](./betamax_cpp), and builds the RE2 validators under [validators](./validators).

## Local Dependencies

You only need this section if you are not using Docker.

For the main regex benchmark workflow (`run_bms.sh quick|single|double|triple|regex`), the required system dependencies are:

- Python 3.10+
- `pip` and `venv` for the recommended isolated environment setup
- CMake 3.16+ to build the bundled C++ backend in [betamax_cpp](./betamax_cpp)
- A C++17 compiler such as `clang++`
- RE2 C++ headers and libraries (`re2/re2.h`, `libre2`), because the native validators under [validators](./validators) are compiled and linked against RE2 and then used as the regex oracles during benchmark runs
- The pre-generated mutation databases under [mutated_files](./mutated_files), which the benchmark runners expect to exist

The Python packages in [requirements.txt](./requirements.txt) are split by purpose:

- `regex`: required by the Python regex helper oracles in [match.py](./match.py) and [match_partial.py](./match_partial.py)
- `matplotlib`: only needed for reporting via [report.py](./report.py)
- `requests`: only needed for corpus/data download via [data_fetch.py](./data_fetch.py)

Optional or workflow-specific dependencies:

- `make`: only needed if you want the shortcuts in [Makefile](./Makefile)
- `pkg-config`: optional, but helps [validators/build_validators.sh](./validators/build_validators.sh) locate RE2 cleanly

The standard regex quickstart in this checkout does **not** require Java, Gradle, or the optional subject-build toolchain.

For `./run_bms.sh quick`, the launcher now tries to bootstrap missing native dependencies automatically:

- missing `clang++`:
  on macOS it prefers `brew install llvm`; if Homebrew is unavailable but `xcode-select` exists, it requests Xcode Command Line Tools installation and asks you to rerun after that completes
- missing RE2:
  on macOS it runs `brew install re2 pkg-config`
  on Linux it uses `apt-get`, `dnf`, or `yum` when available

If you use MacPorts instead of Homebrew, install the equivalent packages with:

```bash
sudo port selfupdate
sudo port install re2 pkgconfig
```

If your compiler cannot find RE2 after MacPorts installation, ensure MacPorts paths are visible to your build tools, for example:

```bash
export PATH=/opt/local/bin:$PATH
export PKG_CONFIG_PATH=/opt/local/lib/pkgconfig:$PKG_CONFIG_PATH
```

Automatic installation still depends on a usable package manager and, on Linux, sufficient privileges.

## Local Setup

After the dependencies above are available, clone the repository, create a virtual environment, and install the Python runtime:

```bash
git clone <repo-url> betaMax
cd betaMax

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Run a small smoke benchmark:

```bash
./run_bms.sh quick
```

This default smoke run:

- installs `clang++` and RE2 automatically when supported package-manager access is available
- builds `betamax_cpp/build/betamax_cpp` automatically if needed
- builds all native regex validators automatically
- runs a small single-mutation benchmark
- writes results to `smoke_single.db`

Run the main regex benchmark suite:

```bash
./run_bms.sh regex
```

This runs:

- `bm_single.py`
- `bm_multiple.py`
- `bm_triple.py`

with the default regex formats and writes:

- `single.db`
- `double.db`
- `triple.db`

If you want separate DB names for a fresh run, use `DB_PREFIX`:

```bash
DB_PREFIX=trial1 ./run_bms.sh regex
```

That produces:

- `trial1_single.db`
- `trial1_double.db`
- `trial1_triple.db`

## Main Entry Points

### betaMax benchmarks

Use [run_bms.sh](./run_bms.sh):

```bash
./run_bms.sh quick
./run_bms.sh single
./run_bms.sh double
./run_bms.sh triple
./run_bms.sh regex
```

Examples:

```bash
./run_bms.sh single --formats date url --limit 10
DB_PREFIX=trial2 ./run_bms.sh regex
```

Notes:

- `regex` runs the main single/double/triple regex suite.
- For `regex`, prefer `DB_PREFIX=...` instead of `--db`, because multiple benchmark scripts are executed.

## Makefile Shortcuts

If you prefer `make`, the top-level [Makefile](./Makefile) exposes the common commands:

```bash
make help
make install
make quick
make single
make double
make triple
make regex
```

Useful variables:

```bash
PYTHON=python3 make install
DB_PREFIX=trial3 make regex
MAX_WORKERS=4 make quick
```

## Regex Validators

For regex benchmarks, native validators under [validators](./validators) are required. These are C++ binaries that include and link RE2. The benchmark automation passes `validators/validate_*` into betaMax as the oracle, so RE2 must be installed before building/running validators.

Build them manually if you want to prepare the environment ahead of time:

```bash
./validators/build_validators.sh
```

If they are missing, [run_bms.sh](./run_bms.sh) and [run_ddmax.sh](./run_ddmax.sh) try to build them automatically before starting the regex benchmarks. `run_bms.sh quick` now rebuilds the full validator set up front, and [validators/build_validators.sh](./validators/build_validators.sh) will also try to install missing `clang++` / RE2 dependencies when a supported package manager is available.

## Outputs

During runs, the repository writes to:

- `single.db`, `double.db`, `triple.db`
- `repair_results/` for per-sample outputs and jar materialization
- `cache/` for learned caches / DFA artifacts

The mutation corpora consumed by the benchmark runners live in:

- `mutated_files/`

Source corpora and original files live in:

- `data/`
- `original_files/`

## Reporting

Use the reporting script that matches where the benchmark wrote its SQLite result DBs.

For local runs that write the default DB names in the repository root, use [report.py](./report.py):

```bash
python report.py
```

This prints summary tables from `single.db`, `double.db`, and `triple.db`.

For Docker runs that write to [docker-results](./docker-results), use [report_docker.py](./report_docker.py):

```bash
python report_docker.py
```

By default this reads `docker-results/betamax_single.db`, `docker-results/betamax_double.db`, and `docker-results/betamax_triple.db`, matching the Docker image's default `DB_PREFIX=/results/betamax`.

If you ran Docker with a custom prefix such as `-e DB_PREFIX=/results/trial1`, pass that prefix when printing the report:

```bash
python report_docker.py --prefix trial1
```

For one-off Docker DBs or local runs with custom DB names, pass explicit paths:

```bash
python report_docker.py --db docker-results/smoke_date.db
python report_docker.py --db trial1_single.db --db trial1_double.db --db trial1_triple.db
```

## Repository Map

Key files and directories:

- [run_bms.sh](./run_bms.sh): main betaMax benchmark launcher
- [Makefile](./Makefile): common shortcuts
- [bm_single.py](./bm_single.py): single-mutation runner
- [bm_multiple.py](./bm_multiple.py): double-mutation runner
- [bm_triple.py](./bm_triple.py): triple-mutation runner
- [betamax_cpp](./betamax_cpp): C++ betaMax backend
- [validators](./validators): native RE2 regex validators used by benchmark automation
- [mutated_files](./mutated_files): mutation databases used as benchmark input
- [repair_results](./repair_results): per-run outputs
- [cache](./cache): precompute cache artifacts

## Troubleshooting

### `Missing Python module 'regex'`

Install the runtime:

```bash
python -m pip install -r requirements.txt
```

### `Missing native regex validator 'validators/validate_*'`

Either rerun:

```bash
./run_bms.sh quick
```

to let the launcher try automatic installation/build, or install RE2 yourself and build the validators explicitly:

```bash
./validators/build_validators.sh
```

If automatic installation is unavailable, check that Homebrew (`brew`) is installed on macOS, or that `apt-get` / `dnf` / `yum` plus the required privileges are available on Linux.

### `clang++ not found`

Either rerun:

```bash
./run_bms.sh quick
```

to let the launcher try automatic installation, or install the compiler toolchain yourself:

- macOS: `brew install llvm`, or install Xcode Command Line Tools
- Linux: install `clang` with your system package manager

### `betaMax C++ binary not found`

Either run:

```bash
./run_bms.sh quick
```

or build it explicitly:

```bash
cmake -S betamax_cpp -B betamax_cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build betamax_cpp/build -j
```

### I want custom benchmark arguments

`run_bms.sh` forwards extra arguments to the underlying `bm_*.py` scripts.

Examples:

```bash
./run_bms.sh single --formats date --limit 5 --resume
```
