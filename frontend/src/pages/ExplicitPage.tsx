/**
 * ExplicitPage — shows photos flagged by NudeNet as containing explicit content.
 *
 * Displays a warning gate first, then a tag-tile grid (one tile per detected
 * label category). Clicking a tile filters photos to that specific label.
 */

import { useEffect, useState, useCallback } from 'react'
import { api, type TopTag } from '../api/client'

interface Props {
  onSelectLabel: (label: string) => void
}

// Labels that are considered "explicit" (EXPOSED) vs "borderline" (COVERED).
// Used only for colouring — all are stored under category='explicit'.
const EXPOSED_LABELS = new Set([
  'FEMALE_GENITALIA_EXPOSED',
  'MALE_GENITALIA_EXPOSED',
  'ANUS_EXPOSED',
  'FEMALE_BREAST_EXPOSED',
  'BUTTOCKS_EXPOSED',
])

function formatLabel(raw: string): string {
  return raw.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, c => c.toUpperCase())
}

export default function ExplicitPage({ onSelectLabel }: Props) {
  const [confirmed, setConfirmed] = useState(false)
  const [tags,      setTags]      = useState<TopTag[]>([])
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.tags.top('explicit', 100)
      setTags(data)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (confirmed) load()
  }, [confirmed, load])

  // ── Warning gate ─────────────────────────────────────────────────────────
  if (!confirmed) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-6 py-20">
        <div className="text-center space-y-3 max-w-sm">
          <p className="text-5xl">🔞</p>
          <h2 className="text-xl font-bold text-white">Explicit Content</h2>
          <p className="text-gray-400 text-sm">
            This section shows photos flagged by NudeNet as containing explicit or
            adult body-part detections. Photos are detected locally — nothing leaves
            your device.
          </p>
          <p className="text-gray-500 text-xs">
            If NudeNet has not run yet, re-run the pipeline with "Force re-tag" enabled.
          </p>
        </div>
        <button
          onClick={() => setConfirmed(true)}
          className="px-6 py-2 rounded-lg bg-red-700 hover:bg-red-600 text-white font-medium transition-colors"
        >
          Show explicit content
        </button>
      </div>
    )
  }

  // ── Content ───────────────────────────────────────────────────────────────
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-2xl font-bold text-white">🔞 Explicit</h2>
        <p className="text-xs text-gray-500 mt-1">
          Detected by NudeNet. Click a label to see matching photos.
        </p>
      </div>

      {error && (
        <div className="bg-red-900/40 border border-red-700 rounded p-3 text-red-300 text-sm">{error}</div>
      )}

      {loading && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-24 bg-gray-800 rounded-xl animate-pulse" />
          ))}
        </div>
      )}

      {!loading && tags.length === 0 && !error && (
        <div className="flex flex-col items-center justify-center py-20 gap-3 text-gray-500">
          <span className="text-5xl">🔞</span>
          <p className="text-sm">No explicit content detected yet.</p>
          <p className="text-xs text-gray-600">
            Run the pipeline (with Force re-tag if photos were already scanned) to
            detect explicit content via NudeNet.
          </p>
        </div>
      )}

      {!loading && tags.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-3">
          {tags.map(tag => {
            const isExposed = EXPOSED_LABELS.has(tag.label)
            return (
              <button
                key={tag.label}
                onClick={() => onSelectLabel(tag.label)}
                className={`
                  text-left p-4 rounded-xl border bg-gray-900 transition-colors
                  ${isExposed
                    ? 'border-red-700 hover:border-red-500'
                    : 'border-amber-700/60 hover:border-amber-500'
                  }
                `}
              >
                <p className={`text-sm font-medium leading-snug ${isExposed ? 'text-red-300' : 'text-amber-300'}`}>
                  {formatLabel(tag.label)}
                </p>
                <p className="text-xs text-gray-500 mt-1">{tag.count} photo{tag.count !== 1 ? 's' : ''}</p>
                {!isExposed && (
                  <p className="text-[10px] text-amber-700 mt-1">covered/partial</p>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
