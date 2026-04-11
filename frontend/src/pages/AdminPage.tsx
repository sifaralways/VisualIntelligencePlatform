/**
 * AdminPage — database housekeeping + stats.
 *
 * Shows live row counts per table and offers scoped reset actions with
 * a confirmation step so the user can't accidentally wipe data.
 */

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { AppSetting, ContactsMatchSuggestion, ContactsMatchStats } from '../api/client'
import RemoteServersPanel from '../components/RemoteServersPanel'

// ─── Types ─────────────────────────────────────────────────────────────────

interface Stats {
  media_files: number
  faces: number
  embeddings: number
  clusters: number
  persons: number
  writeback_queue: number
  thumbnail_files: number
  media_by_state: Record<string, number>
  geo_by_source: Record<string, number>
  photos_with_gps: number
  photos_geo_resolved: number
}

// ─── Reset actions available to the user ───────────────────────────────────

const ACTIONS = [
  {
    scope: 'persons',
    label: 'Clear named persons',
    colour: 'amber',
    description:
      'Removes all named persons. Clusters stay intact — go to the People tab and re-name them.',
    danger: false,
  },
  {
    scope: 'clusters',
    label: 'Clear clusters + persons',
    colour: 'orange',
    description:
      'Removes clusters and persons but keeps face embeddings. Next pipeline run will re-cluster from scratch.',
    danger: false,
  },
  {
    scope: 'faces',
    label: 'Clear faces, embeddings, clusters, persons',
    colour: 'red',
    description:
      'Removes all derived ML data (faces, embeddings, clusters, persons) and thumbnails. Media scan metadata is kept. Next pipeline run will re-detect from scratch.',
    danger: true,
  },
  {
    scope: 'all',
    label: '⚠️  Full factory reset',
    colour: 'red',
    description:
      'Wipes ALL data — media files, faces, embeddings, clusters, persons. Equivalent to a fresh install.',
    danger: true,
  },
] as const

type Scope = (typeof ACTIONS)[number]['scope']

// ─── Stat card ─────────────────────────────────────────────────────────────

function StatCard({ label, value, sub }: { label: string; value: number | string; sub?: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 flex flex-col gap-1">
      <span className="text-xs text-gray-500 uppercase tracking-wider">{label}</span>
      <span className="text-2xl font-semibold text-white tabular-nums">{value}</span>
      {sub && <span className="text-xs text-gray-600">{sub}</span>}
    </div>
  )
}

// ─── Colour helpers ─────────────────────────────────────────────────────────

const btnClass: Record<string, string> = {
  amber:
    'border-amber-600 text-amber-400 hover:bg-amber-600/20',
  orange:
    'border-orange-600 text-orange-400 hover:bg-orange-600/20',
  red:
    'border-red-700 text-red-400 hover:bg-red-700/20',
}

// ─── Contacts Face Match panel ──────────────────────────────────────────────

function thumbUrl(path: string | null): string | null {
  if (!path) return null
  return '/thumbnails/' + path.split('/thumbnails/').pop()
}

