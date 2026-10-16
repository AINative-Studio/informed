/**
 * The quick-onboarding flow (story 4.1).
 *
 * backlog.md 4.1: "pick 3 primary topics and my stance on each in under 2
 * minutes". ui-prd.md 2.2 fixes the shape: three screens — pick topics, pick
 * a stance per topic, consent (with the anonymous-mode alternative shown as
 * an actual choice) — and a handful of hard constraints this component is
 * built around rather than around on top of:
 *
 *   * stance order is a fresh server shuffle, held stable for the rest of
 *     THIS session once fetched (never re-fetched or re-sorted locally);
 *   * nothing is pre-selected;
 *   * the 1-5/8/unlimited tier limits from story 4.2 are not this
 *     component's scope — the quick path picks exactly 3 primary topics,
 *     per backlog.md's own wording, and nothing here claims to be the
 *     general tier editor;
 *   * no score, nor anything score-shaped, is rendered anywhere in this
 *     flow — scoring (Epic 6) does not exist yet, and even once it does,
 *     ui-prd.md's gate is "no score until at least one primary interest
 *     exists", which this flow's own completion screen must not pre-empt.
 *
 * State lives in this one component (useState, no external store) because
 * three screens and ~10 fields do not need one, and TanStack Query's
 * `useQuery` already owns the two reads this flow makes
 * (`fetchTopics`, `fetchStances`).
 */
import { useQuery } from '@tanstack/react-query';
import { useId, useMemo, useState } from 'react';

import {
  type InterestProfilePayload,
  type SaveProfileResponse,
  ProfileSaveError,
  saveAnonymousProfile,
  saveProfile,
} from '../api/profiles';
import { type StanceOption, fetchStances } from '../api/stances';
import { type TopicTree, fetchTopics } from '../api/topics';

/** backlog.md 4.1: "pick 3 primary topics" — the quick path's own fixed count. */
const QUICK_PATH_PRIMARY_COUNT = 3;

type Step = 'topics' | 'stances' | 'consent' | 'done';

interface SelectedTopic {
  topicId: string;
  topicName: string;
  stanceId: string | null;
}

