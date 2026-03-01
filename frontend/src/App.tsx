import { useState } from 'react'
import LibraryPage from './pages/LibraryPage'
import PeoplePage from './pages/PeoplePage'
import DiscoverPage from './pages/DiscoverPage'
import TagsPage from './pages/TagsPage'
import WritebackPage from './pages/WritebackPage'
import PipelinePage from './pages/PipelinePage'
import AdminPage from './pages/AdminPage'
import QualityPage from './pages/QualityPage'
import PhotoGrid from './components/PhotoGrid'
import type { MediaFilter } from './api/client'
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

