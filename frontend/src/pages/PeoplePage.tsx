/**
 * PeoplePage — face tile grid with naming UX.
 *
 * Shows one representative tile per person/cluster.
 * Named persons: click tile → see photos; click ≣ icon → face review.
 * Unnamed clusters: shown first, sorted by size.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Cluster, Person, MergeSuggestion, FaceRow, SimilarCluster } from '../api/client'

interface Props {
  /** Called when user clicks a named person tile to view their photos. */
  onSelectPerson?: (personId: number, name: string) => void
}

export default function PeoplePage({ onSelectPerson }: Props) {
  const [clusters, setClusters] = useState<Cluster[]>([])
  const [persons, setPersons] = useState<Person[]>([])
  const [loading, setLoading] = useState(true)
  const [namingId, setNamingId] = useState<number | null>(null)  // cluster id being named
  const [nameInput, setNameInput] = useState('')
  const [saving, setSaving] = useState(false)

  // ── Rename existing person ────────────────────────────────────────────────
  const [renamingPersonId, setRenamingPersonId] = useState<number | null>(null)
  const [renameInput, setRenameInput] = useState('')
  const [renameSaving, setRenameSaving] = useState(false)
  const [mergeCandidate, setMergeCandidate] = useState<{ personId: number; name: string } | null>(null)
  const [reviewPerson, setReviewPerson] = useState<Person | null>(null)
  const [reviewFaces, setReviewFaces] = useState<FaceRow[]>([])
  const [reviewLoading, setReviewLoading] = useState(false)

  // ── Proactive merge suggestions ─────────────────────────────────────────
  const [suggestion, setSuggestion] = useState<MergeSuggestion | null>(null)
  const [suggestionPersonId, setSuggestionPersonId] = useState<number | null>(null)
  const [suggestionPersonName, setSuggestionPersonName] = useState<string | null>(null)
  const [suggestionBusy, setSuggestionBusy] = useState(false)

  async function fetchNextSuggestion(personId: number) {
    try {
      const list = await api.persons.mergeSuggestions(personId)
      setSuggestion(list.length > 0 ? list[0] : null)
    } catch {
      setSuggestion(null)
    }
  }

  async function startSuggestions(personId: number, name: string) {
    setSuggestionPersonId(personId)
    setSuggestionPersonName(name)
    await fetchNextSuggestion(personId)
  }

  async function acceptSuggestion() {
    if (!suggestionPersonId || !suggestion) return
    setSuggestionBusy(true)
    try {
      await api.persons.addCluster(suggestionPersonId, suggestion.cluster_id)
      await load()
      await fetchNextSuggestion(suggestionPersonId)
    } finally { setSuggestionBusy(false) }
  }

  async function rejectSuggestion() {
    if (!suggestionPersonId || !suggestion) return
    setSuggestionBusy(true)
    try {
      await api.persons.rejectSuggestion(suggestionPersonId, suggestion.cluster_id)
      await fetchNextSuggestion(suggestionPersonId)
    } finally { setSuggestionBusy(false) }
  }

  function dismissSuggestions() {
    setSuggestion(null)
    setSuggestionPersonId(null)
    setSuggestionPersonName(null)
  }
  // ── Cluster dismiss (delete / always ignore) ──────────────────────────────
  const [dismissTarget, setDismissTarget] = useState<Cluster | null>(null)
  const [dismissWorking, setDismissWorking] = useState(false)
  const [similarClusters, setSimilarClusters] = useState<SimilarCluster[]>([])
  const [similarLoading, setSimilarLoading] = useState(false)

  async function openDismiss(cluster: Cluster) {
    setDismissTarget(cluster)
    setSimilarClusters([])
    setSimilarLoading(true)
    try {
      const results = await api.clusters.similar(cluster.id)
      // Only show clusters above a basic similarity threshold to avoid noise
      setSimilarClusters(results.filter(s => s.similarity >= 0.55))
    } catch {
      setSimilarClusters([])
    } finally {
      setSimilarLoading(false)
    }
  }

  async function handleDeleteCluster(clusterId: number) {
    setDismissWorking(true)
    try {
      await api.clusters.delete(clusterId)
      setClusters(prev => prev.filter(c => c.id !== clusterId))
      setDismissTarget(null)
      setSimilarClusters([])
    } finally { setDismissWorking(false) }
  }

  async function handleIgnoreCluster(clusterId: number) {
    setDismissWorking(true)
    try {
      await api.clusters.ignore(clusterId)
      setClusters(prev => prev.filter(c => c.id !== clusterId))
      setDismissTarget(null)
      setSimilarClusters([])
    } finally { setDismissWorking(false) }
  }

  async function load() {
    setLoading(true)
    try {
      const [c, p] = await Promise.all([api.clusters.unnamed(), api.persons.list()])
      setClusters(c)
      setPersons(p)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  async function openReview(person: Person) {
    setReviewPerson(person)
    setReviewFaces([])
    setReviewLoading(true)
    try {
      const faces = await api.faces.byPerson(person.id)
      setReviewFaces(faces)
    } finally {
      setReviewLoading(false)
    }
  }

  async function ejectFace(faceId: number) {
    await api.faces.removeFromPerson(faceId)
    setReviewFaces(f => f.filter(x => x.id !== faceId))
    load() // refresh counts
  }

  async function handleName(clusterId: number) {
    const name = nameInput.trim()
    if (!name) return
    const existing = persons.find(p => p.name?.toLowerCase() === name.toLowerCase())
    if (existing) {
      setMergeCandidate({ personId: existing.id, name: existing.name! })
      return
    }
    setSaving(true)
    try {
      await api.persons.fromCluster(clusterId, name)
      setNamingId(null)
      setNameInput('')
      const refreshed = await api.persons.list()
      setPersons(refreshed)
      // Kick off proactive suggestions for the newly named person
      const newPerson = refreshed.find(p => p.name?.toLowerCase() === name.toLowerCase())
      if (newPerson) startSuggestions(newPerson.id, newPerson.name!)
      load()
    } finally { setSaving(false) }
  }

  async function handleMerge(clusterId: number, personId: number) {
    setSaving(true)
    try {
      await api.persons.addCluster(personId, clusterId)
      setMergeCandidate(null)
      setNamingId(null)
      setNameInput('')
      load()
    } finally { setSaving(false) }
  }

  async function handleRenamePerson(personId: number) {
    const name = renameInput.trim()
    if (!name) return
    setRenameSaving(true)
    try {
      await api.persons.namePerson(personId, name)
      setRenamingPersonId(null)
      setRenameInput('')
      // Optimistically update the name; name_written resets to false since
      // writeback hasn't run yet for the new name.
      setPersons(prev => prev.map(p => p.id === personId ? { ...p, name, name_written: false } : p))
    } finally {
      setRenameSaving(false)
    }
  }

  if (loading) return <div className="text-gray-400 text-sm">Loading people…</div>

  return (
    <div>
      <h1 className="text-xl font-semibold mb-6">People</h1>

      {/* Suggestion modal — proactive same-person proposal */}
      {suggestion && suggestionPersonId !== null && (
        <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">
              Same person as <span className="text-indigo-400">{suggestionPersonName}</span>?
            </p>
            <p className="text-gray-400 text-xs mb-5">
              Similarity {Math.round(suggestion.similarity * 100)}% —
              cluster of {suggestion.member_count} face{suggestion.member_count !== 1 ? 's' : ''}
              {suggestion.is_high_conf === 1 && <span className="ml-1 text-green-400">✓ high confidence</span>}
            </p>
            <div className="flex gap-6 items-center justify-center mb-6">
              {/* Named person representative */}
              <div className="flex flex-col items-center gap-1">
                <div className="w-24 h-24 rounded-xl overflow-hidden bg-gray-800 border border-indigo-600">
                  {(() => {
                    const p = persons.find(x => x.id === suggestionPersonId)
                    const url = p?.representative_thumbnail
                      ? '/thumbnails/' + p.representative_thumbnail.split('/thumbnails/').pop()
                      : null
                    return url
                      ? <img src={url} alt={suggestionPersonName ?? ''} className="w-full h-full object-cover" />
                      : <span className="flex items-center justify-center h-full text-gray-500 text-2xl">👤</span>
                  })()}
                </div>
                <span className="text-xs text-indigo-400 font-medium">{suggestionPersonName}</span>
              </div>
              <span className="text-gray-400 text-2xl">≈</span>
              {/* Candidate cluster */}
              <div className="flex flex-col items-center gap-1">
                <div className="w-24 h-24 rounded-xl overflow-hidden bg-gray-800 border border-gray-600">
                  {(() => {
                    const url = suggestion.representative_thumbnail
                      ? '/thumbnails/' + suggestion.representative_thumbnail.split('/thumbnails/').pop()
                      : null
                    return url
                      ? <img src={url} alt="candidate" className="w-full h-full object-cover" />
                      : <span className="flex items-center justify-center h-full text-gray-500 text-2xl">?</span>
                  })()}
                </div>
                <span className="text-xs text-gray-500">{suggestion.member_count} face{suggestion.member_count !== 1 ? 's' : ''}</span>
              </div>
            </div>
            <div className="flex gap-3">
              <button
                onClick={acceptSuggestion}
                disabled={suggestionBusy}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                {suggestionBusy ? 'Merging…' : '✓ Yes, same person'}
              </button>
              <button
                onClick={rejectSuggestion}
                disabled={suggestionBusy}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                Different person
              </button>
            </div>
            <button
              onClick={dismissSuggestions}
              className="mt-3 w-full text-center text-xs text-gray-600 hover:text-gray-400"
            >
              Stop checking (done for now)
            </button>
          </div>
        </div>
      )}

      {/* Face review panel */}
      {reviewPerson && (
        <div className="fixed inset-0 bg-black/70 flex items-start justify-center z-50 overflow-y-auto py-10">
          <div className="bg-gray-900 rounded-xl p-6 w-full max-w-2xl shadow-xl mx-4">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-white font-semibold text-lg">{reviewPerson.name}</h2>
                <p className="text-gray-400 text-xs mt-0.5">
                  Click ✕ on any face to remove it (false positive correction)
                </p>
              </div>
              <button onClick={() => setReviewPerson(null)}
                className="text-gray-400 hover:text-white text-xl leading-none px-2">✕</button>
            </div>
            {reviewLoading
              ? <p className="text-gray-400 text-sm">Loading…</p>
              : reviewFaces.length === 0
                ? <p className="text-gray-500 text-sm">No faces assigned.</p>
                : (
                  <div className="grid grid-cols-4 sm:grid-cols-6 gap-3">
                    {reviewFaces.map(f => {
                      const url = f.thumbnail_path
                        ? '/thumbnails/' + f.thumbnail_path.split('/thumbnails/').pop()
                        : null
                      return (
                        <div key={f.id} className="relative group">
                          <div className="w-16 h-16 rounded-lg overflow-hidden bg-gray-800">
                            {url
                              ? <img src={url} alt="face" className="w-full h-full object-cover" />
                              : <span className="flex items-center justify-center h-full text-gray-600 text-xl">?</span>}
                          </div>
                          <button
                            onClick={() => ejectFace(f.id)}
                            title="Remove — not this person"
                            className="absolute -top-1.5 -right-1.5 w-5 h-5 bg-red-600 hover:bg-red-500 text-white rounded-full text-xs leading-none flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                            ✕
                          </button>
                          <p className="text-gray-500 text-[10px] mt-0.5 text-center truncate">
                            {(f.detection_conf * 100).toFixed(0)}%
                          </p>
                        </div>
                      )
                    })}
                  </div>
                )
            }
          </div>
        </div>
      )}

      {/* Dismiss confirm modal — delete or always-ignore a cluster */}
      {dismissTarget && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 overflow-y-auto py-6">
          <div className="bg-gray-800 rounded-xl p-6 max-w-lg w-full shadow-xl mx-4">

            {/* Primary face + cluster info */}
            <div className="flex items-center gap-4 mb-5">
              <div className="flex-shrink-0">
                {dismissTarget.representative_thumbnail
                  ? <img src={'/thumbnails/' + dismissTarget.representative_thumbnail.split('/thumbnails/').pop()} alt="face" className="w-20 h-20 rounded-xl object-cover border border-gray-600" />
                  : <div className="w-20 h-20 rounded-xl bg-gray-700 flex items-center justify-center text-2xl border border-gray-600">?</div>}
              </div>
              <div>
                <p className="text-white font-medium">Remove this face?</p>
                <p className="text-gray-400 text-xs mt-0.5">
                  Cluster of {dismissTarget.member_count} face{dismissTarget.member_count !== 1 ? 's' : ''}
                  {dismissTarget.is_high_conf === 1 && <span className="ml-1.5 text-green-400">✓ high confidence</span>}
                </p>
              </div>
            </div>

            {/* Similar faces */}
            {(similarLoading || similarClusters.length > 0) && (
              <div className="mb-5">
                <p className="text-xs text-gray-500 uppercase tracking-wider mb-2">
                  {similarLoading ? 'Looking for similar faces…' : `Similar unnamed faces (${similarClusters.length})`}
                </p>
                {similarLoading
                  ? <div className="flex gap-2">{[...Array(4)].map((_, i) => <div key={i} className="w-14 h-14 rounded-lg bg-gray-700 animate-pulse" />)}</div>
                  : (
                    <div className="flex flex-wrap gap-2">
                      {similarClusters.map(s => {
                        const url = s.representative_thumbnail
                          ? '/thumbnails/' + s.representative_thumbnail.split('/thumbnails/').pop()
                          : null
                        return (
                          <div key={s.cluster_id} className="flex flex-col items-center gap-0.5">
                            <div className="w-14 h-14 rounded-lg overflow-hidden bg-gray-700 border border-gray-600">
                              {url
                                ? <img src={url} alt="similar face" className="w-full h-full object-cover" />
                                : <span className="flex items-center justify-center h-full text-gray-500 text-lg">?</span>}
                            </div>
                            <span className="text-gray-500 text-[10px]">{Math.round(s.similarity * 100)}%</span>
                          </div>
                        )
                      })}
                    </div>
                  )
                }
                <p className="text-gray-600 text-[11px] mt-2">
                  ‘Always ignore’ will also suppress these faces in future scans.
                </p>
              </div>
            )}

            {/* Actions */}
            <div className="flex flex-col gap-2">
              <button
                onClick={() => handleDeleteCluster(dismissTarget.id)}
                disabled={dismissWorking}
                className="bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white rounded-lg py-2.5 text-sm font-medium transition-colors"
              >
                Delete — show again if re-detected
              </button>
              <button
                onClick={() => handleIgnoreCluster(dismissTarget.id)}
                disabled={dismissWorking}
                className="bg-red-900 hover:bg-red-800 disabled:opacity-40 text-white rounded-lg py-2.5 text-sm font-medium transition-colors"
              >
                Always ignore — never show this person
              </button>
              <button
                onClick={() => { setDismissTarget(null); setSimilarClusters([]) }}
                disabled={dismissWorking}
                className="text-gray-500 hover:text-gray-400 text-sm py-1 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Merge dialog */}
      {mergeCandidate && namingId !== null && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full shadow-xl">
            <p className="text-white font-medium mb-2">Same person?</p>
            <p className="text-gray-300 text-sm mb-6">
              "{nameInput}" is already used. Same person or different?
            </p>
            <div className="flex gap-3">
              <button onClick={() => handleMerge(namingId!, mergeCandidate.personId)} disabled={saving}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg py-2 text-sm font-medium">
                Same — merge
              </button>
              <button onClick={async () => {
                setSaving(true)
                try {
                  await api.persons.fromCluster(namingId!, nameInput.trim() + ' (2)')
                  setMergeCandidate(null); setNamingId(null); setNameInput(''); load()
                } finally { setSaving(false) }
              }} disabled={saving}
                className="flex-1 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white rounded-lg py-2 text-sm font-medium">
                Different person
              </button>
            </div>
          </div>
        </div>
      )}

      {clusters.length === 0 && persons.length === 0 && (
        <div className="text-gray-500 text-sm mt-12 text-center">
          No people found yet. Run the pipeline first via the ⚙️ Pipeline tab.
        </div>
      )}

      {/* Unnamed clusters */}
      {clusters.length > 0 && (
        <section className="mb-8">
          <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">
            Unnamed clusters ({clusters.length})
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-4">
            {clusters.map(c => (
              <ClusterTile key={c.id} cluster={c}
                isNaming={namingId === c.id} nameInput={nameInput} saving={saving}
                personNames={persons.filter(p => p.name).map(p => p.name!)}
                onStartNaming={() => { setNamingId(c.id); setNameInput('') }}
                onNameInput={setNameInput}
                onConfirm={() => handleName(c.id)}
                onCancel={() => setNamingId(null)}
                onDismiss={() => openDismiss(c)} />
            ))}
          </div>
        </section>
      )}

      {/* Named persons */}
      {persons.length > 0 && (
        <section>
          <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">
            Named ({persons.length})
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-4">
            {persons.map(p => {
              const thumb = p.representative_thumbnail
              const thumbUrl = thumb ? '/thumbnails/' + thumb.split('/thumbnails/').pop() : null
              const isRenaming = renamingPersonId === p.id
              return (
                <div key={p.id} className="flex flex-col items-center gap-2">
                  {/* Main tile — click to view photos */}
                  <div className="relative group">
                    <button
                      onClick={() => !isRenaming && onSelectPerson?.(p.id, p.name ?? 'Unknown')}
                      title={`View photos of ${p.name}`}
                      className="w-20 h-20 rounded-xl bg-gray-800 border border-gray-700 hover:border-indigo-400 overflow-hidden flex items-center justify-center transition-colors">
                      {thumbUrl
                        ? <img src={thumbUrl} alt={p.name ?? 'person'} className="w-full h-full object-cover" />
                        : <span className="text-2xl">👤</span>}
                    </button>
                    {/* Rename icon — top-left on hover */}
                    {!isRenaming && (
                      <button
                        onClick={() => { setRenamingPersonId(p.id); setRenameInput(p.name ?? '') }}
                        title="Rename"
                        className="absolute -top-1 -left-1 bg-gray-700 hover:bg-indigo-700 border border-gray-600 rounded-full w-5 h-5 text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                      >
                        ✎
                      </button>
                    )}
                    {/* Review icon — top-right on hover */}
                    <button
                      onClick={() => openReview(p)}
                      title="Review faces"
                      className="absolute -top-1 -right-1 bg-gray-700 hover:bg-gray-600 border border-gray-600 rounded-full w-5 h-5 text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                    >
                      ⋯
                    </button>
                  </div>
                  {isRenaming ? (
                    <div className="flex flex-col gap-1 w-full px-1">
                      <input
                        autoFocus
                        value={renameInput}
                        onChange={e => setRenameInput(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') handleRenamePerson(p.id)
                          if (e.key === 'Escape') setRenamingPersonId(null)
                        }}
                        className="w-full bg-gray-800 border border-indigo-500 rounded px-2 py-0.5 text-xs text-white outline-none text-center"
                      />
                      <div className="flex gap-1">
                        <button
                          onClick={() => handleRenamePerson(p.id)}
                          disabled={renameSaving || !renameInput.trim()}
                          className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded px-2 py-0.5 text-xs"
                        >
                          {renameSaving ? '…' : 'Save'}
                        </button>
                        <button
                          onClick={() => setRenamingPersonId(null)}
                          className="flex-1 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded px-2 py-0.5 text-xs"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <span className="text-xs text-center truncate max-w-full px-1 flex items-center gap-1 justify-center">
                      {/* Green = name written to file; Red = DB only, not yet written */}
                      <span
                        title={p.name_written ? 'Name written to photo file' : 'Name saved in database only (not yet written to file)'}
                        className={`inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${p.name_written ? 'bg-green-400' : 'bg-red-400'}`}
                      />
                      <span className="text-gray-200">{p.name}</span>
                    </span>
                  )}
                  <span className="text-xs text-gray-500">{p.photo_count} photo{p.photo_count !== 1 ? 's' : ''}</span>
                  {p.merge_sources_count > 0 && (
                    <span className="text-xs text-indigo-500">⇐ {p.merge_sources_count} merged</span>
                  )}
                  <button
                    onClick={() => startSuggestions(p.id, p.name!)}
                    title="Find similar faces"
                    className="text-xs text-gray-600 hover:text-indigo-400 transition-colors"
                  >
                    ≈ find similar
                  </button>
                </div>
              )
            })}
          </div>
        </section>
      )}
    </div>
  )
}

function ClusterTile({ cluster, isNaming, nameInput, saving, personNames, onStartNaming, onNameInput, onConfirm, onCancel, onDismiss }: {
  cluster: Cluster; isNaming: boolean; nameInput: string; saving: boolean
  personNames: string[]
  onStartNaming: () => void; onNameInput: (v: string) => void; onConfirm: () => void; onCancel: () => void
  onDismiss: () => void
}) {
  const [showSuggestions, setShowSuggestions] = useState(false)
  const thumb = cluster.representative_thumbnail
  const thumbUrl = thumb ? '/thumbnails/' + thumb.split('/thumbnails/').pop() : null

  const filteredNames = nameInput.trim().length > 0
    ? personNames.filter(n => n.toLowerCase().includes(nameInput.toLowerCase()))
    : []

  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative group">
        <button onClick={onStartNaming}
          className={'relative w-20 h-20 rounded-xl overflow-hidden bg-gray-800 border transition-colors ' +
            (isNaming ? 'border-indigo-500' : 'border-gray-700 hover:border-indigo-400')}>
          {thumbUrl
            ? <img src={thumbUrl} alt="face" className="w-full h-full object-cover" />
            : <span className="text-gray-500 text-2xl flex items-center justify-center h-full">?</span>}
          <span className="absolute bottom-0 right-0 bg-indigo-700 text-white text-xs px-1 rounded-tl leading-tight">
            {cluster.member_count}
          </span>
          {cluster.is_high_conf === 1 && (
            <span className="absolute top-0 left-0 bg-green-700 text-white text-xs px-1 rounded-br leading-tight">✓</span>
          )}
        </button>
        {/* Dismiss button — shown on hover */}
        <button
          onClick={e => { e.stopPropagation(); onDismiss() }}
          title="Remove this face"
          className="absolute -top-1 -right-1 w-5 h-5 bg-gray-700 hover:bg-red-700 border border-gray-600 rounded-full text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity text-gray-300 hover:text-white leading-none"
        >
          ✕
        </button>
      </div>
      {isNaming ? (
        <div className="flex flex-col gap-1 w-full">
          {/* Input + autocomplete dropdown */}
          <div className="relative">
            <input
              autoFocus
              value={nameInput}
              onChange={e => { onNameInput(e.target.value); setShowSuggestions(true) }}
              onFocus={() => setShowSuggestions(true)}
              onBlur={() => setShowSuggestions(false)}
              onKeyDown={e => {
                if (e.key === 'Enter') { setShowSuggestions(false); onConfirm() }
                if (e.key === 'Escape') { setShowSuggestions(false); onCancel() }
              }}
              placeholder="Enter name…"
              className="w-full bg-gray-800 border border-indigo-500 rounded px-2 py-0.5 text-xs text-white outline-none"
            />
            {showSuggestions && filteredNames.length > 0 && (
              <ul
                onMouseDown={e => e.preventDefault()}
                className="absolute top-full left-0 right-0 bg-gray-900 border border-gray-700 rounded-b shadow-lg z-20 max-h-36 overflow-y-auto"
              >
                {filteredNames.map(name => (
                  <li
                    key={name}
                    onClick={() => { onNameInput(name); setShowSuggestions(false); }}
                    className="px-2 py-1 text-xs text-gray-200 hover:bg-indigo-700 hover:text-white cursor-pointer truncate"
                  >
                    {name}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <button onClick={() => { setShowSuggestions(false); onConfirm() }} disabled={saving || !nameInput.trim()}
            className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded px-2 py-0.5 text-xs">
            {saving ? '…' : 'Save'}
          </button>
        </div>
      ) : (
        <span className="text-xs text-gray-500 italic">tap to name</span>
      )}
    </div>
  )
}
