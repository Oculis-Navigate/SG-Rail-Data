# Dataset releases

**Contributors collect and review. Administrators publish.** `main` holds reviewed inputs/baselines, not a mutable production pointer. Consumers use the repository's latest GitHub Release. The Python package and schema versions are independent. Use one date-based `YYYY.MM.DD.N` version per release; finish publishing one candidate before merging the next. Stage 3 previews and the D-028 baseline are not themselves published. The verifier builds `stage3-rail-lines.geojson` from the exact approved physical ways, includes OSM attribution/ODbL terms and checks the emitted file hash against the separately signed `config/stage3-release.json` (D-052). An unapproved inventory, date, input or asset hash blocks packaging; renew the release manifest for a new inventory before preparing that candidate.

## Pick a contribution lane first

- **Regular PR:** change code, tests, documentation, UI, pipeline settings or tooling without changing reviewed dataset artifacts. Branch, test, open the PR. No DataMall key, live collection, revision-pin renewal, version allocation, review form or release replay is required. CI still checks configuration and runs ordinary unit/regression tests. The admin reviews and merges; consumers of the latest dataset see **no change**.
- **Dataset PR:** if the diff changes a reviewed baseline, station registry, release manifest, data rules or a versioned changeset, use the candidate procedure below. Mixed code + data PRs follow this lane too. An admin can take a merged code fix and prepare the data candidate separately, or agree a version with a contributor who prepares it. The version must be chosen **before** `prepare`; dispatch publishes that exact reviewed version, not a version invented after merge. Admin PR approval/merge and admin release dispatch are separate actions.
- Audit-only maintenance still gets ledger integrity and audit checks, but does not require a new version just to document a decision. The PR checklist is descriptive; committed/staged paths determine checks, not labels or contributor intent. Never split off a baseline edit as “code only” to avoid its review.

## Who does what, and what access costs money?

| Step | Actor and access | Paid plan? |
|---|---|---|
| Live collection, maps, comparisons, review paperwork, local replay, tests and hooks | Contributor with repository read access, Git, Python 3.12, `uv`, internet and their own DataMall account key in `.env`. Offline replay needs the retained inputs, not a live key. | No paid GitHub account, Copilot, hosted runner, VPN or production token required. A source API credential is separate from GitHub billing. |
| Submit a branch/PR and run CI | Contributor pushes to an allowed fork or branch; proposed permissions do not give ordinary contributors upstream write access. Forks of private repositories depend on organization policy. | PRs are not a premium feature. Standard hosted Actions runners are free for public repos; private repos use the owner's included minutes/storage and may incur overage. |
| Enforce PRs, required CI, CODEOWNERS review and protected `data-v*` tags | Repo/org admin configures branch protection, tag rulesets and the actual write-enabled owner team. A CODEOWNERS file alone does not enforce approval. | Public repo: repository-level controls available on Free. Private organization repo: generally Team or Enterprise (Pro is for personal repositories). |
| Approve deployment to `dataset-production` independently of the PR | Designated environment reviewer. Disable self-review/bypass as appropriate. The environment and its required reviewers must actually exist; referencing the name in YAML is not enough. | Public repo: Free can use required environment reviewers. **Private/internal repo: native required environment reviewers need Enterprise**; Team can use private environments but not this review gate. A personal Pro upgrade will not unlock it for an organization. |
| Dispatch and publish | Repository admin dispatches on `main`; workflow checks both dispatch and rerun actors. The protected job uses a scoped Actions token to create the tag and GitHub Release. | Creating a normal GitHub Release is not premium. Admin is a permission, not a subscription. Confirm immutable-release settings, tag rules and promotion identity with the actual organization admin. |

Plan references: [protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches), [rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets), [environment protection by plan](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments), [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases). Verify repository visibility, organization plan and enabled settings before enabling promotion. If a mandatory gate is unavailable, do not silently omit it: change the plan or agree on and implement a different release design first.

## What the three local hooks actually do

Install per clone: `git config --local core.hooksPath .githooks`; verify with `git config --local --get core.hooksPath`. Keep `origin/main` synchronized with upstream `main`, particularly when contributing from a fork.

| Hook | Trigger and effect |
|---|---|
| `pre-commit` | First checks the staged paths; regular commits exit before invoking Python/uv. Otherwise runs `dataset.py gate --staged` against the merge base with `origin/main` (falls back to `HEAD` if unavailable). Checks ledger history is append-only when touched. For tracked data-rule/baseline or changeset changes: requires **one new versioned changeset**, no unstaged/untracked review inputs or code in monitored directories, a non-stale review base, and successful package verification/offline replay. Ordinary unrelated commits do not trigger a dataset replay. |
| `commit-msg` | Only when `changesets/*/review.json` is staged: requires nonempty lines for station/exit additions and removals, other data, geometry, intervention changes, sign-offs, evidence and run. `Run:` must exactly match `candidate.json`. Checks format, not truth or authority. See [change-log format](change-log-format.md). |
| `pre-push` | Checks the refs being pushed. Regular pushes skip the local data checks; data/audit pushes run `config validate`, `audit validate` and the full unit suite in the frozen environment. It does not collect live data, tag or publish. |

