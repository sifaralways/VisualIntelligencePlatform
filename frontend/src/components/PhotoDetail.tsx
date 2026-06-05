/**
 * PhotoDetail — modal showing full thumbnail + metadata + faces + ML tags.
 * Opens when a photo is clicked in PhotoGrid.
 * Tabs: Details (faces + quick tag chips) | Analysis (editable analysis document)
 */

import { useEffect, useRef, useState } from 'react'
import { api, type TagsByCategory, type FaceRow, type Person, type IgnoreSuggestion } from '../api/client'
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
  canGoPrev?: boolean
  canGoNext?: boolean
  onNavigate?: (delta: number) => void
  onClose: () => void
  onTagRemoved?: () => void
}

interface IgnoreQueueItem extends IgnoreSuggestion {
  person_id: number
  source_cluster_id: number
  source_thumbnail: string | null
}

interface IgnoredSource {
  person_id: number
  source_cluster_id: number
  source_thumbnail: string | null
}

function toThumbUrl(path: string | null | undefined): string | null {
  if (!path) return null
  const rel = path.split('/thumbnails/').pop()
  return rel ? `/thumbnails/${rel}` : null
}

export default function PhotoDetail({
  mediaId,
  filePath,
  canGoPrev = false,
  canGoNext = false,
  onNavigate,
  onClose,
  onTagRemoved,
}: Props) {
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

  // Ignore-all unnamed faces flow
  const [ignoreAllOfferOpen, setIgnoreAllOfferOpen] = useState(false)
  const [ignoreAllWorking, setIgnoreAllWorking] = useState(false)
  const [ignoreAllSuggestThreshold, setIgnoreAllSuggestThreshold] = useState(0.85)
  const [ignoreAllResult, setIgnoreAllResult] = useState<{ ignoredCount: number; autoIgnored: number; suggestionsFound: number } | null>(null)
  const [ignoredSources, setIgnoredSources] = useState<IgnoredSource[]>([])
  const [ignoreSuggestionQueue, setIgnoreSuggestionQueue] = useState<IgnoreQueueItem[]>([])
  const [ignoreSuggestionBusy, setIgnoreSuggestionBusy] = useState(false)

  // Connections graph
  const [connectionsPid,  setConnectionsPid]  = useState<number | null>(null)
  const [connectionsName, setConnectionsName] = useState('')

  // Show ignored faces toggle
  const [showIgnoredFaces, setShowIgnoredFaces] = useState(false)

  const filename = filePath.split('/').pop() ?? ''
  const thumbSrc = api.media.thumbnailUrl(mediaId)

  const loadFaces = (withIgnored = showIgnoredFaces) =>
    api.faces.byMedia(mediaId, withIgnored).catch(() => [] as FaceRow[])

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

  const unnamedClusterIds = Array.from(new Set(
    faces
      .filter(f => f.person_id == null && f.cluster_id != null)
      .map(f => f.cluster_id as number),
  ))
  const hasUnnamedClusters = unnamedClusterIds.length > 0

  async function ignoreAllUnnamedOnPhoto() {
    if (!hasUnnamedClusters) return
    setIgnoreAllWorking(true)
    setIgnoreAllResult(null)
    try {
      const sourceThumbByCluster = new Map<number, string | null>()
      for (const f of faces) {
        if (f.person_id == null && f.cluster_id != null && !sourceThumbByCluster.has(f.cluster_id)) {
          sourceThumbByCluster.set(f.cluster_id, toThumbUrl(f.thumbnail_path))
        }
      }

      const sources: IgnoredSource[] = []

      for (const clusterId of unnamedClusterIds) {
        const result = await api.clusters.ignore(clusterId)
        sources.push({
          person_id: result.person_id,
          source_cluster_id: clusterId,
          source_thumbnail: sourceThumbByCluster.get(clusterId) ?? null,
        })
      }

      setIgnoreAllResult({
        ignoredCount: unnamedClusterIds.length,
        autoIgnored: 0,
        suggestionsFound: 0,
      })
      setIgnoredSources(sources)
      setIgnoreAllOfferOpen(true)

      // Refresh people section state on this photo after ignore mutations.
      const refreshed = await loadFaces()
      setFaces(refreshed)
    } finally {
      setIgnoreAllWorking(false)
    }
  }

  async function runPostIgnoreSuggestions() {
    if (ignoredSources.length === 0) {
      setIgnoreAllOfferOpen(false)
      return
    }

    setIgnoreAllWorking(true)
    try {
      let autoIgnored = 0
      const queue: IgnoreQueueItem[] = []

      for (const source of ignoredSources) {
        const result = await api.persons.ignoredSuggestions(
          source.person_id,
          ignoreAllSuggestThreshold,
          8,
        )
        autoIgnored += result.auto_ignored.length
        for (const s of result.suggestions) {
          queue.push({
            ...s,
            person_id: source.person_id,
            source_cluster_id: source.source_cluster_id,
            source_thumbnail: source.source_thumbnail,
          })
        }
      }

      setIgnoreAllResult(prev => ({
        ignoredCount: prev?.ignoredCount ?? ignoredSources.length,
        autoIgnored,
        suggestionsFound: queue.length,
      }))
      setIgnoreSuggestionQueue(queue)
      setIgnoreAllOfferOpen(false)

      const refreshed = await loadFaces()
      setFaces(refreshed)
    } finally {
      setIgnoreAllWorking(false)
    }
  }

  async function acceptIgnoreSuggestion() {
    const current = ignoreSuggestionQueue[0]
    if (!current) return
    setIgnoreSuggestionBusy(true)
    try {
      await api.persons.addIgnoredCluster(current.person_id, current.cluster_id)
    } catch {
      // Keep UX moving even if the cluster was already handled in another path.
    } finally {
      setIgnoreSuggestionQueue(prev => prev.slice(1))
      const refreshed = await loadFaces()
      setFaces(refreshed)
      setIgnoreSuggestionBusy(false)
    }
  }

  function rejectIgnoreSuggestion() {
    setIgnoreSuggestionQueue(prev => prev.slice(1))
  }

  async function toggleIgnoredFaces() {
    const next = !showIgnoredFaces
    setShowIgnoredFaces(next)
    const refreshed = await loadFaces(next)
    setFaces(refreshed)
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

  async function removeTag(category: string, label: string) {
    await api.tags.remove(mediaId, category, label)
    setTags(prev => {
      if (!prev) return prev
      const updated = { ...prev }
      const arr = updated[category as keyof typeof updated] as string[] | undefined
      if (arr) {
        const next = arr.filter(l => l !== label)
        if (next.length === 0) {
          delete updated[category as keyof typeof updated]
        } else {
          (updated as Record<string, string[]>)[category] = next
        }
      }
      return updated
    })
    onTagRemoved?.()
  }

  // Close on Escape; Arrow keys navigate photos when not typing in an input.
  useEffect(() => {
    const isTypingTarget = (target: EventTarget | null): boolean => {
      const el = target as HTMLElement | null
      if (!el) return false
      const tag = el.tagName
      return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable
    }

    const handler = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target)) return

      if (e.key === 'Escape') {
        if (editMode) { cancelEdit(); return }
        onClose()
        return
      }

      if (!onNavigate) return

      if (e.key === 'ArrowLeft') {
        if (!canGoPrev) return
        e.preventDefault()
        onNavigate(-1)
        return
      }

      if (e.key === 'ArrowRight') {
        if (!canGoNext) return
        e.preventDefault()
        onNavigate(1)
        return
      }

      if (e.key === 'ArrowUp') {
        if (!canGoPrev) return
        e.preventDefault()
        onNavigate(-10)
        return
      }

      if (e.key === 'ArrowDown') {
        if (!canGoNext) return
        e.preventDefault()
        onNavigate(10)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, editMode, onNavigate, canGoPrev, canGoNext])

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
            <div className="min-w-0">
              <p className="text-sm font-medium text-white truncate">{filename}</p>
              <p className="mt-1 text-[11px] text-gray-500 break-all leading-snug">{filePath}</p>
            </div>
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
                    {(faces.length > 0 || showIgnoredFaces) && (
                      <div>
                        <div className="flex items-center justify-between mb-2">
                          <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest">👤 People</p>
                          <button
                            onClick={toggleIgnoredFaces}
                            title={showIgnoredFaces ? 'Hide ignored faces' : 'Show ignored faces'}
                            className={`text-[10px] px-2 py-0.5 rounded-full border transition-colors ${
                              showIgnoredFaces
                                ? 'border-amber-600 bg-amber-900/30 text-amber-300 hover:bg-amber-800/40'
                                : 'border-gray-700 bg-gray-800/50 text-gray-500 hover:text-gray-300 hover:border-gray-600'
                            }`}
                          >
                            {showIgnoredFaces ? '🙈 Hide ignored' : '👁 Show ignored'}
                          </button>
                        </div>
                        <div className="flex flex-wrap gap-3">
                          {faces.map(f => {
                            const isEditing = editMode?.faceId === f.id
                            const isSaving  = savingFace === f.id
                            const isRemoving = removingFace === f.id
                            const named = f.person_name != null
                            const isIgnored = f.is_ignored === true

                            return (
                              <div key={f.id} className="flex flex-col items-center gap-1 group/face relative">
                                {/* Face thumbnail */}
                                <div className="relative">
                                  <div
                                    className={`w-16 h-16 rounded-xl overflow-hidden bg-gray-800 border transition-colors ${
                                      isIgnored
                                        ? 'border-amber-600/60'
                                        : named ? 'border-indigo-700' : 'border-gray-700'
                                    } ${isRemoving ? 'opacity-40' : ''}`}
                                  >
                                    {f.thumbnail_path ? (
                                      <img
                                        src={api.faces.thumbnailUrl(f.id)}
                                        alt={f.person_name ?? 'Unknown'}
                                        className={`w-full h-full object-cover ${isIgnored ? 'opacity-50 grayscale' : ''}`}
                                      />
                                    ) : (
                                      <span className="flex items-center justify-center h-full text-2xl">👤</span>
                                    )}
                                  </div>
                                  {isIgnored && (
                                    <div className="absolute inset-0 flex items-end justify-center pb-1 pointer-events-none">
                                      <span className="text-[8px] bg-amber-900/80 text-amber-300 border border-amber-700/60 rounded px-1 py-px leading-none">ignored</span>
                                    </div>
                                  )}

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
                                        isIgnored
                                          ? 'text-amber-500 italic'
                                          : named ? 'text-gray-200' : 'text-gray-500 italic'
                                      }`}
                                      title={isIgnored ? 'Ignored face — name it to un-ignore' : (f.person_name ?? undefined)}
                                    >
                                      {named ? f.person_name : (isIgnored ? 'tap ✎ to name' : (f.cluster_id != null ? 'tap ✎ to name' : '?'))}
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

                        <div className="mt-3 pt-3 border-t border-gray-800">
                          <button
                            onClick={ignoreAllUnnamedOnPhoto}
                            disabled={!hasUnnamedClusters || ignoreAllWorking}
                            className="w-full text-xs px-3 py-2 rounded-lg border border-red-800 bg-red-950/30 text-red-300 hover:text-red-100 hover:border-red-600 hover:bg-red-900/30 transition-colors disabled:opacity-40"
                            title={hasUnnamedClusters ? 'Ignore all unnamed faces in this photo' : 'No unnamed faces in this photo'}
                          >
                            {ignoreAllWorking
                              ? 'Ignoring unnamed faces…'
                              : hasUnnamedClusters
                                ? `Ignore all unnamed faces (${unnamedClusterIds.length})`
                                : 'No unnamed faces to ignore'}
                          </button>
                        </div>
                      </div>
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
                        {tags.caption && tags.caption.length > 0 && (
                          <Section title="Description" icon="📝">
                            <div className="space-y-2">
                              {tags.caption.map((text, idx) => (
                                <p
                                  key={`${text}-${idx}`}
                                  className="text-xs leading-5 text-gray-200 bg-slate-800/70 border border-slate-700 rounded-lg px-3 py-2"
                                >
                                  {text}
                                </p>
                              ))}
                            </div>
                          </Section>
                        )}
                        {tags.ocr && tags.ocr.length > 0 && (
                          <Section title="Text in Image" icon="🔤">
                            <TagChips labels={tags.ocr} colour="bg-cyan-900/50 text-cyan-200" />
                          </Section>
                        )}
                        {tags.region && tags.region.length > 0 && (
                          <Section title="Other Observations" icon="🔎">
                            <div className="space-y-2">
                              {tags.region.map((text, idx) => (
                                <p
                                  key={`${text}-${idx}`}
                                  className="text-xs leading-5 text-gray-200 bg-gray-800/70 border border-gray-700 rounded-lg px-3 py-2"
                                >
                                  {text}
                                </p>
                              ))}
                            </div>
                          </Section>
                        )}
                        {tags.explicit && tags.explicit.length > 0 && (
                          <Section title="Explicit Content" icon="🔞">
                            <TagChips
                              labels={tags.explicit}
                              colour="bg-red-900/60 text-red-300"
                              onRemove={label => removeTag('explicit', label)}
                            />
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
                        {reprocessing ? '⟳ Queued — re-running models…' : reprocessDone ? '✓ Queued — check progress in Pipeline' : '⟳ Reprocess this photo'}
                      </button>
                      {reprocessDone && (
                        <p className="text-xs text-gray-600 text-center mt-1">
                          Full model reprocess is running in the background.
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

    {ignoreAllOfferOpen && (
      <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
        <div className="bg-gray-900 border border-gray-700 rounded-2xl p-6 max-w-md w-full shadow-2xl">
          <p className="text-white font-semibold text-base mb-1">Find similar faces too?</p>
          <p className="text-gray-400 text-sm mb-4">
            VIP has already ignored all unnamed face clusters in this photo.
            Do you also want similar-face suggestions for additional ignores?
          </p>

          <div className="mb-5">
            <label className="flex items-center justify-between text-xs text-gray-300 mb-2">
              <span>Auto-ignore threshold</span>
              <span className="font-semibold text-white">{Math.round(ignoreAllSuggestThreshold * 100)}%</span>
            </label>
            <input
              type="range"
              min={50}
              max={95}
              step={1}
              value={Math.round(ignoreAllSuggestThreshold * 100)}
              onChange={e => setIgnoreAllSuggestThreshold(Number(e.target.value) / 100)}
              className="w-full accent-red-500"
            />
            <p className="text-[11px] text-gray-500 mt-1">
              Applied only if you choose suggestions.
            </p>
          </div>

          <div className="flex gap-3">
            <button
              onClick={runPostIgnoreSuggestions}
              disabled={ignoreAllWorking}
              className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
            >
              {ignoreAllWorking ? 'Running…' : 'Yes, show suggestions'}
            </button>
            <button
              onClick={() => setIgnoreAllOfferOpen(false)}
              disabled={ignoreAllWorking}
              className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
            >
              No
            </button>
          </div>

          <button
            onClick={() => setIgnoreAllOfferOpen(false)}
            disabled={ignoreAllWorking}
            className="mt-3 w-full text-center text-xs text-gray-600 hover:text-gray-400 disabled:opacity-40"
          >
            Cancel
          </button>
        </div>
      </div>
    )}

    {ignoreSuggestionQueue.length > 0 && (
      <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
        <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl">
          <p className="text-white font-semibold text-lg mb-1">Ignore this similar face too?</p>
          <p className="text-gray-400 text-xs mb-5">
            Similarity {Math.round(ignoreSuggestionQueue[0].similarity * 100)}% — {ignoreSuggestionQueue.length} suggestion{ignoreSuggestionQueue.length !== 1 ? 's' : ''} remaining.
          </p>
          <div className="flex gap-6 items-center justify-center mb-6">
            <div className="flex flex-col items-center gap-1">
              <div className="w-24 h-24 rounded-xl overflow-hidden bg-gray-800 border border-red-700">
                {ignoreSuggestionQueue[0].source_thumbnail
                  ? <img src={ignoreSuggestionQueue[0].source_thumbnail} alt="source face" className="w-full h-full object-cover" />
                  : <span className="flex items-center justify-center h-full text-gray-500 text-2xl">?</span>}
              </div>
              <span className="text-xs text-red-300 font-medium">Source face</span>
            </div>
            <span className="text-gray-500 text-2xl">≈</span>
            <div className="flex flex-col items-center gap-1">
              <div className="w-24 h-24 rounded-xl overflow-hidden bg-gray-800 border border-gray-600">
                {ignoreSuggestionQueue[0].representative_thumbnail
                  ? <img src={'/thumbnails/' + ignoreSuggestionQueue[0].representative_thumbnail.split('/thumbnails/').pop()} alt="suggested face" className="w-full h-full object-cover" />
                  : <span className="flex items-center justify-center h-full text-gray-500 text-2xl">?</span>}
              </div>
              <span className="text-xs text-gray-500">{ignoreSuggestionQueue[0].member_count} face{ignoreSuggestionQueue[0].member_count !== 1 ? 's' : ''}</span>
            </div>
          </div>

          <div className="flex gap-3">
            <button
              onClick={acceptIgnoreSuggestion}
              disabled={ignoreSuggestionBusy}
              className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
            >
              {ignoreSuggestionBusy ? 'Ignoring…' : 'Yes, ignore'}
            </button>
            <button
              onClick={rejectIgnoreSuggestion}
              disabled={ignoreSuggestionBusy}
              className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
            >
              No, keep it
            </button>
          </div>

          <button
            onClick={() => setIgnoreSuggestionQueue([])}
            className="mt-3 w-full text-center text-xs text-gray-600 hover:text-gray-400"
          >
            Stop reviewing for now
          </button>
        </div>
      </div>
    )}

    {ignoreAllResult && ignoreSuggestionQueue.length === 0 && (
      <div className="fixed bottom-4 right-4 z-[60] max-w-sm rounded-xl border border-red-800 bg-red-950/40 px-4 py-3 shadow-2xl">
        <p className="text-sm text-red-200">
          Ignored {ignoreAllResult.ignoredCount} unnamed face cluster{ignoreAllResult.ignoredCount !== 1 ? 's' : ''}.
          {ignoreAllResult.autoIgnored > 0 ? ` Auto-ignored ${ignoreAllResult.autoIgnored} similar cluster${ignoreAllResult.autoIgnored !== 1 ? 's' : ''}.` : ''}
          {ignoreAllResult.suggestionsFound > 0 ? ` Reviewed ${ignoreAllResult.suggestionsFound} suggestion${ignoreAllResult.suggestionsFound !== 1 ? 's' : ''}.` : ''}
        </p>
        <button
          onClick={() => setIgnoreAllResult(null)}
          className="mt-2 text-xs text-red-400 hover:text-red-200"
        >
          Dismiss
        </button>
      </div>
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

function TagChips({ labels, colour, onRemove }: { labels: string[]; colour: string; onRemove?: (label: string) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {labels.map(l => (
        <span key={l} className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium ${colour}`}>
          {l}
          {onRemove && (
            <button
              onClick={() => onRemove(l)}
              title={`Remove tag "${l}"`}
              className="ml-0.5 opacity-60 hover:opacity-100 leading-none"
            >
              ✕
            </button>
          )}
        </span>
      ))}
    </div>
  )
}
