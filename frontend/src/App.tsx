import { useState } from 'react'
import PeoplePage from './pages/PeoplePage'
import SearchPage from './pages/SearchPage'
import WritebackPage from './pages/WritebackPage'
import PipelinePage from './pages/PipelinePage'
import AdminPage from './pages/AdminPage'
import './index.css'

type Page = 'people' | 'search' | 'writeback' | 'pipeline' | 'admin'

const NAV: { id: Page; label: string }[] = [
  { id: 'people',   label: '👤 People' },
  { id: 'search',   label: '🔍 Search' },
  { id: 'writeback',label: '💾 Write to Files' },
  { id: 'pipeline', label: '⚙️ Pipeline' },
  { id: 'admin',    label: '🛠 Admin' },
]

export default function App() {
  const [page, setPage] = useState<Page>('people')

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 flex flex-col">
      {/* Header */}
      <header className="border-b border-gray-800 px-6 py-3 flex items-center gap-6">
        <span className="font-semibold text-white tracking-wide">Visual Intelligence Platform</span>
        <nav className="flex gap-1 ml-4">
          {NAV.map(n => (
            <button
              key={n.id}
              onClick={() => setPage(n.id)}
              className={`px-4 py-1.5 rounded text-sm transition-colors
                ${ page === n.id
                  ? 'bg-indigo-600 text-white'
                  : 'text-gray-400 hover:text-white hover:bg-gray-800'
                }`}
            >
              {n.label}
            </button>
          ))}
        </nav>
      </header>

      {/* Main */}
      <main className="flex-1 p-6">
        {page === 'people'    && <PeoplePage />}
        {page === 'search'    && <SearchPage />}
        {page === 'writeback' && <WritebackPage />}
        {page === 'pipeline'  && <PipelinePage />}
        {page === 'admin'     && <AdminPage />}
      </main>
    </div>
  )
}
