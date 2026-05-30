/**
 * PhotoGrid — reusable paginated photo grid.
 *
 * Accepts a MediaFilter (person_id, tag_category, tag_label, state) and renders
 * photos as a grid of thumbnails. Click a photo to open PhotoDetail.
 */

import { useEffect, useState, useCallback, useRef } from 'react'
import { api, type MediaFile, type MediaFilter, type RemoveResult } from '../api/client'
import PhotoDetail from './PhotoDetail'

const PAGE_SIZE = 100

interface Props {
  filter?: MediaFilter
  title?: string
  /** Optional header slot rendered above the grid */
  headerSlot?: React.ReactNode
  /** Enable multi-select checkboxes + action bar */
  selectable?: boolean
  /** Show a Reprocess button in the multi-select action bar (requires selectable=true) */
  enableReprocess?: boolean
  /** Override the remove handler (ids, force) => RemoveResult; defaults to api.media.removeFromApp */
  onRemoveFromApp?: (ids: number[], force: boolean) => Promise<RemoveResult>
}

export default function PhotoGrid({
  filter = {},
  title,
  headerSlot,
  selectable = false,
  enableReprocess = false,
  onRemoveFromApp,
}: Props) {
  const [photos,   setPhotos]   = useState<MediaFile[]>([])
  const [total,    setTotal]    = useState(0)
  const [offset,   setOffset]   = useState(0)
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState<string | null>(null)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  // Multi-select
  const [selectMode,   setSelectMode]   = useState(false)
  const [selectedIds,  setSelectedIds]  = useState<Set<number>>(new Set())
  const [removing,     setRemoving]     = useState(false)
  const [warning,      setWarning]      = useState<RemoveResult | null>(null)
  const [pendingForce, setPendingForce] = useState<number[]>([])

  // Reprocess
  const [reprocessing,     setReprocessing]     = useState(false)
  const [reprocessQueued,  setReprocessQueued]  = useState(false)

  // Reset to page 0 when filter changes
  const filterKey = JSON.stringify(filter)
  const prevFilterKey = useRef(filterKey)
  useEffect(() => {
    if (prevFilterKey.current !== filterKey) {
      prevFilterKey.current = filterKey
      setOffset(0)
      setPhotos([])
      setSelectedIds(new Set())
      setSelectMode(false)
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
  }, [filterKey, offset, refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load() }, [load])

  // ── Multi-select helpers ────────────────────────────────────────────────

  function toggleSelect(id: number) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function clearSelection() {
    setSelectedIds(new Set())
    setSelectMode(false)
  }

  async function handleRemove(force: boolean) {
    const ids = force ? pendingForce : Array.from(selectedIds)
    if (ids.length === 0) return
    setRemoving(true)
    try {
      const doRemove = onRemoveFromApp
        ? (i: number[], f: boolean) => onRemoveFromApp(i, f)
        : (i: number[], f: boolean) => api.media.removeFromApp(i, f)
      const result = await doRemove(ids, force)
      if (result.status === 'warning') {
        setPendingForce(ids)
        setWarning(result)
        setRemoving(false)
        return
      }
      setWarning(null)
      setPendingForce([])
      clearSelection()
      load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Remove failed')
    } finally {
      setRemoving(false)
    }
  }

  async function handleReprocess() {
    const ids = Array.from(selectedIds)
    if (ids.length === 0) return
    setReprocessing(true)
    setReprocessQueued(false)
    try {
      await api.pipeline.reprocessBatch(ids)
      setReprocessQueued(true)
      clearSelection()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Reprocess failed — pipeline may already be running')
    } finally {
      setReprocessing(false)
    }
  }

  const anySelected = selectedIds.size > 0
  // Show checkboxes when selectMode is active or any item is already selected
  const showCheckboxes = selectMode || anySelected
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
        <div className="flex items-center gap-2">
          {selectable && photos.length > 0 && (
            <button
              onClick={() => {
                if (selectMode) { clearSelection() }
                else setSelectMode(true)
              }}
              className={`text-xs px-3 py-1.5 rounded-lg font-medium transition-colors ${
                selectMode
                  ? 'bg-indigo-600 text-white'
                  : 'bg-gray-800 text-gray-300 hover:text-white hover:bg-gray-700'
              }`}
            >
              {selectMode ? (anySelected ? `${selectedIds.size} selected` : 'Cancel') : '☑ Select'}
            </button>
          )}
          {headerSlot}
        </div>
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
          {photos.map((photo, index) => (
            <PhotoTile
              key={photo.id}
              photo={photo}
              selectable={selectable}
              isSelected={selectedIds.has(photo.id)}
              showCheckboxes={showCheckboxes}
              onSelect={() => toggleSelect(photo.id)}
              onClick={() => {
                if (showCheckboxes && selectable) toggleSelect(photo.id)
                else setSelectedIndex(index)
              }}
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
      {selectedIndex != null && photos[selectedIndex] && (
        <PhotoDetail
          mediaId={photos[selectedIndex].id}
          filePath={photos[selectedIndex].file_path}
          canGoPrev={selectedIndex > 0}
          canGoNext={selectedIndex < photos.length - 1}
          onNavigate={(delta: number) => {
            setSelectedIndex(prev => {
              if (prev == null) return prev
              const next = Math.max(0, Math.min(photos.length - 1, prev + delta))
              return next
            })
          }}
          onClose={() => setSelectedIndex(null)}
          onTagRemoved={() => setRefreshKey(k => k + 1)}
        />
      )}

      {/* ── Multi-select action bar ── */}
      {selectable && anySelected && (
        <div className="fixed bottom-0 left-0 right-0 z-40 flex items-center justify-between gap-4 px-6 py-3 bg-gray-900 border-t border-gray-700 shadow-2xl">
          <span className="text-sm text-gray-300">
            <span className="font-semibold text-white">{selectedIds.size}</span> selected
          </span>
          <div className="flex items-center gap-3">
            <button onClick={clearSelection} className="text-xs text-gray-400 hover:text-white transition-colors">
              Clear
            </button>
            {enableReprocess && (
              <button
                onClick={handleReprocess}
                disabled={reprocessing}
                className="text-sm bg-indigo-700 hover:bg-indigo-600 disabled:opacity-40 text-white font-medium rounded-lg px-4 py-2 transition-colors"
              >
                {reprocessing ? 'Queuing…' : '⟳ Reprocess'}
              </button>
            )}
            <button
              onClick={() => handleRemove(false)}
              disabled={removing}
              className="text-sm bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white font-medium rounded-lg px-4 py-2 transition-colors"
            >
              {removing ? 'Removing…' : '🗑 Remove from app'}
            </button>
          </div>
        </div>
      )}

      {/* ── Reprocess queued toast ── */}
      {reprocessQueued && (
        <div className="fixed bottom-20 right-4 z-50 flex items-center gap-3 bg-indigo-900 border border-indigo-600 rounded-xl px-4 py-3 shadow-2xl">
          <span className="text-indigo-300 text-sm">⟳ Reprocess queued — check the Pipeline tab for progress</span>
          <button
            onClick={() => setReprocessQueued(false)}
            className="text-indigo-400 hover:text-white text-xs transition-colors ml-2"
          >
            ✕
          </button>
        </div>
      )}

      {/* ── Writeback warning modal ── */}
      {warning && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-gray-900 border border-amber-700 rounded-2xl p-6 max-w-sm w-full mx-4 shadow-2xl">
            <h3 className="text-white font-semibold text-base mb-2">⚠️ Pending metadata</h3>
            <p className="text-gray-300 text-sm mb-3">
              {warning.unwritten_count} photo{warning.unwritten_count !== 1 ? 's' : ''} have metadata that hasn't been
              written to file yet. If you remove them now, those changes will be lost.
            </p>
            {warning.unwritten_paths && warning.unwritten_paths.length > 0 && (
              <ul className="text-amber-300 text-xs mb-4 space-y-0.5 max-h-24 overflow-y-auto">
                {warning.unwritten_paths.map((p, i) => (
                  <li key={i} className="truncate">{p.split('/').pop()}</li>
                ))}
                {(warning.unwritten_count ?? 0) > warning.unwritten_paths.length && (
                  <li className="text-gray-500">…and {(warning.unwritten_count ?? 0) - warning.unwritten_paths.length} more</li>
                )}
              </ul>
            )}
            <div className="flex gap-3">
              <button
                onClick={() => { setWarning(null); setPendingForce([]) }}
                className="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-200 text-sm font-medium rounded-lg py-2"
              >
                Cancel
              </button>
              <button
                onClick={() => handleRemove(true)}
                disabled={removing}
                className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white text-sm font-medium rounded-lg py-2"
              >
                {removing ? 'Removing…' : 'Remove anyway'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Single tile
// ---------------------------------------------------------------------------

function PhotoTile({
  photo,
  selectable,
  isSelected,
  showCheckboxes,
  onSelect,
  onClick,
}: {
  photo: MediaFile
  selectable: boolean
  isSelected: boolean
  showCheckboxes: boolean
  onSelect: () => void
  onClick: () => void
}) {
  const [errored, setErrored] = useState(false)
  const src = api.media.thumbnailUrl(photo.id)
  const filename = photo.file_path.split('/').pop() ?? ''
  const date = photo.date_taken ? photo.date_taken.slice(0, 10) : null

  return (
    <button
      onClick={onClick}
      title={photo.file_path}
      className={`relative aspect-square bg-gray-900 rounded overflow-hidden group transition-all focus:outline-none
        ${isSelected
          ? 'ring-2 ring-indigo-400'
          : 'hover:ring-2 hover:ring-indigo-400 focus:ring-2 focus:ring-indigo-400'}`}
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
        <p className="text-gray-300 text-[9px] truncate" title={photo.file_path}>{photo.file_path}</p>
        {date && <p className="text-gray-300 text-[9px]">{date}</p>}
      </div>
      {selectable && (showCheckboxes || isSelected) && (
        <div
          className="absolute top-1.5 left-1.5"
          onClick={e => { e.stopPropagation(); onSelect() }}
        >
          <div className={`w-5 h-5 rounded-full border-2 flex items-center justify-center transition-colors
            ${isSelected
              ? 'bg-indigo-500 border-indigo-400'
              : 'bg-black/60 border-gray-300 hover:border-white'}`}>
            {isSelected && <span className="text-white text-[10px] leading-none">✓</span>}
          </div>
        </div>
      )}
      {/* Selected tint */}
      {isSelected && <div className="absolute inset-0 bg-indigo-500/20 pointer-events-none" />}
    </button>
  )
}
