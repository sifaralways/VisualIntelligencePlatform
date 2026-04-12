/**
 * PhotoDetail — modal showing full thumbnail + metadata + faces + ML tags.
 * Opens when a photo is clicked in PhotoGrid.
 * Tabs: Details (faces + quick tag chips) | Analysis (editable analysis document)
 */

import { useEffect, useRef, useState } from 'react'
import { api, type TagsByCategory, type FaceRow, type Person } from '../api/client'
import AnalysisPanel from './AnalysisPanel'
import ConnectionsGraph from './ConnectionsGraph'

type Tab = 'details' | 'analysis'

// What edit action is in progress for a given face
type EditMode =
  | { type: 'naming';  faceId: number }   // naming an unnamed face
  | { type: 'renaming'; faceId: number }  // renaming a named face
  | null

interface Props {
  mediaId: number
  filePath: string
  onClose: () => void
}

export default function PhotoDetail({ mediaId, filePath, onClose }: Props) {
  const [tags,    setTags]    = useState<TagsByCategory | null>(null)
  const [faces,   setFaces]   = useState<FaceRow[]>([])
  const [persons, setPersons] = useState<Person[]>([])
  const [loading, setLoading] = useState(true)
  const [tab,     setTab]     = useState<Tab>('details')
  const [reprocessing, setReprocessing] = useState(false)
  const [reprocessDone, setReprocessDone] = useState(false)

  // Face edit state
  const [editMode,    setEditMode]    = useState<EditMode>(null)
  const [nameInput,   setNameInput]   = useState('')
  const [savingFace,  setSavingFace]  = useState<number | null>(null) // face id being saved
  const [removingFace, setRemovingFace] = useState<number | null>(null)
  const [showSuggestions, setShowSuggestions] = useState(false)
  const nameInputRef = useRef<HTMLInputElement>(null)

  // Connections graph
  const [connectionsPid,  setConnectionsPid]  = useState<number | null>(null)
  const [connectionsName, setConnectionsName] = useState('')

  const filename = filePath.split('/').pop() ?? ''
  const thumbSrc = api.media.thumbnailUrl(mediaId)

  const loadFaces = () =>
    api.faces.byMedia(mediaId).catch(() => [] as FaceRow[])

  useEffect(() => {
    setLoading(true)
    Promise.all([
      api.media.tags(mediaId).catch(() => ({} as TagsByCategory)),
      loadFaces(),
      api.persons.list().catch(() => [] as Person[]),
    ]).then(([t, f, p]) => {
      setTags(t)
      setFaces(f)
      setPersons(p)
      setLoading(false)
    })
  }, [mediaId]) // eslint-disable-line react-hooks/exhaustive-deps

  async function reprocessPhoto() {
    setReprocessing(true)
    setReprocessDone(false)
    try {
      await api.pipeline.reprocessPhoto(mediaId)
      setReprocessDone(true)
    } finally {
      setReprocessing(false)
    }
  }

  function startNaming(faceId: number) {
    setEditMode({ type: 'naming', faceId })
    setNameInput('')
    setShowSuggestions(false)
    setTimeout(() => nameInputRef.current?.focus(), 50)
  }

  function startRenaming(face: FaceRow) {
    setEditMode({ type: 'renaming', faceId: face.id })
    setNameInput(face.person_name ?? '')
    setShowSuggestions(false)
    setTimeout(() => nameInputRef.current?.focus(), 50)
  }

  function cancelEdit() {
    setEditMode(null)
    setNameInput('')
    setShowSuggestions(false)
  }

  async function saveName(face: FaceRow) {
    const name = nameInput.trim()
    if (!name) return
    setSavingFace(face.id)
    try {
      if (editMode?.type === 'renaming' && face.person_id != null) {
        // Rename the person record
        await api.persons.namePerson(face.person_id, name)
      } else if (editMode?.type === 'naming' && face.cluster_id != null) {
        // Assign to existing person if name matches, else create new
        const match = persons.find(p => p.name?.toLowerCase() === name.toLowerCase())
        if (match) {
          await api.persons.addCluster(match.id, face.cluster_id)
        } else {
          await api.persons.fromCluster(face.cluster_id, name)
        }
      } else if (editMode?.type === 'naming' && face.cluster_id == null) {
        // Lone face (ejected from its previous person/cluster) — assign directly
        await api.persons.assignFace(face.id, name)
      }
      cancelEdit()
      const refreshed = await loadFaces()
      setFaces(refreshed)
      // Refresh persons list so autocomplete stays current
      api.persons.list().then(setPersons).catch(() => {})
    } finally {
      setSavingFace(null)
    }
  }

  async function removeName(face: FaceRow) {
    setRemovingFace(face.id)
    try {
      await api.faces.removeFromPerson(face.id)
      const refreshed = await loadFaces()
      setFaces(refreshed)
    } finally {
      setRemovingFace(null)
    }
  }

  // Close on Escape — but only if no edit is in progress
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (editMode) { cancelEdit(); return }
        onClose()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, editMode]) // eslint-disable-line react-hooks/exhaustive-deps

  // Autocomplete: persons whose name includes the current input
  const nameSuggestions = nameInput.trim().length > 0
    ? persons.filter(p => p.name && p.name.toLowerCase().includes(nameInput.toLowerCase()))
    : []

  return (
    <>
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
        <div className="w-full md:w-80 flex-shrink-0 flex flex-col overflow-hidden">

          {/* Header */}
          <div className="flex items-start justify-between gap-2 p-5 pb-3">
            <p className="text-sm font-medium text-white truncate">{filename}</p>
            <button
              onClick={onClose}
              className="shrink-0 text-gray-400 hover:text-white text-lg leading-none"
            >
              ✕
            </button>
          </div>

          {/* Tab bar */}
          <div className="flex border-b border-gray-700 px-5">
            {(['details', 'analysis'] as Tab[]).map(t => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`pb-2 mr-4 text-xs font-medium capitalize border-b-2 transition-colors ${
                  tab === t
                    ? 'border-blue-500 text-white'
                    : 'border-transparent text-gray-500 hover:text-gray-300'
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-y-auto p-5 space-y-5">

            {tab === 'details' && (
              <>
                {loading && (
                  <div className="text-gray-500 text-sm text-center py-6">Loading details…</div>
                )}

                {!loading && (
                  <>
                    {/* ── People in this photo ── */}
                    {faces.length > 0 && (
                      <Section title="People" icon="👤">
                        <div className="flex flex-wrap gap-3">
                          {faces.map(f => {
                            const isEditing = editMode?.faceId === f.id
                            const isSaving  = savingFace === f.id
                            const isRemoving = removingFace === f.id
                            const named = f.person_name != null

                            return (
                              <div key={f.id} className="flex flex-col items-center gap-1 group/face relative">
                                {/* Face thumbnail */}
                                <div className="relative">
                                  <div
                                    className={`w-16 h-16 rounded-xl overflow-hidden bg-gray-800 border transition-colors ${
                                      named ? 'border-indigo-700' : 'border-gray-700'
                                    } ${isRemoving ? 'opacity-40' : ''}`}
                                  >
                                    {f.thumbnail_path ? (
                                      <img
                                        src={api.faces.thumbnailUrl(f.id)}
                                        alt={f.person_name ?? 'Unknown'}
                                        className="w-full h-full object-cover"
                                      />
                                    ) : (
                                      <span className="flex items-center justify-center h-full text-2xl">👤</span>
                                    )}
                                  </div>

                                  {/* Action buttons — shown on hover when not editing */}
                                  {!isEditing && !isSaving && !isRemoving && (
                                    <div className="absolute -top-1 -right-1 flex gap-0.5 opacity-0 group-hover/face:opacity-100 transition-opacity">
                                      {/* Edit / Name button */}
                                      <button
                                        onClick={() => named ? startRenaming(f) : startNaming(f.id)}
                                        title={named ? 'Rename person' : 'Name this face'}
                                        className="w-5 h-5 rounded-full bg-gray-700 hover:bg-indigo-600 border border-gray-600 text-xs flex items-center justify-center text-gray-300 hover:text-white transition-colors leading-none"
                                      >
                                        ✎
                                      </button>
                                      {/* Remove button — only for named faces */}
                                      {named && (
                                        <button
                                          onClick={() => removeName(f)}
                                          title="Remove person assignment"
                                          className="w-5 h-5 rounded-full bg-gray-700 hover:bg-red-700 border border-gray-600 text-xs flex items-center justify-center text-gray-300 hover:text-white transition-colors leading-none"
                                        >
                                          ✕
                                        </button>
                                      )}
                                    </div>
                                  )}

                                  {/* Saving / removing spinner overlay */}
                                  {(isSaving || isRemoving) && (
                                    <div className="absolute inset-0 flex items-center justify-center bg-black/50 rounded-xl">
                                      <span className="text-white text-xs animate-pulse">…</span>
                                    </div>
                                  )}
                                </div>

                                {/* Name label or inline edit */}
                                {isEditing ? (
                                  <div className="w-20 flex flex-col gap-1 relative">
                                    <input
                                      ref={nameInputRef}
                                      value={nameInput}
                                      onChange={e => { setNameInput(e.target.value); setShowSuggestions(true) }}
                                      onFocus={() => setShowSuggestions(true)}
                                      onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
                                      onKeyDown={e => {
                                        if (e.key === 'Enter') saveName(f)
                                        if (e.key === 'Escape') cancelEdit()
                                      }}
                                      placeholder="Name…"
                                      className="w-full bg-gray-800 border border-indigo-500 rounded px-1.5 py-0.5 text-[10px] text-white outline-none text-center"
                                    />
                                    {/* Autocomplete dropdown */}
                                    {showSuggestions && nameSuggestions.length > 0 && (
                                      <ul className="absolute top-full left-0 right-0 mt-0.5 bg-gray-900 border border-gray-700 rounded shadow-xl z-50 max-h-32 overflow-y-auto">
                                        {nameSuggestions.map(p => (
                                          <li
                                            key={p.id}
                                            onMouseDown={e => e.preventDefault()}
                                            onClick={() => { setNameInput(p.name!); setShowSuggestions(false) }}
                                            className="px-2 py-1 text-[10px] text-gray-200 hover:bg-indigo-700 hover:text-white cursor-pointer truncate"
                                          >
                                            {p.name}
                                          </li>
                                        ))}
                                      </ul>
                                    )}
                                    <div className="flex gap-1">
                                      <button
                                        onMouseDown={e => e.preventDefault()}
                                        onClick={() => saveName(f)}
                                        disabled={!nameInput.trim() || isSaving}
                                        className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded px-1 py-0.5 text-[9px] transition-colors"
                                      >
                                        Save
                                      </button>
                                      <button
                                        onMouseDown={e => e.preventDefault()}
                                        onClick={cancelEdit}
                                        className="flex-1 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded px-1 py-0.5 text-[9px] transition-colors"
                                      >
                                        ✕
                                      </button>
                                    </div>
                                  </div>
                                ) : (
                                  <>
                                    <span
                                      className={`text-[10px] text-center max-w-[4rem] truncate ${
                                        named ? 'text-gray-200' : 'text-gray-500 italic'
                                      }`}
                                      title={f.person_name ?? undefined}
                                    >
                                      {named ? f.person_name : (f.cluster_id != null ? 'tap ✎ to name' : '?')}
                                    </span>
                                    {f.sharpness != null && (
                                      <span
                                        title={`Face sharpness: ${f.sharpness} / 100`}
                                        className={`text-[9px] tabular-nums px-1.5 py-px rounded-full border ${
                                          f.sharpness >= 50
                                            ? 'bg-emerald-900/40 text-emerald-400 border-emerald-700/40'
                                            : f.sharpness >= 20
                                              ? 'bg-amber-900/40 text-amber-400 border-amber-700/40'
                                              : 'bg-red-900/40 text-red-400 border-red-700/40'
                                        }`}
                                      >
                                        ◎ {f.sharpness}
                                      </span>
                                    )}
                                    {named && f.person_id != null && (
                                      <button
                                        onClick={() => { setConnectionsPid(f.person_id!); setConnectionsName(f.person_name ?? '') }}
                                        title="Show connections graph"
                                        className="text-[9px] text-gray-600 hover:text-purple-400 transition-colors"
                                      >
                                        connections
                                      </button>
                                    )}
                                  </>
                                )}
                              </div>
                            )
                          })}
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

                    {/* Reprocess */}
                    <div className="pt-2 border-t border-gray-800">
                      <button
                        onClick={reprocessPhoto}
                        disabled={reprocessing}
                        className="w-full text-xs px-3 py-2 rounded-lg border border-gray-700 bg-gray-800 text-gray-400 hover:text-white hover:border-indigo-500 hover:bg-indigo-900/20 transition-colors disabled:opacity-40"
                      >
                        {reprocessing ? '⟳ Queued — re-detecting faces…' : reprocessDone ? '✓ Queued — check People page shortly' : '⟳ Reprocess this photo'}
                      </button>
                      {reprocessDone && (
                        <p className="text-xs text-gray-600 text-center mt-1">
                          Face detection is running in the background.
                        </p>
                      )}
                    </div>
                  </>
                )}
              </>
            )}

            {tab === 'analysis' && (
              <AnalysisPanel mediaId={mediaId} />
            )}

          </div>
        </div>
      </div>
    </div>

    {connectionsPid !== null && (
      <ConnectionsGraph
        personId={connectionsPid}
        personName={connectionsName}
        onClose={() => setConnectionsPid(null)}
      />
    )}
    </>
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
