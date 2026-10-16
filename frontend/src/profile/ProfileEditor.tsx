/**
 * The full tier-editing profile screen (stories 4.2, 4.3, 4.4).
 *
 * `QuickOnboarding` (story 4.1) picks exactly 3 primary topics and nothing
 * else — see that component's own docstring: "the 1-5/8/unlimited tier
 * limits from story 4.2 are not this component's scope". This component is
 * that scope: a voter can assign ANY subtopic to primary (1-5), secondary
 * (up to 8) or nice-to-have (unlimited), mark a dealbreaker on a primary
 * interest only (story 4.3), see all of it summarized, and come back later
 * to find their saved profile already loaded (story 4.4).
 *
 * Tier limits are enforced in two places on purpose, same as `QuickOnboarding`
 * enforces its own 3-topic cap client-side as well as relying on the
 * backend's 422: the backend (`InterestProfile`, story 14.1) is the actual
 * authority and cannot be bypassed, but the acceptance criterion for story
 * 4.2 is explicit that "violations show inline errors" — a round trip to
 * find out a selection is invalid would fail that criterion even though the
 * backend would eventually say so too. `validateTiers` below is the single
 * pure function both this component and a future unit test can check the
 * boundary against without rendering anything.
 *
 * Tier weights (3.0 / 1.5 / 0.5) are never hard-coded here: they are read
 * once from `fetchTierWeights` (story 4.2's `GET /v1/profiles/methodology`)
 * and only used to label the profile summary — datamodel.md's "never
 * hard-coded" rule, honoured on the client the same way the backend honours
 * it in `app.profiles.methodology`.
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useState } from 'react';

import {
  type Interest,
  type InterestProfilePayload,
  type LoadedProfile,
  ProfileLoadError,
  ProfileSaveError,
  fetchTierWeights,
  loadProfile,
  saveProfile,
} from '../api/profiles';
import { type StanceOption, fetchStances } from '../api/stances';
import { fetchTopics } from '../api/topics';

export const MIN_PRIMARY = 1;
export const MAX_PRIMARY = 5;
export const MAX_SECONDARY = 8;

export type EditorTier = 'none' | 'primary' | 'secondary' | 'nice_to_have';

interface TopicSelection {
  topicId: string;
  topicName: string;
  tier: EditorTier;
  stanceId: string | null;
  dealbreaker: boolean;
}

/**
 * The pure validation rule behind story 4.2's "violations show inline
 * errors" — no network, no component, just the limits prd.md's tier table
 * states. Returns the first violation found, or `null` when the selection
 * is within every limit (a selection with zero primary interests is NOT a
 * "violation" here — see `hasNoPrimaryInterest`, kept separate because it
 * disables saving without being rendered as the same kind of alert).
 */
export function validateTiers(selections: TopicSelection[]): string | null {
  const counts = { primary: 0, secondary: 0, nice_to_have: 0 };
  for (const selection of selections) {
    if (selection.tier !== 'none') counts[selection.tier] += 1;
  }
  if (counts.primary > MAX_PRIMARY) {
    return `At most ${MAX_PRIMARY} primary interests are allowed; you have picked ${counts.primary}.`;
  }
  if (counts.secondary > MAX_SECONDARY) {
    return `At most ${MAX_SECONDARY} secondary interests are allowed; you have picked ${counts.secondary}.`;
  }
  return null;
}

const TIER_LABELS: Record<EditorTier, string> = {
  none: 'None',
  primary: 'Primary',
  secondary: 'Secondary',
  nice_to_have: 'Nice-to-have',
};

