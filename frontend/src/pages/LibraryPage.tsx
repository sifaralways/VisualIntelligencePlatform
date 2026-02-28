/**
 * LibraryPage — the main photo library view.
 *
 * If the library is empty, shows a folder picker + scan button.
 * Otherwise shows the full photo grid with a scan action in the header.
 */

import { useState, useEffect } from 'react'
import { api } from '../api/client'
import PhotoGrid from '../components/PhotoGrid'

interface Props {
  /** Called after a scan starts so parent can show pipeline status */
  onScanStarted?: () => void
}

export default function LibraryPage({ onScanStarted }: Props) {
  const [folder,       setFolder]       = useState('')
  const [totalPhotos,  setTotalPhotos]  = useState<number | null>(null)
  const [scanning,     setScanning]     = useState(false)
  const [scanMsg,      setScanMsg]      = useState<string | null>(null)
  const [scanError,    setScanError]    = useState<string | null>(null)

  // Check if library has any photos on mount
  useEffect(() => {
    api.media.count().then(r => setTotalPhotos(r.count)).catch(() => setTotalPhotos(0))
  }, [scanning])

  const startScan = async () => {
    if (!folder.trim()) return
    setScanning(true)
    setScanMsg(null)
    setScanError(null)
    try {
      await api.pipeline.scan(folder.trim())
      setScanMsg('Pipeline started! Switch to the Pipeline tab to monitor progress.')
      onScanStarted?.()
    } catch (e: unknown) {
      setScanError(e instanceof Error ? e.message : 'Scan failed')
    } finally {
      setScanning(false)
    }
  }

  const FolderBar = (
    <div className="flex items-center gap-2 flex-wrap">
      <input
        type="text"
        value={folder}
        onChange={e => setFolder(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') startScan() }}
        placeholder="/Volumes/SSD/Photos or ~/Pictures"
        className="flex-1 min-w-52 px-3 py-1.5 rounded bg-gray-800 border border-gray-700 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500"
      />
      <button
        onClick={startScan}
        disabled={scanning || !folder.trim()}
        className="px-4 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium transition-colors"
      >
        {scanning ? 'Starting…' : '⚙️ Scan'}
      </button>
      {scanMsg   && <p className="text-green-400 text-xs w-full">{scanMsg}</p>}
      {scanError && <p className="text-red-400  text-xs w-full">{scanError}</p>}
    </div>
  )

  // Empty library — full-screen prompt
  if (totalPhotos === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-8 py-20">
        <div className="text-center space-y-2">
          <p className="text-6xl">📂</p>
          <h2 className="text-2xl font-bold text-white">Welcome to VIP</h2>
          <p className="text-gray-400 text-sm max-w-sm">
            Point VIP at a folder containing your RAW photos. It will scan, detect
            faces, and tag everything automatically.
          </p>
        </div>
        <div className="w-full max-w-lg space-y-3">
          <label className="block text-xs text-gray-400 uppercase tracking-widest">
            Photo folder path
          </label>
          <div className="flex gap-2">
            <input
              type="text"
              value={folder}
              onChange={e => setFolder(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') startScan() }}
              placeholder="/Volumes/SSD/Photos"
              className="flex-1 px-4 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500"
            />
            <button
              onClick={startScan}
              disabled={scanning || !folder.trim()}
              className="px-6 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white font-semibold transition-colors"
            >
              {scanning ? '…' : 'Scan'}
            </button>
          </div>
          {scanMsg   && <p className="text-green-400 text-sm">{scanMsg}</p>}
          {scanError && <p className="text-red-400  text-sm">{scanError}</p>}
          <p className="text-xs text-gray-500">
            Supports CR3, ARW, NEF, DNG, RW2, ORF, RAF. iCloud stubs are skipped
            automatically.
          </p>
        </div>
      </div>
    )
  }

  // Library has photos — show grid with scan bar in header
  return (
    <PhotoGrid
      title="Library"
      headerSlot={FolderBar}
    />
  )
}
