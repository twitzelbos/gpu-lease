# gpu-lease

**Borrow the GPU from long-running services, and be certain they come back.**

On a single-GPU host an inference server and a training service happily consume
the whole card. When a CI job needs that GPU, something has to step aside — and
the hard part isn't stepping aside, it's guaranteeing the services return when
the job crashes, is cancelled, or times out.

`gpu-lease` is a small CLI that pauses a configured set of systemd units for the
duration of a **reference-counted, expiring lease**.

```bash
LEASE=$(sudo gpu-lease acquire --ttl 1800 --reason "$GITHUB_WORKFLOW")
# ... the GPU is yours ...
sudo gpu-lease release "$LEASE"
```

No daemon, no dependencies beyond the Python standard library, and no knowledge
of the services beyond their unit names.

---

## Why not just stop and start the units?

Because `stop` has no deadline. A workflow that stops the services and dies
leaves them stopped, and the inference endpoint stays down until a human
notices.

Every lease carries an expiry. A timer sweeps for stale leases each minute and
brings the services back, so the worst case for a dead runner is that the GPU
stays paused until the TTL elapses.

|  | plain `systemctl` | `gpu-lease` |
|---|---|---|
| runner dies mid-job | services stay down indefinitely | released at TTL, within a minute |
| two jobs need the GPU | second `start` resurrects it under the first | refcounted; resumes on last release |
| duplicate cleanup step | second `start` fights the first | no-op |
| who is holding it, and why | nothing to inspect | `gpu-lease status` |

---

## How it works

```
  acquire ──▶ leases: 0 → 1 ──▶ stop units, wait for VRAM to be freed
  acquire ──▶ leases: 1 → 2 ──▶ (nothing; already paused)
  release ──▶ leases: 2 → 1 ──▶ (nothing; still held)
  release ──▶ leases: 1 → 0 ──▶ start units
     TTL  ──▶ reaper sweeps ──▶ start units if that was the last one
```

Leases live in `/run/gpu-lease` on tmpfs, one JSON file per holder, guarded by
`flock` so refcount transitions are atomic. A reboot clears them, which is
exactly right: nothing is held, and the units start normally on boot.

---

## Commands

| Command | Effect |
|---|---|
| `acquire [--ttl N] [--reason TEXT]` | take a lease; stop the units if first; print the lease id |
| `release <id>` | drop a lease; start the units if last |
| `status [--json]` | holders, expiries, unit states, free VRAM |
| `reap` | release expired leases — run by the timer |

`--ttl` defaults to 3600 s, capped at 14400 s so a typo cannot park the GPU for
a week.

```console
$ sudo gpu-lease status
held      : True (2 lease(s))
gpu free  : 24108 MiB
  deberta-tuner.service        inactive
  sglang.service               inactive
  lease 6b1f0a2c9d34 owner=twitzel held=412s expires_in=1388s reason='nightly-eval'
  lease c40d5e11ab77 owner=twitzel held=95s  expires_in=1705s reason='pr-1284'
```

---

## Ordering is load-bearing

Stopping and starting are **not** mirror images.

**Stopping** — the training service goes first. On shutdown it hands the GPU
back by starting the inference server, so stopping the inference server first
would see it resurrected moments later. After both stops the tool sweeps once
more for precisely that race, then waits for the driver to report the VRAM
actually freed.

**Starting** — the inference server goes first, then the trainer. If the trainer
has queued work it will stop the inference server again within a couple of
seconds: one wasted start. That is the better trade, because a trainer only
restarts a co-tenant *it* stopped, so starting it alone would leave the endpoint
down whenever its queue happened to be empty.

## In-flight training jobs

`acquire` preempts immediately. Stopping the trainer makes its worker unwind
within seconds; a job pauses at its last checkpoint and resumes once the lease
is released. At most the epoch in flight is lost, and only for jobs with
checkpointing enabled.

---

## Install

```bash
sudo ./deploy/install.sh
```

Installs `/usr/local/bin/gpu-lease`, a sudoers grant for the runner user, and
`gpu-lease-reap.timer`.

The grant is on the **tool**, not on `systemctl`. Granting `systemctl stop` for
those units would let the runner stop them indefinitely, with no expiry and no
record of who did it. Routing through the tool means every hold is refcounted,
time-boxed and attributable.

> Because that grant makes the binary root-equivalent for the runner user, the
> installer verifies it is `root root 755` and refuses to continue otherwise. A
> writable copy would be a privilege escalation.

## Configuration

Environment variables, read at startup:

| Variable | Default | Purpose |
|---|---|---|
| `GPU_LEASE_UNITS` | `deberta-tuner.service,sglang.service` | units to manage, in **stop** order |
| `GPU_LEASE_DIR` | `/run/gpu-lease` | lease state |
| `GPU_LEASE_DEFAULT_TTL` | `3600` | default lease seconds |
| `GPU_LEASE_MAX_TTL` | `14400` | hard cap |
| `GPU_LEASE_FREE_TARGET_MIB` | `20000` | free VRAM that counts as released |
| `GPU_LEASE_FREE_WAIT` | `90` | seconds to wait for that |
| `GPU_LEASE_SYSTEMCTL` | `systemctl` | overridable for testing |
| `GPU_LEASE_NVIDIA_SMI` | `nvidia-smi` | overridable for testing |

---

## GitHub Actions

```yaml
jobs:
  gpu-test:
    runs-on: [self-hosted, gpu]
    steps:
      - uses: actions/checkout@v4

      - name: Acquire the GPU
        id: gpu
        run: |
          echo "lease=$(sudo gpu-lease acquire --ttl 1800 \
            --reason '${{ github.workflow }}#${{ github.run_id }}')" >> "$GITHUB_OUTPUT"

      - name: Run GPU tests
        run: pytest tests/gpu

      - name: Release the GPU
        if: always()          # also on failure and cancellation
        run: sudo gpu-lease release "${{ steps.gpu.outputs.lease }}"
```

`if: always()` matters. Without it a failing test holds the lease until it
expires — the reaper is the backstop, not the plan.

---

## Tests

```bash
python3 tests/test_lease.py     # 21 checks; no GPU, no root, no services
```

`systemctl` and `nvidia-smi` are replaced with recording stubs and the state
directory is a temp dir, so the suite drives the real lease logic and the real
ordering without touching the system. It covers refcounting, both orderings,
expiry and reaping, TTL clamping, and releasing an unknown id.

## Limits

- **No queueing.** `acquire` never blocks; holders share by agreement, not by
  scheduling.
- **No proof the GPU is idle** beyond a free-VRAM check — another process could
  claim it in the gap.
- **One-minute reaper resolution**, so a lease can outlive its TTL by that much.

## Licence

MIT — see [LICENSE](LICENSE).
