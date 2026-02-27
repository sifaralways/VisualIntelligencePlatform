/**
 * PeoplePage — face tile grid with naming UX.
 *
 * Shows one representative tile per person/cluster.
 * High-confidence clusters: single tile + count.
 * Unnamed clusters: shown first, sorted by size.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Person } from '../api/client'

export default function PeoplePage() {
  const [persons, setPersons] = useState<Person[]>([])
  const [loading, setLoading] = useState(true)
  const [namingId, setNamingId] = useState<number | null>(null)
  const [nameInput, setNameInput] = useState('')
  const [mergeCandidate, setMergeCandidate] = useState<{ id: number; existingName: string } | null>(null)

  async function load() {
    setLoading(true)
    try {
      const data = await api.persons.list()
      setPersons(data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  async function handleName(personId: number) {
    const name = nameInput.trim()
    if (!name) return

    // Check if this name is already used by another person
    const existing = persons.find(p => p.name?.toLowerCase() === name.toLowerCase() && p.id !== personId)
    if (existing) {
      setMergeCandidate({ id: existing.id, existingName: existing.name! })
      return
    }

    await api.persons.namePerson(personId, name)
    setNamingId(null)
    setNameInput('')
    load()
  }

  async function handleMerge(sourceId: number, intoId: number) {
    await api.persons.merge(sourceId, intoId)
    setMergeCandidate(null)
    setNamingId(null)
    setNameInput('')
    load()
  }

  if (loading) return <div className="text-gray-400 text-sm">Loading people…</div>

  const unnamed = persons.filter(p => !p.name)
  const named   = persons.filter(p =>  p.name)

  return (
    <div>
      <h1 className="text-xl font-semibold mb-6">People</h1>

      {/* Merge confirmation dialog */}
      {mergeCandidate && namingId && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full shadow-xl">
            <p className="text-white font-medium mb-2">Same person?</p>
            <p className="text-gray-300 text-sm mb-6">
              "{nameInput}" is already assigned to another cluster.
              Are these the same person or two different people with the same name?
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => handleMerge(namingId, mergeCandidate.id)}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg py-2 text-sm font-medium"
              >
                Same person — merge
              </button>
              <button
                onClick={async () => {
                  await api.persons.namePerson(namingId!, `${nameInput} (2)`)
                  setMergeCandidate(null)
                  setNamingId(null)
                  setNameInput('')
                  load()
                }}
                className="flex-1 bg-gray-700 hover:bg-gray-600 text-white rounded-lg py-2 text-sm font-medium"
              >
                Different person
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Unnamed clusters */}
      {unnamed.length > 0 && (
        <section className="mb-8">
          <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">
            Unnamed ({unnamed.length})
          </h2>
          <PersonGrid
            persons={unnamed}
            namingId={namingId}
            nameInput={nameInput}
            onStartNaming={(id) => { setNamingId(id); setNameInput('') }}
            onNameInput={setNameInput}
            onConfirmName={handleName}
          />
        </section>
      )}

      {/* Named persons */}
      {named.length > 0 && (
        <section>
          <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">
            Named ({named.length})
          </h2>
          <PersonGrid
            persons={named}
            namingId={namingId}
            nameInput={nameInput}
            onStartNaming={(id) => { setNamingId(id); setNameInput('') }}
            onNameInput={setNameInput}
            onConfirmName={handleName}
          />
        </section>
      )}

      {persons.length === 0 && (
        <div className="text-gray-500 text-sm mt-12 text-center">
          No people found yet. Run the pipeline first via the ⚙️ Pipeline tab.
        </div>
      )}
    </div>
  )
}

function PersonGrid({ persons, namingId, nameInput, onStartNaming, onNameInput, onConfirmName }: {
  persons: Person[]
  namingId: number | null
  nameInput: string
  onStartNaming: (id: number) => void
  onNameInput: (v: string) => void
  onConfirmName: (id: number) => void
}) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-4">
      {persons.map(p => (
        <div key={p.id} className="flex flex-col items-center gap-2">
          {/* Face tile */}
          <button
            onClick={() => onStartNaming(p.id)}
            className="relative group rounded-xl overflow-hidden w-20 h-20 bg-gray-800 border border-gray-700 hover:border-indigo-500 transition-colors"
          >
            <img
              src={`/api/faces/cluster/${p.id}/thumbnail`}  // placeholder until proper endpoint
              alt={p.name ?? 'unnamed'}
              className="w-full h-full object-cover"
              onError={(e) => {
                (e.target as HTMLImageElement).style.display = 'none'
              }}
            />
            <div className="absolute inset-0 flex items-center justify-center text-gray-500 text-xs">
              ?
            </div>
            <div className="absolute bottom-0 right-0 bg-indigo-600 text-white text-xs px-1 rounded-tl">
              {p.photo_count}
            </div>
          </button>

          {/* Name or input */}
          {namingId === p.id ? (
            <div className="flex flex-col gap-1 w-full">
              <input
                autoFocus
                value={nameInput}
                onChange={e => onNameInput(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && onConfirmName(p.id)}
                placeholder="Enter name…"
                className="w-full bg-gray-800 border border-indigo-500 rounded px-2 py-0.5 text-xs text-white outline-none"
              />
              <button
                onClick={() => onConfirmName(p.id)}
                className="bg-indigo-600 hover:bg-indigo-500 text-white rounded px-2 py-0.5 text-xs"
              >
                Save
              </button>
            </div>
          ) : (
            <span className="text-xs text-center text-gray-300 truncate max-w-full px-1">
              {p.name ?? <span className="text-gray-600 italic">tap to name</span>}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
