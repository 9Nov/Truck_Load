"""Runs every test in tests\\ and prints a summary.

Usage:
    py run_tests.py            # all suites
    py run_tests.py engine     # only the suites whose name contains "engine"
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUITES = [
    ("engine", ROOT / "tests" / "test_engine.py", "MILP solver + self-check"),
    ("yusen", ROOT / "tests" / "test_yusen.py", "Yusen ISO Tank standard times + regression"),
    ("stdtimes", ROOT / "tests" / "test_std_times.py", "editable standard-time tables"),
    ("reschedule", ROOT / "tests" / "test_reschedule.py", "mid-day re-plan + GPS ETA"),
    ("swap", ROOT / "tests" / "test_swap.py", "manual swap: late trucks + replacements"),
    ("blackout", ROOT / "tests" / "test_blackout.py", "per-channel maintenance windows"),
    ("template", ROOT / "tests" / "test_template.py", "Excel upload template"),
    ("app", ROOT / "tests" / "test_app.py", "Streamlit UI (AppTest)"),
]


def main(argv):
    wanted = [a.lower() for a in argv[1:]]
    suites = [s for s in SUITES if not wanted or any(w in s[0] for w in wanted)]
    if not suites:
        print(f"no suite matches {wanted}; available: {', '.join(s[0] for s in SUITES)}")
        return 2

    results = []
    for name, path, desc in suites:
        print(f"\n{'=' * 70}\n  {name}  -  {desc}\n{'=' * 70}", flush=True)
        t0 = time.time()
        proc = subprocess.run([sys.executable, str(path)], cwd=ROOT, text=True,
                              encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        took = time.time() - t0
        # Streamlit's bare-mode chatter is noise here, the assertions are what matter
        for line in proc.stdout.splitlines():
            if "ScriptRunContext" in line or "bare mode" in line:
                continue
            print(line)
        results.append((name, proc.returncode == 0, took))

    print(f"\n{'=' * 70}\n  SUMMARY\n{'=' * 70}")
    for name, ok, took in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<10} {took:6.1f}s")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} suites passed"
          + (f" - failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
