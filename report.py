#!/usr/bin/env python3
"""
metrics_analysis_updated.py
----------------------------------
Compute summary tables (Tables 4–8) from the *results* table produced by
`update_repairs_method.py`.

The schema expected in each `results` table is:
    id INTEGER PRIMARY KEY,
    format TEXT,
    fid INTEGER,
    cidx INTEGER,
    algorithm TEXT,
    original_text TEXT,
    broken_text   TEXT,
    repaired_text TEXT,
    fixed INTEGER,                      -- 0 | 1
    iterations INTEGER,
    repair_time REAL,                   -- seconds
    correct_runs INTEGER,
    incorrect_runs INTEGER,
    incomplete_runs INTEGER,
    distance_original_broken   INTEGER,
    distance_broken_repaired   INTEGER,
    distance_original_repaired INTEGER

By default three DBs are analysed (single / double / truncated corruptions).
Modify `DATABASES` as needed.
"""

from __future__ import annotations
import sqlite3, math, sys, os
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".mplconfig"))

def _load_pyplot():
    try:
        import matplotlib
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "matplotlib is required for report plots. Install dependencies with "
            "'python -m pip install -r requirements.txt'."
        ) from exc
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt

# ───────────────────────────────── CONFIG ────────────────────────────────── #
DATABASES       = ["single.db", "double.db", "triple.db"]  # list of DB files to analyse
BETAMAX_ALGORITHMS = ("betamax",)  # alias list for BETAMAX/non-mutated runs
BETAMAX_MUTATION_DB_PAIRS = [
    ("single_non_mutated.db", "single.db"),
    ("double_non_mutated.db", "double.db"),
    ("triple_non_mutated.db", "triple.db"),
]
BETAMAX_ABLATION_DBS = [
    # Legacy override: list of (db_path, K). If empty, report uses the
    # template-based config below.
]
BETAMAX_ABLATION_KS = [50, 25, 12, 6]
BETAMAX_ABLATION_MODES = ["single", "double", "triple"]
BETAMAX_ABLATION_DB_TEMPLATE = "{mode}_k{K}.db"
BETAMAX_PRECOMP_MUT_ABLATION_MS = [0, 20, 40, 60, 80, 100]
BETAMAX_PRECOMP_MUT_ABLATION_MODES = ["single", "double", "triple"]
BETAMAX_PRECOMP_MUT_ABLATION_DB_TEMPLATE = "{mode}_m{M}.db"
BETAMAX_XOVER_ABLATION_CHECKS = [0, 1, 2, 3]
BETAMAX_XOVER_ABLATION_PAIRS = [50]
BETAMAX_XOVER_ABLATION_MODES = ["single", "double", "triple"]
BETAMAX_XOVER_ABLATION_DB_TEMPLATE = "{mode}_xcheck{checks}.db"
BETAMAX_LEARNER_DB_PAIRS = [
    ("double_rpni.db", "rpni"),
    ("double_rpni_xover.db", "rpni_xover"),
]
DEFAULT_TIMEOUT = 300  # seconds; a run counts as success only if repair_time <= DEFAULT_TIMEOUT
# ─────────────────────────────────────────────────────────────────────────── #

# Helpers ─────────────────────────────────────────────────────────────────── #

def _to_numeric_list(vals: List[Any]) -> List[float]:
    """Return a list containing *only* numeric values, safely converted to float."""
    res: List[float] = []
    for v in vals:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            res.append(float(v))
            continue
        # Handle strings that might represent numbers
        if isinstance(v, str):
            try:
                res.append(float(v.strip()))
            except ValueError:
                continue
    return res

def _stats(vals: List[Any]) -> Tuple[float, float]:
    """Return (mean, stdev) computed on *numeric* subset of *vals*."""
    nums = _to_numeric_list(vals)
    n    = len(nums)
    if n == 0:
        return 0.0, 0.0
    mu   = sum(nums) / n
    if n == 1:
        return mu, 0.0
    return mu, math.sqrt(sum((x - mu) ** 2 for x in nums) / n)

def _is_success(fixed: Any, repair_time: Any) -> bool:
    """A run is considered successful only if it fixed within DEFAULT_TIMEOUT."""
    if not bool(fixed):
        return False
    if repair_time is None:
        return False
    try:
        rt = float(repair_time)
    except (TypeError, ValueError):
        return False
    return rt <= float(DEFAULT_TIMEOUT)

def _time_or_timeout(fixed: Any, repair_time: Any) -> float:
    """
    Return repair_time for successful runs; otherwise count as DEFAULT_TIMEOUT.
    This treats timeouts and non-repaired cases as taking the full timeout.
    """
    if _is_success(fixed, repair_time):
        try:
            return float(repair_time)
        except (TypeError, ValueError):
            return float(DEFAULT_TIMEOUT)
    return float(DEFAULT_TIMEOUT)

def _recovery_ratio(original_text: str, repaired_text: str) -> float:
    """
    Recovery = mean((L - d) / L), where L=len(original) and d is deleted data length.
    We compute d as the number of delete operations in the Levenshtein edit script.
    """
    L = len(original_text)
    if L == 0:
        return 1.0
    _, dels, _, _ = edit_distance_with_ops(original_text, repaired_text)
    return (L - dels) / L

# Levenshtein with op‑counts -------------------------------------------------- #

