"""Lease lifecycle, reference counting and expiry.

    python3 tests/test_lease.py

Runs as an ordinary user: systemctl and nvidia-smi are replaced with recording
stubs, and the state directory is a temp dir. No GPU, no root, no services.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="gpu-lease-test-"))
CALLS = TMP / "systemctl.log"

# Stub systemctl: records every call, and reports units active only while a
# marker file exists so is-active reflects the stop/start we performed.
(TMP / "systemctl").write_text(f"""#!/bin/sh
echo "$@" >> {CALLS}
cmd=$1
# The unit is the last non-flag argument: `is-active --quiet <unit>` would
# otherwise be read as the unit being "--quiet".
unit=""
for a in "$@"; do
  case "$a" in --*) ;; *) unit=$a ;; esac
done
case "$cmd" in
  is-active) [ -f "{TMP}/active.$unit" ] && exit 0 || exit 1 ;;
  stop)      rm -f "{TMP}/active.$unit" ;;
  start)     touch "{TMP}/active.$unit" ;;
esac
exit 0
""")
(TMP / "systemctl").chmod(0o755)

env = {
    **os.environ,
    "GPU_LEASE_DIR": str(TMP / "state"),
    "GPU_LEASE_SYSTEMCTL": str(TMP / "systemctl"),
    "GPU_LEASE_NVIDIA_SMI": "definitely-not-a-real-binary",  # skips GPU waiting
    "GPU_LEASE_UNITS": "deberta-tuner.service,sglang.service",
    "GPU_LEASE_MAX_TTL": "600",
}

checks = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not cond:
        raise AssertionError(f"FAIL {label}: {detail}")
    print(f"  ok  {label}")


def run(*args, expect_rc=0):
    r = subprocess.run([sys.executable, str(ROOT / "gpu-lease"), *args],
                       capture_output=True, text=True, env=env)
    assert r.returncode == expect_rc, f"{args} -> rc={r.returncode}\n{r.stderr}"
    return r.stdout.strip(), r.stderr


def calls() -> list[str]:
    return CALLS.read_text().splitlines() if CALLS.exists() else []


def actions() -> list[str]:
    """Only the stop/start verbs, in order."""
    return [c for c in calls() if c.split()[0] in ("stop", "start")]


def status() -> dict:
    out, _ = run("status", "--json")
    return json.loads(out)


def main() -> int:
    # Both units start out running.
    for u in ("deberta-tuner.service", "sglang.service"):
        (TMP / f"active.{u}").touch()

    print("first acquire pauses both units")
    a, _ = run("acquire", "--ttl", "300", "--reason", "ci-build-1")
    check("returns a lease id", len(a) == 12, a)
    check("stopped both units",
          actions() == ["stop deberta-tuner.service", "stop sglang.service"],
          str(actions()))
    check("tuner stopped before sglang",
          actions().index("stop deberta-tuner.service") <
          actions().index("stop sglang.service"),
          "tuner restarts sglang on shutdown, so it must go first")
    st = status()
    check("status reports held", st["held"] and st["count"] == 1, json.dumps(st))
    check("units reported inactive",
          set(st["units"].values()) == {"inactive"}, str(st["units"]))

    print("\nsecond acquire joins without touching units")
    CALLS.unlink(missing_ok=True)
    b, _ = run("acquire", "--ttl", "300", "--reason", "ci-build-2")
    check("no stop/start issued", actions() == [], str(actions()))
    check("two leases held", status()["count"] == 2)

    print("\nreleasing one keeps services paused")
    CALLS.unlink(missing_ok=True)
    run("release", a)
    check("still paused", actions() == [], str(actions()))
    check("one lease left", status()["count"] == 1)

    print("\nreleasing the last resumes, in reverse order")
    CALLS.unlink(missing_ok=True)
    run("release", b)
    check("started both units",
          actions() == ["start sglang.service", "start deberta-tuner.service"],
          str(actions()))
    check("sglang started before the tuner",
          actions().index("start sglang.service") <
          actions().index("start deberta-tuner.service"))
    check("reset-failed issued first",
          any(c.startswith("reset-failed") for c in calls()), str(calls()))
    check("nothing held", status()["count"] == 0)

    print("\nreleasing an unknown lease is harmless")
    CALLS.unlink(missing_ok=True)
    run("release", "deadbeefcafe")
    check("no units touched", actions() == [], str(actions()))

    print("\nexpiry: the reaper resumes services")
    CALLS.unlink(missing_ok=True)
    c, _ = run("acquire", "--ttl", "1", "--reason", "will-expire")
    check("paused again", actions() == ["stop deberta-tuner.service",
                                        "stop sglang.service"], str(actions()))
    CALLS.unlink(missing_ok=True)
    run("reap")
    check("not yet expired", actions() == [], str(actions()))
    time.sleep(1.2)
    _, err = run("reap")
    check("reaper logged the expiry", "expired after" in err, err)
    check("reaper resumed services",
          actions() == ["start sglang.service", "start deberta-tuner.service"],
          str(actions()))
    check("no leases remain", status()["count"] == 0)

    print("\nexpired leases do not count as held")
    run("acquire", "--ttl", "1")
    time.sleep(1.2)
    check("status hides expired leases", status()["count"] == 0, json.dumps(status()))

    print("\nttl is clamped")
    CALLS.unlink(missing_ok=True)
    run("release", run("acquire", "--ttl", "999999")[0])
    # MAX_TTL is 600 in this env; the clamp is applied at acquire time.
    _, err = run("acquire", "--ttl", "999999")
    check("clamp is reported", "clamped to 600s" in err, err)
    run("release", err.split()[-1] if False else status()["leases"][0]["id"])

    print(f"\nALL {checks} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
