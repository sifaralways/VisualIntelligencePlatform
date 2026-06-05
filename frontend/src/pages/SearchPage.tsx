/**
 * SearchPage — natural-language search over local metadata + CLIP index.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { NaturalSearchResult, MediaResult } from '../api/client'
import PhotoDetail from '../components/PhotoDetail'

interface SearchPageProps {
  initialQuery?: string
  mode?: 'natural' | 'classic'
}

function mapClassicRow(row: MediaResult): NaturalSearchResult {
  return {
    media_id: row.id,
    file_path: row.file_path,
    date_taken: row.date_taken,
    persons: row.persons ? row.persons.split(',').map(s => s.trim()).filter(Boolean) : [],
    tags: row.tags ? row.tags.split(',').map(s => s.trim()).filter(Boolean) : [],
    sql_matched: true,
  }
}

export default function SearchPage({ initialQuery = '', mode = 'natural' }: SearchPageProps) {
  const [results, setResults] = useState<NaturalSearchResult[]>([])
  const [count, setCount] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [intent, setIntent] = useState<string>('')
  const [explanation, setExplanation] = useState<string>('')
  const [error, setError] = useState<string>('')
  const [selected, setSelected] = useState<NaturalSearchResult | null>(null)

  useEffect(() => {
    if (initialQuery.trim()) {
      void search(initialQuery)
    } else {
      setResults([])
      setCount(null)
      setIntent('')
      setExplanation('')
      setError('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQuery, mode])

  async function search(raw?: string) {
    const q = (raw ?? initialQuery).trim()
    if (!q) return

    setLoading(true)
    setError('')
    try {
      if (mode === 'classic') {
        const res = await api.search.query({ query: q, limit: 150, offset: 0 })
        setResults(res.results.map(mapClassicRow))
        setCount(res.count)
        setIntent('CLASSIC')
        setExplanation('Wildcard metadata search over people, filenames/folders, tags, OCR/captions/regions.')
      } else {
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

      {initialQuery.trim() ? (
        <p className="text-sm text-gray-300">
          Query: <span className="text-white">{initialQuery.trim()}</span>
        </p>
      ) : (
        <p className="text-sm text-gray-500">Use the top search bar to run a query.</p>
      )}

      {mode === 'classic' && (
        <p className="text-xs text-gray-500 -mt-1">
          Use <span className="text-gray-300">*</span> for any characters and <span className="text-gray-300">?</span> for one character. Matches people, filenames, folders, tags, and Florence text.
        </p>
      )}

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
        {mode === 'classic'
          ? 'Classic search runs locally over SQLite metadata and text tags.'
          : 'Search runs locally across SQLite metadata and CLIP embeddings.'}
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
      title={result.file_path}
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
        <p className="text-gray-300 text-[9px] truncate" title={result.file_path}>{result.file_path}</p>
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
