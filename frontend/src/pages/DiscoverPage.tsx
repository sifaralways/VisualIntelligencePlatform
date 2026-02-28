/**
 * DiscoverPage — shows ML tag tiles for a given category.
 *
 * Used for Animals, Places, and Things sections. Each tile shows a label
 * and photo count. Clicking a tile triggers onSelectTag so the parent
 * can open a filtered photo grid.
 */

import { useEffect, useState, useCallback } from 'react'
import { api, type TopTag } from '../api/client'

type TagCategory = 'animal' | 'place' | 'geography' | 'object'

interface Props {
  category: TagCategory
  /** Called when user clicks a tag tile */
  onSelectTag: (category: TagCategory, label: string, displayTitle: string) => void
}

const CATEGORY_META: Record<TagCategory, { icon: string; colour: string; gridColour: string; label: string }> = {
  animal:    { icon: '🐾', colour: 'text-green-400',  gridColour: 'border-green-700 hover:border-green-400',  label: 'Animals'   },
  place:     { icon: '📍', colour: 'text-pink-400',   gridColour: 'border-pink-700  hover:border-pink-400',   label: 'Places'    },
  geography: { icon: '🌍', colour: 'text-yellow-400', gridColour: 'border-yellow-700 hover:border-yellow-400',label: 'Geography' },
  object:    { icon: '📦', colour: 'text-blue-400',   gridColour: 'border-blue-700  hover:border-blue-400',   label: 'Things'    },
}

export default function DiscoverPage({ category, onSelectTag }: Props) {
  const [tags,    setTags]    = useState<TopTag[]>([])
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState<string | null>(null)

  const meta = CATEGORY_META[category]

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.tags.top(category, 200)
      setTags(data)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, [category])

  useEffect(() => { load() }, [load])

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-2xl font-bold text-white">
          {meta.icon} {meta.label}
        </h2>
        <p className="text-xs text-gray-500 mt-1">
          Identified by VIP's ML pipeline. Click any tile to see matching photos.
        </p>
      </div>

      {error && (
        <div className="bg-red-900/40 border border-red-700 rounded p-3 text-red-300 text-sm">{error}</div>
      )}

      {loading && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="h-24 bg-gray-800 rounded-xl animate-pulse" />
          ))}
        </div>
      )}

      {!loading && tags.length === 0 && !error && (
        <div className="flex flex-col items-center justify-center py-20 gap-3 text-gray-500">
          <span className="text-5xl">{meta.icon}</span>
          <p className="text-sm">
            No {meta.label.toLowerCase()} found yet. Run the full pipeline to generate
            ML tags.
          </p>
        </div>
      )}

      {!loading && tags.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3">
          {tags.map(tag => (
            <button
              key={tag.label}
              onClick={() =>
                onSelectTag(
                  category,
                  tag.label,
                  `${meta.icon} ${tag.label}`,
                )
              }
              className={`flex flex-col items-center justify-center gap-2 p-4 rounded-xl bg-gray-900 border-2 transition-all focus:outline-none focus:ring-2 focus:ring-indigo-400 ${meta.gridColour}`}
            >
              <span className="text-3xl">{meta.icon}</span>
              <span className="text-white text-sm font-medium text-center leading-tight">
                {tag.label}
              </span>
              <span className={`text-xs ${meta.colour}`}>
                {tag.count} photo{tag.count !== 1 ? 's' : ''}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