export function QuickOnboarding() {
  const topicsQuery = useQuery({ queryKey: ['topics'], queryFn: ({ signal }) => fetchTopics(signal) });

  const [step, setStep] = useState<Step>('topics');
  const [selected, setSelected] = useState<SelectedTopic[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveResult, setSaveResult] = useState<
    { mode: 'account'; response: SaveProfileResponse } | { mode: 'anonymous' } | null
  >(null);
  const [isSaving, setIsSaving] = useState(false);

  const allTopics = useMemo(() => topicsQuery.data?.topics ?? [], [topicsQuery.data]);

  function toggleTopic(topic: TopicTree) {
    setSelected((current) => {
      const already = current.some((item) => item.topicId === topic.id);
      if (already) return current.filter((item) => item.topicId !== topic.id);
      if (current.length >= QUICK_PATH_PRIMARY_COUNT) return current; // limit enforced below too
      return [...current, { topicId: topic.id, topicName: topic.name, stanceId: null }];
    });
  }

  function setStance(topicId: string, stanceId: string) {
    setSelected((current) =>
      current.map((item) => (item.topicId === topicId ? { ...item, stanceId } : item)),
    );
  }

  const readyForStances = selected.length === QUICK_PATH_PRIMARY_COUNT;
  const readyForConsent = selected.length === QUICK_PATH_PRIMARY_COUNT && selected.every((item) => item.stanceId !== null);

  function profilePayload(): InterestProfilePayload {
    return {
      interests: selected.map((item) => ({
        topic_id: item.topicId,
        stance_id: item.stanceId as string,
        tier: 'primary',
        intensity: 2,
        dealbreaker: false,
      })),
      evidence_mode: 'blend',
    };
  }

  async function onConsentChoice(choice: 'account' | 'anonymous') {
    setSaveError(null);
    if (choice === 'anonymous') {
      saveAnonymousProfile(profilePayload());
      setSaveResult({ mode: 'anonymous' });
      setStep('done');
      return;
    }

    setIsSaving(true);
    try {
      // No sign-in flow exists yet (iteration 1's walking skeleton has none;
      // see app/profiles/router.py's docstring for why this endpoint accepts
      // an explicit user_id rather than reading a session). A per-browser
      // synthetic id keeps this flow usable and demonstrable before that
      // story lands, without pretending to be real authentication.
      const userId = ensureDemoUserId();
      const response = await saveProfile(userId, profilePayload(), { consent: true });
      setSaveResult({ mode: 'account', response });
      setStep('done');
    } catch (error) {
      setSaveError(error instanceof ProfileSaveError ? error.message : 'We could not save your profile just now. Please try again.');
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section aria-label="Quick onboarding">
      <h2>Quick setup</h2>
      <p>Pick {QUICK_PATH_PRIMARY_COUNT} topics that matter most to you, and your stance on each.</p>

      {step === 'topics' ? (
        <TopicStep
          loading={topicsQuery.isPending}
          error={topicsQuery.isError}
          topics={allTopics}
          selected={selected}
          onToggle={toggleTopic}
          onNext={() => setStep('stances')}
          canProceed={readyForStances}
        />
      ) : null}

      {step === 'stances' ? (
        <StanceStep
          selected={selected}
          onPick={setStance}
          onBack={() => setStep('topics')}
          onNext={() => setStep('consent')}
          canProceed={readyForConsent}
        />
      ) : null}

      {step === 'consent' ? (
        <ConsentStep
          onBack={() => setStep('stances')}
          onChoice={onConsentChoice}
          isSaving={isSaving}
          error={saveError}
        />
      ) : null}

      {step === 'done' && saveResult ? <DoneStep result={saveResult} /> : null}
    </section>
  );
}

/** A per-browser id for the pre-auth demo path. Not a real account. */
function ensureDemoUserId(): string {
  const key = 'informed:demo-user-id';
  const existing = window.localStorage.getItem(key);
  if (existing) return existing;
  const generated = `demo-${Math.random().toString(36).slice(2)}-${Date.now()}`;
  window.localStorage.setItem(key, generated);
  return generated;
}

// --- Step 1: pick exactly 3 primary topics ----------------------------------

function TopicStep({
  loading,
  error,
  topics,
  selected,
  onToggle,
  onNext,
  canProceed,
}: {
  loading: boolean;
  error: boolean;
  topics: TopicTree[];
  selected: SelectedTopic[];
  onToggle: (topic: TopicTree) => void;
  onNext: () => void;
  canProceed: boolean;
}) {
  const headingId = useId();

  if (loading) return <p>Loading topics…</p>;
  if (error) return <p role="alert">We could not load the list of topics just now. Please try again.</p>;

  return (
    <div>
      <h3 id={headingId}>Choose {QUICK_PATH_PRIMARY_COUNT} topics</h3>
      <p aria-live="polite">
        {selected.length} of {QUICK_PATH_PRIMARY_COUNT} picked
      </p>
      <ul aria-labelledby={headingId}>
        {topics.map((topic) => {
          const isSelected = selected.some((item) => item.topicId === topic.id);
          const atLimit = !isSelected && selected.length >= QUICK_PATH_PRIMARY_COUNT;
          return (
            <li key={topic.id}>
              <label>
                <input
                  type="checkbox"
                  checked={isSelected}
                  disabled={atLimit}
                  onChange={() => onToggle(topic)}
                />
                {topic.name}
              </label>
            </li>
          );
        })}
      </ul>
      {selected.length >= QUICK_PATH_PRIMARY_COUNT ? (
        <p role="status">
          You have picked {QUICK_PATH_PRIMARY_COUNT} topics, the quick setup's limit. Unselect one to
          swap it out.
        </p>
      ) : null}
      <button type="button" onClick={onNext} disabled={!canProceed}>
        Next: choose your stance
      </button>
    </div>
  );
}

// --- Step 2: a stance per topic, server-shuffled, never pre-selected -------

function StanceStep({
  selected,
  onPick,
  onBack,
  onNext,
  canProceed,
}: {
  selected: SelectedTopic[];
  onPick: (topicId: string, stanceId: string) => void;
  onBack: () => void;
  onNext: () => void;
  canProceed: boolean;
}) {
  return (
    <div>
      <h3>Choose your stance on each topic</h3>
      {selected.map((item) => (
        <TopicStanceChooser
          key={item.topicId}
          topicId={item.topicId}
          topicName={item.topicName}
          chosenStanceId={item.stanceId}
          onPick={(stanceId) => onPick(item.topicId, stanceId)}
        />
      ))}
      <button type="button" onClick={onBack}>
        Back
      </button>
      <button type="button" onClick={onNext} disabled={!canProceed}>
        Next: save my profile
      </button>
    </div>
  );
}

/**
 * Fetches one topic's stance options once and keeps them in this component's
 * own state for the rest of the session — the client's half of story 3.2's
 * "randomized every session" contract (see `app/stances/service.py`'s
 * docstring: the server owes a fresh shuffle per call, the client owes
 * holding it steady once shown). `useQuery`'s cache, keyed by topic id and
 * never invalidated within this flow, is exactly that: one fetch per topic,
 * reused across re-renders, never re-sorted.
 */
function TopicStanceChooser({
  topicId,
  topicName,
  chosenStanceId,
  onPick,
}: {
  topicId: string;
  topicName: string;
  chosenStanceId: string | null;
  onPick: (stanceId: string) => void;
}) {
  const headingId = useId();
  const query = useQuery({
    queryKey: ['stances', topicId],
    queryFn: ({ signal }) => fetchStances(topicId, signal),
    staleTime: Number.POSITIVE_INFINITY, // never re-fetch mid-session: that would re-shuffle the options
  });

  if (query.isPending) return <p>Loading stance options for {topicName}…</p>;
  if (query.isError) {
    return (
      <p role="alert">
        We could not load stance options for {topicName} just now. Please try again.
      </p>
    );
  }

  const options: StanceOption[] = query.data.options;

  return (
    <fieldset>
      <legend id={headingId}>{topicName}</legend>
      {options.length === 0 ? (
        <p>No stance options are available for this topic yet.</p>
      ) : (
        <ul aria-labelledby={headingId}>
          {options.map((option) => (
            <li key={option.id}>
              <label>
                <input
                  type="radio"
                  name={`stance-${topicId}`}
                  checked={chosenStanceId === option.id}
                  onChange={() => onPick(option.id)}
                />
                {option.label}
                <span> — {option.description}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </fieldset>
  );
}

// --- Step 3: consent gate, with anonymous mode as an actual choice ---------

function ConsentStep({
  onBack,
  onChoice,
  isSaving,
  error,
}: {
  onBack: () => void;
  onChoice: (choice: 'account' | 'anonymous') => void;
  isSaving: boolean;
  error: string | null;
}) {
  return (
    <div>
      <h3>Before we save anything</h3>
      <p>
        Your interests are political-opinion data. Informed encrypts them so that even Informed
        cannot read them, never logs them, and never uses them for targeting.
      </p>
      <div>
        <section aria-label="Save to my account">
          <h4>Save to my account</h4>
          <p>
            Your profile is encrypted and stored so you can come back to it. This requires your
            consent to store special-category political-opinion data under GDPR.
          </p>
          <button type="button" onClick={() => onChoice('account')} disabled={isSaving}>
            {isSaving ? 'Saving…' : 'I consent — save to my account'}
          </button>
        </section>
        <section aria-label="Stay anonymous">
          <h4>Stay anonymous</h4>
          <p>
            Your profile stays only in this browser. Nothing is sent to Informed's servers, and
            nothing is saved to an account.
          </p>
          <button type="button" onClick={() => onChoice('anonymous')} disabled={isSaving}>
            Use anonymous mode instead
          </button>
        </section>
      </div>
      {error ? <p role="alert">{error}</p> : null}
      <button type="button" onClick={onBack} disabled={isSaving}>
        Back
      </button>
    </div>
  );
}

// --- Step 4: confirmation, deliberately score-free --------------------------

function DoneStep({
  result,
}: {
  result: { mode: 'account'; response: SaveProfileResponse } | { mode: 'anonymous' };
}) {
  return (
    <div role="status">
      <h3>Your profile is saved</h3>
      {result.mode === 'account' ? (
        <p>Saved to your account (version {result.response.version}).</p>
      ) : (
        <p>Saved in this browser only, in anonymous mode.</p>
      )}
      <p>
        Scores are not ready to show yet — they depend on which candidates are on your ballot and
        are computed once that feature is built.
      </p>
    </div>
  );
}