export function ProfileEditor() {
  const topicsQuery = useQuery({ queryKey: ['topics'], queryFn: ({ signal }) => fetchTopics(signal) });
  const weightsQuery = useQuery({ queryKey: ['tier-weights'], queryFn: ({ signal }) => fetchTierWeights(signal) });

  const userId = useMemo(() => ensureDemoUserId(), []);
  const profileQuery = useQuery({
    queryKey: ['profile', userId],
    queryFn: ({ signal }) => loadProfile(userId, signal),
  });

  const allSubtopics = useMemo(
    () => topicsQuery.data?.topics.flatMap((topic) => topic.subtopics) ?? [],
    [topicsQuery.data],
  );

  const [selections, setSelections] = useState<Record<string, TopicSelection>>({});
  const [consent, setConsent] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  // Load an existing profile once (story 4.4: "come back to it"). Guarded so
  // a later refetch of the same query (e.g. after a save invalidated it)
  // does not clobber in-progress edits.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    if (hydrated || !profileQuery.data) return;
    setSelections(selectionsFromLoadedProfile(profileQuery.data, allSubtopics));
    setHydrated(true);
  }, [hydrated, profileQuery.data, allSubtopics]);
  useEffect(() => {
    // A voter with no saved profile still counts as "hydrated" once the load
    // finished (data is `null`, not undefined) — otherwise the effect above
    // waits forever for data that will never arrive.
    if (!hydrated && profileQuery.isSuccess && profileQuery.data === null) {
      setHydrated(true);
    }
  }, [hydrated, profileQuery.isSuccess, profileQuery.data]);

  function selectionFor(topic: { id: string; name: string }): TopicSelection {
    return (
      selections[topic.id] ?? {
        topicId: topic.id,
        topicName: topic.name,
        tier: 'none',
        stanceId: null,
        dealbreaker: false,
      }
    );
  }

  function updateSelection(topicId: string, topicName: string, patch: Partial<TopicSelection>) {
    setSelections((current) => {
      const existing =
        current[topicId] ??
        ({ topicId, topicName, tier: 'none', stanceId: null, dealbreaker: false } as TopicSelection);
      const next: TopicSelection = { ...existing, topicName, ...patch };
      // A dealbreaker only makes sense on a primary interest (story 4.3):
      // moving a topic out of primary always clears it, regardless of what
      // the caller asked for, so the rule cannot be bypassed by patching
      // both fields in the same call out of order.
      if (next.tier !== 'primary') next.dealbreaker = false;
      return { ...current, [topicId]: next };
    });
  }

  const active = Object.values(selections).filter((selection) => selection.tier !== 'none');
  const tierError = validateTiers(active);
  const hasPrimary = active.some((selection) => selection.tier === 'primary');
  const missingStance = active.some((selection) => selection.stanceId === null);
  const canSave = hasPrimary && !missingStance && !tierError && consent && !isSaving;

  function profilePayload(): InterestProfilePayload {
    const interests: Interest[] = active.map((selection) => ({
      topic_id: selection.topicId,
      stance_id: selection.stanceId as string,
      tier: selection.tier as Exclude<EditorTier, 'none'>,
      intensity: 2,
      dealbreaker: selection.dealbreaker,
    }));
    return { interests, evidence_mode: 'blend' };
  }

  async function onSave() {
    setSaveError(null);
    setIsSaving(true);
    try {
      const response = await saveProfile(userId, profilePayload(), { consent: true });
      setSaveMessage(`Saved to your account (version ${response.version}).`);
    } catch (error) {
      setSaveError(
        error instanceof ProfileSaveError
          ? error.message
          : 'We could not save your profile just now. Please try again.',
      );
    } finally {
      setIsSaving(false);
    }
  }

  if (topicsQuery.isPending || weightsQuery.isPending || profileQuery.isPending) {
    return <p>Loading your profile…</p>;
  }
  if (topicsQuery.isError) {
    return <p role="alert">We could not load the list of topics just now. Please try again.</p>;
  }
  if (weightsQuery.isError) {
    return <p role="alert">We could not load the current tier weights just now. Please try again.</p>;
  }
  if (profileQuery.isError) {
    const message =
      profileQuery.error instanceof ProfileLoadError
        ? profileQuery.error.message
        : 'We could not load your saved profile just now. Please try again.';
    return <p role="alert">{message}</p>;
  }

  const weights = weightsQuery.data.weights;

  return (
    <section aria-label="Edit my interest profile">
      <h2>My interest profile</h2>
      <p>
        Assign each topic a tier: primary (1–{MAX_PRIMARY}, weight {weights.primary}), secondary
        (up to {MAX_SECONDARY}, weight {weights.secondary}), or nice-to-have (unlimited, weight{' '}
        {weights.nice_to_have}).
      </p>

      {tierError ? <p role="alert">{tierError}</p> : null}
      {!hasPrimary ? <p>You need at least {MIN_PRIMARY} primary interest before you can save.</p> : null}

      <ul>
        {allSubtopics.map((topic) => (
          <TopicRow
            key={topic.id}
            topic={topic}
            selection={selectionFor(topic)}
            onChange={(patch) => updateSelection(topic.id, topic.name, patch)}
          />
        ))}
      </ul>

      <ProfileSummary selections={active} weights={weights} />

      <label>
        <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
        I consent to saving my interests to my account
      </label>

      {saveError ? <p role="alert">{saveError}</p> : null}
      {saveMessage ? <p role="status">{saveMessage}</p> : null}

      <button type="button" onClick={onSave} disabled={!canSave}>
        {isSaving ? 'Saving…' : 'Save my profile'}
      </button>
    </section>
  );
}