def edit_distance_with_ops(a: str, b: str) -> Tuple[int, int, int, int]:
    m, n = len(a), len(b)
    dp   = [[0] * (n + 1) for _ in range(m + 1)]
    op   = [["M"] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dp[i][0] = i; op[i][0] = "D"
    for j in range(1, n + 1):
        dp[0][j] = j; op[0][j] = "I"
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                d, ins, r = dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + 1
                best      = min(d, ins, r)
                dp[i][j]  = best
                op[i][j]  = "DIR"[[d, ins, r].index(best)]
    i = m; j = n; dels = ins = reps = 0
    while i > 0 or j > 0:
        cur = op[i][j]
        if cur == "M":
            i -= 1; j -= 1
        elif cur == "D":
            dels += 1; i -= 1
        elif cur == "I":
            ins  += 1; j -= 1
        else:
            reps += 1; i -= 1; j -= 1
    return dp[m][n], dels, ins, reps

# DB query helper ------------------------------------------------------------- #

def _q(conn: sqlite3.Connection, sql: str, params: Tuple[Any, ...] = ()):  # noqa: D401
    cur = conn.cursor(); cur.execute(sql, params); return cur.fetchall()

# Table 4‑5 general ----------------------------------------------------------- #

def table_4_5_general():
    combined: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for db in DATABASES:
        if not Path(db).is_file():
            print(f"[WARN] Skipping missing DB {db}", file=sys.stderr);
            continue
        conn = sqlite3.connect(db)
        rows = _q(conn, """
            SELECT format, algorithm, distance_broken_repaired, distance_original_repaired,
                   fixed, repair_time, original_text, broken_text, repaired_text
            FROM results""")
        conn.close()
        print(f"\nMetrics for {db}")
        _print_metrics(_aggregate(rows))
        # merge
        for fmt, alg, dbr, dor, fixed, repair_time, original_text, broken_text, repaired_text in rows:
            key = (fmt, alg)
            bucket = combined.setdefault(
                key,
                {"dbr": [], "dor": [], "rt": [], "rt_all": [], "rec_or": [], "rec_br": [], "succ": 0, "tot": 0},
            )
            bucket["tot"] += 1
            bucket["rt_all"].append(_time_or_timeout(fixed, repair_time))
            if _is_success(fixed, repair_time):
                bucket["succ"] += 1
                bucket["dbr"].append(dbr)
                bucket["dor"].append(dor)
                bucket["rt"].append(repair_time)
                if original_text is not None and repaired_text is not None:
                    bucket["rec_or"].append(_recovery_ratio(original_text, repaired_text))
                if broken_text is not None and repaired_text is not None:
                    bucket["rec_br"].append(_recovery_ratio(broken_text, repaired_text))
    print("\nCombined Metrics Across All Databases")
    _print_metrics(combined)


def _aggregate(rows):
    data: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for fmt, alg, dbr, dor, fixed, repair_time, original_text, broken_text, repaired_text in rows:
        key = (fmt, alg)
        bucket = data.setdefault(
            key,
            {"dbr": [], "dor": [], "rt": [], "rt_all": [], "rec_or": [], "rec_br": [], "succ": 0, "tot": 0},
        )
        bucket["tot"] += 1
        bucket["rt_all"].append(_time_or_timeout(fixed, repair_time))
        if _is_success(fixed, repair_time):
            bucket["succ"] += 1
            bucket["dbr"].append(dbr)
            bucket["dor"].append(dor)
            bucket["rt"].append(repair_time)
            if original_text is not None and repaired_text is not None:
                bucket["rec_or"].append(_recovery_ratio(original_text, repaired_text))
            if broken_text is not None and repaired_text is not None:
                bucket["rec_br"].append(_recovery_ratio(broken_text, repaired_text))
    return data


def _print_metrics(data):
    hdr = f"{'Format':<8} {'Alg':<8} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} "\
          f"{'σ OR':>8} {'Avg RecO':>8} {'σ RecO':>8} {'Avg RecB':>8} {'σ RecB':>8} {'Avg t':>8} {'σ t':>8} "\
          f"{'Avg t*':>8} {'σ t*':>8} {'Succ':>6} {'Tot':>6}"
    print("-" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for (fmt, alg), b in sorted(data.items()):
        abr, sbr = _stats(b["dbr"])
        aor, sor = _stats(b["dor"])
        arec_or, srec_or = _stats(b.get("rec_or", []))
        arec_br, srec_br = _stats(b.get("rec_br", []))
        art, srt = _stats(b["rt"])
        art_all, srt_all = _stats(b.get("rt_all", []))
        print(f"{fmt:<8} {alg:<8} {abr:8.2f} {sbr:8.2f} {aor:8.2f} {sor:8.2f} {arec_or:8.4f} {srec_or:8.4f} {arec_br:8.4f} {srec_br:8.4f} "\
              f"{art:8.2f} {srt:8.2f} {art_all:8.2f} {srt_all:8.2f} {b['succ']:6d} {b['tot']:6d}")

# 4‑5 distances --------------------------------------------------------------- #

def table_4_5_distances():
    data: Dict[str, Dict[str, List[Any]]] = {}
    for db in DATABASES:
        if not Path(db).is_file():
            continue
        conn = sqlite3.connect(db)
        rows = _q(
            conn,
            "SELECT algorithm, distance_broken_repaired, distance_original_repaired, "
            "       original_text, broken_text, repaired_text "
            "FROM results "
            "WHERE fixed=1 AND repair_time IS NOT NULL AND repair_time <= ? AND repaired_text IS NOT NULL",
            (DEFAULT_TIMEOUT,),
        )
        conn.close()
        for alg, dbr, dor, original_text, broken_text, repaired_text in rows:
            bucket = data.setdefault(alg, {"dbr": [], "dor": [], "rec_or": [], "rec_br": []})
            bucket["dbr"].append(dbr); bucket["dor"].append(dor)
            if original_text is not None:
                bucket["rec_or"].append(_recovery_ratio(original_text, repaired_text))
            if broken_text is not None:
                bucket["rec_br"].append(_recovery_ratio(broken_text, repaired_text))
    print("\nOverall Distance Metrics Across All Databases")
    print(f"{'Alg':<8} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecO':>9} {'σ RecO':>9} {'Avg RecB':>9} {'σ RecB':>9}")
    print("-" * 86)
    for alg, vals in data.items():
        abr, sbr = _stats(vals["dbr"]); aor, sor = _stats(vals["dor"])
        arec_or, srec_or = _stats(vals.get("rec_or", []))
        arec_br, srec_br = _stats(vals.get("rec_br", []))
        print(f"{alg:<8} {abr:8.2f} {sbr:8.2f} {aor:8.2f} {sor:8.2f} {arec_or:9.4f} {srec_or:9.4f} {arec_br:9.4f} {srec_br:9.4f}")

# 6: fixed counts ------------------------------------------------------------- #

def table_6_count_fixed():
    label_map = {
        "single.db": "1-mutation",
        "double.db": "2-mutations",
        "triple.db": "3-mutations",
    }
    overall_by_format: Dict[Tuple[str, str], int] = {}
    overall_by_alg: Dict[str, int] = {}
    for db in DATABASES:
        if not Path(db).is_file():
            continue
        conn = sqlite3.connect(db)
        rows = _q(conn,
                  "SELECT format, algorithm, COUNT(*) FROM results "
                  "WHERE fixed=1 AND repair_time IS NOT NULL AND repair_time <= ? "
                  "GROUP BY format, algorithm",
                  (DEFAULT_TIMEOUT,))
        conn.close()
        label = label_map.get(db, db)
        print(f"\nRepaired counts by format in {db} ({label}, repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
        if not rows:
            print("  None")
            continue
        hdr = f"{'Format':<12} {'Alg':<12} {'Repaired':>10}"
        print("-" * len(hdr))
        print(hdr)
        print("-" * len(hdr))
        for fmt, alg, cnt in sorted(rows):
            overall_by_format[(fmt, alg)] = overall_by_format.get((fmt, alg), 0) + int(cnt)
            overall_by_alg[alg] = overall_by_alg.get(alg, 0) + int(cnt)
            print(f"{fmt:<12} {alg:<12} {int(cnt):10d}")

    print(f"\nOverall repaired counts by format across DBs (repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
    if not overall_by_format:
        print("  None")
    else:
        hdr = f"{'Format':<12} {'Alg':<12} {'Repaired':>10}"
        print("-" * len(hdr))
        print(hdr)
        print("-" * len(hdr))
        for (fmt, alg), cnt in sorted(overall_by_format.items()):
            print(f"{fmt:<12} {alg:<12} {cnt:10d}")

    print(f"\nOverall repaired counts by algorithm across DBs (repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
    if not overall_by_alg:
        print("  None")
    else:
        for alg, cnt in sorted(overall_by_alg.items()):
            print(f"  {alg:<12} {cnt:10d}")

# 7: perfect repairs ---------------------------------------------------------- #

def table_7_perfect():
    overall_by_format: Dict[Tuple[str, str], int] = {}
    overall_by_alg: Dict[str, int] = {}
    overall_perfect_inputs_by_alg: Dict[str, int] = {}
    overall_total_by_alg: Dict[str, int] = {}
    for db in DATABASES:
        if not Path(db).is_file():
            continue
        conn = sqlite3.connect(db)
        rows = _q(conn,
                  "SELECT format, algorithm, fixed, repair_time, "
                  "       distance_original_broken, distance_original_repaired "
                  "FROM results")
        conn.close()
        print(f"\nPerfect repairs in {db}")
        per_db: Dict[Tuple[str, str], int] = {}
        any_rows = False
        for fmt, alg, fixed, repair_time, dob, dor in rows:
            overall_total_by_alg[alg] = overall_total_by_alg.get(alg, 0) + 1
            if dob == 0:
                overall_perfect_inputs_by_alg[alg] = overall_perfect_inputs_by_alg.get(alg, 0) + 1
            if dor == 0 and _is_success(fixed, repair_time):
                any_rows = True
                per_db[(fmt, alg)] = per_db.get((fmt, alg), 0) + 1
                overall_by_format[(fmt, alg)] = overall_by_format.get((fmt, alg), 0) + 1
                overall_by_alg[alg] = overall_by_alg.get(alg, 0) + 1

        if not any_rows:
            print("  None")
        else:
            for (fmt, alg), cnt in sorted(per_db.items()):
                print(f"  {fmt:<12} {alg:<12} {cnt:6d}")

    print("\nOverall perfect repairs by format across DBs")
    if not overall_by_format:
        print("  None")
    else:
        hdr = f"{'Format':<12} {'Alg':<12} {'Perfect':>8}"
        print("-" * len(hdr))
        print(hdr)
        print("-" * len(hdr))
        for (fmt, alg), cnt in sorted(overall_by_format.items()):
            print(f"{fmt:<12} {alg:<12} {cnt:8d}")

    print("\nOverall perfect repairs by algorithm across DBs")
    if not overall_by_alg:
        print("  None")
    else:
        for alg, cnt in sorted(overall_by_alg.items()):
            print(f"  {alg:<12} {cnt:8d}")

    print("\nPerfect inputs by algorithm across DBs (distance_original_broken=0)")
    if not overall_total_by_alg:
        print("  None")
    else:
        hdr = f"{'Alg':<12} {'PerfectIn':>10} {'Total':>10} {'Pct':>8}"
        print("-" * len(hdr))
        print(hdr)
        print("-" * len(hdr))
        for alg in sorted(overall_total_by_alg.keys()):
            perfect_in = overall_perfect_inputs_by_alg.get(alg, 0)
            total = overall_total_by_alg[alg]
            pct = (perfect_in / total * 100.0) if total else 0.0
            print(f"{alg:<12} {perfect_in:10d} {total:10d} {pct:7.2f}%")

# 8: efficiency --------------------------------------------------------------- #

def table_8_efficiency():
    # Efficiency stats (per successful run), restricted to runs with:
    #   fixed=1 AND repair_time <= DEFAULT_TIMEOUT
    # - oracle runs: COALESCE(iterations, 0)
    # - time: repair_time
    overall_by_format_it: Dict[Tuple[str, str], List[float]] = {}
    overall_by_format_t: Dict[Tuple[str, str], List[float]] = {}
    overall_by_format_t_all: Dict[Tuple[str, str], List[float]] = {}
    overall_by_format_oracle_time: Dict[Tuple[str, str], List[float]] = {}
    overall_by_format_ec_time: Dict[Tuple[str, str], List[float]] = {}
    overall_by_format_learn_time: Dict[Tuple[str, str], List[float]] = {}
    overall_by_alg_it: Dict[str, List[float]] = {}
    overall_by_alg_t: Dict[str, List[float]] = {}
    overall_by_alg_t_all: Dict[str, List[float]] = {}
    overall_by_alg_oracle_time: Dict[str, List[float]] = {}
    overall_by_alg_ec_time: Dict[str, List[float]] = {}
    overall_by_alg_learn_time: Dict[str, List[float]] = {}

    # "Regex format" view: collapse single_/double_/triple_ prefixes so the
    # six formats are: date, time, url, isbn, ipv4, ipv6.
    regex_formats = ("date", "time", "url", "isbn", "ipv4", "ipv6")
    regex_set = set(regex_formats)
    overall_regex_by_format_it: Dict[Tuple[str, str], List[float]] = {}
    overall_regex_by_format_t: Dict[Tuple[str, str], List[float]] = {}
    overall_regex_by_format_t_all: Dict[Tuple[str, str], List[float]] = {}
    overall_regex_by_format_oracle_time: Dict[Tuple[str, str], List[float]] = {}
    overall_regex_by_format_ec_time: Dict[Tuple[str, str], List[float]] = {}
    overall_regex_by_format_learn_time: Dict[Tuple[str, str], List[float]] = {}

    def _to_regex_format(fmt: Any) -> Optional[str]:
        if not isinstance(fmt, str):
            return None
        base = fmt.rsplit("_", 1)[-1]
        return base if base in regex_set else None

    for db in DATABASES:
        if not Path(db).is_file():
            continue
        conn = sqlite3.connect(db)
        cols = {row[1] for row in _q(conn, "PRAGMA table_info(results)")}
        sel_oracle_time = "oracle_time" if "oracle_time" in cols else "NULL"
        sel_ec_time = "ec_time" if "ec_time" in cols else "NULL"
        sel_learn_time = "learn_time" if "learn_time" in cols else "NULL"
        rows = _q(
            conn,
            f"SELECT format, algorithm, COALESCE(iterations, 0), repair_time, "
            f"{sel_oracle_time}, {sel_ec_time}, {sel_learn_time} "
            "FROM results "
            "WHERE fixed=1 AND repair_time IS NOT NULL AND repair_time <= ?",
            (DEFAULT_TIMEOUT,),
        )
        rows_all = _q(
            conn,
            "SELECT format, algorithm, fixed, repair_time FROM results",
        )
        conn.close()

        print(f"\nAverage efficiency by format in {db} (fixed=1, repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
        by_format_it: Dict[Tuple[str, str], List[float]] = {}
        by_format_t: Dict[Tuple[str, str], List[float]] = {}
        by_format_t_all: Dict[Tuple[str, str], List[float]] = {}
        by_format_oracle_time: Dict[Tuple[str, str], List[float]] = {}
        by_format_ec_time: Dict[Tuple[str, str], List[float]] = {}
        by_format_learn_time: Dict[Tuple[str, str], List[float]] = {}
        by_alg_it: Dict[str, List[float]] = {}
        by_alg_t: Dict[str, List[float]] = {}
        by_alg_t_all: Dict[str, List[float]] = {}
        by_alg_oracle_time: Dict[str, List[float]] = {}
        by_alg_ec_time: Dict[str, List[float]] = {}
        by_alg_learn_time: Dict[str, List[float]] = {}
        for fmt, alg, it, rt, oracle_time, ec_time, learn_time in rows:
            itf = float(it or 0)
            rtf = float(rt) if rt is not None else 0.0
            by_format_it.setdefault((fmt, alg), []).append(itf)
            by_format_t.setdefault((fmt, alg), []).append(rtf)
            by_alg_it.setdefault(alg, []).append(itf)
            by_alg_t.setdefault(alg, []).append(rtf)
            overall_by_format_it.setdefault((fmt, alg), []).append(itf)
            overall_by_format_t.setdefault((fmt, alg), []).append(rtf)
            overall_by_alg_it.setdefault(alg, []).append(itf)
            overall_by_alg_t.setdefault(alg, []).append(rtf)

            rfmt = _to_regex_format(fmt)
            if rfmt is not None:
                overall_regex_by_format_it.setdefault((rfmt, alg), []).append(itf)
                overall_regex_by_format_t.setdefault((rfmt, alg), []).append(rtf)

            if oracle_time is not None:
                ot = float(oracle_time or 0.0)
                by_format_oracle_time.setdefault((fmt, alg), []).append(ot)
                by_alg_oracle_time.setdefault(alg, []).append(ot)
                overall_by_format_oracle_time.setdefault((fmt, alg), []).append(ot)
                overall_by_alg_oracle_time.setdefault(alg, []).append(ot)
                if rfmt is not None:
                    overall_regex_by_format_oracle_time.setdefault((rfmt, alg), []).append(ot)
            if ec_time is not None:
                et = float(ec_time or 0.0)
                by_format_ec_time.setdefault((fmt, alg), []).append(et)
                by_alg_ec_time.setdefault(alg, []).append(et)
                overall_by_format_ec_time.setdefault((fmt, alg), []).append(et)
                overall_by_alg_ec_time.setdefault(alg, []).append(et)
                if rfmt is not None:
                    overall_regex_by_format_ec_time.setdefault((rfmt, alg), []).append(et)
            if learn_time is not None:
                lt = float(learn_time or 0.0)
                by_format_learn_time.setdefault((fmt, alg), []).append(lt)
                by_alg_learn_time.setdefault(alg, []).append(lt)
                overall_by_format_learn_time.setdefault((fmt, alg), []).append(lt)
                overall_by_alg_learn_time.setdefault(alg, []).append(lt)
                if rfmt is not None:
                    overall_regex_by_format_learn_time.setdefault((rfmt, alg), []).append(lt)

        for fmt, alg, fixed, rt in rows_all:
            rtf_all = _time_or_timeout(fixed, rt)
            by_format_t_all.setdefault((fmt, alg), []).append(rtf_all)
            by_alg_t_all.setdefault(alg, []).append(rtf_all)
            overall_by_format_t_all.setdefault((fmt, alg), []).append(rtf_all)
            overall_by_alg_t_all.setdefault(alg, []).append(rtf_all)
            rfmt = _to_regex_format(fmt)
            if rfmt is not None:
                overall_regex_by_format_t_all.setdefault((rfmt, alg), []).append(rtf_all)

        if not by_format_it:
            print("  (no efficiency data)")
        else:
            hdr = f"{'Format':<12} {'Alg':<12} {'Avg it':>10} {'σ it':>10} "\
                  f"{'Avg t':>10} {'σ t':>10} "\
                  f"{'Avg t*':>10} {'σ t*':>10} "\
                  f"{'Avg orc_t':>10} {'σ orc_t':>10} "\
                  f"{'Avg ec':>10} {'σ ec':>10} "\
                  f"{'Avg learn':>10} {'σ learn':>10}"
            hdr = hdr.replace("Avg it", "Avg orc").replace("σ it", "σ orc")
            print("-" * len(hdr))
            print(hdr)
            print("-" * len(hdr))
            for (fmt, alg) in sorted(by_format_it.keys()):
                it_mu, it_sd = _stats(by_format_it[(fmt, alg)])
                t_mu, t_sd = _stats(by_format_t[(fmt, alg)])
                t_all_mu, t_all_sd = _stats(by_format_t_all.get((fmt, alg), []))
                ot_mu, ot_sd = _stats(by_format_oracle_time.get((fmt, alg), []))
                ec_mu, ec_sd = _stats(by_format_ec_time.get((fmt, alg), []))
                lt_mu, lt_sd = _stats(by_format_learn_time.get((fmt, alg), []))
                print(f"{fmt:<12} {alg:<12} {it_mu:10.2f} {it_sd:10.2f} "
                      f"{t_mu:10.2f} {t_sd:10.2f} "
                      f"{t_all_mu:10.2f} {t_all_sd:10.2f} "
                      f"{ot_mu:10.2f} {ot_sd:10.2f} "
                      f"{ec_mu:10.2f} {ec_sd:10.2f} "
                      f"{lt_mu:10.2f} {lt_sd:10.2f}")
            print(f"  t*: failures/timeouts counted as {DEFAULT_TIMEOUT:.0f}s")

        print(f"\nAverage efficiency (all formats) in {db} (fixed=1, repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
        if not by_alg_it:
            print("  (no efficiency data)")
        else:
            for alg in sorted(by_alg_it.keys()):
                orc_mu, orc_sd = _stats(by_alg_it[alg])
                t_mu, t_sd = _stats(by_alg_t[alg])
                t_all_mu, t_all_sd = _stats(by_alg_t_all.get(alg, []))
                ot_mu, ot_sd = _stats(by_alg_oracle_time.get(alg, []))
                ec_mu, ec_sd = _stats(by_alg_ec_time.get(alg, []))
                lt_mu, lt_sd = _stats(by_alg_learn_time.get(alg, []))
                print(
                    f"  {alg:<8} orc={orc_mu:10.2f}±{orc_sd:6.2f}  "
                    f"orc_t={ot_mu:10.2f}±{ot_sd:6.2f}s  "
                    f"ec={ec_mu:10.2f}±{ec_sd:6.2f}s  "
                    f"learn={lt_mu:10.2f}±{lt_sd:6.2f}s  "
                    f"t={t_mu:10.2f}±{t_sd:6.2f}s  "
                    f"t*={t_all_mu:10.2f}±{t_all_sd:6.2f}s"
                )
            print(f"  t*: failures/timeouts counted as {DEFAULT_TIMEOUT:.0f}s")

    print(f"\nOverall average efficiency by format across DBs (fixed=1, repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
    if not overall_by_format_it:
        print("  (no efficiency data)")
    else:
        hdr = f"{'Format':<12} {'Alg':<12} {'Avg it':>10} {'σ it':>10} "\
              f"{'Avg t':>10} {'σ t':>10} "\
              f"{'Avg t*':>10} {'σ t*':>10} "\
              f"{'Avg orc_t':>10} {'σ orc_t':>10} "\
              f"{'Avg ec':>10} {'σ ec':>10} "\
              f"{'Avg learn':>10} {'σ learn':>10}"
        hdr = hdr.replace("Avg it", "Avg orc").replace("σ it", "σ orc")
        print("-" * len(hdr))
        print(hdr)
        print("-" * len(hdr))
        for (fmt, alg) in sorted(overall_by_format_it.keys()):
            it_mu, it_sd = _stats(overall_by_format_it[(fmt, alg)])
            t_mu, t_sd = _stats(overall_by_format_t[(fmt, alg)])
            t_all_mu, t_all_sd = _stats(overall_by_format_t_all.get((fmt, alg), []))
            ot_mu, ot_sd = _stats(overall_by_format_oracle_time.get((fmt, alg), []))
            ec_mu, ec_sd = _stats(overall_by_format_ec_time.get((fmt, alg), []))
            lt_mu, lt_sd = _stats(overall_by_format_learn_time.get((fmt, alg), []))
            print(f"{fmt:<12} {alg:<12} {it_mu:10.2f} {it_sd:10.2f} "
                  f"{t_mu:10.2f} {t_sd:10.2f} "
                  f"{t_all_mu:10.2f} {t_all_sd:10.2f} "
                  f"{ot_mu:10.2f} {ot_sd:10.2f} "
                  f"{ec_mu:10.2f} {ec_sd:10.2f} "
                  f"{lt_mu:10.2f} {lt_sd:10.2f}")
        print(f"  t*: failures/timeouts counted as {DEFAULT_TIMEOUT:.0f}s")

    print(f"\nOverall average efficiency by regex format across DBs (mutations combined; fixed=1, repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
    if not overall_regex_by_format_it:
        print("  (no efficiency data)")
    else:
        hdr = f"{'Regex':<8} {'Alg':<12} {'Avg it':>10} {'σ it':>10} "\
              f"{'Avg t':>10} {'σ t':>10} "\
              f"{'Avg t*':>10} {'σ t*':>10} "\
              f"{'Avg orc_t':>10} {'σ orc_t':>10} "\
              f"{'Avg ec':>10} {'σ ec':>10} "\
              f"{'Avg learn':>10} {'σ learn':>10}"
        hdr = hdr.replace("Avg it", "Avg orc").replace("σ it", "σ orc")
        print("-" * len(hdr))
        print(hdr)
        print("-" * len(hdr))

        def _fmt_key(item: Tuple[str, str]) -> Tuple[int, str]:
            fmt, alg = item
            try:
                idx = regex_formats.index(fmt)
            except ValueError:
                idx = 999
            return idx, alg

        for (fmt, alg) in sorted(overall_regex_by_format_it.keys(), key=_fmt_key):
            it_mu, it_sd = _stats(overall_regex_by_format_it[(fmt, alg)])
            t_mu, t_sd = _stats(overall_regex_by_format_t[(fmt, alg)])
            t_all_mu, t_all_sd = _stats(overall_regex_by_format_t_all.get((fmt, alg), []))
            ot_mu, ot_sd = _stats(overall_regex_by_format_oracle_time.get((fmt, alg), []))
            ec_mu, ec_sd = _stats(overall_regex_by_format_ec_time.get((fmt, alg), []))
            lt_mu, lt_sd = _stats(overall_regex_by_format_learn_time.get((fmt, alg), []))
            print(f"{fmt:<8} {alg:<12} {it_mu:10.2f} {it_sd:10.2f} "
                  f"{t_mu:10.2f} {t_sd:10.2f} "
                  f"{t_all_mu:10.2f} {t_all_sd:10.2f} "
                  f"{ot_mu:10.2f} {ot_sd:10.2f} "
                  f"{ec_mu:10.2f} {ec_sd:10.2f} "
                  f"{lt_mu:10.2f} {lt_sd:10.2f}")
        print(f"  t*: failures/timeouts counted as {DEFAULT_TIMEOUT:.0f}s")

    print(f"\nOverall average oracle runs across DBs (all formats, fixed=1, repair_time ≤{DEFAULT_TIMEOUT:.0f}s)")
    if not overall_by_alg_it:
        print("  (no efficiency data)")
    else:
        for alg in sorted(overall_by_alg_it.keys()):
            orc_mu, orc_sd = _stats(overall_by_alg_it[alg])
            t_mu, t_sd = _stats(overall_by_alg_t[alg])
            t_all_mu, t_all_sd = _stats(overall_by_alg_t_all.get(alg, []))
            ot_mu, ot_sd = _stats(overall_by_alg_oracle_time.get(alg, []))
            ec_mu, ec_sd = _stats(overall_by_alg_ec_time.get(alg, []))
            lt_mu, lt_sd = _stats(overall_by_alg_learn_time.get(alg, []))
            print(
                f"  {alg:<8} orc={orc_mu:10.2f}±{orc_sd:6.2f}  "
                f"orc_t={ot_mu:10.2f}±{ot_sd:6.2f}s  "
                f"ec={ec_mu:10.2f}±{ec_sd:6.2f}s  "
                f"learn={lt_mu:10.2f}±{lt_sd:6.2f}s  "
                f"t={t_mu:10.2f}±{t_sd:6.2f}s  "
                f"t*={t_all_mu:10.2f}±{t_all_sd:6.2f}s"
            )
        print(f"  t*: failures/timeouts counted as {DEFAULT_TIMEOUT:.0f}s")

# Surviving‑data ratio -------------------------------------------------------- #

def table_surviving_ratio():
    for mode, src in (("OR", "original_text"), ("CR", "broken_text")):
        overall: Dict[str, List[float]] = {}
        for db in DATABASES:
            if not Path(db).is_file():
                continue
            conn = sqlite3.connect(db)
            rows = _q(
                conn,
                f"SELECT algorithm, {src}, repaired_text FROM results "
                "WHERE fixed=1 AND repaired_text IS NOT NULL AND repair_time IS NOT NULL AND repair_time <= ?",
                (DEFAULT_TIMEOUT,),
            )
            conn.close()
            print(f"\nSurviving‑data ratio ({mode}) in {db}")
            per_db: Dict[str, List[float]] = {}
            for alg, src_txt, rep_txt in rows:
                _, dels, _, _ = edit_distance_with_ops(src_txt, rep_txt)
                ratio = (len(src_txt) - dels) / len(src_txt) if src_txt else 0
                per_db.setdefault(alg, []).append(ratio)
                overall.setdefault(alg, []).append(ratio)
            _print_ratio(per_db)
        print(f"\nOverall surviving‑data ratio ({mode})")
        _print_ratio(overall)


def _print_ratio(buckets: Dict[str, List[float]]):
    if not buckets:
        print("  (no data)"); return
    print(f"{'Alg':<8} {'Avg':>10} {'σ':>10} {'n':>6}")
    for alg, vals in buckets.items():
        mu, sd = _stats(vals)
        print(f"{alg:<8} {mu:10.4f} {sd:10.4f} {len(vals):6d}")


def _betamax_metrics_for_db(db: str) -> Optional[Dict[str, Any]]:
    path = Path(db)
    if not path.is_file():
        print(f"[WARN] Missing DB {db} for BETAMAX comparison", file=sys.stderr)
        return None
    if not BETAMAX_ALGORITHMS:
        print("[WARN] No BETAMAX algorithm aliases configured", file=sys.stderr)
        return None
    placeholders = ",".join("?" for _ in BETAMAX_ALGORITHMS)
    conn = sqlite3.connect(db)
    cols = {row[1] for row in _q(conn, "PRAGMA table_info(results)")}
    sel_learn_time = "learn_time" if "learn_time" in cols else "NULL"
    rows = _q(conn,
              f"SELECT distance_broken_repaired, distance_original_repaired, COALESCE(iterations, 0), {sel_learn_time}, "
              f"       broken_text, repaired_text, fixed, repair_time "
              f"FROM results WHERE algorithm IN ({placeholders})",
              BETAMAX_ALGORITHMS)
    conn.close()
    bucket: Dict[str, Any] = {
        "db": db,
        "dbr": [],
        "dor": [],
        "recb": [],
        "orc": [],
        "learn": [],
        "succ": 0,
        "tot": len(rows),
    }
    for dbr, dor, iters, learn_time, broken_text, repaired_text, fixed, repair_time in rows:
        if fixed and repair_time is not None and repair_time <= DEFAULT_TIMEOUT:
            bucket["succ"] += 1
            bucket["dbr"].append(dbr)
            bucket["dor"].append(dor)
            if broken_text is not None and repaired_text is not None:
                bucket["recb"].append(_recovery_ratio(broken_text, repaired_text))
            bucket["orc"].append(float(iters or 0))
            if learn_time is not None:
                try:
                    bucket["learn"].append(float(learn_time or 0.0))
                except Exception:
                    pass
    abr, sbr = _stats(bucket["dbr"])
    aor, sor = _stats(bucket["dor"])
    arecb, srecb = _stats(bucket["recb"])
    aorc, sorc = _stats(bucket["orc"])
    alearn, slearn = _stats(bucket["learn"])
    bucket.update({
        "avg_br": abr,
        "std_br": sbr,
        "avg_or": aor,
        "std_or": sor,
        "avg_recb": arecb,
        "std_recb": srecb,
        "avg_orc": aorc,
        "std_orc": sorc,
        "avg_learn": alearn,
        "std_learn": slearn,
        "succ_rate": (bucket["succ"] / bucket["tot"]) if bucket["tot"] else 0.0,
        "has_success": bool(bucket["dbr"]),
    })
    return bucket


def _betamax_success_distances_for_db(db: str) -> Optional[Dict[Tuple[Any, ...], Tuple[Any, Any, float]]]:
    """
    Return a map from a per-example key -> (dbr, dor, repair_time) for successful
    BETAMAX rows in *db*.

    Key is (format, fid, cidx) when available, otherwise falls back to (id,).
    If duplicates exist, keep the successful row with the smallest repair_time.
    """
    path = Path(db)
    if not path.is_file():
        return None
    if not BETAMAX_ALGORITHMS:
        return None

    placeholders = ",".join("?" for _ in BETAMAX_ALGORITHMS)
    conn = sqlite3.connect(db)
    cols = {row[1] for row in _q(conn, "PRAGMA table_info(results)")}
    if {"format", "fid", "cidx"}.issubset(cols):
        key_expr = "format, fid, cidx"
        key_arity = 3
    elif "id" in cols:
        key_expr = "id"
        key_arity = 1
    else:
        conn.close()
        return None

    rows = _q(
        conn,
        f"SELECT {key_expr}, distance_broken_repaired, distance_original_repaired, fixed, repair_time "
        f"FROM results WHERE algorithm IN ({placeholders})",
        BETAMAX_ALGORITHMS,
    )
    conn.close()

    out: Dict[Tuple[Any, ...], Tuple[Any, Any, float]] = {}
    for row in rows:
        key_vals = row[:key_arity]
        dbr, dor, fixed, repair_time = row[key_arity:]
        if not _is_success(fixed, repair_time):
            continue
        try:
            rt = float(repair_time)
        except (TypeError, ValueError):
            rt = float(DEFAULT_TIMEOUT)
        key = tuple(key_vals) if isinstance(key_vals, tuple) else (key_vals,)
        prev = out.get(key)
        if prev is None or rt < prev[2]:
            out[key] = (dbr, dor, rt)
    return out


def betamax_mutation_comparison():
    print("\nBETAMAX mutate vs non-mutate")
    hdr = f"{'DB':<24} {'Variant':<10} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecB':>9} {'σ RecB':>9} "\
          f"{'Avg orc':>8} {'σ orc':>8} {'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    print("-" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    any_rows = False
    na8 = f"{'n/a':>8}"; na6 = f"{'n/a':>6}"
    na9 = f"{'n/a':>9}"
    overall: Dict[str, Dict[str, Any]] = {}
    for non_db, mut_db in BETAMAX_MUTATION_DB_PAIRS:
        for variant, db in (("non-mut", non_db), ("mut", mut_db)):
            stats = _betamax_metrics_for_db(db)
            if stats is None:
                print(f"{db:<24} {variant:<10} {na8} {na8} {na8} {na8} {'n/a':>9} {'n/a':>9} {na8} {na8} {na8} {na6} {na6}")
                continue
            any_rows = True
            has_success = stats["has_success"]
            tot = stats["tot"]
            succ = stats["succ"]
            agg = overall.setdefault(variant, {"dbr": [], "dor": [], "recb": [], "succ": 0, "tot": 0})
            agg["dbr"].extend(stats["dbr"])
            agg["dor"].extend(stats["dor"])
            agg["recb"].extend(stats.get("recb", []))
            agg.setdefault("orc", []).extend(stats.get("orc", []))
            agg["succ"] += succ
            agg["tot"]  += tot
            avg_br = f"{stats['avg_br']:8.2f}" if has_success else f"{'n/a':>8}"
            std_br = f"{stats['std_br']:8.2f}" if has_success else f"{'n/a':>8}"
            avg_or = f"{stats['avg_or']:8.2f}" if has_success else f"{'n/a':>8}"
            std_or = f"{stats['std_or']:8.2f}" if has_success else f"{'n/a':>8}"
            avg_recb = f"{stats['avg_recb']:9.4f}" if has_success else f"{'n/a':>9}"
            std_recb = f"{stats['std_recb']:9.4f}" if has_success else f"{'n/a':>9}"
            avg_orc = f"{stats['avg_orc']:8.2f}" if has_success else f"{'n/a':>8}"
            std_orc = f"{stats['std_orc']:8.2f}" if has_success else f"{'n/a':>8}"
            succ_pct = f"{stats['succ_rate'] * 100:7.2f}%" if tot else f"{'n/a':>8}"
            succ_str = f"{succ:6d}" if tot else f"{'n/a':>6}"
            tot_str  = f"{tot:6d}"
            print(f"{db:<24} {variant:<10} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {succ_pct} {succ_str} {tot_str}")
    if not any_rows:
        print("(no BETAMAX rows found in the listed databases)")
        return
    print("-" * len(hdr))
    print("Overall across listed DBs")
    print("-" * len(hdr))
    for variant in ("non-mut", "mut"):
        agg = overall.get(variant)
        if not agg:
            print(f"{'ALL':<24} {variant:<10} {na8} {na8} {na8} {na8} {'n/a':>9} {'n/a':>9} {na8} {na8} {na8} {na6} {na6}")
            continue
        abr, sbr = _stats(agg["dbr"])
        aor, sor = _stats(agg["dor"])
        arecb, srecb = _stats(agg.get("recb", []))
        aorc, sorc = _stats(agg.get("orc", []))
        tot = agg["tot"]
        succ = agg["succ"]
        has_success = len(agg["dbr"]) > 0
        avg_br = f"{abr:8.2f}" if has_success else f"{'n/a':>8}"
        std_br = f"{sbr:8.2f}" if has_success else f"{'n/a':>8}"
        avg_or = f"{aor:8.2f}" if has_success else f"{'n/a':>8}"
        std_or = f"{sor:8.2f}" if has_success else f"{'n/a':>8}"
        avg_recb = f"{arecb:9.4f}" if has_success else f"{'n/a':>9}"
        std_recb = f"{srecb:9.4f}" if has_success else f"{'n/a':>9}"
        avg_orc = f"{aorc:8.2f}" if has_success else f"{'n/a':>8}"
        std_orc = f"{sorc:8.2f}" if has_success else f"{'n/a':>8}"
        succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else f"{'n/a':>8}"
        print(f"{'ALL':<24} {variant:<10} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {succ_pct} {succ:6d} {tot:6d}")


def betamax_ablation_comparison():
    has_template = bool(BETAMAX_ABLATION_KS) and bool(BETAMAX_ABLATION_MODES) and bool(BETAMAX_ABLATION_DB_TEMPLATE)
    runs: List[Tuple[str, int, str]] = []
    if BETAMAX_ABLATION_DBS:
        # Legacy: treat as "double" mode only.
        runs = [(db, k, "double") for (db, k) in BETAMAX_ABLATION_DBS]
    elif has_template:
        k50_fallback = {"single": "single.db", "double": "double.db", "triple": "triple.db"}
        for k in BETAMAX_ABLATION_KS:
            for mode in BETAMAX_ABLATION_MODES:
                db = BETAMAX_ABLATION_DB_TEMPLATE.format(mode=mode, K=k, k=k)
                # Convenience: if k=50 and *_k50.db isn't present, fall back to the
                # default per-mutation DBs (single.db/double.db/triple.db).
                if k == 50 and not Path(db).is_file():
                    db = k50_fallback.get(mode, db)
                runs.append((db, k, mode))
    else:
        return

    print("\nBETAMAX learning-set ablation (constant test set)")
    hdr = f"{'DB':<24} {'Mode':<6} {'K':>4} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecB':>9} {'σ RecB':>9} {'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    print("-" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    na8 = f"{'n/a':>8}"; na6 = f"{'n/a':>6}"
    na9 = f"{'n/a':>9}"

    def _merge_into(agg: Dict[str, Any], stats: Dict[str, Any]) -> None:
        agg["dbr"].extend(stats.get("dbr", []))
        agg["dor"].extend(stats.get("dor", []))
        agg["recb"].extend(stats.get("recb", []))
        agg["succ"] += int(stats.get("succ", 0))
        agg["tot"] += int(stats.get("tot", 0))

    any_rows = False
    per_k: Dict[int, Dict[str, Any]] = {}
    overall = {"dbr": [], "dor": [], "recb": [], "succ": 0, "tot": 0}
    per_k_success_dist: Dict[int, Dict[Tuple[Any, ...], Tuple[Any, Any]]] = {}
    for db, k, mode in runs:
        if not Path(db).is_file():
            print(f"{db:<24} {mode:<6} {k:4d} {na8} {na8} {na8} {na8} {'n/a':>9} {'n/a':>9} {na8} {na6} {na6}")
            continue
        stats = _betamax_metrics_for_db(db)
        if stats is None:
            print(f"{db:<24} {mode:<6} {k:4d} {na8} {na8} {na8} {na8} {'n/a':>9} {'n/a':>9} {na8} {na6} {na6}")
            continue
        any_rows = True
        _merge_into(overall, stats)
        per_k.setdefault(k, {"dbr": [], "dor": [], "recb": [], "succ": 0, "tot": 0})
        _merge_into(per_k[k], stats)

        has_success = stats["has_success"]
        tot = stats["tot"]
        succ = stats["succ"]
        avg_br = f"{stats['avg_br']:8.2f}" if has_success else na8
        std_br = f"{stats['std_br']:8.2f}" if has_success else na8
        avg_or = f"{stats['avg_or']:8.2f}" if has_success else na8
        std_or = f"{stats['std_or']:8.2f}" if has_success else na8
        avg_recb = f"{stats['avg_recb']:9.4f}" if has_success else f"{'n/a':>9}"
        std_recb = f"{stats['std_recb']:9.4f}" if has_success else f"{'n/a':>9}"
        succ_pct = f"{stats['succ_rate'] * 100:7.2f}%" if tot else na8
        succ_str = f"{succ:6d}" if tot else na6
        tot_str = f"{tot:6d}"
        print(f"{db:<24} {mode:<6} {k:4d} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {succ_pct} {succ_str} {tot_str}")

        if k in (50, 25):
            succ_map = _betamax_success_distances_for_db(db) or {}
            if succ_map:
                per_k_success_dist.setdefault(k, {})
                for key, (dbr, dor, _) in succ_map.items():
                    per_k_success_dist[k][(mode,) + key] = (dbr, dor)

    if not any_rows:
        print("(no BETAMAX rows found for ablation DBs)")
        return

    print("-" * len(hdr))
    print("Mutations combined (single+double+triple), grouped by K")
    hdr2 = f"{'K':>4} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecB':>9} {'σ RecB':>9} {'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    print("-" * len(hdr2))
    print(hdr2)
    print("-" * len(hdr2))
    ks_to_show = BETAMAX_ABLATION_KS if (not BETAMAX_ABLATION_DBS and has_template) else sorted(per_k.keys())
    for k in ks_to_show:
        agg = per_k.get(k, {"dbr": [], "dor": [], "recb": [], "succ": 0, "tot": 0})
        abr, sbr = _stats(agg["dbr"])
        aor, sor = _stats(agg["dor"])
        arecb, srecb = _stats(agg["recb"])
        tot = agg["tot"]
        succ = agg["succ"]
        has_success = len(agg["dbr"]) > 0
        avg_br = f"{abr:8.2f}" if has_success else na8
        std_br = f"{sbr:8.2f}" if has_success else na8
        avg_or = f"{aor:8.2f}" if has_success else na8
        std_or = f"{sor:8.2f}" if has_success else na8
        avg_recb = f"{arecb:9.4f}" if has_success else f"{'n/a':>9}"
        std_recb = f"{srecb:9.4f}" if has_success else f"{'n/a':>9}"
        succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
        print(f"{k:4d} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {succ_pct} {succ:6d} {tot:6d}")
    print("-" * len(hdr2))
    abr, sbr = _stats(overall["dbr"])
    aor, sor = _stats(overall["dor"])
    arecb, srecb = _stats(overall["recb"])
    tot = overall["tot"]
    succ = overall["succ"]
    has_success = len(overall["dbr"]) > 0
    avg_br = f"{abr:8.2f}" if has_success else na8
    std_br = f"{sbr:8.2f}" if has_success else na8
    avg_or = f"{aor:8.2f}" if has_success else na8
    std_or = f"{sor:8.2f}" if has_success else na8
    avg_recb = f"{arecb:9.4f}" if has_success else f"{'n/a':>9}"
    std_recb = f"{srecb:9.4f}" if has_success else f"{'n/a':>9}"
    succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
    print(f"{'ALL':>4} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {succ_pct} {succ:6d} {tot:6d}")

    if 50 in per_k_success_dist and 25 in per_k_success_dist:
        common_keys = set(per_k_success_dist[50]) & set(per_k_success_dist[25])
        if common_keys:
            print("\nCommon successful samples for K=50 and K=25 (combined across modes)")
            print(f"n = {len(common_keys)}")
            hdr3 = f"{'K':>4} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8}"
            print("-" * len(hdr3))
            print(hdr3)
            print("-" * len(hdr3))
            for k in (50, 25):
                dbrs = [per_k_success_dist[k][key][0] for key in common_keys]
                dors = [per_k_success_dist[k][key][1] for key in common_keys]
                abr, sbr = _stats(dbrs)
                aor, sor = _stats(dors)
                has_success = len(_to_numeric_list(dbrs)) > 0
                avg_br = f"{abr:8.2f}" if has_success else na8
                std_br = f"{sbr:8.2f}" if has_success else na8
                avg_or = f"{aor:8.2f}" if has_success else na8
                std_or = f"{sor:8.2f}" if has_success else na8
                print(f"{k:4d} {avg_br} {std_br} {avg_or} {std_or}")
            print("-" * len(hdr3))


def betamax_learner_comparison():
    if not BETAMAX_LEARNER_DB_PAIRS:
        return
    print("\nBETAMAX learner comparison (rpni vs rpni_xover)")
    hdr = f"{'DB':<24} {'Learner':<12} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecB':>9} {'σ RecB':>9} {'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    print("-" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    na8 = f"{'n/a':>8}"; na6 = f"{'n/a':>6}"
    buckets: Dict[str, Dict[str, Any]] = {}
    any_rows = False
    for db, learner in BETAMAX_LEARNER_DB_PAIRS:
        stats = _betamax_metrics_for_db(db)
        if stats is None:
            print(f"{db:<24} {learner:<12} {na8} {na8} {na8} {na8} {'n/a':>9} {'n/a':>9} {na8} {na6} {na6}")
            continue
        any_rows = True
        buckets.setdefault(learner, {"dbr": [], "dor": [], "recb": [], "succ": 0, "tot": 0})
        buckets[learner]["dbr"].extend(stats["dbr"])
        buckets[learner]["dor"].extend(stats["dor"])
        buckets[learner]["recb"].extend(stats.get("recb", []))
        buckets[learner]["succ"] += stats["succ"]
        buckets[learner]["tot"]  += stats["tot"]
        has_success = stats["has_success"]
        tot = stats["tot"]
        succ = stats["succ"]
        avg_br = f"{stats['avg_br']:8.2f}" if has_success else na8
        std_br = f"{stats['std_br']:8.2f}" if has_success else na8
        avg_or = f"{stats['avg_or']:8.2f}" if has_success else na8
        std_or = f"{stats['std_or']:8.2f}" if has_success else na8
        avg_recb = f"{stats['avg_recb']:9.4f}" if has_success else f"{'n/a':>9}"
        std_recb = f"{stats['std_recb']:9.4f}" if has_success else f"{'n/a':>9}"
        succ_pct = f"{stats['succ_rate'] * 100:7.2f}%" if tot else na8
        succ_str = f"{succ:6d}" if tot else na6
        print(f"{db:<24} {learner:<12} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {succ_pct} {succ_str} {tot:6d}")
    if not any_rows:
        print("(no BETAMAX rows found for learner DBs)")
        return
    print("-" * len(hdr))
    for learner_name in sorted({l for _, l in BETAMAX_LEARNER_DB_PAIRS}):
        agg = buckets.get(learner_name)
        if not agg:
            print(f"{'ALL':<24} {learner_name:<12} {na8} {na8} {na8} {na8} {'n/a':>9} {'n/a':>9} {na8} {na6} {na6}")
            continue
        abr, sbr = _stats(agg["dbr"])
        aor, sor = _stats(agg["dor"])
        arecb, srecb = _stats(agg["recb"])
        tot = agg["tot"]
        succ = agg["succ"]
        has_success = len(agg["dbr"]) > 0
        avg_br = f"{abr:8.2f}" if has_success else na8
        std_br = f"{sbr:8.2f}" if has_success else na8
        avg_or = f"{aor:8.2f}" if has_success else na8
        std_or = f"{sor:8.2f}" if has_success else na8
        avg_recb = f"{arecb:9.4f}" if has_success else f"{'n/a':>9}"
        std_recb = f"{srecb:9.4f}" if has_success else f"{'n/a':>9}"
        succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
    print(f"{'ALL':<24} {learner_name:<12} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {succ_pct} {succ:6d} {tot:6d}")


def betamax_precompute_mutation_ablation_comparison():
    """
    Ablation over BETAMAX precompute mutation augmentation count (m).

    DBs are expected to be named like:
      single_m{m}.db / double_m{m}.db / triple_m{m}.db
    produced by bm_mutationcap_ablation.py which sets LSTAR_PRECOMPUTE_MUTATIONS.
    """
    has_cfg = bool(BETAMAX_PRECOMP_MUT_ABLATION_MS) and bool(BETAMAX_PRECOMP_MUT_ABLATION_MODES) and bool(BETAMAX_PRECOMP_MUT_ABLATION_DB_TEMPLATE)
    if not has_cfg:
        return

    runs: List[Tuple[str, int, str]] = []
    for m in BETAMAX_PRECOMP_MUT_ABLATION_MS:
        for mode in BETAMAX_PRECOMP_MUT_ABLATION_MODES:
            db = BETAMAX_PRECOMP_MUT_ABLATION_DB_TEMPLATE.format(mode=mode, M=m, m=m)
            runs.append((db, m, mode))

    print("\nBETAMAX precompute mutation ablation (LSTAR_PRECOMPUTE_MUTATIONS)")
    hdr = (
        f"{'DB':<24} {'Mode':<6} {'m':>4} "
        f"{'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} "
        f"{'Avg RecB':>9} {'σ RecB':>9} "
        f"{'Avg orc':>8} {'σ orc':>8} "
        f"{'Avg learn':>9} {'σ learn':>9} "
        f"{'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    )
    print("-" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    na8 = f"{'n/a':>8}"; na6 = f"{'n/a':>6}"
    na9 = f"{'n/a':>9}"

    def _merge_into(agg: Dict[str, Any], stats: Dict[str, Any]) -> None:
        agg["dbr"].extend(stats.get("dbr", []))
        agg["dor"].extend(stats.get("dor", []))
        agg["recb"].extend(stats.get("recb", []))
        agg["orc"].extend(stats.get("orc", []))
        agg["learn"].extend(stats.get("learn", []))
        agg["succ"] += int(stats.get("succ", 0))
        agg["tot"] += int(stats.get("tot", 0))

    any_rows = False
    per_m: Dict[int, Dict[str, Any]] = {}
    overall = {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0}
    for db, m, mode in runs:
        if not Path(db).is_file():
            print(f"{db:<24} {mode:<6} {m:4d} {na8} {na8} {na8} {na8} {na9} {na9} {na8} {na8} {na9} {na9} {na8} {na6} {na6}")
            continue
        stats = _betamax_metrics_for_db(db)
        if stats is None:
            print(f"{db:<24} {mode:<6} {m:4d} {na8} {na8} {na8} {na8} {na9} {na9} {na8} {na8} {na9} {na9} {na8} {na6} {na6}")
            continue

        any_rows = True
        per_m.setdefault(m, {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0})
        _merge_into(per_m[m], stats)
        _merge_into(overall, stats)

        has_success = stats["has_success"]
        tot = stats["tot"]
        succ = stats["succ"]
        avg_br = f"{stats['avg_br']:8.2f}" if has_success else na8
        std_br = f"{stats['std_br']:8.2f}" if has_success else na8
        avg_or = f"{stats['avg_or']:8.2f}" if has_success else na8
        std_or = f"{stats['std_or']:8.2f}" if has_success else na8
        avg_recb = f"{stats['avg_recb']:9.4f}" if has_success else na9
        std_recb = f"{stats['std_recb']:9.4f}" if has_success else na9
        avg_orc = f"{stats['avg_orc']:8.2f}" if has_success else na8
        std_orc = f"{stats['std_orc']:8.2f}" if has_success else na8
        avg_learn = f"{stats['avg_learn']:9.2f}" if has_success else na9
        std_learn = f"{stats['std_learn']:9.2f}" if has_success else na9
        succ_pct = f"{stats['succ_rate'] * 100:7.2f}%" if tot else na8
        succ_str = f"{succ:6d}" if tot else na6
        tot_str = f"{tot:6d}"
        print(f"{db:<24} {mode:<6} {m:4d} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {avg_learn} {std_learn} {succ_pct} {succ_str} {tot_str}")

    if not any_rows:
        print("(no BETAMAX rows found for precompute-mutation ablation DBs)")
        return

    print("-" * len(hdr))
    print("Mutations combined (single+double+triple), grouped by m")
    hdr2 = f"{'m':>4} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecB':>9} {'σ RecB':>9} {'Avg orc':>8} {'σ orc':>8} {'Avg learn':>9} {'σ learn':>9} {'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    print("-" * len(hdr2))
    print(hdr2)
    print("-" * len(hdr2))

    for m in BETAMAX_PRECOMP_MUT_ABLATION_MS:
        agg = per_m.get(m, {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0})
        abr, sbr = _stats(agg["dbr"])
        aor, sor = _stats(agg["dor"])
        arecb, srecb = _stats(agg["recb"])
        aorc, sorc = _stats(agg["orc"])
        alearn, slearn = _stats(agg["learn"])
        tot = agg["tot"]
        succ = agg["succ"]
        has_success = len(agg["dbr"]) > 0
        avg_br = f"{abr:8.2f}" if has_success else na8
        std_br = f"{sbr:8.2f}" if has_success else na8
        avg_or = f"{aor:8.2f}" if has_success else na8
        std_or = f"{sor:8.2f}" if has_success else na8
        avg_recb = f"{arecb:9.4f}" if has_success else na9
        std_recb = f"{srecb:9.4f}" if has_success else na9
        avg_orc = f"{aorc:8.2f}" if has_success else na8
        std_orc = f"{sorc:8.2f}" if has_success else na8
        avg_learn = f"{alearn:9.2f}" if has_success else na9
        std_learn = f"{slearn:9.2f}" if has_success else na9
        succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
        print(f"{m:4d} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {avg_learn} {std_learn} {succ_pct} {succ:6d} {tot:6d}")

    print("-" * len(hdr2))
    abr, sbr = _stats(overall["dbr"])
    aor, sor = _stats(overall["dor"])
    arecb, srecb = _stats(overall["recb"])
    aorc, sorc = _stats(overall["orc"])
    alearn, slearn = _stats(overall["learn"])
    tot = overall["tot"]
    succ = overall["succ"]
    has_success = len(overall["dbr"]) > 0
    avg_br = f"{abr:8.2f}" if has_success else na8
    std_br = f"{sbr:8.2f}" if has_success else na8
    avg_or = f"{aor:8.2f}" if has_success else na8
    std_or = f"{sor:8.2f}" if has_success else na8
    avg_recb = f"{arecb:9.4f}" if has_success else na9
    std_recb = f"{srecb:9.4f}" if has_success else na9
    avg_orc = f"{aorc:8.2f}" if has_success else na8
    std_orc = f"{sorc:8.2f}" if has_success else na8
    avg_learn = f"{alearn:9.2f}" if has_success else na9
    std_learn = f"{slearn:9.2f}" if has_success else na9
    succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
    print(f"{'ALL':>4} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {avg_learn} {std_learn} {succ_pct} {succ:6d} {tot:6d}")


def betamax_xover_ablation_comparison():
    """
    Ablation over rpni_xover cross-check budget.

    Expected DB naming from `bm_xover_checks_ablation.py`:
      {mode}_xcheck{checks}.db
    or a custom template that may also include {pairs}.
    """
    has_cfg = bool(BETAMAX_XOVER_ABLATION_CHECKS) and bool(BETAMAX_XOVER_ABLATION_MODES) and bool(BETAMAX_XOVER_ABLATION_DB_TEMPLATE)
    if not has_cfg:
        return

    runs: List[Tuple[str, int, int, str]] = []
    for pairs in BETAMAX_XOVER_ABLATION_PAIRS:
        for checks in BETAMAX_XOVER_ABLATION_CHECKS:
            for mode in BETAMAX_XOVER_ABLATION_MODES:
                db = BETAMAX_XOVER_ABLATION_DB_TEMPLATE.format(
                    mode=mode,
                    checks=checks,
                    check=checks,
                    pairs=pairs,
                    pair=pairs,
                    p=pairs,
                )
                runs.append((db, checks, pairs, mode))

    print("\nBETAMAX xover ablation (LSTAR_RPNI_XOVER_CHECKS / LSTAR_RPNI_XOVER_PAIRS)")
    hdr = (
        f"{'DB':<28} {'Mode':<6} {'checks':>7} {'pairs':>6} "
        f"{'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} "
        f"{'Avg RecB':>9} {'σ RecB':>9} "
        f"{'Avg orc':>8} {'σ orc':>8} "
        f"{'Avg learn':>9} {'σ learn':>9} "
        f"{'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    )
    print("-" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    na8 = f"{'n/a':>8}"; na6 = f"{'n/a':>6}"
    na9 = f"{'n/a':>9}"

    def _merge_into(agg: Dict[str, Any], stats: Dict[str, Any]) -> None:
        agg["dbr"].extend(stats.get("dbr", []))
        agg["dor"].extend(stats.get("dor", []))
        agg["recb"].extend(stats.get("recb", []))
        agg["orc"].extend(stats.get("orc", []))
        agg["learn"].extend(stats.get("learn", []))
        agg["succ"] += int(stats.get("succ", 0))
        agg["tot"] += int(stats.get("tot", 0))

    any_rows = False
    per_checks: Dict[int, Dict[str, Any]] = {}
    per_pairs: Dict[int, Dict[str, Any]] = {}
    per_budget: Dict[Tuple[int, int], Dict[str, Any]] = {}
    overall = {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0}

    for db, checks, pairs, mode in runs:
        if not Path(db).is_file():
            print(f"{db:<28} {mode:<6} {checks:7d} {pairs:6d} {na8} {na8} {na8} {na8} {na9} {na9} {na8} {na8} {na9} {na9} {na8} {na6} {na6}")
            continue
        stats = _betamax_metrics_for_db(db)
        if stats is None:
            print(f"{db:<28} {mode:<6} {checks:7d} {pairs:6d} {na8} {na8} {na8} {na8} {na9} {na9} {na8} {na8} {na9} {na9} {na8} {na6} {na6}")
            continue

        any_rows = True
        per_checks.setdefault(checks, {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0})
        per_pairs.setdefault(pairs, {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0})
        per_budget.setdefault((pairs, checks), {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0})
        _merge_into(per_checks[checks], stats)
        _merge_into(per_pairs[pairs], stats)
        _merge_into(per_budget[(pairs, checks)], stats)
        _merge_into(overall, stats)

        has_success = stats["has_success"]
        tot = stats["tot"]
        succ = stats["succ"]
        avg_br = f"{stats['avg_br']:8.2f}" if has_success else na8
        std_br = f"{stats['std_br']:8.2f}" if has_success else na8
        avg_or = f"{stats['avg_or']:8.2f}" if has_success else na8
        std_or = f"{stats['std_or']:8.2f}" if has_success else na8
        avg_recb = f"{stats['avg_recb']:9.4f}" if has_success else na9
        std_recb = f"{stats['std_recb']:9.4f}" if has_success else na9
        avg_orc = f"{stats['avg_orc']:8.2f}" if has_success else na8
        std_orc = f"{stats['std_orc']:8.2f}" if has_success else na8
        avg_learn = f"{stats['avg_learn']:9.2f}" if has_success else na9
        std_learn = f"{stats['std_learn']:9.2f}" if has_success else na9
        succ_pct = f"{stats['succ_rate'] * 100:7.2f}%" if tot else na8
        succ_str = f"{succ:6d}" if tot else na6
        tot_str = f"{tot:6d}"
        print(f"{db:<28} {mode:<6} {checks:7d} {pairs:6d} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {avg_learn} {std_learn} {succ_pct} {succ_str} {tot_str}")

    if not any_rows:
        print("(no BETAMAX rows found for xover ablation DBs)")
        return

    print("-" * len(hdr))
    print("Mutations combined (single+double+triple), grouped by checks")
    hdr2 = f"{'checks':>7} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecB':>9} {'σ RecB':>9} {'Avg orc':>8} {'σ orc':>8} {'Avg learn':>9} {'σ learn':>9} {'Succ%':>8} {'Succ':>6} {'Tot':>6}"
    print("-" * len(hdr2))
    print(hdr2)
    print("-" * len(hdr2))
    for checks in BETAMAX_XOVER_ABLATION_CHECKS:
        agg = per_checks.get(checks, {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0})
        abr, sbr = _stats(agg["dbr"])
        aor, sor = _stats(agg["dor"])
        arecb, srecb = _stats(agg["recb"])
        aorc, sorc = _stats(agg["orc"])
        alearn, slearn = _stats(agg["learn"])
        tot = agg["tot"]
        succ = agg["succ"]
        has_success = len(agg["dbr"]) > 0
        avg_br = f"{abr:8.2f}" if has_success else na8
        std_br = f"{sbr:8.2f}" if has_success else na8
        avg_or = f"{aor:8.2f}" if has_success else na8
        std_or = f"{sor:8.2f}" if has_success else na8
        avg_recb = f"{arecb:9.4f}" if has_success else na9
        std_recb = f"{srecb:9.4f}" if has_success else na9
        avg_orc = f"{aorc:8.2f}" if has_success else na8
        std_orc = f"{sorc:8.2f}" if has_success else na8
        avg_learn = f"{alearn:9.2f}" if has_success else na9
        std_learn = f"{slearn:9.2f}" if has_success else na9
        succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
        print(f"{checks:7d} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {avg_learn} {std_learn} {succ_pct} {succ:6d} {tot:6d}")

    if len(set(BETAMAX_XOVER_ABLATION_PAIRS)) > 1:
        print("-" * len(hdr2))
        print("Mutations combined, grouped by pairs")
        hdr3 = f"{'pairs':>6} {'Avg BR':>8} {'σ BR':>8} {'Avg OR':>8} {'σ OR':>8} {'Avg RecB':>9} {'σ RecB':>9} {'Avg orc':>8} {'σ orc':>8} {'Avg learn':>9} {'σ learn':>9} {'Succ%':>8} {'Succ':>6} {'Tot':>6}"
        print("-" * len(hdr3))
        print(hdr3)
        print("-" * len(hdr3))
        for pairs in BETAMAX_XOVER_ABLATION_PAIRS:
            agg = per_pairs.get(pairs, {"dbr": [], "dor": [], "recb": [], "orc": [], "learn": [], "succ": 0, "tot": 0})
            abr, sbr = _stats(agg["dbr"])
            aor, sor = _stats(agg["dor"])
            arecb, srecb = _stats(agg["recb"])
            aorc, sorc = _stats(agg["orc"])
            alearn, slearn = _stats(agg["learn"])
            tot = agg["tot"]
            succ = agg["succ"]
            has_success = len(agg["dbr"]) > 0
            avg_br = f"{abr:8.2f}" if has_success else na8
            std_br = f"{sbr:8.2f}" if has_success else na8
            avg_or = f"{aor:8.2f}" if has_success else na8
            std_or = f"{sor:8.2f}" if has_success else na8
            avg_recb = f"{arecb:9.4f}" if has_success else na9
            std_recb = f"{srecb:9.4f}" if has_success else na9
            avg_orc = f"{aorc:8.2f}" if has_success else na8
            std_orc = f"{sorc:8.2f}" if has_success else na8
            avg_learn = f"{alearn:9.2f}" if has_success else na9
            std_learn = f"{slearn:9.2f}" if has_success else na9
            succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
            print(f"{pairs:6d} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {avg_learn} {std_learn} {succ_pct} {succ:6d} {tot:6d}")

    print("-" * len(hdr2))
    abr, sbr = _stats(overall["dbr"])
    aor, sor = _stats(overall["dor"])
    arecb, srecb = _stats(overall["recb"])
    aorc, sorc = _stats(overall["orc"])
    alearn, slearn = _stats(overall["learn"])
    tot = overall["tot"]
    succ = overall["succ"]
    has_success = len(overall["dbr"]) > 0
    avg_br = f"{abr:8.2f}" if has_success else na8
    std_br = f"{sbr:8.2f}" if has_success else na8
    avg_or = f"{aor:8.2f}" if has_success else na8
    std_or = f"{sor:8.2f}" if has_success else na8
    avg_recb = f"{arecb:9.4f}" if has_success else na9
    std_recb = f"{srecb:9.4f}" if has_success else na9
    avg_orc = f"{aorc:8.2f}" if has_success else na8
    std_orc = f"{sorc:8.2f}" if has_success else na8
    avg_learn = f"{alearn:9.2f}" if has_success else na9
    std_learn = f"{slearn:9.2f}" if has_success else na9
    succ_pct = f"{(succ / tot) * 100:7.2f}%" if tot else na8
    print(f"{'ALL':>7} {avg_br} {std_br} {avg_or} {std_or} {avg_recb} {std_recb} {avg_orc} {std_orc} {avg_learn} {std_learn} {succ_pct} {succ:6d} {tot:6d}")

def _cumulative_success_curve(iterations: List[int], fixed_flags: List[int]) -> Tuple[List[int], List[float]]:
    """
    Given per-case iteration counts and fixed flags, compute cumulative success rate
    as a function of an iteration budget T. For each integer T from 0..max(iter),
    we compute success_rate(T) = (#fixed with iterations <= T) / N_total,
    where N_total is the total number of RPNI/BETAMAX cases in that dataset.
    """
    if not iterations:
        return [], []
    max_iter = max(iterations)
    # Start budgets from 1 (not 0) to match plotting convention.
    xs: List[int] = list(range(1, max_iter + 1)) if max_iter >= 1 else [1]
    ys: List[float] = []
    n_total = len(iterations)
    for T in xs:
        succ = 0
        for it, fixed in zip(iterations, fixed_flags):
            if it is None:
                continue
            if it <= T and fixed:
                succ += 1
        ys.append((succ / n_total) if n_total > 0 else 0.0)
    return xs, ys

def _annotate_success_endpoints(ax, xs: List[int], ys: List[float]) -> None:
    """
    Add % labels for the first/last points, with dashed horizontal guides to the Y axis.
    Also show the earliest iteration budget that reaches the maximum success rate,
    with a vertical dashed guide down to the X axis.
    """
    if not xs or not ys:
        return

    x0, x1 = xs[0], xs[-1]
    y0, y1 = ys[0], ys[-1]
    ymax = max(ys)

    dx = float(x1 - x0)
    stub = x0 + (dx * 0.06 if dx > 0 else 1.0)

    # Avoid label overlap when y0 ~= y1.
    off0 = 0
    off1 = 0
    if abs(y1 - y0) < 0.04:
        off0, off1 = -10, 10
    if y0 <= 0.02:
        off0 = max(off0, 10)
    if y1 >= 1.03:
        off1 = min(off1, -10)

    # Earliest iteration that achieves the max success rate (plateau point).
    eps = 1e-12
    idx_max = next((i for i, y in enumerate(ys) if abs(y - ymax) <= eps), len(xs) - 1)
    x_at_max = xs[idx_max]

    def _label(y: float, yoff_points: int) -> None:
        ax.annotate(
            f"{y * 100:.1f}%",
            xy=(0.0, y),
            xycoords=("axes fraction", "data"),
            xytext=(4, yoff_points),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=9,
            color="0.35",
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.85),
            clip_on=False,
        )

    ax.hlines(y0, xmin=x0, xmax=stub, colors="0.5", linestyles="--", linewidth=1.0, alpha=0.7)
    ax.hlines(y1, xmin=x0, xmax=x1, colors="0.5", linestyles="--", linewidth=1.0, alpha=0.7)
    _label(y0, off0)
    _label(y1, off1)

    ax.vlines(x_at_max, ymin=0.0, ymax=ymax, colors="0.5", linestyles="--", linewidth=1.0, alpha=0.7)
    ax.annotate(
        f"T={x_at_max}",
        xy=(x_at_max, 0.0),
        xycoords=("data", "axes fraction"),
        xytext=(0, 6),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=9,
        color="0.35",
        bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.85),
        clip_on=False,
    )

def plot_rpni_success_vs_iterations():
    """
    Plot RPNI/BETAMAX success rate as a function of repair-iterations budget.

    - Treats algorithms named 'rpni' or 'betamax' as the same family.
    - Produces one PNG per DB in DATABASES.
    - Also produces a combined 3-up 'rpni_success_3up.png' (1/2/3 mutations).
    - Also produces a single combined overlay plot 'betamax_success_across_mutations.png'.
    """
    plt = _load_pyplot()
    alg_names = ("rpni", "betamax")
    curves: Dict[str, Tuple[List[int], List[float], str]] = {}
    label_map = {
        "single.db": "1-mutation",
        "double.db": "2-mutations",
        "triple.db": "3-mutations",
    }

    PLOT_MAX_T = 30

    for db in DATABASES:
        if not Path(db).is_file():
            print(f"[WARN] Skipping missing DB {db} for RPNI plot", file=sys.stderr)
            continue

        conn = sqlite3.connect(db)
        rows = _q(conn,
                  "SELECT iterations, fixed, repair_time FROM results "
                  "WHERE algorithm IN (?, ?)",
                  alg_names)
        conn.close()

        if not rows:
            print(f"[INFO] No BETAMAX rows in {db}", file=sys.stderr)
            continue

        iters: List[int] = []
        fixed_flags: List[int] = []
        for it, fixed, repair_time in rows:
            # iterations can be NULL; treat as 0
            iters.append(int(it) if it is not None else 0)
            is_success = bool(fixed) and (repair_time is not None) and (repair_time <= DEFAULT_TIMEOUT)
            fixed_flags.append(1 if is_success else 0)

        xs, ys = _cumulative_success_curve(iters, fixed_flags)
        if not xs:
            print(f"[WARN] No iteration data for BETAMAX in {db}", file=sys.stderr)
            continue

        # Cap x-axis for comparability across plots.
        if xs[-1] > PLOT_MAX_T:
            # xs starts from 1, so the first PLOT_MAX_T points correspond to T=1..PLOT_MAX_T
            xs = xs[:PLOT_MAX_T]
            ys = ys[:PLOT_MAX_T]

        label = label_map.get(db, db)
        curves[db] = (xs, ys, f"BETAMAX success vs repair-iterations ({label})")

        fig, ax = plt.subplots()
        ax.plot(xs, ys, marker="o")
        ax.set_xlim(1, PLOT_MAX_T)
        ax.margins(x=0.0)
        _annotate_success_endpoints(ax, xs, ys)
        ax.set_xlabel("Repair-iterations budget (T)")
        ax.set_ylabel("Cumulative success rate (fixed with repair-iterations ≤ T)")
        ax.set_ylim(0.0, 1.05)
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.set_title(curves[db][2])
        out_path = f"rpni_success_{db}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"[INFO] Saved BETAMAX success plot for {db} -> {out_path}")

    # 3-up combined figure (single/double/triple), arranged horizontally.
    panel_keys = ["single.db", "double.db", "triple.db"]
    panel_items = [(k, curves[k]) for k in panel_keys if k in curves]
    if len(panel_items) == 3:
        fig, axs = plt.subplots(1, 3, figsize=(15, 4.5), sharey=True, constrained_layout=True)
        for ax, (key, (xs, ys, title)) in zip(axs, panel_items):
            ax.plot(xs, ys, marker="o")
            ax.set_xlim(1, PLOT_MAX_T)
            ax.margins(x=0.0)
            _annotate_success_endpoints(ax, xs, ys)
            ax.set_title(title)
            ax.set_ylim(0.0, 1.05)
            ax.grid(True, linestyle="--", alpha=0.3)
            ax.set_xlabel("Repair-iterations budget (T)")
        axs[0].set_ylabel("Cumulative success rate (fixed with repair-iterations ≤ T)")
        out_path = "rpni_success_3up.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"[INFO] Saved 3-up BETAMAX success plot -> {out_path}")

    # Single combined overlay plot (one axes, three mutation-count curves).
    if panel_items:
        fig, ax = plt.subplots(figsize=(7.6, 4.5), constrained_layout=True)
        plateau_label_slots: Dict[int, int] = {}
        for curve_idx, (key, (xs, ys, _title)) in enumerate(panel_items):
            label = label_map.get(key, key)
            (line,) = ax.plot(xs, ys, marker="o", linewidth=2, label=label)
            if not xs or not ys:
                continue

            color = line.get_color()

            # Start value at budget T=1.
            y0 = ys[0]
            dx0 = 30 if (curve_idx % 2 == 0) else -30
            dy0 = 12 + (curve_idx * 14)
            if y0 <= 0.02:
                dy0 = max(dy0, 18)
            ax.annotate(
                f"{y0 * 100:.1f}%",
                xy=(xs[0], y0),
                xytext=(dx0, dy0),
                textcoords="offset points",
                ha="left" if dx0 > 0 else "right",
                va="bottom",
                fontsize=9,
                color=color,
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.85),
                clip_on=False,
            )

            # Plateau point: earliest budget reaching the maximum success rate.
            ymax = max(ys)
            eps = 1e-12
            idx_max = next((i for i, y in enumerate(ys) if abs(y - ymax) <= eps), len(xs) - 1)
            x_at_max = xs[idx_max]

            slot = plateau_label_slots.get(int(x_at_max), 0)
            plateau_label_slots[int(x_at_max)] = slot + 1

            ax.vlines(x_at_max, ymin=0.0, ymax=ymax, colors=color, linestyles="--", linewidth=1.2, alpha=0.7)
            ax.annotate(
                f"T={x_at_max}",
                xy=(x_at_max, 0.0),
                xycoords=("data", "axes fraction"),
                xytext=(0, 6 + (slot * 10)),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=9,
                color=color,
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.85),
                clip_on=False,
            )
            ax.annotate(
                f"{ymax * 100:.1f}%",
                xy=(x_at_max, ymax),
                xytext=(0, 6 + (slot * 10)),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=9,
                color=color,
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.85),
            )
        ax.set_xlim(1, PLOT_MAX_T)
        ax.margins(x=0.0)
        ax.set_xlabel("Repair-iterations budget (T)")
        ax.set_ylabel("Cumulative success rate (fixed with repair-iterations ≤ T)")
        ax.set_ylim(0.0, 1.05)
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.set_title("BETAMAX success vs repair-iterations (across mutation counts)")
        ax.legend(title="DB / mutation count", loc="best")
        out_path = "betamax_success_across_mutations.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"[INFO] Saved overlay BETAMAX success plot -> {out_path}")

def _success_rates_by_algorithm(db: str) -> Dict[str, Dict[str, float]]:
    """
    Return per-algorithm success stats for a DB.

    Success is defined by `_is_success` (fixed and within DEFAULT_TIMEOUT).
    Output schema per alg:
      {"succ": int, "tot": int, "rate": float}
    """
    path = Path(db)
    if not path.is_file():
        return {}
    conn = sqlite3.connect(db)
    rows = _q(conn, "SELECT algorithm, fixed, repair_time FROM results")
    conn.close()
    out: Dict[str, Dict[str, float]] = {}
    for alg, fixed, repair_time in rows:
        if not isinstance(alg, str) or not alg:
            continue
        bucket = out.setdefault(alg, {"succ": 0.0, "tot": 0.0, "rate": 0.0})
        bucket["tot"] += 1.0
        if _is_success(fixed, repair_time):
            bucket["succ"] += 1.0
    for bucket in out.values():
        tot = bucket["tot"]
        bucket["rate"] = (bucket["succ"] / tot) if tot else 0.0
    return out

def plot_success_rate_by_mutation_count():
    """
    Plot per-algorithm success rate vs mutation count (single/double/triple).

    Produces: success_rate_by_mutation_count.png
    """
    plt = _load_pyplot()
    label_map = {
        "single.db": 1,
        "double.db": 2,
        "triple.db": 3,
    }
    dbs = [db for db in ("single.db", "double.db", "triple.db") if Path(db).is_file()]
    if not dbs:
        print("[WARN] No mutation DBs found for success-rate plot", file=sys.stderr)
        return

    by_db: Dict[str, Dict[str, Dict[str, float]]] = {db: _success_rates_by_algorithm(db) for db in dbs}
    algs = sorted({alg for stats in by_db.values() for alg in stats.keys()})
    if not algs:
        print("[WARN] No algorithms found for success-rate plot", file=sys.stderr)
        return

    fig, ax = plt.subplots(figsize=(7.2, 4.2), constrained_layout=True)
    for alg in algs:
        xs: List[int] = []
        ys: List[float] = []
        for db in ("single.db", "double.db", "triple.db"):
            if db not in by_db:
                continue
            x = label_map[db]
            y = float(by_db[db].get(alg, {}).get("rate", 0.0))
            xs.append(x)
            ys.append(y)
        if not xs:
            continue
        ax.plot(xs, ys, marker="o", linewidth=2, label=alg)
        for x, y in zip(xs, ys):
            ax.annotate(
                f"{y * 100:.1f}%",
                xy=(x, y),
                xytext=(0, 6),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=9,
                color="0.25",
            )

    ax.set_title(f"Success rate vs mutation count (timeout ≤ {DEFAULT_TIMEOUT:.0f}s)")
    ax.set_xlabel("Mutation count")
    ax.set_ylabel("Success rate")
    ax.set_xticks([1, 2, 3], ["1", "2", "3"])
    ax.set_ylim(0.0, 1.05)
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.legend(title="Algorithm", loc="best")
    out_path = "success_rate_by_mutation_count.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"[INFO] Saved success-rate plot -> {out_path}")

# ─────────────────────────────────────────────────────────────────────────── #

def main(include_optional: bool = True, include_plots: bool = True) -> None:
    print("———— general ———————————————")
    table_4_5_general()
    print("———— Levenshtein distances ————")
    table_4_5_distances()
    if include_optional:
        print("———— BETAMAX mutate vs non ————")
        betamax_mutation_comparison()
        print("———— BETAMAX ablation —————————")
        betamax_ablation_comparison()
        print("———— BETAMAX learners ————————")
        betamax_learner_comparison()
        print("———— BETAMAX precompute m —————")
        betamax_precompute_mutation_ablation_comparison()
        print("———— BETAMAX xover budget —————")
        betamax_xover_ablation_comparison()
    # print("———— Table 4‑5 (data survive) ——————————")
    # table_surviving_ratio()
    print("———— count repaired —————————")
    table_6_count_fixed()
    print("———— perfect repairs —————————")
    table_7_perfect()
    print("———— efficiency ———————————")
    table_8_efficiency()
    if include_plots:
        plot_rpni_success_vs_iterations()


if __name__ == "__main__":
    main()
