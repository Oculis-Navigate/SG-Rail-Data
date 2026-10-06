# Pipeline maintenance and decision audit plan

Status: proposed design, 2026-10-05. An [initial retrospective sign-off
inventory](../audit/README.md) and `audit validate` command now exist. The general
review, approval, commit enforcement and publishing workflows below remain
planned. Existing commands are documented in the README.

The goal is that a new maintainer can answer: what changed, what evidence supports
it, who decided, which exact data was approved, and how to reproduce or undo it.
Every hardcoded decision must have a durable explanation. Reusing an unchanged
decision should not require rewriting that explanation every quarter.

All readable change logs, sign-off summaries, commit bodies, and pull request
descriptions must follow the [change-log format](change-log-format.md). Put the
changes and evidence first. Use short active sentences and consistent terms.

## What exists today

| Capability | Current implementation | Missing maintenance capability |
|---|---|---|
| Run evidence | Separate run directories, source snapshots, manifest, `pipeline.jsonl` | One readable report covering both stages and all decisions |
| Change detection | Stage 1/2 `changes.json`, grouped `reviews.json`, coverage | Stable finding IDs and a complete mapping from findings to decisions |
| Manual coordinates | Hash-chained coordinate ledger and 28 retained final pins | Mandatory rationale, evidence and reviewer attribution for future edits |
| Hardcoded rules | Stage 1 reviewed decisions and frozen OneMap/supplemental baselines | One audit mechanism covering every rule, import, renewal and removal |
| Comparison baselines | Checked-in Stage 1 baseline; explicit Stage 2 baseline argument | Approval bound to exact artifact hashes and a durable release manifest |
| Automation | Quarterly/manual workflow uploads evidence, retains nonzero exit status | Review package, persistent evidence archive and explicit release promotion |

There are 30 legacy coordinate ledger events and 28 current reviewed pins. All
their note fields are blank. Preserve that history honestly; do not invent old
justifications. The newer 40 supplemental decisions have explanations and retained
alternatives, but the formats are separate. Current GitHub workflow artifacts are
retained for 90 days, so they cannot be the sole permanent evidence store.

The current Stage 2 comparator records every nonzero coordinate movement, with
movements over 25 m flagged as warnings and smaller ones as information. It does
not yet explicitly compare all coordinate-provenance fields. Proposed review must
also detect changes in provider, trust class, selection mode, decision ID and
source evidence even when the coordinates are identical. A small distance is not
proof of correctness: a point can cross a road or change doorway within 25 m.

## Three linked records

1. **Run log:** generated `pipeline.jsonl` records execution, failures, warnings,
   artifact hashes and which decisions were applied. Humans do not edit it.
2. **Decision history and sign-off ledger:** structured, versioned records contain
   the human or agent's explanation, exact before/after values and evidence. An
   append-only ledger explicitly signs off each intervention and any subsequent
   change or retirement; corrections supersede rather than erase history.
3. **Review report:** generated Markdown presents changes, map comparisons,
   rationale and outstanding work. It links to source snapshots and decision IDs.
   JSON is the canonical record; Markdown is a readable rendering, not a second
   independently maintained source of truth.

Proposed storage:

```text
audit/
  decisions/<decision-id>.json       # immutable submitted decision
  decision-ledger.jsonl              # append-only sign-off and lifecycle events
  evidence/<sha256>/...              # retained source excerpt/image/response
  legacy-import.json                # declared limits of inherited evidence
releases/
  <release-id>/manifest.json         # both stages, decisions and artifact hashes
  current.json                      # pointer to the approved release
runs/<run-id>/
  pipeline.jsonl
  review/report.md
  review/findings.json
  review/decision-usage.json         # applied/stale/superseded/unmatched decisions
  review/package.json               # hashes of everything being reviewed
```

Keep small decision records and excerpts in Git. Store large immutable snapshots
in a durable artifact archive, referenced by content hash and a stable location.
Approved releases and all evidence supporting active decisions must remain
retrievable beyond the CI artifact retention period. Preserve superseded evidence
for historical audit and rollback. A hash proves byte identity, not truth or who
approved it; attribution comes from recorded actors and repository review history.
Never retain credentials or signed download URL secrets in evidence or logs.

## Required decision record

