#!/usr/bin/env python3
import os
import sqlite3
import subprocess
import re
import time
import random
import shutil
import concurrent.futures
import argparse
from typing import Optional

from bm_lstar_mutations import LStarMutationPool, get_mutation_table_name
from bm_betamax_backend import build_betamax_cmd, cpp_bin_path
from bm_ddmax_backend import (
    ddmax_repair_by_deletion,
    oracle_cmd_from_env_or_default,
    require_native_regex_validator,
    select_regex_oracle_arg,
    select_regex_oracle_cmd,
)

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
DATABASE_PATH = "double.db"  # Default results database for double-mutation runs
REPAIR_OUTPUT_DIR = "repair_results"  # Directory where repair outputs are stored
os.makedirs(REPAIR_OUTPUT_DIR, exist_ok=True)

# Supported repair algorithms (CLI choices)
ALL_ALGORITHMS = ["erepair", "earley", "betamax", "ddmax"]


# Default repair algorithms to run (can be overridden via --algorithms)
REPAIR_ALGORITHMS = ["betamax"]

PROJECT_PATHS = {
    "dot": "project/bin/subjects/dot/build/dot_parser",
    "ini": "project/bin/subjects/ini/ini",
    "json": "project/bin/subjects/cjson/cjson",
    "lisp": "project/bin/subjects/sexp-parser/sexp",
    "obj": "project/bin/subjects/obj/build/obj_parser",
    "c": "project/bin/subjects/tiny/tiny",
    # Regex category command strings used by helper paths.
    "date": "python3 match.py Date",
    "iso8601": "python3 match.py ISO8601",
    "time": "python3 match.py Time",
    "url":  "python3 match.py URL",
    "isbn": "python3 match.py ISBN",
    "ipv4": "python3 match.py IPv4",
    "ipv6": "python3 match.py IPv6"
}

# Mapping for regex-based categories
REGEX_DIR_TO_CATEGORY = {
    "date": "Date",
    "iso8601": "ISO8601",
    "time": "Time",
    "url": "URL",
    "isbn": "ISBN",
    "ipv4": "IPv4",
    "ipv6": "IPv6"
}
REGEX_FORMATS = set(REGEX_DIR_TO_CATEGORY.keys())

# Formats exposed on CLI. Keep defaults limited to regex formats so existing
# runs don't suddenly require new mutation DBs.
DEFAULT_FORMATS = ["date", "time", "isbn", "ipv4", "url", "ipv6"]
FORMAT_CHOICES = DEFAULT_FORMATS + ["dot", "ini", "json", "lisp", "obj", "c"]
VALID_FORMATS = FORMAT_CHOICES

JAR_ALGORITHM_MAP = {
    "ddmax": "DDMax",
    "ddmin": "DDMin",
    "ddmaxg": "DDMaxG",
    "baseline": "Baseline",
    "antlr": "Antlr",
}


def _jar_algorithm_name(algorithm: str) -> str:
    return JAR_ALGORITHM_MAP.get((algorithm or "").strip(), algorithm)


def _materialize_jar_output(output_dir: str, input_file: str, output_file: str) -> bool:
    """
    eRepair.jar repair mode writes into an output directory. This helper tries to
    locate the repaired file and copy it into `output_file` so the rest of the
    benchmark pipeline (validation + distance) stays unchanged.
    """
    try:
        base = os.path.basename(input_file)
        direct = os.path.join(output_dir, base)
        if os.path.isfile(direct):
            shutil.copyfile(direct, output_file)
            return True

        candidates: list[str] = []
        for root, _dirs, files in os.walk(output_dir):
            for fn in files:
                p = os.path.join(root, fn)
                if os.path.isfile(p):
                    candidates.append(p)

        if not candidates:
            return False

        ext = os.path.splitext(base)[1].lower()
        same_ext = [p for p in candidates if os.path.splitext(p)[1].lower() == ext] or candidates
        same_ext.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        shutil.copyfile(same_ext[0], output_file)
        return True
    except Exception:
        return False


MUTATION_TYPES = ["double"]

# Train/Test split counts (default 50/50). Override via env BM_TRAIN_K, BM_TEST_K.
TRAIN_K = int(os.environ.get("BM_TRAIN_K", "50"))
TEST_K = int(os.environ.get("BM_TEST_K", "50"))

# Shared mutation pool manager
LSTAR_MUTATION_POOL = LStarMutationPool(TRAIN_K, REGEX_FORMATS, REGEX_DIR_TO_CATEGORY)

CACHE_ROOT = os.environ.get("LSTAR_CACHE_ROOT", "cache")
os.makedirs(CACHE_ROOT, exist_ok=True)

# Whether to use mutated_text pulled from mutated_files/*.db as negatives.
# Default is off because mutated_text can include oracle-accepted strings and
# "broken DB strings" are often not reliable negatives for learning.
BM_NEGATIVES_FROM_DB = os.environ.get("BM_NEGATIVES_FROM_DB", "0").lower() in ("1", "true", "yes")

def _cache_path(fmt: str, learner: Optional[str] = None) -> str:
    """
    Build the cache path for a given format/learner combo so grammars learned
    with different algorithms do not overwrite each other.
    """
    learner_name = (learner or _runtime_betamax_learner()).replace("-", "_")
    return os.path.join(CACHE_ROOT, f"lstar_{fmt}_{learner_name}.dfa")


def _runtime_betamax_learner() -> str:
    """Return learner name for runtime BETAMAX invocation (fixed default for bm_*)."""
    return "rpni_xover"


def _cache_betamax_learner() -> str:
    """Return learner name for cache/precompute steps (fixed default for bm_*)."""
    return "rpni_xover"


def _cpp_dfa_cache_ready(cache_path: str) -> bool:
    """
    Return True iff the DFA cache exists and includes incremental metadata.
    We require:
      - BMXDFA1 header
      - LEARNER rpni_xover
      - MERGE_HISTORY (needed for incremental replay in refine loop)
    """
    try:
        if not os.path.isfile(cache_path):
            return False
        with open(cache_path, "rb") as f:
            head = f.readline().decode("ascii", errors="ignore").strip()
            if head != "BMXDFA1":
                return False
            f.seek(0, os.SEEK_END)
            size = f.tell()
            n = min(size, 65536)
            f.seek(size - n)
            tail = f.read(n).decode("latin-1", errors="ignore")
        return ("LEARNER rpni_xover" in tail) and ("MERGE_HISTORY" in tail)
    except Exception:
        return False


