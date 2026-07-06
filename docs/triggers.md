# What triggers GitBot, and what it acts on

GitBot is webhook-driven. GitLab sends events; GitBot decides — entirely in
deterministic code, before spending any tokens — whether an event is for it.
This page is the authoritative map of that decision. It matches
`brain._should_skip` and the dispatch in `brain.decide_and_act`.

## Cost note: the trigger decision is free

The whole trigger / routing / skip layer is **plain code — no LLM, $0**:
self-event drops, system-note drops, assignment/reviewer checks, `@gitbot`
mention detection (a string check), and "does this comment answer a pending
question?" (a thread-id / author match). A dropped event costs nothing.

The **first** token spend happens only after an event passes every gate and a
workflow starts:

| Stage | Model |
|---|---|
| All trigger / skip / pending-question gates | none (code) |
| Comment intent triage (`answer` / `steer` / `task`) | Haiku (`GITBOT_CLASSIFIER_MODEL`) |
| Assignment triage (implement vs orchestrate + complexity) | Haiku |
| The workflow itself | Haiku / Sonnet / Opus, by complexity |

## What GitLab sends

Subscribe the webhook to: **Issues, Confidential Issues, Merge Requests,
Notes, Confidential Notes** (Push may be enabled but is ignored). GitBot only
routes three event types: `Issue Hook`, `Merge Request Hook`, `Note Hook`.

## Universal drops (checked first, for every event)

- **Actor is GitBot itself** → dropped. This is the anti-loop guard: GitBot
  commenting, pushing, labelling, or opening MRs never re-triggers it.
- **System notes** (GitLab's automatic "changed status", "assigned to",
  "mentioned in !N" notes) → dropped. Only real human comments proceed.

## Issue events (`Issue Hook`)

| Event | Acts? |
|---|---|
| Bot **newly assigned** to an issue (or issue opened with bot already assigned) | ✅ acts |
| Any other update on a bot-assigned issue — label, title, description, milestone, close/reopen | ❌ dropped |
| Issue assigned to someone else | ❌ dropped |

Only the *new assignment* triggers work; editing a bot-assigned issue does not.

## Merge request events (`Merge Request Hook`)

| Event | Acts? |
|---|---|
| Bot **newly requested as reviewer** | ✅ acts (review) |
| Bot **newly assigned** (or MR opened with bot in that role) | ✅ acts |
| New commits, labels, title, approvals | ❌ dropped |
| Any event on an MR **the bot authored** (unless also reviewer / a review request) | ❌ dropped — the bot's own MRs don't re-trigger it |

## Comments (`Note Hook`)

After the system-note drop, a comment **acts if any** of these holds:

1. it **@mentions `@gitbot`**, or
2. the bot **has a role on the target** — assignee / reviewer / author, or
3. it **answers a pending question** the bot asked — no `@` needed: either a
   reply in the question's own thread (anyone may answer), or any comment from
   the user the question was addressed to.

Guard: if a question is pending but the comment neither answers it nor
`@`-mentions the bot, it is dropped, so unrelated chatter can't consume the
question.

### Worked examples

| Situation | Acts? |
|---|---|
| `@gitbot fix the flaky test` (issue or MR) | ✅ acts |
| Reply in the bot's question thread, or from the asked user (no `@`) | ✅ acts — resumes the parked task |
| Plain comment on an MR where the bot is author / assignee / reviewer | ✅ acts — MR role is looked up live |
| Plain comment (no `@`) on a bot-**assigned issue** | ❌ dropped — see asymmetry below |
| Reply to a bot comment with no `@`, no pending question, no role | ❌ dropped |

### Known asymmetry: issue comments vs MR comments

Rule 2 ("bot has a role") fires for **MR** comments — the skip gate looks up
the bot's author/assignee/reviewer role live. It does **not** fire for
**issue** comments: the note handler never populates "bot is assignee" for
issues, so only rule 1 (`@mention`) or rule 3 (pending answer) gets an issue
comment through.

Consequence: assign the bot an issue, then comment "also do X" *without*
`@gitbot`, and it is ignored — though the same comment on an MR would be
picked up. This is protective (the bot doesn't barge into every comment on
issues it's assigned) but inconsistent with MR behavior. Tracked separately.

## What runs when it acts

| Trigger | Workflow |
|---|---|
| Issue assigned / resumed | triage → **implement** (branch + MR) or **orchestrate** (multi-project / CI / admin) |
| MR review-requested / assigned | **review** (inline findings + verdict) |
| Comment, intent `answer` | **mention** (reply only; no labels; light) |
| Comment, intent `task` | fresh work → implement / orchestrate (a `task` comment on an MR pushes to *that* MR) |
| Comment, intent `steer` | resume & adjust the target's existing work |

Comment intent (`answer` / `steer` / `task`) is decided by the Haiku triage
classifier; assignment triage (implement vs orchestrate, plus the 1–10
complexity score that picks the workflow's model tier) is likewise Haiku.