Hooks are local and bypassable, not approvals or security boundaries. CI always runs config validation and tests. For data/audit diffs it also validates the ledger, checks regenerated sign-off pages and runs the package gate. Required CI and server-side permissions are what protect the shared repository.

## Local review (including the first release)

1. Start a branch from current `origin/main`. Install Python 3.12, `uv`, the locked dependencies and a local DataMall key as in the README. Run `git config --local core.hooksPath .githooks` once per clone.
2. Collect locally with `uv run sg-rail-station-data pipeline run --service-date YYYY-MM-DD`. Keep the original immutable run. Inspect both stages' `changes.json`, `reviews.json`, coverage, source revisions, maps and supporting evidence. A 403 or incomplete collection isn't a candidate. If collection or a parser failed before both reports existed, fix it and collect again. Schedule a maintainer reminder for quarterly collections: GitHub no longer fetches sources on a timer.
3. Record findings and proposed decisions using the [audit runbook](../audit/README.md). Obtain actual human authorization for interventions; contributors may propose but may not attribute approval to someone without permission. Record relevant decision/sign-off IDs, exact identities and evidence. Keep original raw evidence in a durable, access-appropriate archive; don't publish secrets, signed URLs or material without redistribution rights. A hash by itself isn't an archive.
4. Apply reviewed changes on the branch. After inspection and sign-off, update the Stage 1/2 comparison baselines from the reviewed outputs if needed; never compare an output only with itself as proof of approval. Replay the original inputs using `pipeline run --replay-sources runs/<live-id> --service-date YYYY-MM-DD`. The final run must exit **0**, and both baseline files must be the exact final `stations.json` bytes. A no-change live run can serve as both runs. Do not edit a finished run. For the initial release collect fresh inputs, not a historical replay.
5. Prepare the candidate (from the repository root):

   ```sh
   uv run python scripts/dataset.py prepare 2026.11.22.1 --base origin/main \
     --collection runs/<live-id> --run-dir runs/<final-id>
   ```

   The new `changesets/<version>/` includes only the four normalized replay inputs, original finding records, a hash manifest and a review form. Do not commit `runs/` or `.env`. In `review.json`, identify the reviewer, explain the practical effect and cite durable evidence with revisions and precise locators. Fill every generated identity/field and original finding entry: explain the old/new values, why accepted or resolved, and cite the applicable new `S-...` sign-off. List the new effective sign-offs in ledger order. If nothing changed, explain why a new dated release is warranted. Natural-language quality, evidence relevance and actor identity require human review; a script cannot certify them.

   ```sh
   uv run python scripts/dataset.py notes 2026.11.22.1
   uv run python scripts/dataset.py verify 2026.11.22.1
   ```

6. Stage inputs, approved decisions, ledger and complete candidate together. The pre-commit hook checks the staged data change against `origin/main`, rejects missing/rewritten packages and verifies offline reproduction. The commit-message hook checks the documented categories and run ID for a staged review. Pre-push runs tests/config/audit. Use `git log` or `git blame` on `changesets/<version>/review.json` and follow its sign-offs to the ledger; the GitHub PR records the authenticated review. Hooks are not access controls. CI independently validates the PR diff and replay with no source credentials. Draft audit records alone do not publish anything. Rebase and regenerate if the reviewed base is stale.

## Required GitHub administration (not configured by a checkout)

Before enabling promotion:

- Protect `main`: PRs, required `Validate / validate` check (select the actual check in Settings), up-to-date branch, dismiss stale approvals, code-owner review, no force pushes/deletes or contributor bypass. Assign `.github/CODEOWNERS` to an **existing write-enabled admin team** in your organization; verify GitHub resolves the team. Protect workflow, validator, config, audit, changesets and CODEOWNERS changes themselves.
- Create `dataset-production`, restrict deployment to protected `main`, require dataset-admin reviewers and disable self-review/bypass as appropriate. Set the **environment variable** `DATASET_PROMOTION_ENABLED=true` only after protections are verified. The workflow separately checks repository admin permission for the dispatch and rerun actors. Check your GitHub plan supports these protections.
- Enable immutable releases and protect creation/update/deletion of `data-v*` tags. Allow creation by the promotion identity (use a narrowly scoped GitHub App if your ruleset cannot distinguish the Actions token); do not give contributors bypass. Give ordinary contributors fork/PR access, **not repository write or release-management permission**: repository writers may otherwise create/edit releases directly, outside this workflow. A protected environment alone doesn't remove that power. Reserve this repo's GitHub `latest` for datasets.
- Delete the Actions `LTA_API_KEY` secret if nothing else uses it. No self-hosted runner, exit node or production credentials on contributors' machines are needed.

## Promote

After admin PR review and merge, an administrator dispatches **Promote dataset** on `main` with the exact version. The protected job checks the dispatch commit is still current, validates audit and release notes, replays the four inputs, verifies both stages' hashes and runs tests. It compares the predecessor with the latest published release and creates a new tag pointing at the reviewed commit. Only then does it publish the JSON/GeoJSON assets, manifest and verbatim `changes.md` as the latest release. Nothing writes back to `main`. Failure or review-required must not publish data. If tag creation succeeded but publication failed, inspect the tag, commit, assets and draft on GitHub; never retarget a partial release to different bytes. Resolve before merging another version. To correct or roll back published data, review and publish a new version explaining what it replaces; don't rewrite history.