def _cmd_with_option_value(cmd, option, value):
    retry_cmd = list(cmd)
    try:
        idx = retry_cmd.index(option)
    except ValueError:
        if value is not None:
            retry_cmd.extend([option, str(value)])
        return retry_cmd
    if value is None:
        del retry_cmd[idx:min(idx + 2, len(retry_cmd))]
    elif idx + 1 < len(retry_cmd):
        retry_cmd[idx + 1] = str(value)
    else:
        retry_cmd.append(str(value))
    return retry_cmd


def _normalize_learner_name(name: str) -> str:
    """
    Normalize learner identifiers coming from env vars so cache naming and CLI
    invocation are consistent.
    """
    n = (name or "").strip()
    if not n:
        return "rpni_xover"
    aliases = {
        "xover_rpni": "rpni_xover",
        "xover-rpni": "rpni_xover",
        "rpni-xover": "rpni_xover",
        "nfa_rpni": "rpni_nfa",
        "nfa-rpni": "rpni_nfa",
        "fuzz_rpni": "rpni_fuzz",
        "fuzz-rpni": "rpni_fuzz",
        "lstar": "lstar_oracle",
        "lstar-oracle": "lstar_oracle",
    }
    return aliases.get(n, n)

# Parser timeout (in seconds)
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


VALIDATION_TIMEOUT = _env_int("BM_VALIDATION_TIMEOUT", 300)

# Repair timeout (in seconds)
REPAIR_TIMEOUT = _env_int("BM_REPAIR_TIMEOUT", 300)

# Verbosity and run-control
QUIET = False          # suppress per-entry stdout/stderr and noisy logs
LIMIT_N = None         # limit number of entries to (re)process
PAUSE_ON_EXIT = False  # wait for keypress before exiting (optional)

# ------------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------------

def create_database(db_path: str):
    """
    Creates a new SQLite database (or overwrites if it already exists).
    This function will create a 'results' table with columns that store
    original/corrupted text, repaired text, and various repair metrics.
    """
    if os.path.exists(db_path):
        print(f"[WARNING] Database '{db_path}' already exists. It will be reused/overwritten.")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create the table if not exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            format TEXT,
            file_id INTEGER,
            corrupted_index INTEGER,
            algorithm TEXT,
            original_text TEXT,
            broken_text TEXT,
            repaired_text TEXT,
            fixed INTEGER,
            iterations INTEGER,
            repair_time REAL,
            correct_runs INTEGER,
            incorrect_runs INTEGER,
            incomplete_runs INTEGER,
            distance_original_broken INTEGER,
            distance_broken_repaired INTEGER,
            distance_original_repaired INTEGER
        )
    """)
    _ensure_results_schema(cursor)
    conn.commit()
    conn.close()
    print(f"[INFO] Created/checked table 'results' in database '{db_path}'.")

def _ensure_results_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(results)")
    existing = {row[1] for row in cursor.fetchall()}
    additions = [
        ("timed_out", "INTEGER DEFAULT 0"),
        ("return_code", "INTEGER"),
        ("ec_time", "REAL DEFAULT 0.0"),
        ("ec_ratio", "REAL DEFAULT 0.0"),
        ("learn_time", "REAL DEFAULT 0.0"),
        ("learn_ratio", "REAL DEFAULT 0.0"),
        ("oracle_time", "REAL DEFAULT 0.0"),
        ("oracle_ratio", "REAL DEFAULT 0.0"),
    ]
    for name, decl in additions:
        if name in existing:
            continue
        try:
            cursor.execute(f"ALTER TABLE results ADD COLUMN {name} {decl}")
        except Exception:
            pass


def _results_columns(cursor: sqlite3.Cursor) -> set[str]:
    cursor.execute("PRAGMA table_info(results)")
    return {row[1] for row in cursor.fetchall()}


def _update_results_row(
    cursor: sqlite3.Cursor,
    conn: sqlite3.Connection,
    *,
    id_: int,
    repaired_text: str,
    fixed: int,
    iterations: int,
    repair_time: float,
    correct_runs: int,
    incorrect_runs: int,
    incomplete_runs: int,
    distance_original_broken: int,
    distance_broken_repaired: int,
    distance_original_repaired: int,
    timed_out: int,
    return_code: int,
    ec_time: float,
    ec_ratio: float,
    learn_time: float,
    learn_ratio: float,
    oracle_time: float,
    oracle_ratio: float,
) -> None:
    cols = _results_columns(cursor)
    assignments = [
        "repaired_text = ?",
        "fixed = ?",
        "iterations = ?",
        "repair_time = ?",
        "correct_runs = ?",
        "incorrect_runs = ?",
        "incomplete_runs = ?",
        "distance_original_broken = ?",
        "distance_broken_repaired = ?",
        "distance_original_repaired = ?",
    ]
    params: list[object] = [
        repaired_text,
        fixed,
        iterations,
        repair_time,
        correct_runs,
        incorrect_runs,
        incomplete_runs,
        distance_original_broken,
        distance_broken_repaired,
        distance_original_repaired,
    ]

    optional = [
        ("timed_out", timed_out),
        ("return_code", return_code),
        ("ec_time", ec_time),
        ("ec_ratio", ec_ratio),
        ("learn_time", learn_time),
        ("learn_ratio", learn_ratio),
        ("oracle_time", oracle_time),
        ("oracle_ratio", oracle_ratio),
    ]
    for col, val in optional:
        if col not in cols:
            continue
        assignments.append(f"{col} = ?")
        params.append(val)

    params.append(id_)
    cursor.execute(
        f"UPDATE results SET {', '.join(assignments)} WHERE id = ?",
        params,
    )
    conn.commit()


_RE_METRIC = re.compile(r"^\[METRICS\]\s+([a-zA-Z0-9_]+)=([0-9]*\.?[0-9]+)\s*$", re.MULTILINE)

def _ensure_text(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def load_test_samples_from_db(mutation_db_path: str):
    """
    Loads test samples from a mutation database.
    """
    if not os.path.exists(mutation_db_path):
        print(f"[ERROR] Mutation database not found: {mutation_db_path}")
        return []

    conn = sqlite3.connect(mutation_db_path)
    table_name = get_mutation_table_name(mutation_db_path, conn)
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, original_text, mutated_text FROM {table_name} ORDER BY id")
    samples = cursor.fetchall()
    conn.close()

    # The format of test_samples should be (file_id, corrupted_index, original_text, corrupted_text)
    test_samples = [(row[0], 0, row[1], row[2]) for row in samples]
    return test_samples


def insert_test_samples_to_db(db_path: str, format_key: str, test_samples: list):
    """
    Insert the given list of (file_id, corrupted_index, original_text, corrupted_text)
    into the 'results' table in the database, for the specified format.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Insert each entry only if it doesn't already exist (resume capability)
    for (file_id, cindex, orig_text, broken_text) in test_samples:
        for alg in REPAIR_ALGORITHMS:
            base_f = format_key.split('_')[-1]
            if base_f in REGEX_FORMATS and alg not in ("erepair", "earley", "betamax", "ddmax"):
                continue
            # Skip if this combination already exists (enables resume)
            cursor.execute(
                "SELECT 1 FROM results WHERE format=? AND file_id=? AND corrupted_index=? AND algorithm=? LIMIT 1",
                (format_key, file_id, cindex, alg)
            )
            if cursor.fetchone():
                continue
            cursor.execute("""
                INSERT INTO results (format, file_id, corrupted_index, algorithm,
                                     original_text, broken_text,
                                     repaired_text, fixed, iterations, repair_time,
                                     correct_runs, incorrect_runs, incomplete_runs,
                                     distance_original_broken, distance_broken_repaired, distance_original_repaired)
                VALUES (?, ?, ?, ?, ?, ?, '', 0, 0, 0.0, 0, 0, 0, 0, 0, 0)
            """, (format_key, file_id, cindex, alg, orig_text, broken_text))
    conn.commit()
    conn.close()


