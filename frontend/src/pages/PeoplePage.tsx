/**
 * PeoplePage — face tile grid with naming UX.
 *
 * Shows one representative tile per person/cluster.
 * High-confidence clusters: single tile + count.
 * Unnamed clusters: shown first, sorted by size.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Cluster, Person } from '../api/client'

export default function PeoplePage() {
  const [clusters, setClusters] = useState<Cluster[]>([])
  const [persons, setPersons] = useState<Person[]>([])
  const [loading, setLoading] = useState(true)
  const [namingId, setNamingId] = useState<number | null>(null)  // cluster id being named
  const [nameInput, setNameInput] = useState('')
  const [saving, setSaving] = useState(false)
  const [mergeCandidate, setMergeCandidate] = useState<{ personId: number; name: string } | null>(null)
  const [reviewPerson, setReviewPerson] = useState<Person | null>(null)
  const [reviewFaces, setReviewFaces] = useState<FaceRow[]>([])
  const [reviewLoading, setReviewLoading] = useState(false)
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

  if (loading) return <div className="text-gray-400 text-sm">Loading people…</div>

  return (
    <div>
      <h1 className="text-xl font-semibold mb-6">People</h1>

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
                onStartNaming={() => { setNamingId(c.id); setNameInput('') }}
                onNameInput={setNameInput}
                onConfirm={() => handleName(c.id)}
                onCancel={() => setNamingId(null)} />
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
              return (
                <div key={p.id} className="flex flex-col items-center gap-2">
                  <button
                    onClick={() => openReview(p)}
                    title="Click to review faces"
                    className="w-20 h-20 rounded-xl bg-gray-800 border border-gray-700 hover:border-indigo-400 overflow-hidden flex items-center justify-center transition-colors">
                    {thumbUrl
                      ? <img src={thumbUrl} alt={p.name ?? 'person'} className="w-full h-full object-cover" />
                      : <span className="text-2xl">👤</span>}
                  </button>
                  <span className="text-xs text-center text-gray-200 truncate max-w-full px-1">{p.name}</span>
                  <span className="text-xs text-gray-500">{p.photo_count} photo{p.photo_count !== 1 ? 's' : ''}</span>
                </div>
              )
            })}
          </div>
        </section>
      )}
    </div>
  )
}

function ClusterTile({ cluster, isNaming, nameInput, saving, onStartNaming, onNameInput, onConfirm, onCancel }: {
  cluster: Cluster; isNaming: boolean; nameInput: string; saving: boolean
  onStartNaming: () => void; onNameInput: (v: string) => void; onConfirm: () => void; onCancel: () => void
}) {
  const thumb = cluster.representative_thumbnail
  const thumbUrl = thumb ? '/thumbnails/' + thumb.split('/thumbnails/').pop() : null

  return (
    <div className="flex flex-col items-center gap-2">
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
      {isNaming ? (
        <div className="flex flex-col gap-1 w-full">
          <input autoFocus value={nameInput} onChange={e => onNameInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') onConfirm(); if (e.key === 'Escape') onCancel() }}
            placeholder="Enter name…"
            className="w-full bg-gray-800 border border-indigo-500 rounded px-2 py-0.5 text-xs text-white outline-none" />
          <button onClick={onConfirm} disabled={saving || !nameInput.trim()}
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
