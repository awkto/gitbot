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
2. the bot holds a **role the operator has configured to follow** (below), or
3. it **answers a pending question** the bot asked — no `@` needed: either a
   reply in the question's own thread (anyone may answer), or any comment from
   the user the question was addressed to.

Guard: if a question is pending but the comment neither answers it nor
`@`-mentions the bot, it is dropped, so unrelated chatter can't consume the
question.

### Which roles follow plain comments (configurable)

For a **plain (non-@mention)** comment, GitBot looks up its role on the target
live and follows the discussion only for roles enabled in **Admin → Triggers &
Behaviour** (env-overridable):

| Role | Setting | Default |
|---|---|---|
| Issue assignee | `GITBOT_ACT_ON_ISSUE_ASSIGNEE_COMMENTS` | ✅ on |
| MR assignee | `GITBOT_ACT_ON_MR_ASSIGNEE_COMMENTS` | ✅ on |
| MR author (bot opened it) | `GITBOT_ACT_ON_MR_AUTHOR_COMMENTS` | ✅ on |
| MR reviewer (only reviewing) | `GITBOT_ACT_ON_MR_REVIEWER_COMMENTS` | ❌ off |

A reviewer is asked to review once, not subscribed to every comment — so
reviewer-only MRs don't follow plain comments by default. An `@mention` still
reaches the bot on any target regardless of these settings.

### Silent observation

When a plain comment reaches GitBot only via a followed role (not an
`@mention`, not a pending answer), the Haiku triage may classify it **`ignore`**
— two people talking, an FYI, a thank-you. GitBot then stays completely silent
(no placeholder, no reply). It engages (`answer` / `steer` / `task`) only when
the comment genuinely asks or tells it to do something. `@mentions` are never
ignored.

### Worked examples

| Situation | Acts? |
|---|---|
| `@gitbot fix the flaky test` (issue or MR) | ✅ acts |
| Reply in the bot's question thread, or from the asked user (no `@`) | ✅ acts — resumes the parked task |
| Plain relevant comment on a bot-**assigned issue** ("this approach is wrong") | ✅ acts (`answer`/`steer`/`task`) |
| Plain chatter between two humans on a bot-assigned issue | ✅ seen, ✅ classified `ignore` → stays silent |
| Plain comment on an MR the bot is **assignee/author** of | ✅ acts |
| Plain comment on an MR the bot only **reviews** | ❌ dropped by default (`@mention` to reach it) |
| Reply to a bot comment with no `@`, no pending question, no followed role | ❌ dropped |

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
