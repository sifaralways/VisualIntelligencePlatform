/**
 * SearchPage — query the local media index.
 * Works entirely against the SQLite DB — files do not need to be on disk.
 */

import { useState } from 'react'
import { api } from '../api/client'
import type { MediaResult } from '../api/client'

export default function SearchPage() {
  const [query, setQuery] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [results, setResults] = useState<MediaResult[]>([])
  const [count, setCount] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)

  async function search() {
    setLoading(true)
    try {
      const res = await api.search.query({
        query: query.trim(),
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: 100,
      })
      setResults(res.results)
      setCount(res.count)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-3xl">
      <h1 className="text-xl font-semibold mb-6">Search</h1>

      {/* Filters */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-4">
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && search()}
          placeholder="Name, camera, keyword…"
          className="col-span-full sm:col-span-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
        />
        <input
          type="date"
          value={dateFrom}
          onChange={e => setDateFrom(e.target.value)}
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
        />
        <input
          type="date"
          value={dateTo}
          onChange={e => setDateTo(e.target.value)}
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
        />
      </div>

      <button
        onClick={search}
        disabled={loading}
        className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg px-5 py-2 text-sm font-medium mb-6"
      >
        {loading ? 'Searching…' : 'Search'}
      </button>

      {/* Results */}
      {count !== null && (
        <p className="text-xs text-gray-400 mb-4">{count} result{count !== 1 ? 's' : ''}</p>
      )}
      <div className="space-y-2">
        {results.map(r => (
          <div key={r.id} className="bg-gray-800 rounded-lg px-4 py-3 text-sm">
            <div className="text-gray-200 truncate">{r.file_path.split('/').slice(-3).join(' / ')}</div>
            <div className="flex gap-4 mt-1 text-xs text-gray-500">
              {r.date_taken && <span>{r.date_taken.slice(0, 10)}</span>}
              {r.camera_model && <span>{r.camera_model}</span>}
              {r.persons && (
                <span className="text-indigo-400">
                  {r.persons.split(',').filter(Boolean).join(', ')}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      {count === 0 && (
        <p className="text-sm text-gray-500 mt-8 text-center">No results found.</p>
      )}

      <p className="mt-8 text-xs text-gray-600">
        Search runs against the local index — files don't need to be on disk.
      </p>
    </div>
  )
}
