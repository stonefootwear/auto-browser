# Secure browser recovery and host memory headroom

## Incident: 2026-10-17

The browser container stayed running, but kernel global-OOM events on October
15-16 killed its Chrome processes. Its cgroup recorded 14 OOM kills without
hitting its own 6 GiB memory limit (recorded peak approximately 1.6 GiB).
The host's 4 GiB swap was full. The controller continued using crashed pages
and repeatedly failed with `Page.screenshot: Target crashed`; eventually its
Playwright driver exited. The broker then denied viewing the no-longer-active
session. The viewer kept reloading every 1.5 seconds, masking terminal closure.

Historical node logs also contained `ENOSPC`. At this intervention disk had
space again; no browser profiles, saved logins, or application data were deleted.

## Applied resource budget

The physical host has approximately 15.2 GiB RAM and runs several other apps.
Do not treat the browser's 6 GiB maximum as reserved physical capacity.

| Component | RAM maximum | RAM plus swap maximum | Best-effort reservation |
| --- | ---: | ---: | ---: |
| Browser | 6 GiB (unchanged) | 8 GiB | 1 GiB |
| Controller | 1 GiB (unchanged) | 1.5 GiB | 256 MiB |

The old equal RAM/total limits disabled container swap. The additional bounded
swap allowance lets the kernel reclaim cold pages during transient host
pressure. Reservations are best-effort cgroup reclaim protection, not a
guarantee against global OOM. No OOM score was changed, and no other app's limits
or availability were altered.

Run `python3 deploy/ensure-browser-swap.py` for a read-only plan, then the same
command with `--apply` to create the reviewed 4 GiB supplemental swap file.
It checks the filesystem, permissions, existing signature, and remaining disk
headroom, verifies activation, and only then adds its exact entry to fstab.
It never replaces or disables the original swap. The operation was applied to
this host, increasing total swap from 4 GiB to 8 GiB, leaving about 10 GiB free
disk immediately after allocation. It persists across host restarts.

Keep the existing one-active-browser admission limit. Swap absorbs temporary
peaks; sustained excess RAM demand requires isolating workloads or additional
physical capacity, not repeatedly increasing swap or weakening auth.

## Recovery behavior

Renderer crashes must be distinguished from a user closing a tab or window.
Recover within the same still-live context, using a healthy tab or a bounded
replacement page; never automatically relaunch a closed browser or revive a
revoked session. Preserve tenant/profile boundaries and normal phone-code
requirements for opening a new human session.

The viewer checks its own authenticated ownership-bound status before loading
noVNC. A temporary outage uses capped backoff; confirmed closure or expired
authentication stops reconnection and presents the existing human opening or
sign-in flow. Both status checks and iframe connection attempts have deadlines.
The VNC authorization path remains fail-closed.

## Operations and rollback evidence

Host evidence is private under:

```
/var/backups/auto-browser/browser-recovery-20261017/
```

It includes the previous container memory settings. The original fstab is
saved as `/etc/fstab.bak-auto-browser-20261017`. Application database backups
and previous image references are retained before code deployment.

The deployed `secure-browser-service` branch contained tab activity and
auto-follow behavior absent from the fork's main branch. That deployed baseline
was merged before shipping, preserving those changes. After integration,
71 focused controller tests and 9 viewer/portal tests passed, together with
targeted lint and tenant compose validation. Database backups passed SQLite
integrity checks. The optional real-browser probe is
`smoke/renderer_recovery.py`; run it only against a disposable browser server
on a temporary private network, never the owner's live profile.

Rollback code by redeploying the saved image IDs for only portal/controller;
do not restore a database over newer user activity. The browser runtime itself
need not restart for these changes. To roll back resource settings, first
confirm actual swap use fits the previous limits. Never run `swapoff` under
memory pressure: remove only the supplemental fstab entry and deactivate its
file during a safe maintenance window with adequate RAM. Preserve the original
swap and all other fstab entries. No automatic rollback should kill a live
browser merely to restore an old budget.
