"""The issue-taxonomy tree, read back for onboarding (story 4.1).

Story 3.1 seeded `issue_topics` as permanent reference data: ~20 top-level
topics, each with ~3 subtopics, stored flat with a `parent_id` self-reference.
Nothing before this story read that table back through an API — the quick,
guided and advanced onboarding paths (prd.md, Feature 1) all start from the
same tree, so one read-only service and one router serve all three rather than
each path inventing its own topic listing.
"""

from app.topics.models import TopicNode, TopicTree, TopicTreeResponse
from app.topics.router import get_topics, router, topic_service
from app.topics.service import TopicService

__all__ = [
    "TopicNode",
    "TopicService",
    "TopicTree",
    "TopicTreeResponse",
    "get_topics",
    "router",
    "topic_service",
]
