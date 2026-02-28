/**
 * PhotoDetail — modal showing full thumbnail + metadata + faces + ML tags.
 * Opens when a photo is clicked in PhotoGrid.
 */

import { useEffect, useState } from 'react'
import { api, type TagsByCategory, type FaceRow } from '../api/client'

interface Props {
  mediaId: number
  filePath: string
  onClose: () => void
}

export default function PhotoDetail({ mediaId, filePath, onClose }: Props) {
  const [tags,    setTags]    = useState<TagsByCategory | null>(null)
  const [faces,   setFaces]   = useState<FaceRow[]>([])
  const [loading, setLoading] = useState(true)

  const filename = filePath.split('/').pop() ?? ''
  const thumbSrc = api.media.thumbnailUrl(mediaId)

  useEffect(() => {
    setLoading(true)
    Promise.all([
      api.media.tags(mediaId).catch(() => ({} as TagsByCategory)),
      api.faces.byMedia(mediaId).catch(() => [] as FaceRow[]),
    ]).then(([t, f]) => {
      setTags(t)
      setFaces(f)
      setLoading(false)
    })
  }, [mediaId])

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl flex flex-col md:flex-row max-w-5xl w-full max-h-[90vh] overflow-hidden">

        {/* ── Photo panel ── */}
        <div className="flex-1 bg-black flex items-center justify-center min-h-64">
          <img
            src={thumbSrc}
            alt={filename}
            className="max-w-full max-h-[80vh] object-contain"
          />
        </div>

        {/* ── Info panel ── */}
        <div className="w-full md:w-80 flex-shrink-0 overflow-y-auto p-5 space-y-5">
          {/* Header */}
          <div className="flex items-start justify-between gap-2">
            <p className="text-sm font-medium text-white truncate">{filename}</p>
            <button
              onClick={onClose}
              className="shrink-0 text-gray-400 hover:text-white text-lg leading-none"
            >
              ✕
            </button>
          </div>

          {loading && (
            <div className="text-gray-500 text-sm text-center py-6">Loading details…</div>
          )}

          {!loading && (
            <>
              {/* People in this photo */}
              {faces.length > 0 && (
                <Section title="People" icon="👤">
                  <div className="flex flex-wrap gap-2">
                    {faces.map(f => (
                      <div key={f.id} className="flex flex-col items-center gap-1">
                        <div className="w-12 h-12 rounded-full overflow-hidden bg-gray-800 border border-gray-700">
                          {f.thumbnail_path ? (
                            <img
                              src={api.faces.thumbnailUrl(f.id)}
                              alt={f.person_name ?? 'Unknown'}
                              className="w-full h-full object-cover"
                            />
                          ) : (
                            <span className="flex items-center justify-center h-full text-xl">👤</span>
                          )}
                        </div>
                        <span className="text-gray-300 text-[10px] text-center max-w-12 truncate">
                          {f.person_name ?? '?'}
                        </span>
                      </div>
                    ))}
                  </div>
                </Section>
              )}

              {/* ML Tags */}
              {tags && Object.keys(tags).length > 0 && (
                <>
                  {tags.object && tags.object.length > 0 && (
                    <Section title="Objects" icon="📦">
                      <TagChips labels={tags.object} colour="bg-blue-800/60 text-blue-200" />
                    </Section>
                  )}
                  {tags.animal && tags.animal.length > 0 && (
                    <Section title="Animals" icon="🐾">
                      <TagChips labels={tags.animal} colour="bg-green-800/60 text-green-200" />
                    </Section>
                  )}
                  {tags.geography && tags.geography.length > 0 && (
                    <Section title="Scene" icon="🌍">
                      <TagChips labels={tags.geography} colour="bg-yellow-800/60 text-yellow-200" />
                    </Section>
                  )}
                  {tags.place && tags.place.length > 0 && (
                    <Section title="Places" icon="📍">
                      <TagChips labels={tags.place} colour="bg-pink-800/60 text-pink-200" />
                    </Section>
                  )}
                </>
              )}

              {/* Nothing tagged yet */}
              {(!tags || Object.keys(tags).length === 0) && faces.length === 0 && (
                <p className="text-gray-600 text-sm text-center py-4">
                  No tags yet — run the full pipeline to generate them.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Section({ title, icon, children }: { title: string; icon: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-2">
        {icon} {title}
      </p>
      {children}
    </div>
  )
}

function TagChips({ labels, colour }: { labels: string[]; colour: string }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {labels.map(l => (
        <span key={l} className={`text-xs px-2 py-0.5 rounded-full font-medium ${colour}`}>
          {l}
        </span>
      ))}
    </div>
  )
}
