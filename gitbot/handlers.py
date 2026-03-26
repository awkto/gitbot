"""Event handlers - one function per event type the bot cares about."""

import logging

from gitbot import llm, gitlab_client as glc
from gitbot.config import settings
from gitbot.models import Task
from gitbot.prompts import issue_analysis, mr_summary, code_review, mention_response

log = logging.getLogger(__name__)


async def handle_issue_assigned(payload: dict) -> None:
    """Bot was assigned to an issue - read it and respond."""
    attrs = payload["object_attributes"]
    project_id = payload["project"]["id"]
    issue_iid = attrs["iid"]

    system, prompt = issue_analysis(
        settings.llm_family,
        title=attrs["title"],
        description=attrs.get("description", "") or "",
    )

    response = await llm.complete(Task.ISSUE_ANALYSIS, system=system, prompt=prompt)
    glc.post_note_on_issue(project_id, issue_iid, response)
    log.info("Responded to issue #%s in project %s", issue_iid, project_id)


async def handle_mr_assigned(payload: dict) -> None:
    """Bot was assigned to an MR - look at the diff and comment."""
    attrs = payload["object_attributes"]
    project_id = payload["project"]["id"]
    mr_iid = attrs["iid"]

    diff = glc.get_mr_diff(project_id, mr_iid)

    system, prompt = mr_summary(
        settings.llm_family,
        title=attrs["title"],
        description=attrs.get("description", "") or "",
        diff=diff,
    )

    response = await llm.complete(Task.MR_SUMMARY, system=system, prompt=prompt)
    glc.post_note_on_mr(project_id, mr_iid, response)
    log.info("Responded to MR !%s in project %s", mr_iid, project_id)


async def handle_mr_review_requested(payload: dict) -> None:
    """Bot was added as a reviewer - do a code review."""
    attrs = payload["object_attributes"]
    project_id = payload["project"]["id"]
    mr_iid = attrs["iid"]

    diff = glc.get_mr_diff(project_id, mr_iid)

    system, prompt = code_review(
        settings.llm_family,
        title=attrs["title"],
        description=attrs.get("description", "") or "",
        diff=diff,
    )

    response = await llm.complete(Task.CODE_REVIEW, system=system, prompt=prompt)
    glc.post_note_on_mr(project_id, mr_iid, response)
    log.info("Reviewed MR !%s in project %s", mr_iid, project_id)


async def handle_mention(payload: dict) -> None:
    """Bot was @mentioned in a note/comment."""
    project_id = payload["project"]["id"]
    note = payload["object_attributes"]
    note_body = note.get("note", "")
    noteable_type = note.get("noteable_type", "")
    discussion_id = note.get("discussion_id")

    noteable_title = ""
    noteable_iid = None
    if noteable_type == "MergeRequest" and "merge_request" in payload:
        mr = payload["merge_request"]
        noteable_title = mr.get("title", "")
        noteable_iid = mr["iid"]
    elif noteable_type == "Issue" and "issue" in payload:
        issue = payload["issue"]
        noteable_title = issue.get("title", "")
        noteable_iid = issue["iid"]

    system, prompt = mention_response(
        settings.llm_family,
        note_body=note_body,
        noteable_type=noteable_type,
        noteable_title=noteable_title,
    )

    response = await llm.complete(Task.MENTION_RESPONSE, system=system, prompt=prompt)

    if discussion_id and noteable_iid and noteable_type in ("MergeRequest", "Issue"):
        glc.reply_to_discussion(
            project_id, noteable_type, noteable_iid, discussion_id, response
        )
        log.info("Replied to discussion %s", discussion_id)
    else:
        log.warning("Could not find discussion context to reply to, dropping response")
