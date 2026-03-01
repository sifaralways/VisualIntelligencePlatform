/**
 * PipelinePage — trigger and monitor the ingest pipeline.
 */

import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { WsEvent } from '../api/client'

export default function PipelinePage() {
  const [folder, setFolder] = useState('')
  const [status, setStatus] = useState<string>('idle')
  const [events, setEvents] = useState<WsEvent[]>([])
  const wsRef = useRef<WebSocket | null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Connect to progress WebSocket
    const ws = new WebSocket(`ws://localhost:7474/ws/progress`)
    wsRef.current = ws
    ws.onmessage = (msg) => {
      try {
        const ev: WsEvent = JSON.parse(msg.data)
        if (ev.event !== 'ping') {
          setEvents(prev => [...prev.slice(-200), ev])
          if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
        }
        if (ev.event === 'pipeline_complete') setStatus('idle')
        if (ev.event === 'pipeline_start')    setStatus('running')
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

  async function rescanAll() {
    setEvents([])
    setStatus('running')
    try {
      await api.pipeline.rescan()
    } catch (e: any) {
      setStatus('error')
      setEvents([{ event: 'error', message: e.message ?? 'Rescan failed' }])
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

      {/* Rescan all existing library photos */}
      <div className="mb-4 p-3 bg-gray-900 border border-gray-800 rounded-lg flex items-center justify-between gap-4">
        <div>
          <p className="text-sm text-gray-200 font-medium">Rescan Entire Library</p>
          <p className="text-xs text-gray-500">Re-runs all pipeline phases on every photo already in the library (quality, faces, tags).</p>
        </div>
        <button
          onClick={rescanAll}
          disabled={status === 'running'}
          className="shrink-0 bg-yellow-700 hover:bg-yellow-600 disabled:opacity-40 text-white rounded-lg px-4 py-2 text-sm font-medium"
        >
          Rescan All
        </button>
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
            {ev.phase    && <span className="mr-2">phase={ev.phase}</span>}
            {ev.done != null && ev.total != null && (
              <span className="mr-2">{ev.done}/{ev.total}</span>
            )}
            {ev.scanned  != null && <span className="mr-2">scanned={ev.scanned}</span>}
            {ev.skipped  != null && <span className="mr-2">skipped={ev.skipped}</span>}
            {ev.clusters != null && <span className="mr-2">clusters={ev.clusters}</span>}
            {ev.merged   != null && <span className="mr-2 text-green-400">auto-merged={ev.merged}</span>}
            {ev.count    != null && <span className="mr-2 text-orange-400">quality-issues={ev.count}</span>}
            {ev.message  && <span className="text-yellow-400">{ev.message}</span>}
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
