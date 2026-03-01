import { useState, useEffect, useCallback } from 'react'
import { api } from '../api/client'
import type { QualityIssue } from '../api/client'

type IssueFilter = 'all' | 'blurry' | 'closed_eyes'

const FILTER_LABELS: Record<IssueFilter, string> = {
  all: 'All Issues',
  blurry: 'Blurry',
  closed_eyes: 'Closed Eyes',
}

export default function QualityPage() {
  const [filter, setFilter] = useState<IssueFilter>('all')
  const [items, setItems] = useState<QualityIssue[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [showConfirm, setShowConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    try {
      const data = await api.media.quality(filter)
      setItems(data)
    } catch (e: any) {
      setError(e.message ?? 'Failed to load quality issues')
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => { load() }, [load])

  const toggleSelect = (id: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const selectAll = () => setSelected(new Set(items.map(i => i.id)))
  const clearAll  = () => setSelected(new Set())

  const handleDelete = async () => {
    setDeleting(true)
    try {
      await api.media.bulkDelete([...selected])
      setShowConfirm(false)
      await load()
    } catch (e: any) {
      setError(e.message ?? 'Delete failed')
      setShowConfirm(false)
    } finally {
      setDeleting(false)
    }
  }

  const filename = (path: string) => path.split('/').pop() ?? path

  return (
    <div className="p-6 h-full flex flex-col overflow-hidden">
      <h1 className="text-xl font-semibold mb-1">Quality Review</h1>
      <p className="text-gray-400 text-sm mb-5">
        Photos flagged as blurry or with closed eyes. Select and delete bad shots.
      </p>

      {/* Filter tabs */}
      <div className="flex gap-2 mb-5">
        {(Object.keys(FILTER_LABELS) as IssueFilter[]).map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-4 py-1.5 rounded-full text-sm font-medium transition-colors ${
              filter === f
                ? 'bg-indigo-600 text-white'
                : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
            }`}
          >
            {FILTER_LABELS[f]}
          </button>
        ))}
      </div>

      {/* Toolbar */}
      {!loading && items.length > 0 && (
        <div className="flex items-center gap-3 mb-4">
          <button
            onClick={selected.size === items.length ? clearAll : selectAll}
            className="text-xs text-indigo-400 hover:text-indigo-300 underline"
          >
            {selected.size === items.length ? 'Deselect all' : 'Select all'}
          </button>
          {selected.size > 0 && (
            <>
              <span className="text-xs text-gray-400">{selected.size} selected</span>
              <button
                onClick={() => setShowConfirm(true)}
                className="ml-auto bg-red-700 hover:bg-red-600 text-white text-sm font-medium px-4 py-1.5 rounded-lg transition-colors"
              >
                Delete {selected.size} photo{selected.size > 1 ? 's' : ''}
              </button>
            </>
          )}
        </div>
      )}

      {/* Content */}
      {error && (
        <div className="text-red-400 text-sm bg-red-900/20 rounded-lg px-4 py-3 mb-4">{error}</div>
      )}

      {loading ? (
        <div className="text-gray-400 text-sm">Loading…</div>
      ) : items.length === 0 ? (
        <div className="text-gray-500 text-sm">No quality issues found — looking good!</div>
      ) : (
        <div className="overflow-y-auto flex-1">
          <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
            {items.map(item => {
              const isSelected = selected.has(item.id)
              return (
                <div
                  key={item.id}
                  onClick={() => toggleSelect(item.id)}
                  className={`relative rounded-xl overflow-hidden cursor-pointer border-2 transition-all ${
                    isSelected
                      ? 'border-indigo-500 ring-2 ring-indigo-500/40'
                      : 'border-transparent hover:border-gray-600'
                  }`}
                >
                  {/* Thumbnail */}
                  <div className="aspect-[4/3] bg-gray-800">
                    {item.thumbnail_url ? (
                      <img
                        src={item.thumbnail_url}
                        alt={filename(item.file_path)}
                        className="w-full h-full object-cover"
                        loading="lazy"
                      />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-gray-600 text-xs">
                        No thumb
                      </div>
                    )}
                  </div>

                  {/* Selection indicator */}
                  <div
                    className={`absolute top-2 left-2 w-5 h-5 rounded-full border-2 flex items-center justify-center transition-colors ${
                      isSelected
                        ? 'bg-indigo-600 border-indigo-600'
                        : 'bg-black/50 border-gray-400'
                    }`}
                  >
                    {isSelected && <span className="text-white text-xs leading-none">✓</span>}
                  </div>

                  {/* Badges */}
                  <div className="absolute top-2 right-2 flex flex-col gap-1 items-end">
                    {item.is_blurry === 1 && (
                      <span className="bg-orange-900/90 text-orange-300 text-[10px] px-1.5 py-0.5 rounded font-medium">
                        {item.long_exposure ? '🌙 Exposure' : '🔍 Blurry'}
                      </span>
                    )}
                    {item.has_closed_eyes === 1 && (
                      <span className="bg-purple-900/90 text-purple-300 text-[10px] px-1.5 py-0.5 rounded font-medium">
                        😑 Eyes
                      </span>
                    )}
                  </div>

                  {/* Footer */}
                  <div className="bg-black/70 px-2 py-1.5">
                    <p className="text-[11px] text-gray-300 truncate">{filename(item.file_path)}</p>
                    {item.blur_score != null && (
                      <p className="text-[10px] text-gray-500">
                        Sharpness: {item.blur_score.toFixed(1)}
                      </p>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Confirm delete modal */}
      {showConfirm && (
        <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-sm w-full shadow-2xl mx-4">
            <h2 className="text-white font-semibold text-lg mb-2">Delete {selected.size} photo{selected.size > 1 ? 's' : ''}?</h2>
            <p className="text-gray-400 text-sm mb-6">
              This permanently removes the selected files from VIP. Original files on disk are <strong className="text-white">not</strong> deleted.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setShowConfirm(false)}
                disabled={deleting}
                className="px-5 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 font-medium disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="px-5 py-2 rounded-lg bg-red-700 hover:bg-red-600 text-sm text-white font-semibold disabled:opacity-40"
              >
                {deleting ? 'Deleting…' : 'Yes, delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