| Field group | Required content |
|---|---|
| Identity | Decision ID, schema version, stage, stable station/exit IDs, exact affected fields; preserve MRT/LRT/shared scope |
| Trigger | Originating run ID, stable finding IDs, prior release ID, source page/revision/hash changes |
| Change | Typed exact before/after values, coordinate system/order, provider and trust class; create/delete uses explicit absence |
| Interpretation | Decision category, explanation, alternatives rejected, limitations and unresolved uncertainty |
| Evidence | URL, publisher, page/document title, retrieval time, source revision/publication date where available, snapshot hash/path and precise locator |
| Claims | What each evidence item supports: exit existence, location, opening date, closure, accessibility, bus mapping, etc. |
| Method | Source import, manual placement, georeferencing, source correction or physical relocation; approximation/accuracy when known |
| Actors | Author and actor type; agent tool/version when available; human approver or explicitly recorded delegated authority |
| Validity | Effective dates where relevant, exact source guards, review-due date, superseded decision IDs |
| Integrity | Expected input hashes, resulting rule/baseline hashes, related approval event and package hash |

Unknown fields, invalid types, missing required evidence, empty explanations,
stale before-values or contradictory active decisions fail validation. A software
check can require these fields and verify hashes; it cannot establish that a
written explanation is true. The reviewer must inspect the cited evidence.

An agent may prepare an explanation and evidence package. It must identify itself
as an agent and distinguish inference from observation. It cannot manufacture a
human approval. If a maintainer delegates a class of decisions to an agent, record
that scope explicitly and reference the authorization in each approval event.

Bulk acceptance is allowed for an enumerated set of identical cases: name every
affected identity and its before/after hash. Never use a wildcard approval such
as “accept all warnings from this source.” Original provider provenance and
trust class survive every import; accepting a OneMap point does not relabel it
as a manually inspected entrance.

## Sign-off ledger for every intervention

`audit/decision-ledger.jsonl` is the canonical sign-off history, separate from
machine execution logs. Every new override, coordinate edit, trusted import,
source-guard renewal and intervention removal needs an explicit sign-off event.
Each event records:

- Unique event ID, timestamp, decision ID/version and exact affected identities.
- Operation: add, amend, revalidate, retire or replace; disposition: approved or
  rejected. References connect a replacement to the decision it supersedes.
- Author, approver and actor types, including any delegated authorization.
- Rationale and evidence references, before/after hashes, originating run and
  exact reviewed package hash.
- Previous event hash and current event hash to make accidental history edits
  detectable. Repository review history establishes attribution; this chain is
  not a substitute for authenticated approval.

An active intervention must resolve to an approved ledger event. Removing an
intervention also requires a decision: explain why it is no longer needed, what
source/rule replaces it, and whether the resulting output changes. For example,
retiring a manual gap fill because Kartographer now supplies the entrance must
record both coordinates and their provenance even if the points are identical.
Retirement removes the active rule, never its historic ledger records.

Unchanged decisions are referenced in each run's usage log without duplicating
sign-offs. An expired or changed guard requires a new revalidation event. An agent
can draft the event; it can only approve within explicitly recorded delegated
authority. A commit message or a Git `Signed-off-by` trailer alone is not evidence
that the specific data decision was approved.

## Required commit summaries

Every commit must say whether it changes station/exit data or intervention
records. Data-changing commits list counts **and exact identities** for:

- Stations added/removed, and any station identity or code changes.
- Exits added/removed, preserving station name and MRT/LRT/shared scope.
- Other material changes such as moved pins, closures, platforms or bus links.
- Interventions added, amended, revalidated or retired, with decision IDs and
  corresponding sign-off event IDs.

Generate a proposed commit message from the semantic diff and ledger delta;
the author supplies a clear subject and explanation. Use explicit `none` for
empty categories. Compare changes against the commit's parent; separately name
the prior approved release used for a release-wide audit. New exits at newly
added stations are counted and listed too. For large changes, include counts
and key changes in the body and reference a checked-in complete identity list.
Do not describe an added catalog entry as an operational opening unless the
effective-date and service evidence support that statement.

Illustrative template, with placeholder values rather than actual changes:

