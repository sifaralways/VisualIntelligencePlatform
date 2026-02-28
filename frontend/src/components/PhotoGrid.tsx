/**
 * PhotoGrid — reusable paginated photo grid.
 *
 * Accepts a MediaFilter (person_id, tag_category, tag_label, state) and renders
 * photos as a grid of thumbnails. Click a photo to open PhotoDetail.
 */

import { useEffect, useState, useCallback, useRef } from 'react'
import { api, type MediaFile, type MediaFilter } from '../api/client'
import PhotoDetail from './PhotoDetail'

const PAGE_SIZE = 100

interface Props {
  filter?: MediaFilter
  title?: string
  /** Optional header slot rendered above the grid */
  headerSlot?: React.ReactNode
}

export default function PhotoGrid({ filter = {}, title, headerSlot }: Props) {
  const [photos,  setPhotos]  = useState<MediaFile[]>([])
  const [total,   setTotal]   = useState(0)
  const [offset,  setOffset]  = useState(0)
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState<string | null>(null)
  const [selected, setSelected] = useState<MediaFile | null>(null)

  // Reset to page 0 when filter changes
  const filterKey = JSON.stringify(filter)
  const prevFilterKey = useRef(filterKey)
  useEffect(() => {
    if (prevFilterKey.current !== filterKey) {
      prevFilterKey.current = filterKey
      setOffset(0)
      setPhotos([])
    }
  }, [filterKey])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [items, cnt] = await Promise.all([
        api.media.list({ ...filter, limit: PAGE_SIZE, offset }),
        api.media.count(filter),
      ])
      setPhotos(items)
      setTotal(cnt.count)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load photos')
    } finally {
      setLoading(false)
    }
  }, [filterKey, offset]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load() }, [load])

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1

  return (
    <div className="flex flex-col gap-4 h-full">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          {title && <h2 className="text-xl font-semibold text-white">{title}</h2>}
          {!loading && (
            <p className="text-xs text-gray-500 mt-0.5">{total.toLocaleString()} photo{total !== 1 ? 's' : ''}</p>
          )}
        </div>
        {headerSlot}
      </div>

      {/* Error */}
      {error && (
        <div className="bg-red-900/40 border border-red-700 rounded p-3 text-red-300 text-sm">{error}</div>
      )}

      {/* Loading skeleton */}
      {loading && photos.length === 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-1">
          {Array.from({ length: 30 }).map((_, i) => (
            <div key={i} className="aspect-square bg-gray-800 rounded animate-pulse" />
          ))}
        </div>
      )}

      {/* Empty state */}
      {!loading && photos.length === 0 && !error && (
        <div className="flex flex-col items-center justify-center py-24 text-gray-500 gap-3">
          <span className="text-5xl">📷</span>
          <p className="text-sm">No photos found. Run the pipeline to index your library.</p>
        </div>
      )}

      {/* Grid */}
      {photos.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-1">
          {photos.map(photo => (
            <PhotoTile
              key={photo.id}
              photo={photo}
              onClick={() => setSelected(photo)}
            />
          ))}
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-4 pt-2">
          <button
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0}
            className="px-4 py-1.5 rounded bg-gray-800 text-gray-300 text-sm disabled:opacity-30 hover:bg-gray-700"
          >
            ← Prev
          </button>
          <span className="text-gray-400 text-sm">
            Page {currentPage} of {totalPages}
          </span>
          <button
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + PAGE_SIZE >= total}
            className="px-4 py-1.5 rounded bg-gray-800 text-gray-300 text-sm disabled:opacity-30 hover:bg-gray-700"
          >
            Next →
          </button>
        </div>
      )}

      {/* Detail modal */}
      {selected && (
        <PhotoDetail
          mediaId={selected.id}
          filePath={selected.file_path}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Single tile
// ---------------------------------------------------------------------------

function PhotoTile({ photo, onClick }: { photo: MediaFile; onClick: () => void }) {
  const [errored, setErrored] = useState(false)
  const src = api.media.thumbnailUrl(photo.id)

  const filename = photo.file_path.split('/').pop() ?? ''
  const date = photo.date_taken ? photo.date_taken.slice(0, 10) : null

  return (
    <button
      onClick={onClick}
      title={`${filename}${date ? `  •  ${date}` : ''}`}
      className="relative aspect-square bg-gray-900 rounded overflow-hidden group hover:ring-2 hover:ring-indigo-400 transition-all focus:outline-none focus:ring-2 focus:ring-indigo-400"
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
      {/* Hover overlay with filename */}
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent p-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
        <p className="text-white text-[10px] truncate">{filename}</p>
        {date && <p className="text-gray-300 text-[9px]">{date}</p>}
      </div>
    </button>
  )
}
