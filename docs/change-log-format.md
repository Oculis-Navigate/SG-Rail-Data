# Change-log format

Use this format for a pipeline review, a commit, and a pull request.

Use simple technical English.
Use short sentences and active voice.
Use one term for one meaning.
Use the same station and exit identity in all files.

Do not describe all the work that you did.
Record the change and the evidence.
List only changed data.

## Changes

List these items:

- Stations added or removed.
- Exits added or removed.
- Coordinates that moved.
- Source or trust-class changes.
- Bus, platform, level, closure, hours, or accessibility changes.
- Released network-geometry lines, branches, segments, or provenance changes.
- Interventions added, changed, revalidated, or retired.

Use `None` when there is no item.

For each item, give:

- The station ID.
- The exit ID, when applicable.
- The line, branch, segment and source relation or way IDs for network geometry.
- The old value.
- The new value.
- The effective date, when known.

## Evidence

For each change, give:

- The publisher and source title.
- The URL or retained file path.
- The source revision or publication date.
- The retrieval date.
- The file hash.
- The exact table row, page, marker, record, OSM relation, or OSM way.
- The OSM element version and timestamp for network geometry.
- The claim that the evidence supports.

State when the evidence supports an inference only.
Do not use a source revision change as proof of a physical change.

## Human decisions

For each intervention, give:

- The decision ID.
- The sign-off ID.
- The decision status.
- The reason for the decision.
- The alternatives that the reviewer did not select.
- The reviewer or delegated authority.

An agent must identify an inference as an inference.
An agent must not record a human approval without authorization.

## Validation

Give the commands and results.
Give the pipeline run ID.
Give the approved baseline ID.
Give the number of tests that passed.
Give all remaining warnings and errors.

## Template

```text
Changes
- station-x/MRT:E moved from [old] to [new].
- Stations added: None.
- Stations removed: None.
- Exits added: None.
- Exits removed: None.

Evidence
- E-101: Operator notice, page 2, hash <sha256>.
  It states that Exit E moved on <date>.
- E-102: Operator map, marker E, hash <sha256>.
  It shows the new entrance position.

Human decisions
- D-101 / S-201: Approved the new Exit E coordinate.
  The reviewer did not select the old coordinate because <reason>.

Validation
- Run: <run-id>.
- Baseline: <release-id>.
- Tests: <count> passed.
- Warnings: None.
- Errors: None.
```

## Commit summary

The commit body must list these categories:

```text
Stations added: None
Stations removed: None
Exits added: None
Exits removed: None
Other data changes: <items or None>
Network geometry changes: <line/segment identities or None>
Interventions added: <decision IDs or None>
Interventions changed or revalidated: <decision IDs or None>
Interventions retired: <decision IDs or None>
Sign-offs: <sign-off IDs or None>
Evidence: <audit package or report path>
Run: <run-id>
```

Do not use `None` when the category contains an item.
Do not omit a category.