```text
data: add Station X Exit E and retire Station Y's manual gap fill

Stations added: none
Stations removed: none
Exits added: 1 — station-x/MRT:E
Exits removed: none
Other data changes: station-y/MRT:B uses Kartographer; coordinates unchanged
Interventions added: D-101 — station-x/MRT:E coordinate gap fill
Interventions amended/revalidated: none
Interventions retired: D-042 — station-y/MRT:B; replaced by Kartographer
Sign-offs: S-201 (D-101), S-202 (retire D-042)
Audit: audit/change-sets/<change-id>.json
Run: <run-id>
```

A documentation/code-only commit with no data or intervention record changes
can state `Data/intervention records: unchanged (documentation/code only)`.
That statement does not claim the code cannot affect a future pipeline run.

CI must check that semantic changes and intervention deltas match the commit
summary/change-set, and that each intervention operation has a valid sign-off.
Local hooks can assist but are not the enforcement mechanism. Squash merges
must retain the complete summary and sign-off references for the combined diff.
Draft decisions may be committed as drafts, explicitly labelled pending; they
cannot be activated in pipeline configuration or promoted without sign-off.
The approved package hash covers the reviewed payload, excluding the approval
event itself, so signing it does not create a circular hash dependency.

## Example: moving Exit E at a hypothetical station

The readable decision should say:

> Station X, MRT Exit E: replace the old coordinate with the new coordinate
> shown in the before/after map. SgWiki revision R2 moves its marker relative to
> R1. Operator notice N states the entrance was relocated effective date D;
> retained image N, page 2, marks the new doorway. The new point was manually
> placed at that doorway, with approximate accuracy recorded. The notice
> supports the physical relocation; the image supports placement. The previous
> point and rejected alternative remain in the record. Authored by A, approved
> by B for the exact review package hash.

This is a template, not a claim about a real station. Actual records must contain
real URLs, snapshots, dates, coordinates and locators before approval.

A moved SgWiki marker alone supports “the source coordinate changed,” not “the
physical exit moved.” If no relocation evidence exists, classify it as a source
correction or an uncertain source update. A news article saying a station opened
does not establish exact coordinates. External corroboration is particularly
useful for claimed relocation, opening or closure; do not require a news article
for a small coordinate correction supported by a clear operator map.

## Routine run and review process

1. **Collect a candidate run.** Use a pinned code revision, locked dependencies,
   explicit service date and last approved release. Retain raw source revisions
   and hashes. A network or parsing failure is a failed collection, not evidence
   that an exit or station disappeared. Never overwrite the last approved data.
2. **Generate the review package.** Show grouped severity/reason counts first,
   followed by each station's exit additions/removals, coordinate/provider changes,
   source revisions, bus links, landmarks, levels/platforms, operating hours and
   accessibility/closure claims. Include partial inventories, stale rules, source
   coverage and overdue reviews even when there is no coordinate change.
3. **Explain affected decisions.** Link each finding to a new decision or an
   existing applicable decision. The map view should show old/new pins, alternatives,
   displacement and source evidence. Pin movement alone cannot save an approved
   change: rationale and evidence are mandatory. An unchanged reused rule appears
   in `decision-usage.json` with its original explanation and current guard result.
4. **Validate and replay.** Apply proposed decisions to a separate candidate
   configuration and replay the exact captured inputs. Tests cover the changed
   parser/rule, and the semantic diff must equal the changes being approved.
   Findings can only be discharged by exact matching decisions; malformed source
   contracts require a validated parser/schema fix and cannot be waived away.
5. **Approve the exact package.** Record hashes for outputs, source snapshots,
   code, configuration, decisions and the prior release. If any reviewed bytes
   change, approval no longer applies. Baseline replacement must reference this
   approval; merely passing today's output as its own baseline is not approval.
6. **Promote and retain.** Publish both stages together as one versioned release,
   then atomically update the current pointer. Keep the previous release available.
   Rollback creates a recorded event naming the old release and reason; it does
   not delete the rejected release or its decisions. Distinguish approved data
   artifacts from any actual deployment to the application.

Use separate submission and approval events so human actions happen outside a
finished immutable run. A follow-up run references the new decision set and the
original run; never edit old logs to make a failed run look successful.

## Situations maintainers must inspect

