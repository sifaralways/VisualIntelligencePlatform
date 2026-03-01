import { useState, useEffect, useRef, useCallback } from 'react'
import LibraryPage from './pages/LibraryPage'
import PeoplePage from './pages/PeoplePage'
import DiscoverPage from './pages/DiscoverPage'
import TagsPage from './pages/TagsPage'
import WritebackPage from './pages/WritebackPage'
import PipelinePage from './pages/PipelinePage'
import AdminPage from './pages/AdminPage'
import QualityPage from './pages/QualityPage'
import PhotoGrid from './components/PhotoGrid'
import { api } from './api/client'
import type { MediaFilter, WsEvent, MergeSuggestionItem } from './api/client'
import './index.css'

// ---------------------------------------------------------------------------
// View state machine
// ---------------------------------------------------------------------------

type SidebarSection = 'library' | 'people' | 'animals' | 'places' | 'things' | 'tags' | 'pipeline' | 'writeback' | 'admin' | 'quality'

interface FilteredView {
  title: string
  filter: MediaFilter
  backTo: SidebarSection
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App() {
  const [section, setSection]       = useState<SidebarSection>('library')
  const [filtered, setFiltered]     = useState<FilteredView | null>(null)

  // ── Global pipeline notifications ─────────────────────────────────────────
  const wsRef = useRef<WebSocket | null>(null)
  const [qualityCount,     setQualityCount]     = useState<number | null>(null)
  const [mergeSuggestions, setMergeSuggestions] = useState<MergeSuggestionItem[]>([])
  const [mergeWorking,     setMergeWorking]     = useState(false)

  useEffect(() => {
    function connect() {
      const ws = new WebSocket('ws://localhost:7474/ws/progress')
      wsRef.current = ws
      ws.onmessage = (msg) => {
        try {
          const ev: WsEvent = JSON.parse(msg.data)
          if (ev.event === 'merge_suggestions' && ev.suggestions?.length) {
            setMergeSuggestions(prev => {
              // Append only new ones (by cluster_id)
              const existingIds = new Set(prev.map(s => s.cluster_id))
              const fresh = ev.suggestions!.filter(s => !existingIds.has(s.cluster_id))
              return [...prev, ...fresh]
            })
          }
          if (ev.event === 'quality_issues_found' && ev.count) {
            setQualityCount(ev.count)
          }
        } catch {}
      }
      ws.onclose = () => {
        // Auto-reconnect after 3 s
        setTimeout(connect, 3000)
      }
    }
    connect()
    return () => wsRef.current?.close()
  }, [])

  // Handle merge suggestion: accept (merge) or skip
  const handleMergeAction = useCallback(async (action: 'merge' | 'skip') => {
    const top = mergeSuggestions[0]
    if (!top) return
    setMergeWorking(true)
    try {
      if (action === 'merge') {
        await api.persons.addCluster(top.person_id, top.cluster_id)
      } else {
        await api.persons.rejectSuggestion(top.person_id, top.cluster_id)
      }
    } catch { /* ignore */ }
    setMergeWorking(false)
    setMergeSuggestions(prev => prev.slice(1))
  }, [mergeSuggestions])

  /** Navigate to a filtered photo grid (e.g. photos of Alice, photos of dogs) */
  function openFiltered(filter: MediaFilter, title: string, backTo: SidebarSection) {
    setFiltered({ filter, title, backTo })
  }

  /** Go back from filtered view to the discover/people section */
  function closeFiltered() {
    setFiltered(null)
  }

  /** Switch sidebar section — clears any filtered sub-view */
  function navigate(s: SidebarSection) {
    setSection(s)
    setFiltered(null)
  }

  // ── Main content ──────────────────────────────────────────────────────────

  let mainContent: React.ReactNode

  if (filtered) {
    mainContent = (
      <div className="flex flex-col gap-4 h-full">
        <button
          onClick={closeFiltered}
          className="self-start flex items-center gap-1.5 text-sm text-gray-400 hover:text-white transition-colors"
        >
          ← Back
        </button>
        <PhotoGrid filter={filtered.filter} title={filtered.title} />
      </div>
    )
  } else {
    switch (section) {
      case 'library':
        mainContent = <LibraryPage onScanStarted={() => navigate('pipeline')} />
        break
      case 'people':
        mainContent = (
          <PeoplePage
            onSelectPerson={(id, name) =>
              openFiltered({ person_id: id }, `👤 ${name}`, 'people')
            }
          />
        )
        break
      case 'animals':
        mainContent = (
          <DiscoverPage
            category="animal"
            onSelectTag={(cat, label, title) =>
              openFiltered({ tag_category: cat, tag_label: label }, title, 'animals')
            }
          />
        )
        break
      case 'places':
        mainContent = (
          <DiscoverPage
            category="place"
            onSelectTag={(cat, label, title) =>
              openFiltered({ tag_category: cat, tag_label: label }, title, 'places')
            }
          />
        )
        break
      case 'things':
        mainContent = (
          <DiscoverPage
            category="object"
            onSelectTag={(cat, label, title) =>
              openFiltered({ tag_category: cat, tag_label: label }, title, 'things')
            }
          />
        )
        break
      case 'tags':
        mainContent = <TagsPage />
        break
      case 'pipeline':
        mainContent = <PipelinePage />
        break
      case 'writeback':
        mainContent = <WritebackPage />
        break
      case 'quality':
        mainContent = <QualityPage />
        break
      case 'admin':
        mainContent = <AdminPage />
        break
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 flex flex-col">
      {/* ── Top bar ── */}
      <header className="h-11 border-b border-gray-800 flex items-center px-4 gap-3 shrink-0">
        <span className="font-semibold text-white text-sm tracking-wide">📸 VIP</span>
        <span className="text-gray-600 text-xs">Visual Intelligence Platform</span>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* ── Sidebar ── */}
        <aside className="w-48 shrink-0 border-r border-gray-800 py-3 flex flex-col gap-1 overflow-y-auto bg-gray-950">
          <NavGroup label="Library">
            <NavItem id="library"   icon="📚" label="All Photos"   active={section === 'library'   && !filtered} onClick={() => navigate('library')} />
          </NavGroup>

          <NavGroup label="People & Places">
            <NavItem id="people"    icon="👤" label="People"       active={(section === 'people'   || filtered?.backTo === 'people')  } onClick={() => navigate('people')} />
            <NavItem id="animals"   icon="🐾" label="Animals"      active={(section === 'animals'  || filtered?.backTo === 'animals') } onClick={() => navigate('animals')} />
            <NavItem id="places"    icon="📍" label="Places"       active={(section === 'places'   || filtered?.backTo === 'places')  } onClick={() => navigate('places')} />
            <NavItem id="things"    icon="📦" label="Things"       active={(section === 'things'   || filtered?.backTo === 'things')  } onClick={() => navigate('things')} />
            <NavItem id="tags"      icon="🏷️" label="All Tags"     active={section === 'tags'      && !filtered} onClick={() => navigate('tags')} />
          </NavGroup>

          <NavGroup label="Tools">
            <NavItem id="pipeline"  icon="⚙️" label="Pipeline"     active={section === 'pipeline'  && !filtered} onClick={() => navigate('pipeline')} />
            <NavItem id="writeback" icon="💾" label="Write to Files" active={section === 'writeback' && !filtered} onClick={() => navigate('writeback')} />
            <NavItem id="quality"   icon="🎯" label="Quality"       active={section === 'quality'   && !filtered} onClick={() => navigate('quality')} />
            <NavItem id="admin"     icon="🛠" label="Admin"         active={section === 'admin'     && !filtered} onClick={() => navigate('admin')} />
          </NavGroup>
        </aside>

        {/* ── Main content ── */}
        <main className="flex-1 overflow-y-auto p-6">
          {mainContent}
        </main>
      </div>

      {/* ── Global notification stack (bottom-right) ── */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-3 max-w-sm w-full pointer-events-none">

        {/* Quality issues banner */}
        {qualityCount !== null && qualityCount > 0 && (
          <div className="pointer-events-auto bg-orange-950 border border-orange-700 rounded-xl px-4 py-3 shadow-2xl flex items-center gap-3">
            <span className="text-orange-400 text-lg">🔍</span>
            <div className="flex-1 min-w-0">
              <p className="text-white text-sm font-medium">Quality issues found</p>
              <p className="text-orange-300 text-xs">{qualityCount} photo{qualityCount > 1 ? 's' : ''} may be blurry or have closed eyes</p>
            </div>
            <button
              onClick={() => { navigate('quality'); setQualityCount(null) }}
              className="text-xs bg-orange-700 hover:bg-orange-600 text-white rounded-lg px-3 py-1.5 font-medium whitespace-nowrap"
            >
              Review
            </button>
            <button
              onClick={() => setQualityCount(null)}
              className="text-gray-400 hover:text-white text-lg leading-none px-1"
            >
              ×
            </button>
          </div>
        )}

        {/* Merge suggestion card */}
        {mergeSuggestions.length > 0 && (() => {
          const s = mergeSuggestions[0]
          return (
            <div className="pointer-events-auto bg-gray-900 border border-indigo-700 rounded-xl p-4 shadow-2xl">
              <p className="text-white text-sm font-semibold mb-1">Same person?</p>
              <p className="text-gray-400 text-xs mb-3">
                This cluster ({s.member_count} photo{s.member_count > 1 ? 's' : ''}) looks like{' '}
                <span className="text-indigo-300 font-medium">{s.person_name}</span>
                <span className="ml-1 text-gray-500">({Math.round(s.similarity * 100)}% match)</span>
              </p>
              <div className="flex items-center gap-3 mb-4">
                {/* Person face */}
                <div className="w-16 h-16 rounded-xl bg-gray-800 border border-gray-700 overflow-hidden flex items-center justify-center">
                  {s.person_face_id ? (
                    <img src={`/api/faces/${s.person_face_id}/thumbnail`} alt={s.person_name} className="w-full h-full object-cover" onError={e => { (e.target as HTMLImageElement).style.display='none' }} />
                  ) : (
                    <span className="text-gray-500 text-xs text-center leading-tight px-1">{s.person_name}</span>
                  )}
                </div>
                <span className="text-gray-500 text-xl">≈</span>
                {/* Cluster face */}
                <div className="w-16 h-16 rounded-xl bg-gray-800 border border-gray-700 overflow-hidden flex items-center justify-center">
                  {s.cluster_face_id ? (
                    <img src={`/api/faces/${s.cluster_face_id}/thumbnail`} alt="" className="w-full h-full object-cover" onError={e => { (e.target as HTMLImageElement).style.display='none' }} />
                  ) : (
                    <span className="w-full h-full flex items-center justify-center text-gray-600 text-xs">?</span>
                  )}
                </div>
              </div>
              {mergeSuggestions.length > 1 && (
                <p className="text-gray-600 text-[10px] mb-2">+{mergeSuggestions.length - 1} more suggestion{mergeSuggestions.length > 2 ? 's' : ''}</p>
              )}
              <div className="flex gap-2">
                <button
                  onClick={() => handleMergeAction('merge')}
                  disabled={mergeWorking}
                  className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium rounded-lg py-2"
                >
                  Merge
                </button>
                <button
                  onClick={() => handleMergeAction('skip')}
                  disabled={mergeWorking}
                  className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-200 text-sm font-medium rounded-lg py-2"
                >
                  Different person
                </button>
              </div>
            </div>
          )
        })()}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sidebar sub-components
// ---------------------------------------------------------------------------

function NavGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="px-2 pb-1">
      <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest px-2 py-1.5">{label}</p>
      {children}
    </div>
  )
}

function NavItem({ id, icon, label, active, onClick }: {
  id: string; icon: string; label: string; active: boolean; onClick: () => void
}) {
  return (
    <button
      key={id}
      onClick={onClick}
      className={`w-full flex items-center gap-2.5 px-3 py-1.5 rounded-lg text-sm transition-colors text-left
        ${active
          ? 'bg-indigo-600/80 text-white'
          : 'text-gray-400 hover:text-white hover:bg-gray-800'}`}
    >
      <span className="text-base leading-none">{icon}</span>
      <span className="truncate">{label}</span>
    </button>
  )
}

