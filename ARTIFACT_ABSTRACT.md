# Artifact Abstract

## Paper title

**βMax: Black-Box Data Repair Without Format Specifications**

## Link

The accepted-paper PDF is included with this artifact as [ASE_beta_max_repairer.pdf](ASE_beta_max_repairer.pdf).

Accepted paper URL: **TODO: add the reviewer-accessible accepted-paper URL before submission.**

## Purpose

This artifact provides the implementation, benchmark scripts, datasets, native validators, precomputed DFA caches, reporting scripts, and Docker environment for evaluating **βMax**, a structured-data repair system for settings where formal format specifications, regular expressions, grammars, or precise parser error messages are unavailable.

βMax uses only valid example strings and a black-box validator that accepts or rejects candidate strings. From these inputs, it infers approximate format models, caches DFA-based repair guidance, and repairs corrupted records for structured formats including dates, times, ISBNs, IPv4 addresses, IPv6 addresses, and URLs. The artifact supports smoke tests, the main single-/double-/triple-mutation regex benchmark workflow, and summary reporting from generated SQLite result databases.

## Badge

We apply for the following ACM artifact badges.

**Functional.** The artifact is documented, complete, and exercisable through a Docker image that packages the required compiler toolchain, CMake, RE2, Python dependencies, the βMax C++ backend, validators, benchmark datasets, and precomputed DFA caches. The README provides smoke-test commands, full benchmark commands, and reporting commands. The artifact includes evidence of verification through a Docker smoke-test workflow and generated SQLite result summaries.

**Reusable.** The artifact is organized to support reuse beyond the original evaluation. Benchmark runners, validators, mutation databases, learned DFA caches, reporting scripts, and the C++ backend are separated. Reviewers can run selected formats, adjust limits, reuse the Docker image, inspect SQLite results, or run scripts locally. The precomputed caches are included so runs can avoid expensive precomputation and fall back to cached DFA models if precomputation times out.

**Available.** The artifact is public on GitHub and Docker Hub. For the ACM Available badge, the final submission should also include an archival DOI from Zenodo, FigShare, Dryad, or a similar long-term repository.

Artifact DOI: **TODO: add archival DOI before submission.**

## Technology

Expected reviewer skills are basic command-line usage, basic Docker usage, and optional familiarity with Python and SQLite for inspecting generated result databases. No knowledge of βMax internals is required for the standard workflow.

Recommended Docker-path requirements are Docker 24+ or a compatible Docker runtime, internet access to pull the image, 4 CPU cores, 8 GB RAM, and at least 5 GB of free disk space. The Docker workflow should work on Linux, macOS, or Windows with Docker Desktop or an equivalent Docker-compatible runtime. No GPU or unusual hardware is required.

For non-Docker local execution, reviewers need Python 3.10+, CMake 3.16+, a C++17 compiler, RE2 development headers/libraries, and the Python packages in [requirements.txt](requirements.txt). The Docker path is recommended because it avoids manual dependency installation.

## Provenance

The artifact can be obtained from:

- GitHub repository: <https://github.com/Jacksadventure/betaMax>
- Artifact branch: `main`
- Docker image: `idk233333/betamax:latest`
- Docker image digest: `sha256:be2335cc23aa93bda5b4b73ec6fc3bdbc5f34bee3c95ddbe2cda16040dd4ba98`

The Docker image contains the benchmark environment, including the C++ βMax backend, native validators, benchmark scripts, mutation databases, and precomputed DFA caches.

## Instructions

Pull the published Docker image and give it the short local tag used by the README examples:

```bash
docker pull idk233333/betamax:latest
docker tag idk233333/betamax:latest betamax:latest
```

Run a small smoke test and store results on the host:

```bash
mkdir -p docker-results
docker run --rm \
  -v "$PWD/docker-results:/results" \
  betamax:latest quick --formats date --db /results/smoke_date.db
```

Print the smoke-test report:

```bash
python report_docker.py --db docker-results/smoke_date.db
```

Run the main regex benchmark suite:

```bash
docker run --rm \
  -v "$PWD/docker-results:/results" \
  betamax:latest regex
```

This writes:

```text
docker-results/betamax_single.db
docker-results/betamax_double.db
docker-results/betamax_triple.db
```

Print the main Docker benchmark report:

```bash
python report_docker.py
```

For a smaller selected-format run:

```bash
docker run --rm \
  -v "$PWD/docker-results:/results" \
  betamax:latest single --formats date url --limit 10
```

For a custom output prefix:

```bash
docker run --rm \
  -v "$PWD/docker-results:/results" \
  -e DB_PREFIX=/results/trial1 \
  betamax:latest regex
python report_docker.py --prefix trial1
```

The benchmark input datasets live under `mutated_files/` as SQLite databases, with one database per mutation setting and format, such as `single_date.db`, `double_url.db`, and `triple_ipv4.db`. The generated result databases contain a `results` table with fields including `format`, `algorithm`, `original_text`, `broken_text`, `repaired_text`, `fixed`, `iterations`, `repair_time`, `correct_runs`, `incorrect_runs`, `incomplete_runs`, `distance_original_broken`, `distance_broken_repaired`, and `distance_original_repaired`. The reporting scripts aggregate these fields to compute success counts, repair quality, timing, and efficiency metrics.

The Docker smoke test should take only a few minutes after the image is pulled. The full benchmark suite is longer because it runs multiple formats and corruption settings. No GPU, cloud service, special OS image, or proprietary dependency is required.