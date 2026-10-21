# ADR-0012: Publish a fresh history, and sign off every commit

- **Status:** Accepted
- **Date:** 2026-10-21

## Context
ADR-0010 planned to publish the repo with its full commit history. Before publishing, we reviewed every commit, branch and pull request. The early history held internal working notes and a personal email address that were never meant to be public. GitHub keeps pull-request refs and the commits they point to after a force-push, so rewriting the history in place would not have removed them.

Once the repo is public, people outside DigiSignals will send changes. Each change needs a clear statement that its author has the right to submit it under Apache 2.0.

## Decision
- The public repository `DigiSignals-Inc/energy-lab-for-sonic` starts from one first commit that holds the reviewed tree. The private repository where the lab was developed stays private.
- Earlier contributors are credited in that first commit with `Co-authored-by` lines, using addresses they agreed to make public.
- From the first public commit on, `main` is never rewritten. A branch ruleset blocks force pushes and deletion.
- Every commit is signed off under the [Developer Certificate of Origin](https://developercertificate.org/) (`git commit -s`), as [CONTRIBUTING.md](../../CONTRIBUTING.md) explains. Maintainers check the sign-off before merging. There is no separate contributor agreement.

## Alternatives considered
- **Make the private repository public as it is:** it would publish the internal notes and the personal address.
- **Rewrite the history in place and force-push:** the old commits stay reachable through the existing pull request, so this would not remove them.
- **A contributor license agreement (CLA):** more work for contributors and needs a bot. The DCO plus Section 5 of Apache 2.0 is enough for a project this size. SONiC itself uses both, and we can add a CLA later if we need one.

## Consequences
- `git log` and `git blame` in the public repo start at the first public commit. The DigiSignals team keeps the earlier history privately.
- Commit hashes quoted in older notes don't exist in the public repo.
- A pull request with a commit that isn't signed off waits until the author signs it off (`git commit --amend -s --no-edit`, or `git rebase --signoff main` for a whole branch).
- Contributors' names and the email addresses in their commits are public. Anyone who prefers can commit with their GitHub noreply address.
