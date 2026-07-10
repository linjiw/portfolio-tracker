# Security and privacy

This repository processes broker exports and produces artifacts containing position sizes,
cost basis, transactions, cash flows, and portfolio history. Treat every real input and every
generated artifact as private financial data.

## Data-handling contract

- Keep broker CSVs, `output/`, `outputs/`, screenshots, logs, local credentials, and
  `data/option_chain_snapshots/` out of Git. The ignore rules cover these paths, but do not
  override them with `git add -f`.
- Store Telegram and FMP credentials only in the documented current-user-owned `0600` files
  under `~/.config/ptrak/`; never place credentials in this repository or a LaunchAgent plist.
- Runtime artifact directories must be `0700` and files `0600`. Producers should use
  `scripts/artifact_io.py` for strict-JSON, atomic, private publication.
- Before publishing a branch, verify both the current index and history:

  ```sh
  git ls-files | rg '^(output|outputs|screenshots|data/option_chain_snapshots)/|(^|/)(telegram|fmp)\.json$|\.env$'
  git log --all --name-only --pretty=format: | sort -u | rg '^(output|outputs|screenshots|data/option_chain_snapshots)/'
  ```

## Historical generated-data exposure

A July 2026 audit confirmed that older Git objects contained generated dashboards, sync logs,
and caches even though the current tree and current `origin/main` no longer track `output/`.
The dashboard and sync-log objects contain private portfolio-derived fields. Some detached
historical commits were still addressable on the public GitHub repository at audit time.

Do not paste object IDs or artifact contents into public issues. History removal is a coordinated,
destructive incident-response operation; it is intentionally not performed by normal review or
automation.

### Safe remediation procedure

1. Temporarily make the remote private and pause pushes. Notify every collaborator; do not let
   anyone merge an old clone after cleanup.
2. Make an encrypted, offline mirror backup for recovery and legal/audit needs.
3. In a new disposable mirror clone, inventory every branch, tag, pull-request ref, and historical
   private path. Run a secret scanner over all objects. Assess whether any API token, account
   identifier, or other credential requires rotation; portfolio facts cannot be “rotated.”
4. Install `git-filter-repo` from its trusted upstream. First run `git filter-repo --analyze`, then
   perform a dry run that removes generated paths from all refs. Review the changed-ref and object
   reports before doing the real rewrite. A representative path set is `output/`, `outputs/`,
   `screenshots/`, and `data/option_chain_snapshots/`; add every path found by the inventory.
5. Re-run the full object/path and secret scans against the rewritten mirror. Confirm source,
   examples, tags, and active branches still exist and no generated private blob is reachable.
6. Coordinate protected-branch changes, then force-update the affected branches and tags. Delete
   obsolete remote branches rather than leaving old objects reachable. Require collaborators to
   re-clone; do not rebase or merge pre-cleanup clones.
7. Ask GitHub Support to purge cached views, pull-request refs, and dangling sensitive objects.
   Rewriting reachable refs alone does not erase forks, clones, caches, or already downloaded data.
8. Keep the repository private until direct historical object URLs no longer resolve and the
   post-remediation scan is clean. Record the incident and rotation decisions without recording
   the sensitive values themselves.

No recognizable committed API token or private-key signature was found in the current-tree scan
performed with this audit. That result does not replace an all-object secret scanner during the
history-remediation procedure.

## Dependency integrity

`requirements.txt` uses bounded compatible ranges, not a hash-locked environment. For a
decision-support deployment, build a reviewed lock file with hashes for the target Python/macOS
environment, scan it for known vulnerabilities, and update it deliberately. Do not assume a fresh
install on a later date is bit-for-bit equivalent to a validated run.