function SimilarityBadge({ pct }: { pct: number }) {
  const colour = pct >= 90
    ? 'bg-emerald-600/20 text-emerald-300 border-emerald-600/40'
    : pct >= 70
      ? 'bg-amber-600/20 text-amber-300 border-amber-600/40'
      : 'bg-gray-700 text-gray-400 border-gray-600'
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${colour}`}>
      {pct}%
    </span>
  )
}

function SuggestionCard({
  match,
  onAccept,
  onSkip,
  accepted,
  skipped,
}: {
  match: ContactsMatchSuggestion
  onAccept: (m: ContactsMatchSuggestion) => Promise<void>
  onSkip: (clusterId: number) => void
  accepted: boolean
  skipped: boolean
}) {
  const [busy, setBusy] = useState(false)
  const url = thumbUrl(match.thumbnail_path)

  if (skipped) return null
  if (accepted) {
    return (
      <div className="flex items-center gap-3 bg-gray-900 border border-gray-800 rounded-xl px-4 py-3 opacity-50">
        {url && <img src={url} alt="face" className="w-12 h-12 rounded-lg object-cover border border-emerald-700" />}
        <div className="flex-1 min-w-0">
          <span className="text-sm text-gray-400 line-through">{match.contact_name}</span>
          <p className="text-xs text-emerald-400 mt-0.5">✓ Named</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-3 bg-gray-900 border border-gray-800 rounded-xl px-4 py-3">
      {url
        ? <img src={url} alt="face" className="w-12 h-12 rounded-lg object-cover border border-gray-700 shrink-0" />
        : <div className="w-12 h-12 rounded-lg bg-gray-800 shrink-0" />
      }
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-gray-200 truncate">{match.contact_name}</p>
        <div className="flex items-center gap-2 mt-0.5">
          <SimilarityBadge pct={match.similarity_pct} />
          <span className="text-xs text-gray-500">{match.cluster_size} photo{match.cluster_size !== 1 ? 's' : ''}</span>
        </div>
      </div>
      <div className="flex gap-2 shrink-0">
        <button
          onClick={async () => { setBusy(true); await onAccept(match); setBusy(false) }}
          disabled={busy}
          className="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white transition-colors"
        >
          {busy ? '…' : '✓ Accept'}
        </button>
        <button
          onClick={() => onSkip(match.cluster_id)}
          disabled={busy}
          className="px-3 py-1.5 rounded-lg text-xs font-medium border border-gray-700 text-gray-400 hover:bg-gray-800 disabled:opacity-40 transition-colors"
        >
          Skip
        </button>
      </div>
    </div>
  )
}

function ContactsMatchPanel() {
  const [threshold, setThreshold] = useState(0.60)
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [matches, setMatches] = useState<ContactsMatchSuggestion[]>([])
  const [stats, setStats] = useState<ContactsMatchStats | null>(null)
  const [accepted, setAccepted] = useState<Set<number>>(new Set())
  const [skipped, setSkipped] = useState<Set<number>>(new Set())
  const [acceptError, setAcceptError] = useState<string | null>(null)

  const autoMatches = matches.filter(m => m.auto_name)
  const reviewMatches = matches.filter(m => !m.auto_name)
  const pendingAutoCount = autoMatches.filter(m => !accepted.has(m.cluster_id) && !skipped.has(m.cluster_id)).length

  async function runMatch() {
    setStatus('running')
    setError(null)
    setMatches([])
    setStats(null)
    setAccepted(new Set())
    setSkipped(new Set())
    setAcceptError(null)
    try {
      const res = await api.admin.contactsMatch(threshold)
      setMatches(res.matches)
      setStats(res.stats)
      setStatus('done')
    } catch (e: unknown) {
      const err = e as { message?: string }
      setError(err?.message ?? 'Unknown error')
      setStatus('error')
    }
  }

  async function acceptOne(match: ContactsMatchSuggestion) {
    setAcceptError(null)
    try {
      await api.persons.fromCluster(match.cluster_id, match.contact_name)
      setAccepted(prev => new Set([...prev, match.cluster_id]))
    } catch (e: unknown) {
      const err = e as { message?: string }
      setAcceptError(`Failed to name cluster ${match.cluster_id}: ${err?.message ?? 'unknown'}`)
    }
  }

  async function acceptAll() {
    setAcceptError(null)
    for (const match of autoMatches) {
      if (accepted.has(match.cluster_id) || skipped.has(match.cluster_id)) continue
      try {
        await api.persons.fromCluster(match.cluster_id, match.contact_name)
        setAccepted(prev => new Set([...prev, match.cluster_id]))
      } catch (e: unknown) {
        const err = e as { message?: string }
        setAcceptError(`Failed to name "${match.contact_name}": ${err?.message ?? 'unknown'}`)
        break
      }
    }
  }

  function skip(clusterId: number) {
    setSkipped(prev => new Set([...prev, clusterId]))
  }

  return (
    <section className="mt-10">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider">
            Contacts Face Match
          </h2>
          <p className="text-xs text-gray-600 mt-0.5">
            Match your macOS Contacts photos against unnamed face clusters.
          </p>
        </div>
        {status === 'done' && (
          <button
            onClick={runMatch}
            className="text-xs text-indigo-400 hover:text-indigo-300"
          >
            ↻ Re-run
          </button>
        )}
      </div>

      {/* Controls */}
      {(status === 'idle' || status === 'error') && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 flex flex-col gap-4">
          <div>
            <div className="flex justify-between text-xs text-gray-400 mb-1">
              <span>Minimum similarity threshold</span>
              <span className="font-semibold text-white">{Math.round(threshold * 100)}%</span>
            </div>
            <input
              type="range" min={0.40} max={0.95} step={0.05}
              value={threshold}
              onChange={e => setThreshold(parseFloat(e.target.value))}
              className="w-full accent-indigo-500"
            />
            <div className="flex justify-between text-xs text-gray-600 mt-0.5">
              <span>40% — broad</span>
              <span className="text-gray-500">≥90% auto-named</span>
              <span>95% — strict</span>
            </div>
          </div>
          {error && (
            <p className="text-xs text-red-400 bg-red-950/30 border border-red-900/40 rounded-lg px-3 py-2">
              {error}
            </p>
          )}
          <button
            onClick={runMatch}
            className="self-start px-4 py-2 rounded-lg text-sm font-medium bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
          >
            Run Contacts Match
          </button>
        </div>
      )}

      {/* Running */}
      {status === 'running' && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-6 flex flex-col items-center gap-3">
          <div className="w-6 h-6 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-gray-400">
            Scanning Contacts and matching faces…
          </p>
          <p className="text-xs text-gray-600">
            This may take 30–120 seconds depending on how many contacts have photos.
          </p>
        </div>
      )}

      {/* Results */}
      {status === 'done' && (
        <div className="flex flex-col gap-5">
          {/* Stats banner */}
          {stats && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              <StatCard label="Contacts scanned" value={stats.total_contacts} />
              <StatCard label="With detectable face" value={stats.contacts_with_face} />
              <StatCard label="Unnamed clusters" value={stats.unnamed_clusters} />
              <StatCard label="Time" value={`${stats.elapsed_seconds}s`} />
            </div>
          )}

          {matches.length === 0 ? (
            <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 text-sm text-gray-400">
              No matches found above {Math.round(threshold * 100)}% threshold.
              Try lowering the similarity slider and re-running.
            </div>
          ) : (
            <>
              {acceptError && (
                <p className="text-xs text-red-400 bg-red-950/30 border border-red-900/40 rounded-lg px-3 py-2">
                  {acceptError}
                </p>
              )}

              {/* Auto-nameable (≥90%) */}
              {autoMatches.length > 0 && (
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="text-xs font-semibold text-emerald-400 uppercase tracking-wider">
                      ✅ Auto-nameable — ≥90% confidence ({autoMatches.length})
                    </h3>
                    {pendingAutoCount > 0 && (
                      <button
                        onClick={acceptAll}
                        className="text-xs bg-emerald-700 hover:bg-emerald-600 text-white rounded-lg px-3 py-1.5 font-medium transition-colors"
                      >
                        Accept All ({pendingAutoCount})
                      </button>
                    )}
                  </div>
                  <div className="flex flex-col gap-2">
                    {autoMatches.map(m => (
                      <SuggestionCard
                        key={m.cluster_id}
                        match={m}
                        onAccept={acceptOne}
                        onSkip={skip}
                        accepted={accepted.has(m.cluster_id)}
                        skipped={skipped.has(m.cluster_id)}
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* Suggestions (60–90%) */}
              {reviewMatches.length > 0 && (
                <div>
                  <h3 className="text-xs font-semibold text-amber-400 uppercase tracking-wider mb-2">
                    👀 Review suggestions — {Math.round(threshold * 100)}–90% ({reviewMatches.length})
                  </h3>
                  <div className="flex flex-col gap-2">
                    {reviewMatches.map(m => (
                      <SuggestionCard
                        key={m.cluster_id}
                        match={m}
                        onAccept={acceptOne}
                        onSkip={skip}
                        accepted={accepted.has(m.cluster_id)}
                        skipped={skipped.has(m.cluster_id)}
                      />
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  )
}

// ─── Main component ─────────────────────────────────────────────────────────

export default function AdminPage() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)
  const [confirm, setConfirm] = useState<Scope | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string | null>(null)

  // ── Settings state ───────────────────────────────────────────────────────
  const [settings, setSettings] = useState<AppSetting[]>([])
  const [settingsEdits, setSettingsEdits] = useState<Record<string, number>>({})
  const [settingsBusy, setSettingsBusy] = useState(false)
  const [settingsMsg, setSettingsMsg] = useState<string | null>(null)
  const [settingsLoading, setSettingsLoading] = useState(true)
  const [settingsError, setSettingsError] = useState<string | null>(null)

  async function loadSettings() {
    setSettingsLoading(true)
    setSettingsError(null)
    try {
      const s = await api.settings.getAll()
      setSettings(s)
      setSettingsEdits({})
    } catch (e: unknown) {
      const err = e as { message?: string }
      setSettingsError(err?.message ?? 'Failed to load settings')
    } finally {
      setSettingsLoading(false)
    }
  }

  useEffect(() => { loadSettings() }, [])

  const isDirty = useMemo(
    () => Object.keys(settingsEdits).length > 0,
    [settingsEdits],
  )

  function editSetting(key: string, raw: string) {
    const num = parseFloat(raw)
    if (!isNaN(num)) setSettingsEdits(prev => ({ ...prev, [key]: num }))
  }

  function val(s: AppSetting): number {
    return s.key in settingsEdits ? settingsEdits[s.key] : s.value
  }

  async function saveSettings() {
    if (!isDirty) return
    setSettingsBusy(true)
    setSettingsMsg(null)
    try {
      await api.settings.update(settingsEdits)
      setSettingsMsg('Settings saved.')
      await loadSettings()
    } catch (e: unknown) {
      const err = e as { message?: string }
      setSettingsMsg('Error: ' + (err?.message ?? 'unknown'))
    } finally {
      setSettingsBusy(false)
    }
  }

  async function resetSettings() {
    setSettingsBusy(true)
    setSettingsMsg(null)
    try {
      await api.settings.reset()
      setSettingsMsg('Reset to defaults.')
      await loadSettings()
    } catch (e: unknown) {
      const err = e as { message?: string }
      setSettingsMsg('Error: ' + (err?.message ?? 'unknown'))
    } finally {
      setSettingsBusy(false)
    }
  }

  // Group settings by the `group` field
  const settingGroups = useMemo(() => {
    const map = new Map<string, AppSetting[]>()
    for (const s of settings) {
      const arr = map.get(s.group) ?? []
      arr.push(s)
      map.set(s.group, arr)
    }
    return map
  }, [settings])

  async function loadStats() {
    setLoading(true)
    try {
      const s = await api.admin.stats()
      setStats(s)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadStats() }, [])

  async function doReset(scope: Scope) {
    setBusy(true)
    setResult(null)
    try {
      const res = await api.admin.reset(scope)
      setResult(res.detail ?? 'Done.')
      setConfirm(null)
      await loadStats()
    } catch (e: unknown) {
      const err = e as { message?: string }
      setResult('Error: ' + (err?.message ?? 'unknown'))
    } finally {
      setBusy(false)
    }
  }

  const confirmAction = ACTIONS.find(a => a.scope === confirm)

  return (
    <div className="max-w-3xl">
      <p className="text-sm text-gray-500 mb-6">Database stats and selective data reset.</p>

      {/* ── Stats ─────────────────────────────────────────────── */}
      <section className="mb-10">
        <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">
          Database overview
        </h2>
        {loading ? (
          <p className="text-gray-500 text-sm">Loading…</p>
        ) : stats ? (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
              <StatCard label="Media files"  value={stats.media_files} />
              <StatCard label="Faces"        value={stats.faces} />
              <StatCard label="Embeddings"   value={stats.embeddings} />
              <StatCard label="Clusters"     value={stats.clusters} />
              <StatCard label="Named persons" value={stats.persons} />
              <StatCard label="Writeback queue" value={stats.writeback_queue} />
              <StatCard label="Thumbnail files" value={stats.thumbnail_files} />
            </div>
            {stats.media_by_state && Object.keys(stats.media_by_state).length > 0 && (
              <div className="text-xs text-gray-600 mt-1 flex flex-wrap gap-4">
                {Object.entries(stats.media_by_state).map(([state, n]) => (
                  <span key={state}>
                    <span className="text-gray-400">{n}</span> × {state}
                  </span>
                ))}
              </div>
            )}

            {/* ── Geo-resolution stats ─────────────────────────── */}
            <div className="mt-4 bg-gray-900 border border-gray-800 rounded-xl px-4 py-3">
              <p className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-2">Location resolution</p>
              <div className="flex flex-wrap gap-6 text-sm">
                <span className="text-gray-400">
                  GPS photos: <span className="text-white font-medium">{stats.photos_with_gps ?? 0}</span>
                </span>
                <span className="text-gray-400">
                  Resolved: <span className="text-white font-medium">{stats.photos_geo_resolved ?? 0}</span>
                  {stats.photos_with_gps > 0 && (
                    <span className="text-gray-600 ml-1">
                      ({Math.round(((stats.photos_geo_resolved ?? 0) / stats.photos_with_gps) * 100)}%)
                    </span>
                  )}
                </span>
                {['mapkit', 'nominatim'].map(src => (
                  <span key={src} className="text-gray-400">
                    {src === 'mapkit' ? 'MapKit' : 'Nominatim'}:
                    {' '}<span className={`font-medium ${src === 'mapkit' ? 'text-green-400' : 'text-yellow-400'}`}>
                      {stats.geo_by_source?.[src] ?? 0}
                    </span>
                    <span className="text-gray-600 ml-1">place tags</span>
                  </span>
                ))}
              </div>
            </div>
          </>
        ) : (
          <p className="text-red-400 text-sm">Failed to load stats.</p>
        )}
        <button
          onClick={loadStats}
          disabled={loading}
          className="mt-3 text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-40"
        >
          ↻ Refresh
        </button>
      </section>

      {/* ── Reset actions ────────────────────────────────────────── */}
      <section>
        <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">
          Reset data
        </h2>
        <div className="flex flex-col gap-3">
          {ACTIONS.map(action => (
            <div
              key={action.scope}
              className="flex items-start justify-between gap-4 bg-gray-900 border border-gray-800 rounded-xl px-5 py-4"
            >
              <div className="flex-1">
                <p className="text-sm font-medium text-gray-200 mb-0.5">{action.label}</p>
                <p className="text-xs text-gray-500">{action.description}</p>
              </div>
              <button
                onClick={() => { setResult(null); setConfirm(action.scope) }}
                className={`shrink-0 border rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${btnClass[action.colour]}`}
              >
                Clear
              </button>
            </div>
          ))}
        </div>
      </section>

      {/* ── Result banner ────────────────────────────────────────── */}
      {result && (
        <div className="mt-6 bg-gray-900 border border-gray-700 rounded-xl px-5 py-3 text-sm text-gray-300">
          {result}
        </div>
      )}

      {/* ── Contacts Face Match ───────────────────────────────────── */}
      <ContactsMatchPanel />

      {/* ── ML Settings ──────────────────────────────────────────── */}
      <section className="mt-10">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider">
              ML Model Settings
            </h2>
            <div className="flex gap-2">
              {isDirty && (
                <button
                  onClick={saveSettings}
                  disabled={settingsBusy}
                  className="text-xs bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg px-3 py-1.5 font-medium transition-colors"
                >
                  {settingsBusy ? 'Saving…' : 'Save Changes'}
                </button>
              )}
              <button
                onClick={resetSettings}
                disabled={settingsBusy}
                className="text-xs border border-gray-700 text-gray-400 hover:bg-gray-800 disabled:opacity-40 rounded-lg px-3 py-1.5 font-medium transition-colors"
              >
                Reset to Defaults
              </button>
            </div>
          </div>

          {settingsMsg && (
            <div className="mb-4 bg-gray-900 border border-gray-700 rounded-xl px-4 py-2.5 text-sm text-gray-300">
              {settingsMsg}
            </div>
          )}

          {settingsLoading && (
            <p className="text-gray-500 text-sm">Loading settings…</p>
          )}

          {settingsError && !settingsLoading && (
            <div className="bg-gray-900 border border-red-900/60 rounded-xl px-5 py-4">
              <p className="text-red-400 text-sm font-medium mb-1">Could not load settings</p>
              <p className="text-gray-500 text-xs mb-3">{settingsError}</p>
              <p className="text-gray-600 text-xs">
                If this is a fresh deployment, restart the backend so migration 004 is applied,
                then refresh this page.
              </p>
              <button
                onClick={loadSettings}
                className="mt-3 text-xs text-indigo-400 hover:text-indigo-300"
              >
                ↻ Retry
              </button>
            </div>
          )}

          <div className="flex flex-col gap-6">
            {Array.from(settingGroups.entries()).map(([group, items]) => (
              <div key={group}>
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
                  {group}
                </h3>
                <div className="flex flex-col gap-3">
                  {items.map(s => (
                    <div
                      key={s.key}
                      className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4"
                    >
                      <div className="flex items-start justify-between gap-4 mb-2">
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium text-gray-200">{s.label}</p>
                          <p className="text-xs text-gray-500 mt-0.5">{s.description}</p>
                        </div>
                        {s.options && s.options.length > 0 ? (
                          /* ── Segmented control for any setting with an options list ── */
                          <div className="shrink-0 flex items-center gap-1 bg-gray-800 rounded-lg p-1">
                            {s.options.map(opt => (
                              <button
                                key={opt.value}
                                onClick={() => editSetting(s.key, String(opt.value))}
                                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                                  val(s) === opt.value
                                    ? 'bg-indigo-600 text-white shadow'
                                    : 'text-gray-400 hover:text-gray-200'
                                }`}
                              >
                                {opt.label}
                              </button>
                            ))}
                          </div>
                        ) : (
                          /* ── Number input for float/int settings without explicit options ── */
                          <div className="shrink-0 flex items-center gap-2">
                            <input
                              type="number"
                              min={s.min}
                              max={s.max}
                              step={s.step}
                              value={val(s)}
                              onChange={e => editSetting(s.key, e.target.value)}
                              className="w-24 bg-gray-800 border border-gray-700 text-white text-right text-sm rounded-lg px-2 py-1 focus:outline-none focus:border-indigo-500"
                            />
                          </div>
                        )}
                      </div>
                      {/* Slider only for continuous numeric settings, not option lists */}
                      {(!s.options || s.options.length === 0) && (
                        <>
                          <input
                            type="range"
                            min={s.min}
                            max={s.max}
                            step={s.step}
                            value={val(s)}
                            onChange={e => editSetting(s.key, e.target.value)}
                            className="w-full accent-indigo-500"
                          />
                          <div className="flex justify-between text-xs text-gray-600 mt-0.5">
                            <span>{s.min}</span>
                            <span className="text-gray-500">default: {s.default}</span>
                            <span>{s.max}</span>
                          </div>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

      {/* ── Remote Servers ──────────────────────────────────────── */}
      <RemoteServersPanel />

      {/* ── Confirmation modal ───────────────────────────────────── */}
      {confirm && confirmAction && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-sm w-full shadow-2xl">
            <p className="text-white font-semibold mb-2">{confirmAction.label}</p>
            <p className="text-gray-400 text-sm mb-6">{confirmAction.description}</p>
            {confirmAction.danger && (
              <p className="text-red-400 text-xs font-medium mb-4">
                ⚠️ This cannot be undone.
              </p>
            )}
            <div className="flex gap-3">
              <button
                onClick={() => doReset(confirm)}
                disabled={busy}
                className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-lg py-2 text-sm font-medium"
              >
                {busy ? 'Clearing…' : 'Yes, clear it'}
              </button>
              <button
                onClick={() => setConfirm(null)}
                disabled={busy}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-lg py-2 text-sm font-medium"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