function TopicRow({
  topic,
  selection,
  onChange,
}: {
  topic: { id: string; name: string };
  selection: TopicSelection;
  onChange: (patch: Partial<TopicSelection>) => void;
}) {
  const selectId = useId();
  const stanceLabelId = useId();

  const stanceQuery = useQuery({
    queryKey: ['stances', topic.id],
    queryFn: ({ signal }) => fetchStances(topic.id, signal),
    enabled: selection.tier !== 'none',
    staleTime: Number.POSITIVE_INFINITY,
  });

  return (
    <li>
      <fieldset role="group" aria-label={topic.name}>
        <legend>{topic.name}</legend>
        <label htmlFor={selectId}>Tier</label>
        <select
          id={selectId}
          value={selection.tier}
          onChange={(event) => onChange({ tier: event.target.value as EditorTier, stanceId: null })}
        >
          {(Object.keys(TIER_LABELS) as EditorTier[]).map((tier) => (
            <option key={tier} value={tier}>
              {TIER_LABELS[tier]}
            </option>
          ))}
        </select>

        {selection.tier !== 'none' ? (
          <>
            <label id={stanceLabelId} htmlFor={`${selectId}-stance`}>
              Stance
            </label>
            {stanceQuery.isPending ? (
              <span>Loading stance options…</span>
            ) : stanceQuery.isError ? (
              <span role="alert">We could not load stance options for this topic.</span>
            ) : (
              <select
                id={`${selectId}-stance`}
                aria-labelledby={stanceLabelId}
                value={selection.stanceId ?? ''}
                onChange={(event) => onChange({ stanceId: event.target.value || null })}
              >
                <option value="">Choose a stance…</option>
                {stanceQuery.data.options.map((option: StanceOption) => (
                  <option key={option.id} value={option.id}>
                    {option.label}
                  </option>
                ))}
              </select>
            )}
          </>
        ) : null}

        {selection.tier === 'primary' ? (
          <label>
            <input
              type="checkbox"
              checked={selection.dealbreaker}
              onChange={(event) => onChange({ dealbreaker: event.target.checked })}
            />
            Dealbreaker
          </label>
        ) : null}
      </fieldset>
    </li>
  );
}

function ProfileSummary({
  selections,
  weights,
}: {
  selections: TopicSelection[];
  weights: Record<'primary' | 'secondary' | 'nice_to_have', number>;
}) {
  return (
    <section aria-label="Profile summary">
      <h3>Profile summary</h3>
      {selections.length === 0 ? (
        <p>No interests chosen yet.</p>
      ) : (
        <ul>
          {(['primary', 'secondary', 'nice_to_have'] as const).map((tier) => {
            const inTier = selections.filter((selection) => selection.tier === tier);
            if (inTier.length === 0) return null;
            return (
              <li key={tier}>
                <h4>
                  {TIER_LABELS[tier]} (weight {weights[tier]})
                </h4>
                <ul>
                  {inTier.map((selection) => (
                    <li key={selection.topicId}>
                      {selection.topicName}
                      {selection.dealbreaker ? ' — Dealbreaker' : ''}
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function selectionsFromLoadedProfile(
  profile: LoadedProfile,
  subtopics: { id: string; name: string }[],
): Record<string, TopicSelection> {
  const nameById = new Map(subtopics.map((topic) => [topic.id, topic.name]));
  const result: Record<string, TopicSelection> = {};
  for (const interest of profile.interests) {
    result[interest.topic_id] = {
      topicId: interest.topic_id,
      topicName: nameById.get(interest.topic_id) ?? interest.topic_id,
      tier: interest.tier,
      stanceId: interest.stance_id,
      dealbreaker: interest.dealbreaker,
    };
  }
  return result;
}

/** A per-browser id for the pre-auth demo path. Not a real account.
 *
 * Shares the same storage key `QuickOnboarding` uses, so a voter who saves
 * through either screen loads the same profile back through either screen —
 * two editors for one profile, not two profiles.
 */
function ensureDemoUserId(): string {
  const key = 'informed:demo-user-id';
  const existing = window.localStorage.getItem(key);
  if (existing) return existing;
  const generated = `demo-${Math.random().toString(36).slice(2)}-${Date.now()}`;
  window.localStorage.setItem(key, generated);
  return generated;
}