def validate_with_external_tool(file_path: str, format_key: str, algorithm: str) -> bool:
    """
    Validate a repaired file.
    - erepair: use match_partial.py Category
    - regex benchmarks otherwise require validators/validate_* (native RE2 binary)
    """
    base_format = format_key.split('_')[-1]
    try:
        if base_format in REGEX_FORMATS:
            category = REGEX_DIR_TO_CATEGORY.get(base_format, base_format)
            if algorithm == "erepair":
                cmd = ["python3", "match_partial.py", category, file_path]
            else:
                cmd = [require_native_regex_validator(base_format), file_path]
        else:
            # Fallback for non-regex formats (not used in current VALID_FORMATS)
            proj = PROJECT_PATHS.get(base_format)
            if not proj:
                print(f"[ERROR] No validator for non-regex format: {base_format}")
                return False
            cmd = [proj, file_path]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=VALIDATION_TIMEOUT
        )
        return (result.returncode == 0)
    except subprocess.TimeoutExpired:
        print(f"[ERROR] Validation timeout for '{file_path}', format '{format_key}' (algorithm={algorithm})")
        return False
    except Exception as e:
        print(f"[ERROR] Could not run validator for format '{format_key}' (algorithm={algorithm}): {e}")
        return False


def _normalize_for_distance(text: Optional[str]) -> str:
    """
    Treat trailing newline/CR characters as insignificant when comparing strings.
    This keeps benchmark distances aligned with human intuition for single-line inputs.
    """
    if text is None:
        return ""
    return text.rstrip("\r\n")


def levenshtein_distance(a: str, b: str) -> int:
    """Calculate the Levenshtein distance between two strings."""
    a = _normalize_for_distance(a)
    b = _normalize_for_distance(b)
    if not a: return len(b)
    if not b: return len(a)

    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]

    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j

    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,       # Deletion
                dp[i][j - 1] + 1,       # Insertion
                dp[i - 1][j - 1] + cost # Substitution
            )
    return dp[-1][-1]


def extract_oracle_info(stdout: str):
    """
    Parse oracle summary lines. Supports both:
      - *** Number of required oracle runs: <n> correct: <ok> incorrect: <bad> incomplete: <inc> ***
      - *** Number of required oracle runs: <n> correct: <ok> incorrect: <bad>
    """
    # New format (no incomplete, no trailing ***)
    match = re.search(r"\*\*\* Number of required oracle runs: (\d+) correct: (\d+) incorrect: (\d+)\s*$", stdout, re.MULTILINE)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), 0
    # Old format (with incomplete and trailing ***)
    match = re.search(r"\*\*\* Number of required oracle runs: (\d+) correct: (\d+) incorrect: (\d+) incomplete: (\d+) \*\*\*", stdout)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
    return 0, 0, 0, 0


def extract_oracle_seconds_total(stdout: str) -> float:
    last_val = 0.0
    for m in _RE_METRIC.finditer(stdout or ""):
        if m.group(1) != "oracle_seconds_total":
            continue
        try:
            last_val = float(m.group(2))
        except Exception:
            continue
    return last_val


