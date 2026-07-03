#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi
MAX_WORKERS="${MAX_WORKERS:-3}"
MODE="${1:-quick}"
if [[ $# -gt 0 ]]; then
  shift
fi
EXTRA_ARGS=("$@")

# Benchmark defaults used by the betaMax runners.
export LSTAR_PRECOMPUTE_TIMEOUT="${LSTAR_PRECOMPUTE_TIMEOUT:-18000}"
export LSTAR_CACHE_LEARNER="${LSTAR_CACHE_LEARNER:-rpni_xover}"
export LSTAR_LEARNER="${LSTAR_LEARNER:-rpni_xover}"
export LSTAR_PARSE_TIMEOUT="${LSTAR_PARSE_TIMEOUT:-600}"
export LSTAR_EC_TIMEOUT="${LSTAR_EC_TIMEOUT:-600}"
export LSTAR_PRECOMPUTE_MUTATIONS="${LSTAR_PRECOMPUTE_MUTATIONS:-60}"
export BM_NEGATIVES_FROM_DB="${BM_NEGATIVES_FROM_DB:-0}"
export BETAMAX_DEBUG_ORACLE="${BETAMAX_DEBUG_ORACLE:-0}"
export BM_LSTAR_MUTATION_COUNT="${BM_LSTAR_MUTATION_COUNT:-${LSTAR_PRECOMPUTE_MUTATIONS}}"

DEFAULT_REGEX_FORMATS=(date time isbn ipv4 url ipv6)

die() {
  echo "[ERROR] $*" >&2
  exit 1
}

has_flag() {
  local needle="$1"
  shift || true
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "$needle" ]]; then
      return 0
    fi
  done
  return 1
}

has_extra_flag() {
  has_flag "$1" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
}

print_cmd() {
  printf '[RUN]'
  local arg
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
}

resolve_executable() {
  local candidate="$1"
  if [[ -z "$candidate" ]]; then
    return 1
  fi
  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] || return 1
    printf '%s\n' "$candidate"
    return 0
  fi
  command -v "$candidate" 2>/dev/null || return 1
}

