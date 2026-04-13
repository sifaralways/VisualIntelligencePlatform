/**
 * SearchPage — natural-language search over local metadata + CLIP index.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { NaturalSearchResult } from '../api/client'
import PhotoDetail from '../components/PhotoDetail'

interface SearchPageProps {
  initialQuery?: string
}

export default function SearchPage({ initialQuery = '' }: SearchPageProps) {
  const [query, setQuery] = useState(initialQuery)
  const [results, setResults] = useState<NaturalSearchResult[]>([])
  const [count, setCount] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [intent, setIntent] = useState<string>('')
  const [explanation, setExplanation] = useState<string>('')
  const [error, setError] = useState<string>('')
  const [selected, setSelected] = useState<NaturalSearchResult | null>(null)

  useEffect(() => {
    setQuery(initialQuery)
    if (initialQuery.trim()) {
      void search(initialQuery)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQuery])

  async function search(raw?: string) {
    const q = (raw ?? query).trim()
    if (!q) return

    setLoading(true)
    setError('')
    try {
      const res = await api.search.natural({
        query: q,
        limit: 100,
      })
      setResults(res.results)
      setCount(res.count)
      setIntent(res.intent)
      setExplanation(res.explanation)
      if (res.error) {
        setError(res.error)
      }
    } catch (e: any) {
      setError(String(e?.message || e || 'Search failed'))
      setResults([])
      setCount(0)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Search</h1>

      <div className="grid grid-cols-1 gap-3">
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && search()}
          placeholder="Ask in natural language, e.g. Who accompanied Akshat in Blue Mountains trip in 2016"
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
        />
      </div>

      <button
        onClick={() => { void search() }}
        disabled={loading}
        className="self-start bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg px-5 py-2 text-sm font-medium"
      >
        {loading ? 'Searching…' : 'Search'}
      </button>

      {(intent || explanation) && (
        <div className="rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2">
          {intent && <p className="text-[11px] text-indigo-300 mb-1">Route: {intent}</p>}
          {explanation && <p className="text-xs text-gray-400">{explanation}</p>}
        </div>
      )}

      {error && (
        <p className="text-sm text-red-400">{error}</p>
      )}

      {count !== null && (
        <p className="text-xs text-gray-400">{count} result{count !== 1 ? 's' : ''}</p>
      )}

      {/* Loading skeleton */}
      {loading && results.length === 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-1">
          {Array.from({ length: 20 }).map((_, i) => (
            <div key={i} className="aspect-square bg-gray-800 rounded animate-pulse" />
          ))}
        </div>
      )}

      {/* Thumbnail grid */}
      {results.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-1">
          {results.map(r => (
            <SearchTile key={r.media_id} result={r} onClick={() => setSelected(r)} />
          ))}
        </div>
      )}

      {count === 0 && !loading && (
        <p className="text-sm text-gray-500 mt-8 text-center">No results found.</p>
      )}

      <p className="mt-4 text-xs text-gray-600">
        Search runs locally across SQLite metadata and CLIP embeddings.
      </p>

      {/* Detail modal */}
      {selected && (
        <PhotoDetail
          mediaId={selected.media_id}
          filePath={selected.file_path}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tile
// ---------------------------------------------------------------------------
function SearchTile({ result, onClick }: { result: NaturalSearchResult; onClick: () => void }) {
  const [errored, setErrored] = useState(false)
  const src = api.media.thumbnailUrl(result.media_id)
  const filename = result.file_path.split('/').pop() ?? ''
  const date = result.date_taken ? result.date_taken.slice(0, 10) : null

  return (
    <button
      onClick={onClick}
      title={`${filename}${date ? `  •  ${date}` : ''}`}
      className="relative aspect-square bg-gray-900 rounded overflow-hidden group transition-all focus:outline-none hover:ring-2 hover:ring-indigo-400 focus:ring-2 focus:ring-indigo-400"
    >
      {errored ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-gray-600 gap-1">
          <span className="text-2xl">📷</span>
          <span className="text-xs truncate px-1 max-w-full">{filename}</span>
        </div>
      ) : (
        <img
          src={src}
          alt={filename}
          loading="lazy"
          onError={() => setErrored(true)}
          className="w-full h-full object-cover transition-transform duration-200 group-hover:scale-105"
        />
      )}
      {/* Hover overlay */}
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent p-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
        <p className="text-white text-[10px] truncate">{filename}</p>
        {date && <p className="text-gray-300 text-[9px]">{date}</p>}
        {result.persons?.length > 0 && (
          <p className="text-indigo-300 text-[9px] truncate">{result.persons.join(', ')}</p>
        )}
        {result.clip_score !== undefined && (
          <p className="text-emerald-400 text-[9px]">{(result.clip_score * 100).toFixed(0)}% match</p>
        )}
      </div>
    </button>
  )
}
