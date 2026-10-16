/**
 * The `GET /v1/topics` client (story 4.1).
 *
 * Mirrors `backend/app/topics/models.py`. Every onboarding path (quick,
 * guided, advanced) reads the same tree from here; the quick path (this
 * story) shows only the top-level topics and, once one is picked, that
 * topic's own subtopics — it never needs the advanced browser's search.
 */

import { apiUrl } from './ballot';

/** One subtopic: the level a saved interest's `topic_id` actually names. */
export interface TopicNode {
  id: string;
  name: string;
}

/** One top-level topic and its nested subtopics. */
export interface TopicTree {
  id: string;
  name: string;
  subtopics: TopicNode[];
}

export interface TopicTreeResponse {
  locale: string;
  taxonomy_version: number;
  topics: TopicTree[];
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

/** Raised for any non-2xx answer from `/v1/topics`. */
export class TopicsError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'TopicsError';
    this.status = status;
  }
}

/** Fetch the whole issue-taxonomy tree for one locale. */
export async function fetchTopics(signal?: AbortSignal): Promise<TopicTreeResponse> {
  const url = apiUrl(API_BASE_URL, '/v1/topics', window.location.origin);
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new TopicsError('We could not load the list of topics just now. Please try again.', response.status);
  }
  return (await response.json()) as TopicTreeResponse;
}