find_clangxx() {
  local candidate

  if [[ -n "${CXX:-}" ]]; then
    candidate="$(resolve_executable "$CXX" || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  if [[ "$(uname -s)" == "Darwin" ]] && command -v xcrun >/dev/null 2>&1; then
    candidate="$(xcrun --sdk macosx --find clang++ 2>/dev/null || true)"
    if [[ -n "$candidate" ]] && [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  for candidate in clang++ /opt/homebrew/opt/llvm/bin/clang++ /usr/local/opt/llvm/bin/clang++; do
    candidate="$(resolve_executable "$candidate" || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

configure_clang_toolchain_env() {
  local clangxx
  local clang

  clangxx="$(find_clangxx || true)"
  [[ -n "$clangxx" ]] || return 1

  export CXX="$clangxx"
  clang="${clangxx%++}"
  if [[ -x "$clang" ]]; then
    export CC="$clang"
  fi
  return 0
}

require_command() {
  local cmd="$1"
  local hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    die "Missing command '$cmd'. $hint"
  fi
}

require_python_module() {
  local module="$1"
  local pkg_hint="$2"
  if ! "$PYTHON_BIN" -c "import ${module}" >/dev/null 2>&1; then
    die "Missing Python module '${module}'. Install dependencies with '${PYTHON_BIN} -m pip install -r requirements.txt'${pkg_hint:+ (needs ${pkg_hint})}."
  fi
}

is_regex_format() {
  case "$1" in
    date|time|isbn|ipv4|ipv6|url|iso8601|pathfile)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

selected_formats_from_args() {
  local -a values=()
  local capture=0
  local arg
  for arg in "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"; do
    if [[ "$capture" == "1" ]]; then
      if [[ "$arg" == --* ]]; then
        break
      fi
      values+=("$arg")
    elif [[ "$arg" == "--formats" ]]; then
      capture=1
    fi
  done

  if (( ${#values[@]} == 0 )); then
    printf '%s\n' "$@"
    return 0
  fi
  printf '%s\n' "${values[@]}"
}

build_all_native_regex_validators() {
  [[ -x "./validators/build_validators.sh" ]] || die "Missing ./validators/build_validators.sh."
  echo "[INFO] Building all native regex validators..."
  print_cmd ./validators/build_validators.sh
  ./validators/build_validators.sh
}

ensure_native_regex_validators() {
  local -a requested=("$@")
  local -a needed=()
  local fmt
  local validator

  for fmt in "${requested[@]+"${requested[@]}"}"; do
    if is_regex_format "$fmt"; then
      needed+=("$fmt")
    fi
  done

  if (( ${#needed[@]} == 0 )); then
    return 0
  fi

  for fmt in "${needed[@]+"${needed[@]}"}"; do
    validator="validators/validate_${fmt}"
    if [[ ! -x "$validator" ]]; then
      echo "[INFO] Native RE2 validator '$validator' not found. Building validators..."
      build_all_native_regex_validators
      break
    fi
  done

  for fmt in "${needed[@]+"${needed[@]}"}"; do
    validator="validators/validate_${fmt}"
    [[ -x "$validator" ]] || die "Missing native regex validator '$validator'. Install RE2 and run './validators/build_validators.sh'."
  done
}

default_db_path() {
  local stem="$1"
  if [[ -n "${DB_PREFIX:-}" ]]; then
    printf '%s_%s.db\n' "$DB_PREFIX" "$stem"
  else
    printf '%s.db\n' "$stem"
  fi
}

ensure_cpp_engine() {
  if [[ -x "betamax_cpp/build/betamax_cpp" ]]; then
    return 0
  fi
  if ! configure_clang_toolchain_env; then
    echo "[INFO] clang++ not found. Bootstrapping the compiler toolchain via validators/build_validators.sh..."
    build_all_native_regex_validators
    configure_clang_toolchain_env || die "clang++ is still unavailable after automatic installation."
  fi
  require_command cmake "Install CMake or build betamax_cpp manually."
  echo "[INFO] betamax_cpp/build/betamax_cpp not found. Building the C++ backend..."
  print_cmd cmake -S betamax_cpp -B betamax_cpp/build -DCMAKE_BUILD_TYPE=Release
  cmake -S betamax_cpp -B betamax_cpp/build -DCMAKE_BUILD_TYPE=Release
  print_cmd cmake --build betamax_cpp/build -j
  cmake --build betamax_cpp/build -j
}

ensure_betamax_backend() {
  require_python_module regex regex
  ensure_cpp_engine
}

ensure_mutation_dbs() {
  local mut_type="$1"
  shift || true
  local fmt
  local missing=0
  for fmt in "$@"; do
    local db_path="mutated_files/${mut_type}_${fmt}.db"
    if [[ ! -f "$db_path" ]]; then
      echo "[ERROR] Missing mutation DB: $db_path" >&2
      missing=1
    fi
  done
  if [[ "$missing" != "0" ]]; then
    die "Required mutation DBs are missing under mutated_files/."
  fi
}

ensure_truncation_subjects() {
  if has_extra_flag "--formats"; then
    return 0
  fi
  if has_extra_flag "--truncated-json-validator"; then
    return 0
  fi
  if [[ -n "${BM_TRUNCATED_JSON_VALIDATOR:-}" ]]; then
    return 0
  fi
  local validator="project/bin/subjects/cjson/cjson"
  if [[ ! -x "$validator" ]]; then
    die "Missing JSON subject validator '$validator'. Build subject artifacts with './build_all.sh' before running truncation benchmarks."
  fi
}

run_single() {
  local -a selected_formats=()
  while IFS= read -r fmt; do
    selected_formats+=("$fmt")
  done < <(selected_formats_from_args "${DEFAULT_REGEX_FORMATS[@]}")
  ensure_native_regex_validators "${selected_formats[@]+"${selected_formats[@]}"}"
  ensure_betamax_backend
  if ! has_extra_flag "--formats"; then
    ensure_mutation_dbs single "${DEFAULT_REGEX_FORMATS[@]}"
  fi
  local db_name
  db_name="$(default_db_path single)"
  local -a cmd=("$PYTHON_BIN" "bm_single.py")
  if ! has_extra_flag "--db"; then
    cmd+=(--db "$db_name")
  fi
  if ! has_extra_flag "--formats"; then
    cmd+=(--formats "${DEFAULT_REGEX_FORMATS[@]}")
  fi
  if ! has_extra_flag "--algorithms"; then
    cmd+=(--algorithms betamax)
  fi
  if ! has_extra_flag "--max-workers"; then
    cmd+=(--max-workers "$MAX_WORKERS")
  fi
  cmd+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")
  print_cmd "${cmd[@]}"
  "${cmd[@]}"
}

run_double() {
  local -a selected_formats=()
  while IFS= read -r fmt; do
    selected_formats+=("$fmt")
  done < <(selected_formats_from_args "${DEFAULT_REGEX_FORMATS[@]}")
  ensure_native_regex_validators "${selected_formats[@]+"${selected_formats[@]}"}"
  ensure_betamax_backend
  if ! has_extra_flag "--formats"; then
    ensure_mutation_dbs double "${DEFAULT_REGEX_FORMATS[@]}"
  fi
  local db_name
  db_name="$(default_db_path double)"
  local -a cmd=("$PYTHON_BIN" "bm_multiple.py")
  if ! has_extra_flag "--db"; then
    cmd+=(--db "$db_name")
  fi
  if ! has_extra_flag "--formats"; then
    cmd+=(--formats "${DEFAULT_REGEX_FORMATS[@]}")
  fi
  if ! has_extra_flag "--algorithms"; then
    cmd+=(--algorithms betamax)
  fi
  if ! has_extra_flag "--max-workers"; then
    cmd+=(--max-workers "$MAX_WORKERS")
  fi
  cmd+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")
  print_cmd "${cmd[@]}"
  "${cmd[@]}"
}

run_triple() {
  local -a selected_formats=()
  while IFS= read -r fmt; do
    selected_formats+=("$fmt")
  done < <(selected_formats_from_args "${DEFAULT_REGEX_FORMATS[@]}")
  ensure_native_regex_validators "${selected_formats[@]+"${selected_formats[@]}"}"
  ensure_betamax_backend
  if ! has_extra_flag "--formats"; then
    ensure_mutation_dbs triple "${DEFAULT_REGEX_FORMATS[@]}"
  fi
  local db_name
  db_name="$(default_db_path triple)"
  local -a cmd=("$PYTHON_BIN" "bm_triple.py")
  if ! has_extra_flag "--db"; then
    cmd+=(--db "$db_name")
  fi
  if ! has_extra_flag "--formats"; then
    cmd+=(--formats "${DEFAULT_REGEX_FORMATS[@]}")
  fi
  if ! has_extra_flag "--algorithms"; then
    cmd+=(--algorithms betamax)
  fi
  if ! has_extra_flag "--max-workers"; then
    cmd+=(--max-workers "$MAX_WORKERS")
  fi
  cmd+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")
  print_cmd "${cmd[@]}"
  "${cmd[@]}"
}

run_quick() {
  local quick_limit="${BM_QUICK_LIMIT:-3}"
  local quick_precompute="${BM_QUICK_PRECOMPUTE_MUTATIONS:-10}"
  local quick_formats_raw="${BM_QUICK_FORMATS:-date}"
  local -a quick_formats=()
  local -a selected_formats=()
  read -r -a quick_formats <<< "$quick_formats_raw"
  if [[ "${#quick_formats[@]}" -eq 0 ]]; then
    quick_formats=(date)
  fi
  while IFS= read -r fmt; do
    selected_formats+=("$fmt")
  done < <(selected_formats_from_args "${quick_formats[@]}")
  build_all_native_regex_validators
  ensure_native_regex_validators "${selected_formats[@]+"${selected_formats[@]}"}"
  ensure_betamax_backend
  if ! has_extra_flag "--formats"; then
    ensure_mutation_dbs single "${quick_formats[@]}"
  fi
  local db_name
  db_name="$(default_db_path smoke_single)"
  local -a cmd=("$PYTHON_BIN" "bm_single.py")
  if ! has_extra_flag "--db"; then
    cmd+=(--db "$db_name")
  fi
  if ! has_extra_flag "--formats"; then
    cmd+=(--formats "${quick_formats[@]}")
  fi
  if ! has_extra_flag "--limit"; then
    cmd+=(--limit "$quick_limit")
  fi
  if ! has_extra_flag "--algorithms"; then
    cmd+=(--algorithms betamax)
  fi
  if ! has_extra_flag "--max-workers"; then
    cmd+=(--max-workers "$MAX_WORKERS")
  fi
  cmd+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")
  printf '[RUN] %s=%q %s=%q' \
    "LSTAR_PRECOMPUTE_MUTATIONS" "$quick_precompute" \
    "BM_LSTAR_MUTATION_COUNT" "$quick_precompute"
  local arg
  for arg in "${cmd[@]}"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  LSTAR_PRECOMPUTE_MUTATIONS="$quick_precompute" \
  BM_LSTAR_MUTATION_COUNT="$quick_precompute" \
  BM_FRESH_DB="${BM_FRESH_DB:-1}" \
    "${cmd[@]}"
}

run_regex_suite() {
  if has_extra_flag "--db"; then
    die "Do not pass --db with mode 'regex'. Use DB_PREFIX=<name> ./run_bms.sh regex or run single/double/triple separately."
  fi
  run_single
  run_double
  run_triple
}

run_truncation() {
  ensure_betamax_backend
  ensure_truncation_subjects
  local db_name
  db_name="$(default_db_path truncation)"
  local -a cmd=("$PYTHON_BIN" "bm_truncation.py")
  if ! has_extra_flag "--db"; then
    cmd+=(--db "$db_name")
  fi
  if ! has_extra_flag "--formats"; then
    cmd+=(--formats json)
  fi
  if ! has_extra_flag "--algorithms"; then
    cmd+=(--algorithms betamax)
  fi
  if ! has_extra_flag "--max-workers"; then
    cmd+=(--max-workers "$MAX_WORKERS")
  fi
  cmd+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")
  print_cmd "${cmd[@]}"
  "${cmd[@]}"
}

usage() {
  cat <<'EOF'
Usage:
  ./run_bms.sh <mode> [extra bm_* args]

Modes:
  quick       Smoke test: small single-mutation run (default DB: smoke_single.db)
  single      Run bm_single.py on the main regex benchmarks
  double      Run bm_multiple.py on the main regex benchmarks
  triple      Run bm_triple.py on the main regex benchmarks
  regex       Run single + double + triple regex benchmarks
  truncation  Run the JSON truncation benchmark (requires subject binaries)
  all         Run regex + truncation benchmarks
  help        Show this message

Examples:
  ./run_bms.sh quick
  ./run_bms.sh single --formats date url --limit 10
  DB_PREFIX=trial1 ./run_bms.sh regex
  ./run_bms.sh truncation --limit 10

Useful env vars:
  PYTHON_BIN                   Python interpreter to use (default: python3)
  MAX_WORKERS                  Passed to bm_*.py if --max-workers is omitted
  BM_QUICK_FORMATS             Formats used by quick mode (default: "date")
  BM_QUICK_LIMIT               Sample limit for quick mode (default: 3)
  BM_QUICK_PRECOMPUTE_MUTATIONS Precompute mutation count for quick mode (default: 10)
  LSTAR_PRECOMPUTE_MUTATIONS_FALLBACK
                               Mutation count used after precompute timeout when no cache is ready
  DB_PREFIX                    Prefix per-mode DB names, e.g. trial1_single.db

Notes:
  - Extra args are forwarded to the underlying bm_*.py runner(s).
  - For mode 'regex', use DB_PREFIX instead of --db because three scripts are run.
  - Regex benchmark modes require native RE2 validators under validators/validate_*; the launcher builds them automatically if needed.
  - The supported quickstart path in this checkout uses the C++ backend under betamax_cpp/.
EOF
}

case "$MODE" in
  quick)
    run_quick
    ;;
  single)
    run_single
    ;;
  double|multiple)
    run_double
    ;;
  triple)
    run_triple
    ;;
  regex|main)
    run_regex_suite
    ;;
  truncation|json)
    run_truncation
    ;;
  all)
    if has_extra_flag "--db"; then
      die "Do not pass --db with mode 'all'. Use DB_PREFIX=<name> ./run_bms.sh all."
    fi
    run_regex_suite
    run_truncation
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    die "Unknown mode '$MODE'."
    ;;
esac