| Trigger | Required action |
|---|---|
| SgWiki revision changes; hardcoded coordinate/rule used | Preserve current blocking guard. Review relevant page/map differences. Renew with an explicit explanation even if the coordinate stays unchanged; never bump all revision pins automatically. |
| Revision change is formatting only | Record old/new revisions, unchanged relevant semantic content and a no-data-change revalidation. A grouped decision may cover named affected exits on that page. |
| Kartographer changes | Show overlay and page hashes plus pin differences. Review small movements and provenance changes too. Retain warning severity for the source-change case; require a disposition before promotion. |
| New exit | Verify identity/scope, source evidence, opening/effective date where stated, destinations and bus links; add coordinates or leave explicitly unresolved. Update Stage 1 and Stage 2 in the same package. |
| Exit disappears or closes | Determine closure, renaming, merger or missing source evidence. Preserve historic identity and closure evidence; do not infer closure from a missing table row. Check downstream bus links and alternatives. |
| New station/line | Reconcile GTFS service dates with the station catalog; add explicit station/page/code mappings, all relevant Stage 1 semantics and Stage 2 coordinates. A future wiki page alone does not make a station operational. |
| Platform/level/bus/access change | Cite the exact row, map legend or notice. Check line/direction and MRT/LRT scope, repeated platform letters on different levels, and shared versus separate exits. No invented inaccessible/accessibility classification. |
| Source contract failure or partial evidence | Inspect retained raw response. Add support and a regression fixture for a valid new format. Partial information must remain visibly partial or have an exact evidenced override. |
| Sources disagree | Retain all candidates and say why one was selected. Source agreement is useful evidence but may reflect shared upstream data. Do not average coordinates or silently replace provenance. |
| No wiki edit but a hardcoded point may be stale | Review due dates still apply. Recheck supporting operator/other evidence; unchanged wiki content cannot prove the real-world entrance is unchanged. |

Quarterly collection is the normal cadence, supplemented by manual runs when a
known opening, closure or major source change warrants one. Review new changes and
due decisions first; unchanged decisions should remain easy to trace without
flooding the report. Proposed default: yearly revalidation for frozen imports,
earlier for uncertain placements or planned works. A due decision blocks a new
release until revalidated; the last approved release remains available with its
age visible. An effective future change requires a run on the relevant date.

Warning grouping must not hide individual exceptions. Every changed hardcoded
field, including a no-coordinate-change revision renewal, needs an exact decision.
Identical coordinate values with a different source or trust class still count
as an auditable change. Nonzero movements are always listed; severity can help
prioritization but must not silently suppress small changes.

## Migration and implementation phases

1. **Runbook and review report.** Turn this proposal into an operator runbook and
   generate `review/report.md` plus stable findings from existing artifacts. Show
   current source counts, decision usage and precise review actions. Add explicit
   provenance diffs. No change to resolution precedence.
2. **Unified decisions and evidence.** Introduce strict schemas and append-only
   events covering Stage 1 rules, Stage 2 pins, imports and guard renewals. Add a
   decision editor/CLI and require notes/evidence for future edits. Migrate existing
   records by reference, preserving the original ledger bytes/hash chain. Mark
   missing historic rationale as `legacy_rationale_missing`, with an explicit
   imported-snapshot acceptance and follow-up review; do not claim it was recovered.
3. **Enforced approval and promotion.** Reject config edits that have no matching
   approved decision in the sign-off ledger, including intervention removals.
   Generate and validate commit summaries against semantic and ledger changes.
   Bind acceptance to the exact package, implement release manifests, baseline
   advancement and rollback, and retain evidence durably.
4. **CI and review UI integration.** Produce a readable workflow summary and
   downloadable report/map package. Connect map editing to decision submission;
   keep draft pins distinct from approved data. Scheduled runs collect and report;
   they do not approve themselves. Add due-review visibility and maintenance-owner
   notification using the repository's chosen workflow.

Bootstrap the first Stage 2 release from a fresh live run. The current 614/614
offline replay used a temporary Woodlands North revision-adjusted fixture and
must not be promoted as a verified live baseline.

Acceptance tests should demonstrate that an unchanged rerun reuses the same
decisions, a pin edit without evidence is rejected, a source-only change is
visible, a harmless wiki edit requires an explicit renewal, a real relocation
preserves before/after evidence, and a new station cannot pass through a catalog
or coordinate gap silently. Tampered evidence, stale approvals and undocumented
config edits must block promotion. A rollback must restore a matched pair of
Stage 1/2 artifacts while preserving all audit history.
Also test unsigned intervention removal, a commit summary omitting an added
exit, and a squash summary missing sign-off references: all must fail validation.
