/**
 * AdminPage — database housekeeping + stats.
 *
 * Shows live row counts per table and offers scoped reset actions with
 * a confirmation step so the user can't accidentally wipe data.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'

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

// ─── Main component ─────────────────────────────────────────────────────────

export default function AdminPage() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)
  const [confirm, setConfirm] = useState<Scope | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string | null>(null)

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
      <h1 className="text-xl font-semibold mb-1">Admin</h1>
      <p className="text-sm text-gray-500 mb-8">Database stats and selective data reset.</p>

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
