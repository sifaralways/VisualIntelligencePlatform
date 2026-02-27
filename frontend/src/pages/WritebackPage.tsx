/**
 * WritebackPage — review and confirm metadata writes to original files.
 *
 * IMPORTANT: Files must be on local disk (not iCloud stubs) when confirming.
 * ExifTool creates _original backups on first write.
 */

import { useEffect, useState } from 'react'
import { api, WritebackItem } from '../api/client'

export default function WritebackPage() {
  const [items, setItems] = useState<WritebackItem[]>([])
  const [warning, setWarning] = useState('')
  const [loading, setLoading] = useState(true)
  const [confirming, setConfirming] = useState(false)
  const [result, setResult] = useState<{ written: number; failed: number } | null>(null)

  async function loadPreview() {
    setLoading(true)
    setResult(null)
    try {
      const p = await api.writeback.preview()
      setItems(p.items)
      setWarning(p.warning)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadPreview() }, [])

  async function confirmAll() {
    setConfirming(true)
    try {
      const res = await api.writeback.confirm()
      setResult(res)
      loadPreview()
    } finally {
      setConfirming(false)
    }
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-xl font-semibold mb-2">Write to Files</h1>
      <p className="text-sm text-gray-400 mb-6">
        Names and tags will be written into the original RAW files using ExifTool.
        Review below, then confirm.
      </p>

      {warning && (
        <div className="bg-yellow-900/40 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200 mb-4">
          ⚠️ {warning}
        </div>
      )}

      {result && (
        <div className={`rounded-lg px-4 py-3 text-sm mb-4 ${
          result.failed > 0 ? 'bg-red-900/40 border border-red-700 text-red-200' : 'bg-green-900/40 border border-green-700 text-green-200'
        }`}>
          ✅ Written: {result.written} &nbsp;&nbsp; ❌ Failed: {result.failed}
        </div>
      )}

      {loading ? (
        <p className="text-gray-400 text-sm">Loading preview…</p>
      ) : items.length === 0 ? (
        <p className="text-gray-500 text-sm">No pending writes. Name some people first.</p>
      ) : (
        <>
          <div className="mb-4 text-sm text-gray-300">
            {items.length} file{items.length !== 1 ? 's' : ''} pending
          </div>

          {/* Preview list */}
          <div className="space-y-2 mb-6 max-h-96 overflow-y-auto pr-1">
            {items.map(item => (
              <div key={item.queue_id} className="bg-gray-800 rounded-lg px-4 py-3">
                <div className="text-xs text-gray-400 truncate mb-1">
                  {item.file_path.split('/').slice(-3).join(' / ')}
                </div>
                {Object.entries(item.fields).map(([tag, values]) => (
                  <div key={tag} className="text-xs">
                    <span className="text-indigo-400">{tag}</span>
                    <span className="text-gray-300 ml-2">{values.join(', ')}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>

          <button
            onClick={confirmAll}
            disabled={confirming}
            className="bg-green-700 hover:bg-green-600 disabled:opacity-40 text-white rounded-lg px-6 py-2.5 text-sm font-medium"
          >
            {confirming ? 'Writing…' : `Confirm & Write ${items.length} file${items.length !== 1 ? 's' : ''}`}
          </button>

          <p className="mt-3 text-xs text-gray-500">
            ExifTool will create .CR3_original backups automatically (first write only).
          </p>
        </>
      )}
    </div>
  )
}
