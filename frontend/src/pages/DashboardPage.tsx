import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { AdminStats } from '../api/client'

function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 flex flex-col gap-1">
      <span className="text-xs text-gray-500 uppercase tracking-wider">{label}</span>
      <span className="text-2xl font-semibold text-white tabular-nums">{value}</span>
    </div>
  )
}

export default function DashboardPage() {
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [loading, setLoading] = useState(true)

  async function load() {
    setLoading(true)
    try {
      const s = await api.admin.stats()
      setStats(s)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  return (
    <div className="max-w-5xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-lg font-semibold text-white">Dashboard</h1>
        <button
          onClick={load}
          disabled={loading}
          className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-40"
        >
          ↻ Refresh
        </button>
      </div>

      {loading ? (
        <p className="text-gray-500 text-sm">Loading…</p>
      ) : !stats ? (
        <p className="text-red-400 text-sm">Failed to load dashboard stats.</p>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
            <StatCard label="Media files" value={stats.media_files} />
            <StatCard label="Faces" value={stats.faces} />
            <StatCard label="Embeddings" value={stats.embeddings} />
            <StatCard label="Clusters" value={stats.clusters} />
            <StatCard label="Named persons" value={stats.persons} />
            <StatCard label="Writeback pending" value={stats.writeback_queue} />
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
      )}
    </div>
  )
}
