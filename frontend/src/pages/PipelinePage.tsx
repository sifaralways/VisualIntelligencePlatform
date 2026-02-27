/**
 * PipelinePage — trigger and monitor the ingest pipeline.
 */

import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'

interface ProgressEvent {
  event: string
  done?: number
  total?: number
  phase?: string
  scanned?: number
  skipped?: number
  processed?: number
  clusters?: number
  message?: string
}

export default function PipelinePage() {
  const [folder, setFolder] = useState('')
  const [status, setStatus] = useState<string>('idle')
  const [events, setEvents] = useState<ProgressEvent[]>([])
  const wsRef = useRef<WebSocket | null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Connect to progress WebSocket
    const ws = new WebSocket(`ws://localhost:7474/ws/progress`)
    wsRef.current = ws
    ws.onmessage = (msg) => {
      try {
        const ev: ProgressEvent = JSON.parse(msg.data)
        if (ev.event !== 'ping') {
          setEvents(prev => [...prev.slice(-200), ev])
          if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
        }
      } catch {}
    }
    return () => ws.close()
  }, [])

  async function startScan() {
    if (!folder.trim()) return
    setEvents([])
    setStatus('running')
    try {
      await api.pipeline.scan(folder.trim())
    } catch (e) {
      setStatus('error')
    }
  }

  async function refreshStatus() {
    const s = await api.pipeline.status()
    setStatus(s.status)
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-xl font-semibold mb-6">Pipeline</h1>

      {/* Folder input */}
      <div className="mb-4">
        <label className="block text-sm text-gray-400 mb-1">Media folder path</label>
        <div className="flex gap-2">
          <input
            value={folder}
            onChange={e => setFolder(e.target.value)}
            placeholder="/Volumes/SSD/Photos"
            className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
          />
          <button
            onClick={startScan}
            disabled={status === 'running' || !folder.trim()}
            className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg px-4 py-2 text-sm font-medium"
          >
            {status === 'running' ? 'Running…' : 'Start Scan'}
          </button>
          <button
            onClick={refreshStatus}
            className="bg-gray-700 hover:bg-gray-600 text-white rounded-lg px-3 py-2 text-sm"
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Status badge */}
      <div className="mb-4 flex items-center gap-2">
        <span className="text-sm text-gray-400">Status:</span>
        <span className={`text-xs px-2 py-0.5 rounded font-medium ${
          status === 'idle'    ? 'bg-gray-700 text-gray-300' :
          status === 'running' ? 'bg-yellow-600 text-white' :
          status === 'error'   ? 'bg-red-700 text-white' :
                                  'bg-green-700 text-white'
        }`}>
          {status}
        </span>
      </div>

      {/* Live event log */}
      <div
        ref={logRef}
        className="bg-gray-900 border border-gray-700 rounded-lg p-3 h-80 overflow-y-auto font-mono text-xs text-gray-300 space-y-1"
      >
        {events.length === 0 && (
          <span className="text-gray-600">Pipeline events will appear here…</span>
        )}
        {events.map((ev, i) => (
          <div key={i} className="leading-relaxed">
            <span className="text-indigo-400 mr-2">[{ev.event}]</span>
            {ev.phase && <span className="mr-2">phase={ev.phase}</span>}
            {ev.done != null && ev.total != null && (
              <span className="mr-2">{ev.done}/{ev.total}</span>
            )}
            {ev.scanned != null && <span className="mr-2">scanned={ev.scanned}</span>}
            {ev.skipped != null && <span className="mr-2">skipped={ev.skipped}</span>}
            {ev.clusters != null && <span className="mr-2">clusters={ev.clusters}</span>}
            {ev.message && <span className="text-yellow-400">{ev.message}</span>}
          </div>
        ))}
      </div>

      <p className="mt-4 text-xs text-gray-500">
        Tip: Files must be fully downloaded from iCloud before scanning.
        iCloud stubs will be detected and skipped automatically.
      </p>
    </div>
  )
}
