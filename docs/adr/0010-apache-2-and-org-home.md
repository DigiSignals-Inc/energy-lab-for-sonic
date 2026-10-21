# ADR-0010: Apache 2.0, published by the DigiSignals-Inc organization

- **Status:** Accepted. The commit-history decision is superseded by [ADR-0012](0012-fresh-public-history-and-dco.md).
- **Date:** 2026-10-08

## Context
The repo started as a private repo on a personal account. Before making it public, we had to settle three things that are hard to change later:

- **A license.** Without one, people could view and fork the public repo, but not legally change it or redistribute it.
- **A home.** Every clone command and every shared link names the repo's owner. A moved repo redirects only until someone reuses the old name, so the time to move is before anyone has the URL.
- **Commit authors.** Every commit carries its author's name and email, and these become public with the repo.

SONiC's own code is licensed under Apache 2.0, for example sonic-buildimage, sonic-swss and sonic-utilities. We plan to send fixes upstream once this repo is public, and some of this code may move into SONiC projects.

## Decision
- [LICENSE](../../LICENSE) holds the unmodified Apache License 2.0 text, so GitHub detects it as Apache 2.0. There is no NOTICE file and no per-file license header.
- The License section of [README.md](../../README.md) links to LICENSE and states: "SONiC is a trademark of The Linux Foundation. This project is not affiliated with or endorsed by The Linux Foundation or the SONiC project."
- The repo lives at `github.com/DigiSignals-Inc/energy-lab-for-sonic`. Development before the first public release happened in a private repository in the organization, and both `git clone` commands in the README use the public URL.
- The repo was first called `sonic-energy-lab`. We renamed it before going public, because the Linux Foundation's [trademark usage rules](https://www.linuxfoundation.org/legal/trademark-usage) say a mark "should not be used as part of your product name" and give "<your product name> for <The Linux Foundation mark>" as the correct form. Hence "energy lab for SONiC". The settings prefix changed from `SEL_` to `LAB_` at the same time.
- ~~Commits keep their authors' names and company email. We do not rewrite history.~~ Superseded by ADR-0012: the public repository starts from one reviewed first commit.

## Alternatives considered
- **MIT:** it is short and permissive, but it has no explicit patent grant. Code taken upstream would also carry a different license from the SONiC code around it.
- **Stay on the personal account:** the repo's settings would depend on one person. Moving it after launch would change the URL that people had already cloned and linked.
- **Rewrite commit emails to noreply addresses:** this changes every commit hash, and we are fine with the company address being public.

## Consequences
- Code can move into SONiC projects under the same license, with no relicensing. Upstream pull requests still follow SONiC's own process: signed-off commits (`git commit -s`) and SONiC's contributor license agreement, which the Linux Foundation's EasyCLA bot checks on each pull request.
- Users get a patent license from every contributor (Section 3). That license ends for anyone who sues, claiming the work infringes a patent.
- Anyone who redistributes the code must include the license, keep any copyright, trademark and attribution notices (here, the README trademark line) and mark the files they change (Section 4).
- Pull requests to this repo come in under Apache 2.0 unless the contributor states otherwise (Section 5).
- The license grants no rights to DigiSignals' names or marks (Section 6). SONiC is The Linux Foundation's mark, not ours, so we use the name only to describe what the lab runs and keep the trademark notice in the README.
- The organization's owners and admins control visibility and repository settings. A member with Write access can push but cannot make the repo public.
- Once the repo is public, anyone can see contributors' names and the email addresses in their commits.
