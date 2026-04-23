/**
 * PeoplePage — face tile grid with naming UX.
 *
 * Shows one representative tile per person/cluster.
 * Named persons: click tile → see photos; click ≣ icon → face review.
 * Unnamed clusters: shown first, sorted by size.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Cluster, Person, MergeSuggestion, FaceRow, SimilarCluster, MergePersonsResult, FindSimilarSuggestion, FindSimilarAllResult, IgnoredPerson, IgnoreSuggestion } from '../api/client'
import ConnectionsGraph from '../components/ConnectionsGraph'

interface Props {
  /** Called when user clicks a named person tile to view their photos. */
  onSelectPerson?: (personId: number, name: string) => void
  /** Called when user clicks an unnamed cluster tile to view its photos. */
  onSelectCluster?: (clusterId: number) => void
}

export default function PeoplePage({ onSelectPerson, onSelectCluster }: Props) {
  const [clusters, setClusters] = useState<Cluster[]>([])
  const [persons, setPersons] = useState<Person[]>([])
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState<'named' | 'unnamed' | 'ignored'>('named')

  // ── Ignored faces tab ────────────────────────────────────────────────────
  const [ignoredPersons, setIgnoredPersons] = useState<IgnoredPerson[]>([])
  const [ignoredLoading, setIgnoredLoading] = useState(false)
  const [ignoredLoaded, setIgnoredLoaded] = useState(false)
  const [unignoringId, setUnignoringId] = useState<number | null>(null)
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
  const [reviewPortraitFaceId, setReviewPortraitFaceId] = useState<number | null>(null)
  const [settingPortrait, setSettingPortrait] = useState<number | null>(null)
  const [confirmUnname, setConfirmUnname] = useState(false)
  const [unnaming, setUnnaming] = useState(false)

  // ── Unnamed cluster face review ──────────────────────────────────────────
  const [reviewCluster, setReviewCluster] = useState<Cluster | null>(null)
  const [reviewClusterFaces, setReviewClusterFaces] = useState<FaceRow[]>([])
  const [reviewClusterLoading, setReviewClusterLoading] = useState(false)

  // ── Multi-select for named persons ─────────────────────────────────────
  const [namedSelectMode, setNamedSelectMode] = useState(false)
  const [namedSelected, setNamedSelected] = useState<Set<number>>(new Set())
  const [namedBulkWorking, setNamedBulkWorking] = useState(false)
  const [namedMergeOpen, setNamedMergeOpen] = useState(false)
  const [namedMergeNameInput, setNamedMergeNameInput] = useState('')

  // ── Named faces search ────────────────────────────────────────────────────
  const [nameSearch, setNameSearch] = useState('')
  const [namedMergeResult, setNamedMergeResult] = useState<MergePersonsResult | null>(null)

  function toggleNamedSelect(id: number) {
    setNamedSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  function exitNamedSelectMode() {
    setNamedSelectMode(false)
    setNamedSelected(new Set())
    setNamedMergeOpen(false)
    setNamedMergeNameInput('')
    setNamedMergeResult(null)
  }

  async function handleNamedBulkMerge() {
    const name = namedMergeNameInput.trim()
    if (namedSelected.size < 2 || !name) return
    setNamedBulkWorking(true)
    try {
      const selectedPersons = persons.filter(p => namedSelected.has(p.id))
      // Survivor = most photos; ties → first in list
      const survivor = selectedPersons.reduce((a, b) => b.photo_count > a.photo_count ? b : a)
      const others = selectedPersons.filter(p => p.id !== survivor.id)
      let result: MergePersonsResult | null = null
      for (const other of others) {
        result = await api.persons.mergePersons(other.id, survivor.id, name)
      }
      setNamedMergeResult(result)
      await load()
    } finally {
      setNamedBulkWorking(false)
    }
  }

  // ── Proactive merge suggestions ─────────────────────────────────────────
  const [suggestion, setSuggestion] = useState<MergeSuggestion | null>(null)
  const [suggestionPersonId, setSuggestionPersonId] = useState<number | null>(null)
  const [suggestionPersonName, setSuggestionPersonName] = useState<string | null>(null)
  const [suggestionBusy, setSuggestionBusy] = useState(false)

  // ── Find Similar (bulk scan across all named persons) ─────────────────────
  const [findSimilarOpen, setFindSimilarOpen] = useState(false)
  const [findSimilarThreshold, setFindSimilarThreshold] = useState(0.85)
  const [findSimilarWorking, setFindSimilarWorking] = useState(false)
  const [findSimilarResult, setFindSimilarResult] = useState<{ autoMerged: number; suggestionsFound: number } | null>(null)
  const [bulkSuggestionQueue, setBulkSuggestionQueue] = useState<FindSimilarSuggestion[]>([])
  const [bulkSuggestionWorking, setBulkSuggestionWorking] = useState(false)

  async function handleFindSimilar() {
    setFindSimilarOpen(false)
    setFindSimilarWorking(true)
    setFindSimilarResult(null)
    try {
      const result: FindSimilarAllResult = await api.persons.findSimilarAll(findSimilarThreshold)

      // Split suggestions: high-confidence → auto-merge; rest → ask user
      const highConf  = result.suggestions.filter(s => s.is_high_conf === 1)
      const needsReview = result.suggestions.filter(s => s.is_high_conf !== 1)

      for (const s of highConf) {
        await api.persons.addCluster(s.person_id, s.cluster_id)
      }

      const totalAutoMerged = result.auto_merged.length + highConf.length
      if (totalAutoMerged > 0) await load()

      setBulkSuggestionQueue(needsReview)
      setFindSimilarResult({ autoMerged: totalAutoMerged, suggestionsFound: needsReview.length })
    } finally {
      setFindSimilarWorking(false)
    }
  }

  async function acceptBulkSuggestion() {
    const current = bulkSuggestionQueue[0]
    if (!current) return
    setBulkSuggestionWorking(true)
    try {
      await api.persons.addCluster(current.person_id, current.cluster_id)
      setBulkSuggestionQueue(q => q.slice(1))
      await load()
    } finally { setBulkSuggestionWorking(false) }
  }

  async function rejectBulkSuggestion() {
    const current = bulkSuggestionQueue[0]
    if (!current) return
    setBulkSuggestionWorking(true)
    try {
      await api.persons.rejectSuggestion(current.person_id, current.cluster_id)
      setBulkSuggestionQueue(q => q.slice(1))
    } finally { setBulkSuggestionWorking(false) }
  }

  async function fetchNextSuggestion(personId: number) {
    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const list = await api.persons.mergeSuggestions(personId)
        if (list.length === 0) { setSuggestion(null); break }
        const s = list[0]
        if (s.is_high_conf === 1) {
          // auto-merge silently — don't ask the user
          await api.persons.addCluster(personId, s.cluster_id)
          await load()
          // loop: fetch next suggestion (the merged cluster is now excluded)
        } else {
          setSuggestion(s)
          break
        }
      }
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
  const [ignoreSuggestTarget, setIgnoreSuggestTarget] = useState<Cluster | null>(null)
  const [ignoreSuggestAction, setIgnoreSuggestAction] = useState<'delete' | 'ignore' | null>(null)
  const [ignoreSuggestThreshold, setIgnoreSuggestThreshold] = useState(0.85)
  const [ignoreSuggestWorking, setIgnoreSuggestWorking] = useState(false)
  const [ignoreSuggestionQueue, setIgnoreSuggestionQueue] = useState<IgnoreSuggestion[]>([])
  const [ignoreSuggestionSource, setIgnoreSuggestionSource] = useState<Cluster | null>(null)
  const [ignoreSuggestionPersonId, setIgnoreSuggestionPersonId] = useState<number | null>(null)
  const [ignoreSuggestionBusy, setIgnoreSuggestionBusy] = useState(false)
  const [ignoreSuggestionResult, setIgnoreSuggestionResult] = useState<{ autoIgnored: number; suggestionsFound: number; action: 'delete' | 'ignore' } | null>(null)

  // ── Connections graph modal ──────────────────────────────────────────────
  const [connectionsPersonId, setConnectionsPersonId] = useState<number | null>(null)
  const [connectionsPersonName, setConnectionsPersonName] = useState<string>('')

  // ── Multi-select for unnamed clusters ────────────────────────────────────
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [bulkWorking, setBulkWorking] = useState(false)
  const [bulkNameOpen, setBulkNameOpen] = useState(false)
  const [bulkNameInput, setBulkNameInput] = useState('')
  const [bulkNameSuggestions, setBulkNameSuggestions] = useState(false)
  const lastSelectedIdxRef = useRef<number>(-1)

  // Keep a stable, prefiltered list for autocomplete paths.
  const namedPersonNames = useMemo(
    () => persons.filter(p => p.name).map(p => p.name!),
    [persons],
  )

  function toggleSelect(id: number) {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  function rangeSelect(clickedIdx: number, shiftHeld: boolean) {
    if (!shiftHeld || lastSelectedIdxRef.current < 0) {
      // Normal click — toggle just this tile
      toggleSelect(clusters[clickedIdx].id)
      lastSelectedIdxRef.current = clickedIdx
      return
    }
    // Shift+click — select the inclusive range
    const lo = Math.min(lastSelectedIdxRef.current, clickedIdx)
    const hi = Math.max(lastSelectedIdxRef.current, clickedIdx)
    setSelected(prev => {
      const next = new Set(prev)
      for (let i = lo; i <= hi; i++) next.add(clusters[i].id)
      return next
    })
    lastSelectedIdxRef.current = clickedIdx
  }

  function selectAll() {
    setSelected(new Set(clusters.map(c => c.id)))
    lastSelectedIdxRef.current = clusters.length - 1
  }

  function selectHighConf() {
    setSelected(new Set(clusters.filter(c => c.is_high_conf === 1).map(c => c.id)))
    lastSelectedIdxRef.current = -1
  }

  function exitSelectMode() {
    setSelectMode(false)
    setSelected(new Set())
    setBulkNameOpen(false)
    setBulkNameInput('')
    lastSelectedIdxRef.current = -1
  }

  async function handleBulkDelete() {
    if (selected.size === 0) return
    setBulkWorking(true)
    try {
      await Promise.all([...selected].map(id => api.clusters.delete(id)))
      setClusters(prev => prev.filter(c => !selected.has(c.id)))
      exitSelectMode()
    } finally { setBulkWorking(false) }
  }

  async function handleBulkIgnore() {
    if (selected.size === 0) return
    setBulkWorking(true)
    try {
      await Promise.all([...selected].map(id => api.clusters.ignore(id)))
      setClusters(prev => prev.filter(c => !selected.has(c.id)))
      exitSelectMode()
    } finally { setBulkWorking(false) }
  }

  async function handleBulkName() {
    const name = bulkNameInput.trim()
    if (!name || selected.size === 0) return
    setBulkWorking(true)
    try {
      const ids = [...selected]
      const existing = persons.find(p => p.name?.toLowerCase() === name.toLowerCase())
      if (existing) {
        // Merge all selected clusters into the existing person
        await Promise.all(ids.map(id => api.persons.addCluster(existing.id, id)))
      } else {
        // Create person from first cluster, add remaining clusters to it
        const [first, ...rest] = ids
        const result = await api.persons.fromCluster(first, name)
        if (rest.length > 0) {
          await Promise.all(rest.map(id => api.persons.addCluster(result.person_id, id)))
        }
      }
      await load()
      exitSelectMode()
    } finally { setBulkWorking(false) }
  }

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
      if (ignoredLoaded) await loadIgnored()
    } finally { setDismissWorking(false) }
  }

  async function handleIgnoreCluster(clusterId: number) {
    setDismissWorking(true)
    try {
      await api.clusters.ignore(clusterId)
      setClusters(prev => prev.filter(c => c.id !== clusterId))
      setDismissTarget(null)
      setSimilarClusters([])
      if (ignoredLoaded) await loadIgnored()
    } finally { setDismissWorking(false) }
  }

  function openIgnoreSuggestionPrompt(action: 'delete' | 'ignore') {
    if (!dismissTarget) return
    // Start a new ignore-suggestion session; clear stale queue state from previous runs.
    setIgnoreSuggestionQueue([])
    setIgnoreSuggestionPersonId(null)
    setIgnoreSuggestionSource(null)
    setIgnoreSuggestTarget(dismissTarget)
    setIgnoreSuggestAction(action)
    setIgnoreSuggestThreshold(0.85)
    setDismissTarget(null)
  }

  function closeIgnoreSuggestionPrompt() {
    setIgnoreSuggestTarget(null)
    setIgnoreSuggestAction(null)
    setIgnoreSuggestThreshold(0.85)
  }

  async function skipIgnoreSuggestions() {
    if (!ignoreSuggestTarget || !ignoreSuggestAction) return
    if (ignoreSuggestAction === 'delete') {
      await handleDeleteCluster(ignoreSuggestTarget.id)
    } else {
      await handleIgnoreCluster(ignoreSuggestTarget.id)
    }
    closeIgnoreSuggestionPrompt()
  }

  async function runIgnoreSuggestions() {
    if (!ignoreSuggestTarget || !ignoreSuggestAction) return
    setIgnoreSuggestWorking(true)
    try {
      const result = await api.clusters.ignoreSuggestions(
        ignoreSuggestTarget.id,
        ignoreSuggestAction,
        ignoreSuggestThreshold,
      )
      await load()
      if (ignoredLoaded) await loadIgnored()
      setIgnoreSuggestionSource(ignoreSuggestTarget)
      setIgnoreSuggestionPersonId(result.person_id)
      setIgnoreSuggestionQueue(result.suggestions)
      setIgnoreSuggestionResult({
        autoIgnored: result.auto_ignored.length,
        suggestionsFound: result.suggestions.length,
        action: ignoreSuggestAction,
      })
    } finally {
      setIgnoreSuggestWorking(false)
      closeIgnoreSuggestionPrompt()
    }
  }

  async function acceptIgnoreSuggestion() {
    const current = ignoreSuggestionQueue[0]
    if (!current || ignoreSuggestionPersonId == null) return
    setIgnoreSuggestionBusy(true)
    try {
      await api.persons.addIgnoredCluster(ignoreSuggestionPersonId, current.cluster_id)
      setIgnoreSuggestionQueue(q => {
        const next = q.slice(1)
        if (next.length === 0) {
          setIgnoreSuggestionPersonId(null)
          setIgnoreSuggestionSource(null)
        }
        return next
      })
      await load()
      if (ignoredLoaded) await loadIgnored()
    } finally {
      setIgnoreSuggestionBusy(false)
    }
  }

  async function rejectIgnoreSuggestion() {
    const current = ignoreSuggestionQueue[0]
    if (!current || ignoreSuggestionPersonId == null) return
    setIgnoreSuggestionBusy(true)
    try {
      await api.persons.rejectSuggestion(ignoreSuggestionPersonId, current.cluster_id)
      setIgnoreSuggestionQueue(q => {
        const next = q.slice(1)
        if (next.length === 0) {
          setIgnoreSuggestionPersonId(null)
          setIgnoreSuggestionSource(null)
        }
        return next
      })
    } finally {
      setIgnoreSuggestionBusy(false)
    }
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

  async function loadIgnored() {
    setIgnoredLoading(true)
    try {
      const items = await api.persons.listIgnored()
      setIgnoredPersons(items)
      setIgnoredLoaded(true)
    } finally {
      setIgnoredLoading(false)
    }
  }

  async function handleUnignore(personId: number) {
    setUnignoringId(personId)
    try {
      await api.persons.unignore(personId)
      setIgnoredPersons(prev => prev.filter(p => p.id !== personId))
      // Reload unnamed clusters so the restored faces appear immediately
      const fresh = await api.clusters.unnamed()
      setClusters(fresh)
    } finally {
      setUnignoringId(null)
    }
  }

  useEffect(() => { load() }, [])

  // Lazy-load ignored faces when tab is first activated
  useEffect(() => {
    if (activeTab === 'ignored' && !ignoredLoaded) {
      loadIgnored()
    }
  }, [activeTab])

  async function openReview(person: Person) {
    setReviewPerson(person)
    setReviewFaces([])
    setReviewPortraitFaceId(null)
    setConfirmUnname(false)
    setReviewLoading(true)
    try {
      const faces = await api.faces.byPerson(person.id)
      setReviewFaces(faces)
      // Determine current portrait face id from representative thumbnail path
      // No dedicated field returned yet — we'll track it locally via setPortrait calls
    } finally {
      setReviewLoading(false)
    }
  }

  async function setPortraitFace(faceId: number) {
    if (!reviewPerson) return
    setSettingPortrait(faceId)
    try {
      await api.persons.setPortrait(reviewPerson.id, faceId)
      setReviewPortraitFaceId(faceId)
      // Reload from server so persons grid + suggestion modal use the confirmed thumbnail
      await load()
    } finally {
      setSettingPortrait(null)
    }
  }

  async function ejectFace(faceId: number) {
    await api.faces.removeFromPerson(faceId)
    setReviewFaces(f => f.filter(x => x.id !== faceId))
    load() // refresh counts
  }

  async function handleUnnamePerson() {
    if (!reviewPerson) return
    setUnnaming(true)
    try {
      await api.persons.delete(reviewPerson.id)
      setReviewPerson(null)
      setConfirmUnname(false)
      await load()
    } finally {
      setUnnaming(false)
    }
  }

  async function openClusterReview(cluster: Cluster) {
    setReviewCluster(cluster)
    setReviewClusterFaces([])
    setReviewClusterLoading(true)
    try {
      const faces = await api.faces.byCluster(cluster.id)
      setReviewClusterFaces(faces)
    } finally {
      setReviewClusterLoading(false)
    }
  }

  async function ejectFromCluster(faceId: number) {
    await api.faces.removeFromCluster(faceId)
    setReviewClusterFaces(f => f.filter(x => x.id !== faceId))
    load() // refresh cluster counts
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
      {/* ── Sub-tab bar ────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-1 mb-6 border-b border-gray-800 pb-0">
        <button
          onClick={() => setActiveTab('named')}
          className={`px-4 py-2 text-sm font-medium rounded-t-lg transition-colors -mb-px border-b-2 ${
            activeTab === 'named'
              ? 'border-indigo-500 text-white'
              : 'border-transparent text-gray-400 hover:text-white'
          }`}
        >
          Named Faces
          {persons.length > 0 && (
            <span className={`ml-1.5 text-xs px-1.5 py-0.5 rounded-full ${
              activeTab === 'named' ? 'bg-indigo-600 text-white' : 'bg-gray-800 text-gray-500'
            }`}>
              {persons.length}
            </span>
          )}
        </button>
        <button
          onClick={() => setActiveTab('unnamed')}
          className={`px-4 py-2 text-sm font-medium rounded-t-lg transition-colors -mb-px border-b-2 ${
            activeTab === 'unnamed'
              ? 'border-indigo-500 text-white'
              : 'border-transparent text-gray-400 hover:text-white'
          }`}
        >
          Unnamed Faces
          {clusters.length > 0 && (
            <span className={`ml-1.5 text-xs px-1.5 py-0.5 rounded-full ${
              activeTab === 'unnamed' ? 'bg-indigo-600 text-white' : 'bg-gray-800 text-gray-500'
            }`}>
              {clusters.length}
            </span>
          )}
        </button>
        <button
          onClick={() => setActiveTab('ignored')}
          className={`px-4 py-2 text-sm font-medium rounded-t-lg transition-colors -mb-px border-b-2 ${
            activeTab === 'ignored'
              ? 'border-red-500 text-white'
              : 'border-transparent text-gray-400 hover:text-white'
          }`}
        >
          Ignored
          {ignoredPersons.length > 0 && (
            <span className={`ml-1.5 text-xs px-1.5 py-0.5 rounded-full ${
              activeTab === 'ignored' ? 'bg-red-700 text-white' : 'bg-gray-800 text-gray-500'
            }`}>
              {ignoredPersons.length}
            </span>
          )}
        </button>
      </div>

      {/* ── Named-person multi-select merge dialog ────────────────────────── */}
      {namedMergeOpen && (
        <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl mx-4">
            {namedMergeResult ? (
              /* ── Success state ── */
              <>
                <p className="text-white font-semibold text-lg mb-2 text-center">Merge complete ✓</p>
                <p className="text-gray-400 text-sm text-center mb-5">
                  <span className="text-indigo-300 font-medium">{namedMergeResult.survivor_name}</span> now has{' '}
                  {namedMergeResult.photos_queued_for_writeback} photo{namedMergeResult.photos_queued_for_writeback !== 1 ? 's' : ''} queued for writeback.
                  Old names will be removed from files on next write.
                </p>
                <button
                  onClick={exitNamedSelectMode}
                  className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
                >
                  Done
                </button>
              </>
            ) : (
              /* ── Confirm state ── */
              <>
                <p className="text-white font-semibold text-lg mb-1">
                  Merge {namedSelected.size} people?
                </p>
                <p className="text-gray-400 text-xs mb-5">
                  All faces and photos will be combined into one person. Files will be updated on next writeback.
                </p>

                {/* Thumbnails of selected persons */}
                <div className="flex gap-3 flex-wrap justify-center mb-5">
                  {persons.filter(p => namedSelected.has(p.id)).map(p => {
                    const url = p.representative_thumbnail
                      ? '/thumbnails/' + p.representative_thumbnail.split('/thumbnails/').pop()
                      : null
                    return (
                      <div key={p.id} className="flex flex-col items-center gap-1">
                        <div className="w-16 h-16 rounded-xl overflow-hidden bg-gray-800 border border-gray-600">
                          {url
                            ? <img src={url} alt={p.name ?? ''} className="w-full h-full object-cover" />
                            : <span className="flex items-center justify-center h-full text-gray-500 text-2xl">👤</span>}
                        </div>
                        <span className="text-xs text-gray-300 max-w-[64px] truncate text-center">{p.name}</span>
                        <span className="text-[10px] text-gray-500">{p.photo_count} photo{p.photo_count !== 1 ? 's' : ''}</span>
                      </div>
                    )
                  })}
                </div>

                {/* Quick-name buttons — one per unique selected name */}
                <p className="text-xs text-gray-500 mb-1.5">Merged name:</p>
                <div className="flex gap-2 mb-2 flex-wrap">
                  {persons
                    .filter(p => namedSelected.has(p.id))
                    .map(p => p.name)
                    .filter((n): n is string => !!n)
                    .filter((n, i, a) => a.indexOf(n) === i)
                    .map(n => (
                      <button key={n}
                        onClick={() => setNamedMergeNameInput(n)}
                        className={`rounded-lg py-1.5 px-3 text-xs font-medium border transition-colors ${
                          namedMergeNameInput === n
                            ? 'bg-indigo-600 border-indigo-500 text-white'
                            : 'bg-gray-800 border-gray-600 text-gray-300 hover:border-indigo-400'
                        }`}
                      >
                        {n}
                      </button>
                    ))}
                </div>
                <input
                  value={namedMergeNameInput}
                  onChange={e => setNamedMergeNameInput(e.target.value)}
                  placeholder="Custom name…"
                  className="w-full bg-gray-800 border border-gray-600 focus:border-indigo-500 rounded-lg px-3 py-2 text-sm text-white outline-none mb-5"
                />

                <div className="flex gap-3">
                  <button
                    onClick={handleNamedBulkMerge}
                    disabled={namedBulkWorking || !namedMergeNameInput.trim()}
                    className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
                  >
                    {namedBulkWorking ? 'Merging…' : 'Merge'}
                  </button>
                  <button
                    onClick={() => { setNamedMergeOpen(false); setNamedMergeNameInput('') }}
                    disabled={namedBulkWorking}
                    className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

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

      {/* Unnamed cluster face review panel */}
      {reviewCluster && (
        <div className="fixed inset-0 bg-black/70 flex items-start justify-center z-50 overflow-y-auto py-10">
          <div className="bg-gray-900 rounded-xl p-6 w-full max-w-2xl shadow-xl mx-4">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-white font-semibold text-lg">
                  Cluster · {reviewCluster.member_count} face{reviewCluster.member_count !== 1 ? 's' : ''}
                </h2>
                <p className="text-gray-400 text-xs mt-0.5">
                  Click ✕ on a face to remove it from this cluster
                </p>
              </div>
              <button onClick={() => setReviewCluster(null)}
                className="text-gray-400 hover:text-white text-xl leading-none px-2">✕</button>
            </div>
            {reviewClusterLoading
              ? <p className="text-gray-400 text-sm">Loading…</p>
              : reviewClusterFaces.length === 0
                ? <p className="text-gray-500 text-sm">No faces in this cluster.</p>
                : (
                  <div className="grid grid-cols-4 sm:grid-cols-6 gap-3">
                    {reviewClusterFaces.map(f => {
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
                            onClick={() => ejectFromCluster(f.id)}
                            title="Remove — not in this cluster"
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

      {/* Face review panel */}
      {reviewPerson && (
        <div className="fixed inset-0 bg-black/70 flex items-start justify-center z-50 overflow-y-auto py-10">
          <div className="bg-gray-900 rounded-xl p-6 w-full max-w-2xl shadow-xl mx-4">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-white font-semibold text-lg">{reviewPerson.name}</h2>
                <p className="text-gray-400 text-xs mt-0.5">
                  Click ✕ to remove a face &nbsp;·&nbsp; Click ★ to set as primary thumbnail
                </p>
              </div>
              <div className="flex items-center gap-2">
                {confirmUnname ? (
                  <>
                    <span className="text-xs text-red-400">Remove name &amp; release all faces?</span>
                    <button
                      onClick={handleUnnamePerson}
                      disabled={unnaming}
                      className="text-xs px-2.5 py-1 rounded-lg bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white transition-colors"
                    >
                      {unnaming ? 'Removing…' : 'Yes, un-name'}
                    </button>
                    <button
                      onClick={() => setConfirmUnname(false)}
                      disabled={unnaming}
                      className="text-xs px-2 py-1 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setConfirmUnname(true)}
                    title="Remove name and release faces back to unnamed pool"
                    className="text-xs px-2.5 py-1 rounded-lg border border-red-800 bg-red-900/20 text-red-400 hover:bg-red-900/40 hover:text-red-300 transition-colors"
                  >
                    Un-name
                  </button>
                )}
                <button onClick={() => { setReviewPerson(null); setConfirmUnname(false) }}
                  className="text-gray-400 hover:text-white text-xl leading-none px-2">✕</button>
              </div>
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
                      const isPortrait = reviewPortraitFaceId === f.id
                      const isSetting = settingPortrait === f.id
                      return (
                        <div key={f.id} className="relative group">
                          <div className={`w-16 h-16 rounded-lg overflow-hidden bg-gray-800 ${
                            isPortrait ? 'ring-2 ring-yellow-400' : ''
                          }`}>
                            {url
                              ? <img src={url} alt="face" className="w-full h-full object-cover" />
                              : <span className="flex items-center justify-center h-full text-gray-600 text-xl">?</span>}
                          </div>
                          {/* Remove button */}
                          <button
                            onClick={() => ejectFace(f.id)}
                            title="Remove — not this person"
                            className="absolute -top-1.5 -right-1.5 w-5 h-5 bg-red-600 hover:bg-red-500 text-white rounded-full text-xs leading-none flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                            ✕
                          </button>
                          {/* Portrait / primary button */}
                          {!isPortrait && (
                            <button
                              onClick={() => setPortraitFace(f.id)}
                              disabled={!!settingPortrait}
                              title="Set as primary thumbnail"
                              className="absolute -top-1.5 -left-1.5 w-5 h-5 bg-gray-700 hover:bg-yellow-500 disabled:opacity-40 text-yellow-300 hover:text-white rounded-full text-xs leading-none flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                              {isSetting ? '…' : '★'}
                            </button>
                          )}
                          {isPortrait && (
                            <span
                              title="Current primary thumbnail"
                              className="absolute -top-1.5 -left-1.5 w-5 h-5 bg-yellow-500 text-white rounded-full text-xs leading-none flex items-center justify-center">
                              ★
                            </span>
                          )}
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
                onClick={() => openIgnoreSuggestionPrompt('delete')}
                disabled={dismissWorking}
                className="bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white rounded-lg py-2.5 text-sm font-medium transition-colors"
              >
                Delete — show again if re-detected
              </button>
              <button
                onClick={() => openIgnoreSuggestionPrompt('ignore')}
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
          No people found yet. Run the pipeline first via the Pipeline panel.
        </div>
      )}

      {activeTab === 'unnamed' && clusters.length === 0 && persons.length > 0 && (
        <div className="text-gray-500 text-sm mt-12 text-center">
          No unnamed clusters — all faces have been assigned.
        </div>
      )}

      {activeTab === 'named' && persons.length === 0 && clusters.length > 0 && (
        <div className="text-gray-500 text-sm mt-12 text-center">
          No named persons yet. Switch to <button onClick={() => setActiveTab('unnamed')} className="text-indigo-400 hover:text-indigo-300 underline">Unnamed Faces</button> to name some.
        </div>
      )}

      {/* ── Unnamed clusters tab ─────────────────────────────────────────── */}
      {activeTab === 'unnamed' && clusters.length > 0 && (
        <section className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider">
              Unnamed clusters ({clusters.length})
            </h2>
            <div className="flex items-center gap-2">
              {selectMode && (
                <>
                  <button
                    onClick={selectAll}
                    className="text-xs px-2.5 py-1 rounded-lg border border-gray-600 bg-gray-800 text-gray-400 hover:text-white hover:border-gray-400 transition-colors"
                  >All</button>
                  {clusters.some(c => c.is_high_conf === 1) && (
                    <button
                      onClick={selectHighConf}
                      className="text-xs px-2.5 py-1 rounded-lg border border-gray-600 bg-gray-800 text-gray-400 hover:text-white hover:border-gray-400 transition-colors"
                    >✓ High-conf</button>
                  )}
                  <span className="text-xs text-gray-500">Click · Shift+click range</span>
                  {selected.size > 0 && (
                    <span className="text-xs text-gray-400">{selected.size} selected</span>
                  )}
                </>
              )}
              <button
                onClick={() => { if (selectMode) exitSelectMode(); else setSelectMode(true) }}
                className={`text-xs px-2.5 py-1 rounded-lg border transition-colors ${
                  selectMode
                    ? 'bg-indigo-700 border-indigo-500 text-white'
                    : 'bg-gray-800 border-gray-600 text-gray-400 hover:text-white hover:border-gray-400'
                }`}
              >
                {selectMode ? 'Cancel' : 'Select'}
              </button>
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-4">
            {clusters.map((c, idx) => (
              <ClusterTile key={c.id} cluster={c}
                isNaming={!selectMode && namingId === c.id} nameInput={nameInput} saving={saving}
                personNames={namedPersonNames}
                selectMode={selectMode}
                isSelected={selected.has(c.id)}
                onToggleSelect={(shiftHeld) => rangeSelect(idx, shiftHeld)}
                onViewPhotos={() => onSelectCluster?.(c.id)}
                onStartNaming={() => { if (!selectMode) { setNamingId(c.id); setNameInput('') } }}
                onNameInput={setNameInput}
                onConfirm={() => handleName(c.id)}
                onCancel={() => setNamingId(null)}
                onDismiss={() => openDismiss(c)}
                onReview={() => openClusterReview(c)} />
            ))}
          </div>

          {/* Bulk action bar — floats at bottom when items are selected */}
          {selectMode && selected.size > 0 && (
            <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 bg-gray-900 border border-gray-700 rounded-2xl px-5 py-3 shadow-2xl">
              {/* Name input row — shown when Name button is clicked */}
              {bulkNameOpen && (
                <div className="flex items-center gap-2 mb-3 relative">
                  <div className="relative flex-1">
                    <input
                      autoFocus
                      value={bulkNameInput}
                      onChange={e => { setBulkNameInput(e.target.value); setBulkNameSuggestions(true) }}
                      onFocus={() => setBulkNameSuggestions(true)}
                      onBlur={() => setBulkNameSuggestions(false)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') { setBulkNameSuggestions(false); handleBulkName() }
                        if (e.key === 'Escape') { setBulkNameOpen(false); setBulkNameInput('') }
                      }}
                      placeholder="Enter name…"
                      className="w-full bg-gray-800 border border-indigo-500 rounded-lg px-3 py-1.5 text-sm text-white outline-none min-w-[180px]"
                    />
                    {bulkNameSuggestions && bulkNameInput.trim().length > 0 && (() => {
                      const query = bulkNameInput.toLowerCase()
                      const matches = namedPersonNames.filter(n => n.toLowerCase().includes(query))
                      return matches.length > 0 ? (
                        <ul
                          onMouseDown={e => e.preventDefault()}
                          className="absolute bottom-full mb-1 left-0 right-0 bg-gray-900 border border-gray-700 rounded-lg shadow-xl z-50 max-h-40 overflow-y-auto"
                        >
                          {matches.map(name => (
                            <li
                              key={name}
                              onClick={() => { setBulkNameInput(name); setBulkNameSuggestions(false) }}
                              className="px-3 py-1.5 text-sm text-gray-200 hover:bg-indigo-700 hover:text-white cursor-pointer truncate"
                            >
                              {name}
                            </li>
                          ))}
                        </ul>
                      ) : null
                    })()}
                  </div>
                  <button
                    onClick={() => { setBulkNameSuggestions(false); handleBulkName() }}
                    disabled={bulkWorking || !bulkNameInput.trim()}
                    className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium px-4 py-1.5 rounded-lg transition-colors whitespace-nowrap"
                  >
                    {bulkWorking ? 'Saving…' : 'Save'}
                  </button>
                  <button
                    onClick={() => { setBulkNameOpen(false); setBulkNameInput('') }}
                    className="text-gray-500 hover:text-gray-300 text-sm transition-colors"
                  >
                    ✕
                  </button>
                </div>
              )}
              {/* Main action row */}
              <div className="flex items-center gap-3">
                <span className="text-sm text-gray-300 mr-1">{selected.size} face{selected.size !== 1 ? 's' : ''}</span>
                <button
                  onClick={() => { setBulkNameOpen(o => !o); setBulkNameInput('') }}
                  disabled={bulkWorking}
                  className={`text-sm font-medium px-4 py-1.5 rounded-lg transition-colors disabled:opacity-40 ${
                    bulkNameOpen
                      ? 'bg-indigo-700 text-white'
                      : 'bg-indigo-600 hover:bg-indigo-500 text-white'
                  }`}
                >
                  Name
                </button>
                <button
                  onClick={handleBulkDelete}
                  disabled={bulkWorking}
                  className="bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white text-sm font-medium px-4 py-1.5 rounded-lg transition-colors"
                >
                  {bulkWorking ? 'Working…' : 'Delete'}
                </button>
                <button
                  onClick={handleBulkIgnore}
                  disabled={bulkWorking}
                  className="bg-red-900 hover:bg-red-800 disabled:opacity-40 text-white text-sm font-medium px-4 py-1.5 rounded-lg transition-colors"
                >
                  Always ignore
                </button>
                <button
                  onClick={exitSelectMode}
                  disabled={bulkWorking}
                  className="text-gray-500 hover:text-gray-300 text-sm transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {/* ── Named persons tab ───────────────────────────────────────────── */}
      {activeTab === 'named' && persons.length > 0 && (
        <section>
        <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider">
              Named ({nameSearch.trim()
                ? `${persons.filter(p => (p.name ?? '').toLowerCase().includes(nameSearch.toLowerCase())).length} of ${persons.length}`
                : persons.length})
            </h2>
            <div className="flex items-center gap-2">
              {namedSelectMode && namedSelected.size > 0 && (
                <span className="text-xs text-gray-400">{namedSelected.size} selected</span>
              )}
              {/* Find Similar — only useful when unnamed clusters exist */}
              {!namedSelectMode && clusters.length > 0 && (
                <button
                  onClick={() => setFindSimilarOpen(true)}
                  disabled={findSimilarWorking}
                  title={`Scan all ${persons.length} named persons against ${clusters.length} unnamed clusters`}
                  className="text-xs px-2.5 py-1 rounded-lg border border-yellow-700 bg-yellow-900/20 text-yellow-400 hover:bg-yellow-900/40 hover:text-yellow-300 transition-colors disabled:opacity-40"
                >
                  {findSimilarWorking ? 'Scanning…' : '≈ Find Similar'}
                </button>
              )}
              <button
                onClick={() => { setNamedSelectMode(s => !s); setNamedSelected(new Set()) }}
                className={`text-xs px-2.5 py-1 rounded-lg border transition-colors ${
                  namedSelectMode
                    ? 'bg-indigo-700 border-indigo-500 text-white'
                    : 'bg-gray-800 border-gray-600 text-gray-400 hover:text-white hover:border-gray-400'
                }`}
              >
                {namedSelectMode ? 'Cancel' : 'Select'}
              </button>
            </div>
          </div>

          {/* Find Similar result banner */}
          {findSimilarResult && bulkSuggestionQueue.length === 0 && (
            <div className="mb-4 flex items-center justify-between rounded-xl bg-indigo-900/30 border border-indigo-800 px-4 py-2.5">
              <p className="text-sm text-indigo-300">
                Scan complete —{' '}
                {findSimilarResult.autoMerged > 0
                  ? <span className="text-green-400 font-medium">{findSimilarResult.autoMerged} auto-merged</span>
                  : <span>0 auto-merged</span>}
                {', '}
                {findSimilarResult.suggestionsFound > 0
                  ? <span className="text-yellow-400 font-medium">{findSimilarResult.suggestionsFound} reviewed manually</span>
                  : <span>no manual review needed</span>}
              </p>
              <button onClick={() => setFindSimilarResult(null)} className="text-indigo-600 hover:text-indigo-400 text-sm ml-4">✕</button>
            </div>
          )}

          {ignoreSuggestionResult && ignoreSuggestionQueue.length === 0 && (
            <div className="mb-4 flex items-center justify-between rounded-xl bg-red-900/20 border border-red-800 px-4 py-2.5">
              <p className="text-sm text-red-200">
                Ignore suggestions complete for the {ignoreSuggestionResult.action === 'delete' ? 'delete' : 'always ignore'} action —{' '}
                {ignoreSuggestionResult.autoIgnored > 0
                  ? <span className="text-red-300 font-medium">{ignoreSuggestionResult.autoIgnored} auto-ignored</span>
                  : <span>0 auto-ignored</span>}
                {', '}
                {ignoreSuggestionResult.suggestionsFound > 0
                  ? <span className="text-yellow-300 font-medium">{ignoreSuggestionResult.suggestionsFound} reviewed manually</span>
                  : <span>no manual review remaining</span>}
              </p>
              <button onClick={() => setIgnoreSuggestionResult(null)} className="text-red-500 hover:text-red-300 text-sm ml-4">✕</button>
            </div>
          )}

          {/* Search box */}
          <div className="relative mb-4 max-w-xs">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 text-sm pointer-events-none">🔍</span>
            <input
              type="text"
              value={nameSearch}
              onChange={e => setNameSearch(e.target.value)}
              placeholder="Search by name…"
              className="w-full bg-gray-900 border border-gray-700 rounded-lg pl-8 pr-8 py-1.5 text-sm text-white placeholder-gray-600 outline-none focus:border-indigo-500 transition-colors"
            />
            {nameSearch && (
              <button
                onClick={() => setNameSearch('')}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 text-xs leading-none"
              >✕</button>
            )}
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-4">
            {[...persons].sort((a, b) => (a.name ?? '').localeCompare(b.name ?? ''))
              .filter(p => !nameSearch.trim() || (p.name ?? '').toLowerCase().includes(nameSearch.toLowerCase()))
              .map(p => {
              const thumb = p.representative_thumbnail
              const thumbUrl = thumb ? '/thumbnails/' + thumb.split('/thumbnails/').pop() : null
              const isRenaming = renamingPersonId === p.id
              const isNamedSelected = namedSelected.has(p.id)
              return (
                <div key={p.id} className="flex flex-col items-center gap-2">
                  {/* Main tile */}
                  <div className="relative group">
                    <button
                      onClick={() => {
                        if (namedSelectMode) {
                          toggleNamedSelect(p.id)
                        } else if (!isRenaming) {
                          onSelectPerson?.(p.id, p.name ?? 'Unknown')
                        }
                      }}
                      title={namedSelectMode ? (isNamedSelected ? 'Deselect' : 'Select') : `View photos of ${p.name}`}
                      className={`w-20 h-20 rounded-xl bg-gray-800 border overflow-hidden flex items-center justify-center transition-colors ${
                        namedSelectMode && isNamedSelected
                          ? 'border-indigo-500 ring-2 ring-indigo-600'
                          : namedSelectMode
                            ? 'border-gray-700 hover:border-indigo-300 cursor-pointer'
                            : 'border-gray-700 hover:border-indigo-400'
                      }`}
                    >
                      {thumbUrl
                        ? <img src={thumbUrl} alt={p.name ?? 'person'} className="w-full h-full object-cover" />
                        : <span className="text-2xl">👤</span>}
                      {/* Selection indicator overlay */}
                      {namedSelectMode && (
                        <span className={`absolute top-1 right-1 w-5 h-5 rounded-full border-2 flex items-center justify-center text-xs font-bold transition-colors ${
                          isNamedSelected ? 'bg-indigo-500 border-indigo-400 text-white' : 'bg-black/40 border-gray-400'
                        }`}>
                          {isNamedSelected ? '✓' : ''}
                        </span>
                      )}
                    </button>
                    {/* Rename icon — top-left on hover (hidden in select mode) */}
                    {!isRenaming && !namedSelectMode && (
                      <button
                        onClick={() => { setRenamingPersonId(p.id); setRenameInput(p.name ?? '') }}
                        title="Rename"
                        className="absolute -top-1 -left-1 bg-gray-700 hover:bg-indigo-700 border border-gray-600 rounded-full w-5 h-5 text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                      >
                        ✎
                      </button>
                    )}
                    {/* Review icon — top-right on hover (hidden in select mode) */}
                    {!namedSelectMode && (
                      <button
                        onClick={() => openReview(p)}
                        title="Review faces"
                        className="absolute -top-1 -right-1 bg-gray-700 hover:bg-gray-600 border border-gray-600 rounded-full w-5 h-5 text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                      >
                        ⋯
                      </button>
                    )}
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
                  {!namedSelectMode && (
                    <button
                      onClick={() => startSuggestions(p.id, p.name!)}
                      title="Find similar faces"
                      className="text-xs text-gray-600 hover:text-indigo-400 transition-colors"
                    >
                      ≈ similar
                    </button>
                  )}
                  {/* ── Connections graph ──────────────────────────────── */}
                  {!namedSelectMode && (
                    <button
                      onClick={() => { setConnectionsPersonId(p.id); setConnectionsPersonName(p.name ?? '') }}
                      title="Show social connection graph"
                      className="text-xs text-gray-600 hover:text-purple-400 transition-colors"
                    >
                      Connections
                    </button>
                  )}
                </div>
              )
            })}
          </div>

          {/* Bulk merge bar — floats at bottom when 2+ named persons are selected */}
          {namedSelectMode && namedSelected.size >= 2 && (
            <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 bg-gray-900 border border-gray-700 rounded-2xl px-5 py-3 shadow-2xl">
              <div className="flex items-center gap-3">
                <span className="text-sm text-gray-300 mr-1">
                  {namedSelected.size} people
                </span>
                <button
                  onClick={() => {
                    const selectedPersons = persons.filter(p => namedSelected.has(p.id))
                    const survivor = selectedPersons.reduce((a, b) => b.photo_count > a.photo_count ? b : a)
                    setNamedMergeNameInput(survivor.name ?? '')
                    setNamedMergeResult(null)
                    setNamedMergeOpen(true)
                  }}
                  disabled={namedBulkWorking}
                  className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium px-4 py-1.5 rounded-lg transition-colors"
                >
                  Merge
                </button>
                <button
                  onClick={exitNamedSelectMode}
                  disabled={namedBulkWorking}
                  className="text-gray-500 hover:text-gray-300 text-sm transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {ignoreSuggestTarget && ignoreSuggestAction && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Ignore suggestion</p>
            <p className="text-gray-400 text-sm mb-5">
              VIP can auto-ignore very close matches and then show you weaker similar faces for manual review.
            </p>
            <div className="flex items-center gap-4 mb-5">
              {ignoreSuggestTarget.representative_thumbnail
                ? <img src={'/thumbnails/' + ignoreSuggestTarget.representative_thumbnail.split('/thumbnails/').pop()} alt="source face" className="w-20 h-20 rounded-xl object-cover border border-gray-600" />
                : <div className="w-20 h-20 rounded-xl bg-gray-800 border border-gray-700 flex items-center justify-center text-gray-500 text-2xl">?</div>}
              <div>
                <p className="text-white text-sm font-medium">
                  After {ignoreSuggestAction === 'delete' ? 'deleting' : 'ignoring'} this face, check for other similar faces?
                </p>
                <p className="text-gray-500 text-xs mt-1">
                  Matches above the threshold are auto-ignored. Lower-confidence matches are shown one by one.
                </p>
              </div>
            </div>
            <div className="mb-6">
              <label className="flex items-center justify-between text-sm text-gray-300 mb-2">
                <span>Auto-ignore threshold</span>
                <span className="font-semibold text-white">{Math.round(ignoreSuggestThreshold * 100)}%</span>
              </label>
              <input
                type="range"
                min={50}
                max={95}
                step={1}
                value={Math.round(ignoreSuggestThreshold * 100)}
                onChange={e => setIgnoreSuggestThreshold(Number(e.target.value) / 100)}
                className="w-full accent-red-500"
              />
              <p className="text-xs text-gray-500 mt-2">
                Similarity {Math.round(ignoreSuggestThreshold * 100)}% and above is ignored automatically.
              </p>
            </div>
            <div className="flex gap-3">
              <button
                onClick={runIgnoreSuggestions}
                disabled={ignoreSuggestWorking}
                className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                {ignoreSuggestWorking ? 'Running…' : 'Yes'}
              </button>
              <button
                onClick={skipIgnoreSuggestions}
                disabled={ignoreSuggestWorking}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                No
              </button>
            </div>
          </div>
        </div>
      )}

      {ignoreSuggestionQueue.length > 0 && ignoreSuggestionSource && (
        <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Ignore this similar face too?</p>
            <p className="text-gray-400 text-xs mb-5">
              Similarity {Math.round(ignoreSuggestionQueue[0].similarity * 100)}% — {ignoreSuggestionQueue.length} suggestion{ignoreSuggestionQueue.length !== 1 ? 's' : ''} remaining.
            </p>
            <div className="flex gap-6 items-center justify-center mb-6">
              <div className="flex flex-col items-center gap-1">
                <div className="w-24 h-24 rounded-xl overflow-hidden bg-gray-800 border border-red-700">
                  {ignoreSuggestionSource.representative_thumbnail
                    ? <img src={'/thumbnails/' + ignoreSuggestionSource.representative_thumbnail.split('/thumbnails/').pop()} alt="source face" className="w-full h-full object-cover" />
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
              onClick={() => {
                setIgnoreSuggestionQueue([])
                setIgnoreSuggestionPersonId(null)
                setIgnoreSuggestionSource(null)
              }}
              className="mt-3 w-full text-center text-xs text-gray-600 hover:text-gray-400"
            >
              Stop reviewing for now
            </button>
          </div>
        </div>
      )}

      {/* ── Connections graph modal ────────────────────────────────────────── */}
      {connectionsPersonId !== null && (
        <ConnectionsGraph
          personId={connectionsPersonId}
          personName={connectionsPersonName}
          onClose={() => setConnectionsPersonId(null)}
          onNavigatePerson={(pid, name) => {
            setConnectionsPersonId(null)
            onSelectPerson?.(pid, name)
          }}
        />
      )}

      {/* ── Find Similar threshold dialog ──────────────────────────────────── */}
      {findSimilarOpen && (
        <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-sm w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Find Similar Faces</p>
            <p className="text-gray-400 text-sm mb-5">
              Scans all {persons.length} named person{persons.length !== 1 ? 's' : ''} against{' '}
              {clusters.length} unnamed cluster{clusters.length !== 1 ? 's' : ''} for visual similarity.
            </p>

            <label className="block text-xs text-gray-400 mb-2">
              Auto-merge threshold
              <span className="ml-1 text-gray-600">(above this → merged without asking)</span>
            </label>
            <div className="flex items-center gap-3 mb-2">
              <input
                type="range" min={50} max={99} step={1}
                value={Math.round(findSimilarThreshold * 100)}
                onChange={e => setFindSimilarThreshold(Number(e.target.value) / 100)}
                className="flex-1 accent-indigo-500"
              />
              <span className="text-sm text-white font-mono w-10 text-right">
                {Math.round(findSimilarThreshold * 100)}%
              </span>
            </div>
            <p className="text-xs text-gray-600 mb-6">
              Matches between 50% and {Math.round(findSimilarThreshold * 100)}% will be shown for manual review.
            </p>

            <div className="flex gap-3">
              <button
                onClick={handleFindSimilar}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                Scan now
              </button>
              <button
                onClick={() => setFindSimilarOpen(false)}
                className="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Bulk suggestion review modal ───────────────────────────────────── */}
      {bulkSuggestionQueue.length > 0 && (() => {
        const current = bulkSuggestionQueue[0]
        const livePerson = persons.find(x => x.id === current.person_id)
        const personThumbRaw = livePerson?.representative_thumbnail ?? current.person_thumbnail
        const personThumbUrl = personThumbRaw
          ? '/thumbnails/' + personThumbRaw.split('/thumbnails/').pop()
          : null
        const clusterThumbUrl = current.representative_thumbnail
          ? '/thumbnails/' + current.representative_thumbnail.split('/thumbnails/').pop()
          : null
        return (
          <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50">
            <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl mx-4">
              <div className="flex items-center justify-between mb-1">
                <p className="text-white font-semibold text-lg">
                  Same person as <span className="text-indigo-400">{current.person_name}</span>?
                </p>
                <span className="text-xs text-gray-600 ml-3 flex-shrink-0">
                  {bulkSuggestionQueue.length} remaining
                </span>
              </div>
              <p className="text-gray-400 text-xs mb-5">
                Similarity {Math.round(current.similarity * 100)}% —{' '}
                cluster of {current.member_count} face{current.member_count !== 1 ? 's' : ''}
                {current.is_high_conf === 1 && <span className="ml-1 text-green-400">✓ high confidence</span>}
              </p>

              <div className="flex gap-6 items-center justify-center mb-6">
                {/* Named person */}
                <div className="flex flex-col items-center gap-1">
                  <div className="w-24 h-24 rounded-xl overflow-hidden bg-gray-800 border border-indigo-600">
                    {personThumbUrl
                      ? <img src={personThumbUrl} alt={current.person_name} className="w-full h-full object-cover" />
                      : <span className="flex items-center justify-center h-full text-gray-500 text-2xl">👤</span>}
                  </div>
                  <span className="text-xs text-indigo-400 font-medium">{current.person_name}</span>
                </div>
                <span className="text-gray-400 text-2xl">≈</span>
                {/* Candidate cluster */}
                <div className="flex flex-col items-center gap-1">
                  <div className="w-24 h-24 rounded-xl overflow-hidden bg-gray-800 border border-gray-600">
                    {clusterThumbUrl
                      ? <img src={clusterThumbUrl} alt="candidate" className="w-full h-full object-cover" />
                      : <span className="flex items-center justify-center h-full text-gray-500 text-2xl">?</span>}
                  </div>
                  <span className="text-xs text-gray-500">{current.member_count} face{current.member_count !== 1 ? 's' : ''}</span>
                </div>
              </div>

              <div className="flex gap-3">
                <button
                  onClick={acceptBulkSuggestion}
                  disabled={bulkSuggestionWorking}
                  className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
                >
                  {bulkSuggestionWorking ? 'Merging…' : '✓ Yes, same person'}
                </button>
                <button
                  onClick={rejectBulkSuggestion}
                  disabled={bulkSuggestionWorking}
                  className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
                >
                  Different person
                </button>
              </div>
              <button
                onClick={() => { setBulkSuggestionQueue([]); setFindSimilarResult(r => r ? { ...r, suggestionsFound: r.suggestionsFound } : null) }}
                className="mt-3 w-full text-center text-xs text-gray-600 hover:text-gray-400 transition-colors"
              >
                Stop reviewing — skip remaining
              </button>
            </div>
          </div>
        )
      })()}

      {/* ── Ignored faces tab ──────────────────────────────────────────── */}
      {activeTab === 'ignored' && (
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider">
              Always-ignored faces ({ignoredPersons.length})
            </h2>
          </div>

          {ignoredLoading && (
            <div className="text-gray-500 text-sm text-center py-10">Loading…</div>
          )}

          {!ignoredLoading && ignoredPersons.length === 0 && (
            <div className="text-gray-500 text-sm text-center mt-12">
              No ignored faces. Use "Always ignore" on an unnamed cluster to hide it permanently.
            </div>
          )}

          {!ignoredLoading && ignoredPersons.length > 0 && (
            <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-4">
              {ignoredPersons.map(p => {
                const thumbUrl = p.representative_thumbnail
                  ? '/thumbnails/' + p.representative_thumbnail.split('/thumbnails/').pop()
                  : null
                const isRestoring = unignoringId === p.id
                return (
                  <div key={p.id} className="flex flex-col items-center gap-2">
                    <div className="relative">
                      <button
                        onClick={() => onSelectPerson?.(p.id, 'Ignored face')}
                        title="View photos"
                        className="w-20 h-20 rounded-xl overflow-hidden bg-gray-800 border border-red-900 opacity-60 hover:opacity-90 hover:border-indigo-500 transition-all"
                      >
                        {thumbUrl
                          ? <img src={thumbUrl} alt="ignored face" className="w-full h-full object-cover" />
                          : <span className="flex items-center justify-center h-full text-gray-600 text-2xl">👤</span>}
                      </button>
                      <span className="absolute bottom-0 right-0 bg-red-900 text-red-300 text-xs px-1 rounded-tl leading-tight">
                        {p.photo_count}
                      </span>
                    </div>
                    <button
                      onClick={() => handleUnignore(p.id)}
                      disabled={isRestoring}
                      className="text-xs px-2.5 py-1 rounded-lg border border-gray-600 bg-gray-800 text-gray-400 hover:text-white hover:border-indigo-400 hover:bg-indigo-900/30 transition-colors disabled:opacity-40"
                    >
                      {isRestoring ? 'Restoring…' : 'Restore'}
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </section>
      )}
    </div>
  )
}

function ClusterTile({ cluster, isNaming, nameInput, saving, personNames, selectMode, isSelected, onToggleSelect, onViewPhotos, onStartNaming, onNameInput, onConfirm, onCancel, onDismiss, onReview }: {
  cluster: Cluster; isNaming: boolean; nameInput: string; saving: boolean
  personNames: string[]
  selectMode: boolean; isSelected: boolean; onToggleSelect: (shiftHeld: boolean) => void
  onViewPhotos: () => void
  onStartNaming: () => void; onNameInput: (v: string) => void; onConfirm: () => void; onCancel: () => void
  onDismiss: () => void
  onReview: () => void
}) {
  const [showSuggestions, setShowSuggestions] = useState(false)
  const thumb = cluster.representative_thumbnail
  const thumbUrl = thumb ? '/thumbnails/' + thumb.split('/thumbnails/').pop() : null

  const filteredNames = (isNaming && showSuggestions && nameInput.trim().length > 0)
    ? personNames.filter(n => n.toLowerCase().includes(nameInput.toLowerCase()))
    : []

  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative group">
        <button
          onClick={e => selectMode ? onToggleSelect(e.shiftKey) : onViewPhotos()}
          title={selectMode ? (isSelected ? 'Deselect' : 'Select') : `View photos`}
          className={'relative w-20 h-20 rounded-xl overflow-hidden bg-gray-800 border transition-colors ' +
            (selectMode
              ? isSelected
                ? 'border-indigo-400 ring-2 ring-indigo-500'
                : 'border-gray-600 hover:border-indigo-300'
              : isNaming ? 'border-indigo-500' : 'border-gray-700 hover:border-indigo-400')}>
          {thumbUrl
            ? <img src={thumbUrl} alt="face" className="w-full h-full object-cover" />
            : <span className="text-gray-500 text-2xl flex items-center justify-center h-full">?</span>}
          <span className="absolute bottom-0 right-0 bg-indigo-700 text-white text-xs px-1 rounded-tl leading-tight">
            {cluster.member_count}
          </span>
          {cluster.is_high_conf === 1 && (
            <span className="absolute top-0 left-0 bg-green-700 text-white text-xs px-1 rounded-br leading-tight">✓</span>
          )}
          {/* Selection checkbox overlay */}
          {selectMode && (
            <span className={`absolute top-1 right-1 w-5 h-5 rounded-full border-2 flex items-center justify-center text-xs font-bold transition-colors ${
              isSelected ? 'bg-indigo-500 border-indigo-400 text-white' : 'bg-black/40 border-gray-400'
            }`}>
              {isSelected ? '✓' : ''}
            </span>
          )}
        </button>
        {/* Name icon — shown on hover, hidden in select mode */}
        {!selectMode && !isNaming && (
          <button
            onClick={e => { e.stopPropagation(); onStartNaming() }}
            title="Name this face"
            className="absolute -top-1 -left-1 bg-gray-700 hover:bg-indigo-700 border border-gray-600 rounded-full w-5 h-5 text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
          >
            ✎
          </button>
        )}
        {/* Review button — shown on hover, hidden in select mode */}
        {!selectMode && !isNaming && (
          <button
            onClick={e => { e.stopPropagation(); onReview() }}
            title="Review faces in this cluster"
            className="absolute -top-1 -right-1 bg-gray-700 hover:bg-gray-600 border border-gray-600 rounded-full w-5 h-5 text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity text-gray-300 hover:text-white leading-none"
          >
            ⋯
          </button>
        )}
        {/* Dismiss button — shown on hover, hidden in select mode and review button position */}
        {!selectMode && (
          <button
            onClick={e => { e.stopPropagation(); onDismiss() }}
            title="Remove this face"
            className="absolute -bottom-1 -right-1 w-5 h-5 bg-gray-700 hover:bg-red-700 border border-gray-600 rounded-full text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity text-gray-300 hover:text-white leading-none"
          >
            ✕
          </button>
        )}
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
        <span className="text-xs text-gray-500 italic">✎ to name</span>
      )}
    </div>
  )
}