def repair_and_update_entry(cursor, conn, row):
    """
    Given a single row from the 'results' table, run the repair tool, measure results,
    and update the row in the database.
    """
    (id_, format_key, file_id, corrupted_index, algorithm,
     original_text, broken_text, _repaired, _fixed, _iter, _rtime,
     _correct, _incorrect, _incomplete, _distOB, _distBR, _distOR) = row

    if not QUIET:
        print(f"[INFO] Repairing ID={id_}, format={format_key}, algorithm={algorithm}, file_id={file_id}, corrupted_index={corrupted_index}")

    # Prepare temporary input and output files
    base_format = format_key.split('_')[-1]
    ext = base_format
    if algorithm != "erepair":
        input_file = f"temp_{id_}_{random.randint(0, 9999)}_input.{ext}"
        output_file = os.path.join(REPAIR_OUTPUT_DIR, f"repair_{id_}_output.{ext}")
    else:
        input_file = f"temp_{id_}_{random.randint(0, 9999)}_input.{format_key}"
        output_file = os.path.join(REPAIR_OUTPUT_DIR, f"repair_{id_}_output.{format_key}")

    with open(input_file, "w", encoding="utf-8") as f:
        f.write(broken_text)

    distance_original_broken = levenshtein_distance(original_text, broken_text)
    distance_broken_repaired = -1
    distance_original_repaired = -1

    # By default, we mark as not fixed
    repaired_text = ""
    fixed = 0
    iterations, correct_runs, incorrect_runs, incomplete_runs = 0, 0, 0, 0
    repair_time = 0.0
    timed_out = 0
    return_code = None
    ec_time = 0.0
    ec_ratio = 0.0
    learn_time = 0.0
    learn_ratio = 0.0
    oracle_time = 0.0
    oracle_ratio = 0.0

    # Choose the repair command
    pos_file = None
    jar_out_dir: Optional[str] = None
    internal_ddmax = False
    ddmax_oracle_cmd: Optional[list[str]] = None
    if algorithm == "erepair":
        base_format = format_key.split('_')[-1]
        if base_format in REGEX_FORMATS:
            category = REGEX_DIR_TO_CATEGORY.get(base_format, base_format)
            oracle_executable = f"python3 match_partial.py {category}"
        else:
            oracle_executable = PROJECT_PATHS.get(base_format)
        if not oracle_executable:
            print(f"[ERROR] No oracle executable for format {base_format}")
            return
        cmd = ["./erepair", oracle_executable, input_file, output_file]
    elif algorithm == "earley":
        base_format = format_key.split('_')[-1]
        if base_format in REGEX_FORMATS:
            category = REGEX_DIR_TO_CATEGORY.get(base_format, base_format)
            oracle_executable = f"re2-server:{category}"
        else:
            oracle_executable = PROJECT_PATHS.get(base_format)
        if not oracle_executable:
            print(f"[ERROR] No oracle executable for format {base_format}")
            return
        cmd = ["./earleyrepairer", oracle_executable, input_file, output_file]
    elif algorithm == "betamax":
        # Build top-K positives (K=20) from the corresponding mutation DB (e.g., mutated_files/single_date.db)
        K=TRAIN_K
        base_format = format_key.split('_')[-1]
        mutation_type = format_key.split('_')[0]  # e.g., "single"
        category = REGEX_DIR_TO_CATEGORY.get(base_format, base_format)
        runtime_learner = _runtime_betamax_learner()
        cache_learner = _cache_betamax_learner()
        # Benchmarks use the same learner for cache init and runtime repair.
        cache_path = _cache_path(base_format, cache_learner)
        if not os.path.exists(cache_path):
            cache_path = _cache_path(base_format, runtime_learner)
        pos_file = f"temp_pos_{id_}_{random.randint(0, 9999)}.txt"
        neg_file = f"temp_neg_{id_}_{random.randint(0, 9999)}.txt"
        seed_pos: list[str] = []
        seed_neg: list[str] = []
        mut_pos: list[str] = []
        mut_neg: list[str] = []
        if LSTAR_MUTATION_POOL:
            try:
                LSTAR_MUTATION_POOL.ensure_format(format_key)
            except Exception as pool_err:
                if not QUIET:
                    print(f"[WARN] (ID={id_}) Failed to prime mutation pool for {format_key}: {pool_err}")
            seed_pos = LSTAR_MUTATION_POOL.get_seed_positives(format_key)
            seed_neg = LSTAR_MUTATION_POOL.get_seed_negatives(format_key)
            mut_pos = LSTAR_MUTATION_POOL.get_mutation_positives(format_key)
            mut_neg = LSTAR_MUTATION_POOL.get_mutation_negatives(format_key)

        filtered_pos: list[str] = []
        if seed_pos:
            filtered_pos = [
                (s or "").rstrip("\n")
                for s in seed_pos
                if (s or "") != (original_text or "")
            ]
            if not QUIET:
                print(f"[DEBUG] (ID={id_}) Loaded {len(filtered_pos)} positives from seed pool (LOO applied).")

        try:
            if not filtered_pos:
                mdb_path = os.path.join("mutated_files", f"{format_key}.db")
                with sqlite3.connect(mdb_path) as conn2:
                    table_name = get_mutation_table_name(mdb_path, conn2)
                    cur2 = conn2.cursor()
                    cur2.execute(f"SELECT original_text FROM {table_name} ORDER BY LENGTH(original_text), id LIMIT {K}")
                    rows = cur2.fetchall()
                filtered_pos = [
                    (r[0] or "").rstrip("\n")
                    for r in rows
                    if (r[0] or "") != (original_text or "")
                ]
                if not QUIET:
                    print(f"[DEBUG] (ID={id_}) Loaded positives directly from DB: count={len(filtered_pos)}")
            if mut_pos:
                extra = [(s or "").rstrip("\n") for s in mut_pos]
                filtered_pos.extend(extra)
                if not QUIET:
                    print(f"[DEBUG] (ID={id_}) Added {len(extra)} mutation positives to pool.")
            with open(pos_file, "w", encoding="utf-8") as pf:
                for line in filtered_pos:
                    pf.write(line + "\n")
            pos_count = len(filtered_pos)
        except Exception as e:
            pos_count = 0
            try:
                with open(pos_file, "w", encoding="utf-8") as pf:
                    pass
                if not QUIET:
                    print(f"[DEBUG] (ID={id_}) LOO positives fallback: wrote empty pos file due to error: {e}")
            except Exception:
                pass

        # Build initial negatives from first 20 mutated_text (initial hypothesis)
        try:
            neg_lines: list[str] = []
            if seed_neg:
                neg_lines = [(s or "").rstrip("\n") for s in seed_neg]
            if mut_neg:
                extra_neg = [(s or "").rstrip("\n") for s in mut_neg]
                neg_lines.extend(extra_neg)
                if not QUIET:
                    print(f"[DEBUG] (ID={id_}) Added {len(extra_neg)} mutation negatives to pool.")
            if not neg_lines and BM_NEGATIVES_FROM_DB:
                mdb_path = os.path.join("mutated_files", f"{format_key}.db")
                with sqlite3.connect(mdb_path) as conn3:
                    table_name = get_mutation_table_name(mdb_path, conn3)
                    cur3 = conn3.cursor()
                    cur3.execute(f"SELECT mutated_text FROM {table_name} ORDER BY LENGTH(mutated_text), id LIMIT {K}")
                    rows = cur3.fetchall()
                neg_lines = [(r[0] or "").rstrip("\n") for r in rows]
            with open(neg_file, "w", encoding="utf-8") as nf:
                for line in neg_lines:
                    nf.write(line + "\n")
        except Exception as e:
            # Ensure the negatives file exists even if empty
            try:
                with open(neg_file, "w", encoding="utf-8") as nf:
                    pass
                if not QUIET:
                    print(f"[DEBUG] (ID={id_}) Negatives fallback: wrote empty neg file due to error: {e}")
            except Exception:
                pass

        # Use the native RE2 validator for regex benchmarks.
        attempts = 500
        oracle_override = os.environ.get("LSTAR_ORACLE_VALIDATOR")
        if oracle_override:
            oracle_cmd = oracle_override
        else:
            if base_format in REGEX_FORMATS:
                oracle_cmd = select_regex_oracle_arg(base_format, category)
            else:
                oracle_cmd = PROJECT_PATHS.get(base_format)
        cmd = build_betamax_cmd(
            engine="cpp",
            positives=pos_file,
            negatives=neg_file,
            cache_path=cache_path,
            category=category,
            broken_file=input_file,
            output_file=output_file,
            attempts=attempts,
            mutations=60,
            learner=runtime_learner,
            oracle_cmd=oracle_cmd,
        )
    elif algorithm == "ddmax" and base_format in REGEX_FORMATS:
        internal_ddmax = True
        category = REGEX_DIR_TO_CATEGORY.get(base_format, base_format)
        ddmax_oracle_cmd = oracle_cmd_from_env_or_default(select_regex_oracle_cmd(base_format, category))
        cmd = None
    else:
        jar_out_dir = os.path.join(REPAIR_OUTPUT_DIR, f"jar_{id_}_{random.randint(0, 9999)}")
        os.makedirs(jar_out_dir, exist_ok=True)
        cmd = [
            "java", "-jar", "./project/bin/erepair.jar",
            "-r", "-a", _jar_algorithm_name(algorithm),
            "-i", input_file,
            "-o", jar_out_dir,
        ]

    try:
        start_time = time.time()
        stdout = ""
        stderr = ""
        proc = None

        local_timeout = REPAIR_TIMEOUT
        if algorithm == "betamax":
            try:
                if os.environ.get("LSTAR_EC_TIMEOUT"):
                    local_timeout = int(os.environ["LSTAR_EC_TIMEOUT"])
            except Exception:
                pass

        if internal_ddmax:
            if not ddmax_oracle_cmd:
                raise RuntimeError("ddmax oracle command not configured")
            if not QUIET:
                print(f"[DEBUG] (ID={id_}, ALG={algorithm}) Running internal DDMax (deletions-only) with oracle: {' '.join(ddmax_oracle_cmd)}")
                print(f"[DEBUG] (ID={id_}, ALG={algorithm}) Timeout set to: {local_timeout}s")
            per_call = float(os.environ.get("DDMAX_PER_CALL_TIMEOUT", "2.0"))
            repaired_text, stats = ddmax_repair_by_deletion(
                broken_text=broken_text,
                oracle_cmd_prefix=ddmax_oracle_cmd,
                file_suffix=ext,
                timeout_s=float(local_timeout),
                per_call_timeout_s=per_call,
            )
            with open(output_file, "w", encoding="utf-8") as rf:
                rf.write(repaired_text)
            repair_time = time.time() - start_time
            return_code = 0
            timed_out = 1 if stats.timed_out else 0
            stdout = (
                f"*** Number of required oracle runs: {stats.total_runs} "
                f"correct: {stats.accepted_runs} incorrect: {stats.rejected_runs} incomplete: 0 ***\n"
            )
        else:
            if not QUIET:
                print(f"[DEBUG] (ID={id_}, ALG={algorithm}) Running command: {' '.join(str(x) for x in cmd)}")
            # Prepare environment for subprocess (ensure penalty pruning is applied in ec_runtime)
            env = dict(os.environ)
            if algorithm == "betamax":
                # Raise EC parse timeout per attempt unless overridden (set to 100s)
                env.setdefault("LSTAR_PARSE_TIMEOUT", os.environ.get("LSTAR_PARSE_TIMEOUT", "100.0"))
                env["BETAMAX_EMIT_METRICS"] = "1"
                cache_p = cache_path
                if not QUIET:
                    try:
                        sz = os.path.getsize(cache_p) if os.path.exists(cache_p) else 'NA'
                        print(f"[DEBUG] (ID={id_}, ALG={algorithm}) Cache path: {cache_p}, exists={os.path.exists(cache_p)}, size={sz}")
                    except Exception as _e:
                        print(f"[DEBUG] (ID={id_}, ALG={algorithm}) Cache stats unavailable: {cache_p}, err={_e}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            if not QUIET:
                print(f"[DEBUG] (ID={id_}, ALG={algorithm}) Timeout set to: {local_timeout}s")
            stdout, stderr = proc.communicate(timeout=local_timeout)
            repair_time = time.time() - start_time
            return_code = proc.returncode

        # Extract oracle info (optional)
        it_o, correct_runs, incorrect_runs, incomplete_runs = extract_oracle_info(stdout)
        if algorithm == "betamax":
            iterations = it_o
        else:
            iterations = it_o
            if algorithm == "erepair":
                oracle_time = extract_oracle_seconds_total(stdout)
                if repair_time > 0:
                    oracle_ratio = oracle_time / repair_time

        if not QUIET:
            print(f"--- STDOUT (ID={id_}) ---\n{stdout}\n")
            print(f"--- STDERR (ID={id_}) ---\n{stderr}\n")

        if (return_code == 0) and jar_out_dir:
            _materialize_jar_output(jar_out_dir, input_file, output_file)

        if (return_code == 0) and os.path.exists(output_file):
            # Read the repaired output
            with open(output_file, "r", encoding="utf-8") as rf:
                repaired_text = rf.read()

            # Validate the repaired file
            if validate_with_external_tool(output_file, format_key, algorithm):
                fixed = 1

            # Compute Levenshtein distances
            distance_broken_repaired = levenshtein_distance(broken_text, repaired_text)
            distance_original_repaired = levenshtein_distance(original_text, repaired_text)

    except subprocess.TimeoutExpired as e:
        print(f"[INFO] Repair timed out for entry ID={id_}, alg={algorithm}, timeout={local_timeout}s; marked not fixed.")
        timed_out = 1
        repair_time = float(local_timeout)
        stdout = _ensure_text(getattr(e, "output", None))
        stderr = _ensure_text(getattr(e, "stderr", None))
        try:
            if proc:
                proc.kill()
        except Exception:
            pass
        try:
            if proc:
                out2, err2 = proc.communicate(timeout=5)
                stdout += _ensure_text(out2)
                stderr += _ensure_text(err2)
        except Exception:
            pass
        return_code = proc.returncode if (proc and proc.returncode is not None) else -9
    except Exception as e:
        print(f"[ERROR] Repair failed for entry ID={id_}: {e}")
    finally:
        # Clean up temp files
        if os.path.exists(input_file):
            os.remove(input_file)
        if os.path.exists(output_file):
            os.remove(output_file)
        if jar_out_dir and os.path.exists(jar_out_dir):
            shutil.rmtree(jar_out_dir, ignore_errors=True)
        if pos_file and os.path.exists(pos_file):
            os.remove(pos_file)
        if 'neg_file' in locals() and neg_file and os.path.exists(neg_file):
            # Persist negatives for future refinement (append into per-format accumulator)
            try:
                os.makedirs("negative", exist_ok=True)
                accum_path = os.path.join("negative", f"accum_{base_format}.txt")
                with open(neg_file, "r", encoding="utf-8") as nf, open(accum_path, "a", encoding="utf-8") as af:
                    for line in nf:
                        af.write(line)
            except Exception:
                pass
            os.remove(neg_file)

    _update_results_row(
        cursor,
        conn,
        id_=id_,
        repaired_text=repaired_text,
        fixed=fixed,
        iterations=iterations,
        repair_time=repair_time,
        correct_runs=correct_runs,
        incorrect_runs=incorrect_runs,
        incomplete_runs=incomplete_runs,
        distance_original_broken=distance_original_broken,
        distance_broken_repaired=distance_broken_repaired,
        distance_original_repaired=distance_original_repaired,
        timed_out=timed_out,
        return_code=return_code,
        ec_time=ec_time,
        ec_ratio=ec_ratio,
        learn_time=learn_time,
        learn_ratio=learn_ratio,
        oracle_time=oracle_time,
        oracle_ratio=oracle_ratio,
    )


def rerun_repairs_for_selected_formats(db_path: str, selected_formats=None, max_workers=None):
    """
    Re-run (or run for the first time) repairs for the specified formats.
    If selected_formats is None, it will use all in DEFAULT_FORMATS.
    """
    if not selected_formats:
        selected_formats = DEFAULT_FORMATS

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    _ensure_results_schema(cursor)
    conn.commit()

    # Fetch entries for the desired formats
    cursor.execute("""
        SELECT id, format, file_id, corrupted_index, algorithm,
               original_text, broken_text, repaired_text, fixed,
               iterations, repair_time, correct_runs, incorrect_runs,
               incomplete_runs, distance_original_broken, distance_broken_repaired,
               distance_original_repaired
        FROM results
    """)
    entries = cursor.fetchall()
    if not QUIET:
        print(f"[INFO] Loaded {len(entries)} unfinished entries from DB")

    # Filter only those in the selected formats and allowed algorithms for regex formats
    def _allowed(row):
        fmt_key = row[1]     # e.g., "single_date"
        alg = row[4]         # algorithm column
        base_f = fmt_key.split('_')[-1]
        # Honor CLI --algorithms selection; skip entries not requested this run
        if alg not in REPAIR_ALGORITHMS:
            return False
        if base_f in REGEX_FORMATS and alg not in ("earley", "erepair", "betamax", "ddmax"):
            return False
        return fmt_key in selected_formats

    filtered_entries = [row for row in entries if _allowed(row)]

    if LIMIT_N:
        filtered_entries = filtered_entries[:LIMIT_N]
    if not QUIET:
        print(f"[INFO] Found {len(filtered_entries)} entries to (re)process.")

    def _worker(row):
        # Each thread uses its own connection to avoid SQLite locking issues
        thread_conn = sqlite3.connect(db_path, timeout=30)
        thread_cursor = thread_conn.cursor()
        try:
            repair_and_update_entry(thread_cursor, thread_conn, row)
        finally:
            thread_conn.close()

    if not max_workers:
        max_workers = os.cpu_count() or 4
    if not QUIET:
        print(f"[INFO] Starting ThreadPoolExecutor with max_workers={max_workers}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(_worker, filtered_entries)

    conn.close()
    print("[INFO] Repair process completed!")


# ------------------------------------------------------------------------------
# Main script flow
# ------------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Benchmark runner with resume support")
    parser.add_argument("--db", default=DATABASE_PATH, help="Path to results SQLite DB")
    parser.add_argument("--formats", nargs="+", choices=FORMAT_CHOICES,
                        help="Formats to include (default: regex formats only)")
    parser.add_argument("--mutations", nargs="+", default=MUTATION_TYPES,
                        help="Either mutation type names (e.g. 'double') or a numeric cap "
                             "that limits how many samples per format to load.")
    parser.add_argument("--algorithms", nargs="+", choices=ALL_ALGORITHMS, help="Override algorithms to run")
    parser.add_argument("--resume-only", action="store_true", help="Skip sample insertion, only resume unfinished repairs")
    parser.add_argument("--resume", action="store_true", help="Alias of --resume-only (also skips precompute)")
    parser.add_argument("--max-workers", type=int, help="Max parallel workers (default: cpu count)")
    parser.add_argument("--quiet", action="store_true", help="Reduce console output (suppress per-entry stdout/stderr)")
    parser.add_argument("--limit", type=int, help="Limit number of entries to (re)process")
    parser.add_argument("--pause-on-exit", action="store_true", help="Pause for keypress before exiting")
    parser.add_argument("--lstar-mutation-count", type=int, default=0,
                        help="Number of oracle-classified mutations to pre-generate per format for L* pools (0 disables).")
    parser.add_argument("--lstar-mutation-deterministic", action="store_true",
                        help="Use deterministic mutation enumeration (default: random sampling).")
    parser.add_argument("--lstar-mutation-seed", type=int,
                        help="Optional seed applied to the random sampler (ignored for deterministic mode).")
    args = parser.parse_args()

    # Allow "--mutations 20" style usage by separating numeric caps from
    # actual mutation-type tokens.
    sample_cap_override: int | None = None
    normalized_mutation_types: list[str] = []
    for token in args.mutations:
        if token.isdigit():
            sample_cap_override = int(token)
        else:
            normalized_mutation_types.append(token)
    if not normalized_mutation_types:
        normalized_mutation_types = MUTATION_TYPES.copy()
    args.mutations = normalized_mutation_types
    args.sample_cap = sample_cap_override

    # Apply runtime flags
    global QUIET, LIMIT_N, PAUSE_ON_EXIT
    QUIET = bool(args.quiet)
    LIMIT_N = args.limit
    PAUSE_ON_EXIT = bool(args.pause_on_exit)
    exe = cpp_bin_path()
    if not os.path.exists(exe):
        raise SystemExit(
            f"[ERROR] C++ betaMax binary not found: {exe}\n"
            "Build it first:\n"
            "  cmake -S betamax_cpp -B betamax_cpp/build -DCMAKE_BUILD_TYPE=Release\n"
            "  cmake --build betamax_cpp/build -j"
        )

    db_path = args.db
    if os.environ.get("BM_FRESH_DB", "0").lower() in ("1", "true", "yes") and not (args.resume_only or args.resume):
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"[INFO] Removed existing database '{db_path}' for a fresh run.")

    if args.algorithms:
        # override algorithms in-place
        REPAIR_ALGORITHMS[:] = args.algorithms
    # 1) Create or reuse the database
    create_database(db_path)

    # Precompute DFA caches for the C++ backend.
    if "betamax" in REPAIR_ALGORITHMS:
        os.makedirs(CACHE_ROOT, exist_ok=True)
        cache_learner = _cache_betamax_learner()
        runtime_learner = _runtime_betamax_learner()
        for mutation_type in (args.mutations if args.mutations else MUTATION_TYPES):
            for fmt in (args.formats if args.formats else DEFAULT_FORMATS):
                format_key = f"{mutation_type}_{fmt}"
                # Use shared cache per format across all bm_* (single/double/triple)
                cache_path = _cache_path(fmt, cache_learner)
                if _cpp_dfa_cache_ready(cache_path):
                    continue
                # Pick a source DB for precompute: prefer env LSTAR_CACHE_SOURCE_MUTATION, else fallback to single/double/triple
                preferred = os.environ.get("LSTAR_CACHE_SOURCE_MUTATION", "single")
                mutation_db_path = None
                for src in [preferred, "single", "double", "triple"]:
                    cand = os.path.join("mutated_files", f"{src}_{fmt}.db")
                    if os.path.exists(cand):
                        mutation_db_path = cand
                        break
                if not mutation_db_path:
                    continue
                try:
                    pos_file = f"temp_pos_cache_{format_key}_{random.randint(0,9999)}.txt"
                    neg_file = f"temp_neg_cache_{format_key}_{random.randint(0,9999)}.txt"
                    pre_k = int(os.environ.get("LSTAR_PRECOMP_K", str(TRAIN_K)))
                    connc = sqlite3.connect(mutation_db_path)
                    curc = connc.cursor()
                    table_name = get_mutation_table_name(mutation_db_path, connc)
                    curc.execute(f"SELECT original_text FROM {table_name} ORDER BY LENGTH(original_text), id LIMIT {pre_k}")
                    rows = curc.fetchall()
                    with open(pos_file, "w", encoding="utf-8") as pf:
                        for r in rows:
                            line = (r[0] or "").rstrip("\n")
                            pf.write(line + "\n")
                    with open(neg_file, "w", encoding="utf-8") as nf:
                        if BM_NEGATIVES_FROM_DB:
                            curc.execute(f"SELECT mutated_text FROM {table_name} ORDER BY LENGTH(mutated_text), id LIMIT {pre_k}")
                            rows = curc.fetchall()
                            for r in rows:
                                line = (r[0] or "").rstrip("\n")
                                nf.write(line + "\n")
                    connc.close()
                    category = REGEX_DIR_TO_CATEGORY.get(fmt, fmt)
                    oracle_override = os.environ.get("LSTAR_ORACLE_VALIDATOR")
                    if oracle_override:
                        oracle_cmd = oracle_override
                    else:
                        if fmt in REGEX_FORMATS:
                            oracle_cmd = select_regex_oracle_arg(fmt, category)
                        else:
                            oracle_cmd = PROJECT_PATHS.get(fmt)
                    learner_pre = cache_learner
                    pre_mut = int(os.environ.get("LSTAR_PRECOMPUTE_MUTATIONS", "60"))

                    exe = cpp_bin_path()
                    cmd = [
                        exe,
                        "--positives", pos_file,
                        "--negatives", neg_file,
                        "--category", category,
                        "--dfa-cache", cache_path,
                        "--init-cache",
                        "--learner", learner_pre,
                        "--repo-root", ".",
                        "--incremental",
                    ]
                    if oracle_cmd:
                        cmd += ["--oracle-validator", oracle_cmd]
                    if os.environ.get("LSTAR_EQ_DISABLE_SAMPLING", "").lower() in ("1", "true", "yes"):
                        cmd += ["--eq-disable-sampling"]
                    if os.environ.get("LSTAR_EQ_MAX_LENGTH"):
                        cmd += ["--eq-max-length", os.environ["LSTAR_EQ_MAX_LENGTH"]]
                    if os.environ.get("LSTAR_EQ_SAMPLES_PER_LENGTH"):
                        cmd += ["--eq-samples-per-length", os.environ["LSTAR_EQ_SAMPLES_PER_LENGTH"]]
                    if os.environ.get("LSTAR_EQ_MAX_ORACLE"):
                        cmd += ["--eq-max-oracle", os.environ["LSTAR_EQ_MAX_ORACLE"]]
                    if os.environ.get("LSTAR_EQ_MAX_ROUNDS"):
                        cmd += ["--eq-max-rounds", os.environ["LSTAR_EQ_MAX_ROUNDS"]]
                    if pre_mut > 0:
                        cmd += ["--mutations", str(pre_mut)]
                    print(f"[DEBUG] Precompute cache for {format_key}: {' '.join(cmd)} (K={pre_k})")
                    pre_tmo = int(os.environ.get("LSTAR_PRECOMPUTE_TIMEOUT", "600"))
                    env = dict(os.environ)
                    _t0 = time.time()
                    run_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                    # Allow disabling timeout via LSTAR_PRECOMPUTE_TIMEOUT=0
                    if pre_tmo > 0:
                        run_kwargs["timeout"] = pre_tmo
                    _res = subprocess.run(cmd, **run_kwargs)
                    try:
                        _sz = os.path.getsize(cache_path) if os.path.exists(cache_path) else "NA"
                        _dt = time.time() - _t0
                        print(f"[INFO] Precompute cache for {format_key} finished in {_dt:.2f}s, size={_sz}")
                    except Exception:
                        pass
                except subprocess.TimeoutExpired:
                    if _cpp_dfa_cache_ready(cache_path):
                        print(f"[WARN] Precompute timeout for {format_key}, but cache is ready at {cache_path}; using existing cache.")
                        continue
                    fallback_mut = int(os.environ.get(
                        "LSTAR_PRECOMPUTE_MUTATIONS_FALLBACK",
                        str(max(0, min(10, pre_mut // 2))),
                    ))
                    if fallback_mut == pre_mut:
                        print(f"[WARN] Precompute timeout for {format_key}; no smaller mutation fallback configured, skipping.")
                        continue
                    print(f"[WARN] Precompute timeout for {format_key} (mutations={pre_mut}). Retrying with mutations={fallback_mut} ...")
                    try:
                        retry_cmd = _cmd_with_option_value(cmd, "--mutations", str(fallback_mut) if fallback_mut > 0 else None)
                        subprocess.run(retry_cmd, **run_kwargs)
                        try:
                            _sz = os.path.getsize(cache_path) if os.path.exists(cache_path) else "NA"
                            print(f"[INFO] Precompute retry for {format_key} finished, size={_sz}")
                        except Exception:
                            pass
                    except subprocess.TimeoutExpired:
                        print(f"[WARN] Precompute second timeout for {format_key}, skipping.")
                    except Exception as e2:
                        print(f"[WARN] Precompute retry failed for {format_key}: {e2}")
                except Exception as e:
                    print(f"[WARN] Precompute failed for {format_key}: {e}")
                finally:
                    try:
                        if os.path.exists(pos_file): os.remove(pos_file)
                    except Exception:
                        pass
                    try:
                        if os.path.exists(neg_file): os.remove(neg_file)
                    except Exception:
                        pass


    # 2) Optionally insert tasks (idempotent)
    if not (args.resume_only or args.resume):
        for mutation_type in args.mutations:
            for fmt in (args.formats if args.formats else DEFAULT_FORMATS):
                # Construct DB path, e.g., mutated_files/double_dot.db
                db_name = f"{mutation_type}_{fmt}.db"
                mutation_db_path = os.path.join("mutated_files", db_name)

                if not os.path.exists(mutation_db_path):
                    print(f"[INFO] Skipping, not found: {mutation_db_path}")
                    continue

                print(f"[INFO] Loading samples from {mutation_db_path}")
                samples = load_test_samples_from_db(mutation_db_path)

                total_samples = len(samples)
                if not total_samples:
                    print(f"[INFO] No samples found in '{mutation_db_path}'")
                    continue

                # Use first TRAIN_K for learning grammar (L*), and next TEST_K as test set.
                # Respect an optional numeric cap (from \"--mutations 20\") if provided.
                cap = args.sample_cap
                if cap is not None:
                    budget = min(cap, total_samples)
                else:
                    max_budget = TRAIN_K + TEST_K
                    if max_budget <= 0 or total_samples <= max_budget:
                        budget = total_samples
                    else:
                        budget = max_budget

                limited = samples[:budget]
                train_limit = min(TRAIN_K, len(limited)) if cap is None else min(TRAIN_K, len(limited))
                remaining = len(limited) - train_limit

                if remaining <= 0:
                    train_limit = 0
                    remaining = len(limited)

                if cap is not None:
                    test_limit = remaining
                else:
                    test_limit = min(TEST_K, remaining) if TEST_K > 0 else remaining
                    if test_limit <= 0:
                        test_limit = remaining

                test_samples = limited[train_limit:train_limit + test_limit]
                if LIMIT_N is not None and LIMIT_N > 0:
                    test_samples = test_samples[:LIMIT_N]

                if test_samples:
                    # Insert each test sample into the 'results' table for each algorithm
                    format_key = f"{mutation_type}_{fmt}"
                    effective_train = train_limit
                    effective_test = len(test_samples)
                    print(f"[INFO] Inserting {effective_test} test samples "
                          f"(train_used={effective_train}, test_used={effective_test}) for {format_key}")
                    insert_test_samples_to_db(db_path, format_key, test_samples)
                else:
                    print(f"[INFO] No samples found in '{mutation_db_path}'")

    # 3) Resume/Run unfinished repairs
    formats_for_rerun = [f"{m}_{f}" for m in args.mutations for f in (args.formats if args.formats else DEFAULT_FORMATS)]

    if LSTAR_MUTATION_POOL:
        LSTAR_MUTATION_POOL.set_mutation_config(
            args.lstar_mutation_count,
            not args.lstar_mutation_deterministic,
            args.lstar_mutation_seed
        )
        LSTAR_MUTATION_POOL.ensure_many(formats_for_rerun)

    rerun_repairs_for_selected_formats(db_path, selected_formats=formats_for_rerun, max_workers=args.max_workers)

    if PAUSE_ON_EXIT:
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
