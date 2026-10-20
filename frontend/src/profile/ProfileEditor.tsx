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
  type DeleteProfileResult,
  type Interest,
  type InterestProfilePayload,
  type LoadedProfile,
  type ProfileExport,
  ProfileLoadError,
  ProfileSaveError,
  clearAnonymousProfile,
  deleteProfile,
  exportProfile,
  fetchConsentOffer,
  fetchTierWeights,
  loadAnonymousProfile,
  loadProfile,
  saveAnonymousProfile,
  saveProfile,
} from '../api/profiles';
import { type StanceOption, fetchStances } from '../api/stances';
import { fetchTopics } from '../api/topics';
import { cacheProfileOffline, readCachedProfileOffline } from '../offline/encryptedProfileCache';

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

/**
 * Where this voter's profile goes (stories 14.2, 14.3).
 *
 * `'account'` is the encrypted, server-stored path and needs explicit
 * consent; `'anonymous'` keeps the profile in this browser and makes no write
 * to the server at all. A single piece of state rather than two booleans, so
 * "consented but anonymous" and "neither chosen" are not representable.
 */
export type StorageMode = 'account' | 'anonymous';

export function ProfileEditor() {
  const topicsQuery = useQuery({ queryKey: ['topics'], queryFn: ({ signal }) => fetchTopics(signal) });
  const weightsQuery = useQuery({ queryKey: ['tier-weights'], queryFn: ({ signal }) => fetchTierWeights(signal) });
  // Story 14.2: the wording comes from the server, versioned, so the consent
  // record names text that can be reproduced later. Never hard-coded here —
  // the same rule the tier weights follow, for a stronger reason: wording
  // compiled into the client cannot be resolved from a stored record.
  const consentQuery = useQuery({
    queryKey: ['consent-offer'],
    queryFn: ({ signal }) => fetchConsentOffer(undefined, signal),
    staleTime: Number.POSITIVE_INFINITY,
  });

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

  // --- Stories 14.3 and 14.4 state ---------------------------------------
  const [storageMode, setStorageMode] = useState<StorageMode>('account');
  const [exportState, setExportState] = useState<ProfileExport | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [deleteStage, setDeleteStage] = useState<'idle' | 'confirming' | 'deleting'>('idle');
  const [deleteResult, setDeleteResult] = useState<DeleteProfileResult | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // --- Story 7.7: encrypted offline profile cache ------------------------
  //
  // `showingOfflineCopy` is distinct from `hydrated` below: hydration is
  // "the selections state has been filled in from *some* source", which an
  // offline-cache read also satisfies, while this flag specifically drives
  // the "you're viewing a cached offline copy" notice the feature file
  // requires — a voter who is mid-edit should not keep seeing that banner
  // on an unrelated re-render once a later successful network load clears
  // it (the "regaining the network" scenario).
  const [showingOfflineCopy, setShowingOfflineCopy] = useState(false);

  // Load an existing profile once (story 4.4: "come back to it"). Guarded so
  // a later refetch of the same query (e.g. after a save invalidated it)
  // does not clobber in-progress edits.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    if (hydrated || storageMode !== 'account' || !profileQuery.data) return;
    setSelections(selectionsFromLoadedProfile(profileQuery.data, allSubtopics));
    setHydrated(true);
    setShowingOfflineCopy(false);
    // Story 7.7: every successful network load refreshes this browser's
    // offline copy, encrypted, so the cache a later offline visit reads back
    // is never more than one successful load stale. Fire-and-forget: a
    // cache-write failure (e.g. IndexedDB unavailable in private browsing)
    // must not block the editor from showing the profile it just loaded —
    // the same "never let a storage failure take down the screen" posture
    // `PrivacyActions`' blob-URL fallback already follows elsewhere in this
    // file.
    void cacheProfileOffline(profileQuery.data);
  }, [hydrated, storageMode, profileQuery.data, allSubtopics]);
  useEffect(() => {
    // A voter with no saved profile still counts as "hydrated" once the load
    // finished (data is `null`, not undefined) — otherwise the effect above
    // waits forever for data that will never arrive.
    if (!hydrated && storageMode === 'account' && profileQuery.isSuccess && profileQuery.data === null) {
      setHydrated(true);
    }
  }, [hydrated, storageMode, profileQuery.isSuccess, profileQuery.data]);

  // Story 7.7: "view my profile offline" — when the network load genuinely
  // fails (offline, or any other fetch/server error `loadProfile` surfaces
  // as `profileQuery.isError`), fall back to this browser's own encrypted
  // cache rather than showing only an error. A stale cached copy that is
  // clearly labelled as cached is strictly better at a polling place than no
  // profile at all; `showingOfflineCopy` is what labels it.
  useEffect(() => {
    if (hydrated || storageMode !== 'account' || !profileQuery.isError) return;
    let cancelled = false;
    void readCachedProfileOffline().then((cached) => {
      if (cancelled || hydrated) return;
      if (cached) {
        setSelections(selectionsFromLoadedProfile(cached.profile, allSubtopics));
        setShowingOfflineCopy(true);
      }
      // Hydrated either way: a voter with network trouble and no cache yet
      // still sees the query's own error message below rather than a
      // spinner that never resolves.
      setHydrated(true);
    });
    return () => {
      cancelled = true;
    };
  }, [hydrated, storageMode, profileQuery.isError, allSubtopics]);

  // Story 14.3: anonymous mode hydrates from this browser instead, so the
  // "survives a reload" half of the acceptance criterion holds on the same
  // code path the account mode uses — one editor, two stores, never a second
  // reduced-function screen. Keyed on `storageMode` rather than done once at
  // mount because the choice is made after the component is already up.
  const [anonymousHydrated, setAnonymousHydrated] = useState(false);
  useEffect(() => {
    if (storageMode !== 'anonymous' || anonymousHydrated) return;
    const stored = loadAnonymousProfile();
    if (stored) {
      setSelections(
        selectionsFromLoadedProfile(
          {
            interests: stored.profile.interests,
            evidence_mode: stored.profile.evidence_mode ?? 'blend',
            followed_person_ids: stored.profile.followed_person_ids ?? [],
          },
          allSubtopics,
        ),
      );
    }
    setAnonymousHydrated(true);
  }, [storageMode, anonymousHydrated, allSubtopics]);

  /**
   * Switching mode resets the per-mode result messages.
   *
   * A stale "Saved to your account (version 2)" still on screen after a
   * switch to anonymous mode would be a privacy claim about the wrong store,
   * which is exactly the kind of thing a voter choosing anonymous mode is
   * reading the screen to check.
   */
  function onStorageModeChange(mode: StorageMode) {
    setStorageMode(mode);
    setSaveMessage(null);
    setSaveError(null);
    setExportState(null);
    setExportError(null);
    setDeleteStage('idle');
    setDeleteResult(null);
    setDeleteError(null);
    if (mode === 'anonymous') setConsent(false);
  }

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
  // Consent gates the ACCOUNT path only (stories 14.2, 14.3). Anonymous mode
  // asks for none because there is nothing to consent to: no row is written
  // anywhere, so there is no special-category processing to find a lawful
  // basis for. Requiring a consent tick to write to the voter's own browser
  // would be theatre, and worse, it would imply something was stored.
  const needsConsent = storageMode === 'account';
  const canSave =
    hasPrimary && !missingStance && !tierError && (!needsConsent || consent) && !isSaving;

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

    // Story 14.3: the anonymous branch returns before any network call can be
    // reached. Written as an early return rather than a ternary inside the
    // try block so there is no code path on which an anonymous save could
    // fall through to `saveProfile` — the property the tests assert by
    // counting non-GET requests.
    if (storageMode === 'anonymous') {
      try {
        saveAnonymousProfile(profilePayload());
        setSaveMessage(
          'Saved in this browser only. Nothing about your politics was sent to Informed.',
        );
      } catch {
        // `localStorage` can throw (private browsing, quota). Surfaced rather
        // than swallowed: silently dropping the profile is the one failure
        // mode ui-prd.md's "real, equally-functional path" cannot tolerate.
        setSaveError(
          'This browser would not let us store your profile, so nothing was saved. ' +
            'Check whether private browsing or site data is blocked.',
        );
      }
      return;
    }

    setIsSaving(true);
    try {
      const payload = profilePayload();
      const response = await saveProfile(userId, payload, {
        consent: true,
        // The version this component actually rendered (story 14.2). Passing
        // the fetched value rather than a local constant is what makes the
        // stored consent record resolvable back to text the voter saw.
        consentVersion: consentQuery.data?.consent_version,
      });
      setSaveMessage(`Saved to your account (version ${response.version}).`);
      // Story 7.7: cache the just-saved profile offline, encrypted,
      // immediately — not only on a later `loadProfile` success. This
      // component never invalidates/refetches `profileQuery` after a save
      // (there is no `useQueryClient` call anywhere in this file), so
      // without this line a voter who saves and then goes offline before
      // ever reloading the editor would have nothing in the offline cache
      // at all: `profileQuery.data` is still whatever it was when the
      // component mounted (`null`, for a first-time save), and the
      // load-side hydration effect's own cache write never fires for a
      // `null` profile. Fire-and-forget for the same reason that effect's
      // write is: a storage failure must not turn a successful save into an
      // error on screen.
      void cacheProfileOffline({
        interests: payload.interests,
        evidence_mode: payload.evidence_mode ?? 'blend',
        followed_person_ids: payload.followed_person_ids ?? [],
      });
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

  /** Story 14.4: fetch the export document and offer it as a file. */
  async function onExport() {
    setExportError(null);
    setExportState(null);
    setIsExporting(true);
    try {
      setExportState(await exportProfile(userId));
    } catch (error) {
      setExportError(
        error instanceof ProfileLoadError
          ? error.message
          : 'We could not export your profile just now. Please try again.',
      );
    } finally {
      setIsExporting(false);
    }
  }

  /**
   * Story 14.4's delete, behind a confirmation.
   *
   * The confirmation is a separate render stage rather than `window.confirm`:
   * it has to state WHAT is irreversible (the key shred, which no backup can
   * undo), and a native dialog cannot be styled, cannot be read by the BDD
   * suite the way a rendered warning can, and is suppressed outright in some
   * embedded browsers — a destructive action silently losing its guard is
   * the worst available failure.
   */
  async function onConfirmDelete() {
    setDeleteError(null);
    setDeleteStage('deleting');
    try {
      const result = await deleteProfile(userId);
      setDeleteResult(result);
      // The editor must not keep showing interests that no longer exist
      // anywhere, so the local state is cleared to match the server.
      setSelections({});
      setSaveMessage(null);
      setExportState(null);
      setDeleteStage('idle');
    } catch (error) {
      setDeleteError(
        error instanceof ProfileSaveError
          ? error.message
          : 'We could not delete your profile just now. Nothing has been deleted.',
      );
      setDeleteStage('idle');
    }
  }

  /** Story 14.3's browser-side equivalent of delete: forget, locally. */
  function onClearAnonymous() {
    clearAnonymousProfile();
    setSelections({});
    setSaveMessage('Cleared. Nothing of your profile remains in this browser.');
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
  // Story 7.7: a failed network load for the profile is no longer always
  // fatal to this screen — if the offline-fallback effect above found a
  // cached copy, `showingOfflineCopy` is true and `hydrated` has already
  // been set from it, so the editor renders normally (with the offline
  // notice below) instead of stopping here. Only a genuine "no cache
  // either" case still shows the plain error.
  if (profileQuery.isError && !showingOfflineCopy) {
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
      {showingOfflineCopy ? (
        <p role="status">
          You're offline, so this is showing an offline copy of your profile, last cached
          {' '}
          {/* The exact timestamp is not read anywhere by this file; the
              notice's job is to tell the voter this is a cached offline
              copy at all, per story 7.7's acceptance criterion and the
              feature file's "shows that it is displaying a cached offline
              copy" step. */}
          on this device.
        </p>
      ) : null}
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

      <StorageModeChoice
        mode={storageMode}
        onChange={onStorageModeChange}
        anonymousAlternative={consentQuery.data?.anonymous_alternative}
      />

      {storageMode === 'account' ? (
        <ConsentPanel
          offer={consentQuery.data}
          isPending={consentQuery.isPending}
          isError={consentQuery.isError}
          consent={consent}
          onConsentChange={setConsent}
        />
      ) : null}

      {saveError ? <p role="alert">{saveError}</p> : null}
      {saveMessage ? <p role="status">{saveMessage}</p> : null}

      <button type="button" onClick={onSave} disabled={!canSave}>
        {isSaving ? 'Saving…' : 'Save my profile'}
      </button>

      {storageMode === 'anonymous' ? (
        <button type="button" onClick={onClearAnonymous}>
          Clear my profile from this browser
        </button>
      ) : (
        <PrivacyActions
          onExport={onExport}
          isExporting={isExporting}
          exportState={exportState}
          exportError={exportError}
          deleteStage={deleteStage}
          deleteResult={deleteResult}
          deleteError={deleteError}
          onAskDelete={() => {
            setDeleteError(null);
            setDeleteResult(null);
            setDeleteStage('confirming');
          }}
          onConfirmDelete={onConfirmDelete}
          onCancelDelete={() => setDeleteStage('idle')}
        />
      )}
    </section>
  );
}

/**
 * The account-or-anonymous choice (stories 14.2, 14.3).
 *
 * Radios, not a checkbox labelled "stay anonymous": ui-prd.md 2.2 requires
 * the alternative to be "side by side, as an actual choice — not a pre-checked
 * box", and two radios are the only control that renders two options as peers.
 * The anonymous option's own description comes from the server's consent offer
 * (`anonymous_alternative`), so the wording a voter weighs against consent is
 * the same wording the consent record's version names.
 */
function StorageModeChoice({
  mode,
  onChange,
  anonymousAlternative,
}: {
  mode: StorageMode;
  onChange: (mode: StorageMode) => void;
  anonymousAlternative?: string;
}) {
  return (
    <fieldset>
      <legend>Where should this profile live?</legend>
      <label>
        <input
          type="radio"
          name="storage-mode"
          value="account"
          checked={mode === 'account'}
          onChange={() => onChange('account')}
        />
        Save to my account, encrypted
      </label>
      <label>
        <input
          type="radio"
          name="storage-mode"
          value="anonymous"
          checked={mode === 'anonymous'}
          onChange={() => onChange('anonymous')}
        />
        Stay anonymous — nothing leaves this device
      </label>
      {anonymousAlternative ? <p>{anonymousAlternative}</p> : null}
    </fieldset>
  );
}

/**
 * The GDPR special-category consent ask (story 14.2).
 *
 * Every word of it is the server's, from `GET /v1/profiles/consent`, so the
 * version recorded in the consent ledger resolves to text that can be
 * reproduced later. A render failure here disables the save rather than
 * falling back to wording of the client's own: consenting to text the ledger
 * cannot name would make the record unverifiable, which is the one outcome
 * the whole versioning scheme exists to prevent.
 */
function ConsentPanel({
  offer,
  isPending,
  isError,
  consent,
  onConsentChange,
}: {
  offer?: {
    consent_version: string;
    summary: string;
    what_is_stored: string[];
    your_rights: string[];
  };
  isPending: boolean;
  isError: boolean;
  consent: boolean;
  onConsentChange: (value: boolean) => void;
}) {
  if (isPending) return <p>Loading the consent information…</p>;
  if (isError || !offer) {
    return (
      <p role="alert">
        We could not load the consent information just now, so nothing can be saved to an
        account. You can still use anonymous mode, or try again.
      </p>
    );
  }
  // The wrapper carries NO `aria-label`, deliberately. Its heading already
  // names the section for a screen reader, and any wrapper label containing
  // the word "consent" would make `getByLabelText(/consent/i)` ambiguous
  // between this section and the checkbox — which is the control both a
  // screen-reader user and a test actually mean by "consent".
  return (
    <section>
      <h3>Before we store anything in your account</h3>
      <p>{offer.summary}</p>
      <ul>
        {offer.what_is_stored.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <h4>Your rights</h4>
      <ul>
        {offer.your_rights.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <label>
        <input
          type="checkbox"
          checked={consent}
          onChange={(event) => onConsentChange(event.target.checked)}
        />
        I consent to saving my interests to my account
      </label>
      <p>Consent version {offer.consent_version}</p>
    </section>
  );
}

/**
 * Export and permanent delete (story 14.4) — prd.md's "one-tap export and
 * permanent deletion", rendered only on the account path because anonymous
 * mode has nothing stored to export or erase (its browser-side equivalent is
 * the "clear from this browser" button beside it).
 *
 * The export is offered as a download link built from a blob URL rather than
 * triggered automatically: a file appearing in a downloads folder without the
 * voter choosing a moment is startling, and the document contains their
 * political opinions in plaintext.
 */
function PrivacyActions({
  onExport,
  isExporting,
  exportState,
  exportError,
  deleteStage,
  deleteResult,
  deleteError,
  onAskDelete,
  onConfirmDelete,
  onCancelDelete,
}: {
  onExport: () => void;
  isExporting: boolean;
  exportState: ProfileExport | null;
  exportError: string | null;
  deleteStage: 'idle' | 'confirming' | 'deleting';
  deleteResult: DeleteProfileResult | null;
  deleteError: string | null;
  onAskDelete: () => void;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
}) {
  /**
   * The export document as a URL the browser can save to a file.
   *
   * A blob URL where the browser supports one, falling back to a `data:` URL
   * where it does not. The fallback is not only for test environments: some
   * embedded and privacy-hardened browsers withhold `createObjectURL`, and an
   * export button that throws — taking the whole editor's render down with it,
   * which is exactly what happened before this guard — is a far worse outcome
   * than a longer URL. Either way the voter's political opinions stay in this
   * document and are never uploaded anywhere to be turned into a link.
   */
  const downloadUrl = useMemo(() => {
    if (!exportState) return null;
    const json = JSON.stringify(exportState, null, 2);
    if (typeof URL.createObjectURL === 'function') {
      return URL.createObjectURL(new Blob([json], { type: 'application/json' }));
    }
    return `data:application/json;charset=utf-8,${encodeURIComponent(json)}`;
  }, [exportState]);

  // A blob URL pins its data in memory for the document's lifetime, so each
  // superseded one is revoked when a new export replaces it. A data: URL has
  // nothing to revoke, and `revokeObjectURL` may not exist at all on a browser
  // that lacked `createObjectURL`.
  useEffect(() => {
    if (!downloadUrl?.startsWith('blob:') || typeof URL.revokeObjectURL !== 'function') return;
    return () => URL.revokeObjectURL(downloadUrl);
  }, [downloadUrl]);

  return (
    <section aria-label="My data">
      <h3>My data</h3>

      <button type="button" onClick={onExport} disabled={isExporting}>
        {isExporting ? 'Preparing your export…' : 'Export my profile'}
      </button>
      {exportError ? <p role="alert">{exportError}</p> : null}
      {exportState && downloadUrl ? (
        <p>
          <a href={downloadUrl} download="informed-profile-export.json">
            Download my profile as JSON
          </a>{' '}
          (version {exportState.version}, {exportState.consent.length} consent record
          {exportState.consent.length === 1 ? '' : 's'})
        </p>
      ) : null}

      {deleteStage === 'idle' ? (
        <button type="button" onClick={onAskDelete}>
          Delete my profile
        </button>
      ) : (
        <div role="group" aria-label="Confirm deletion">
          <p>
            This is permanent and cannot be undone. Your profile is deleted and its encryption
            key is destroyed, so no copy or backup can ever be read again — by anyone, including
            Informed.
          </p>
          <button type="button" onClick={onConfirmDelete} disabled={deleteStage === 'deleting'}>
            {deleteStage === 'deleting' ? 'Deleting…' : 'Yes, delete my profile permanently'}
          </button>
          <button type="button" onClick={onCancelDelete} disabled={deleteStage === 'deleting'}>
            Cancel — keep my profile
          </button>
        </div>
      )}
      {deleteError ? <p role="alert">{deleteError}</p> : null}
      {deleteResult ? (
        <p role="status">
          Your profile and its encryption key were permanently deleted
          {deleteResult.consent_withdrawal_recorded ? ', and your consent withdrawal recorded' : ''}
          , in {deleteResult.elapsed_seconds} seconds.
        </p>
      ) : null}
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
