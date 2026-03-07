/**
 * RemoteServersPanel — Admin section for setting up SSH remote write servers.
 *
 * Step-by-step wizard:
 *   1  Server details  (host, port, user, label)
 *   2  SSH key         (generate + show pubkey | auto-deploy via password)
 *   3  Test SSH        (verify passwordless connection works)
 *   4  Verify ExifTool (confirm exiftool is installed on remote)
 *   5  Path mapping    (local prefix → remote prefix) + test a real file
 *   6  Concurrency + save
 *
 * Saved servers are shown as cards.  Each card has Edit / Delete / Enable toggle.
 */

import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { RemoteServer, RemoteServerConfig } from '../api/client'

// ─── Wizard step definitions ─────────────────────────────────────────────────

const STEPS = [
  { id: 1, title: 'Server details' },
  { id: 2, title: 'SSH key' },
  { id: 3, title: 'Test connection' },
  { id: 4, title: 'Verify ExifTool' },
  { id: 5, title: 'Path mapping' },
  { id: 6, title: 'Review & save' },
] as const

type StepId = (typeof STEPS)[number]['id']

// ─── Result pill ─────────────────────────────────────────────────────────────

function Pill({ ok, msg }: { ok: boolean; msg: string }) {
  return (
    <div className={`mt-3 rounded-lg px-4 py-2.5 text-sm ${
      ok
        ? 'bg-green-900/30 border border-green-700 text-green-300'
        : 'bg-red-900/30 border border-red-700 text-red-300'
    }`}>
      {ok ? '✓ ' : '✗ '}{msg}
    </div>
  )
}

// ─── Input helper ────────────────────────────────────────────────────────────

function Field({
  label, hint, value, onChange, type = 'text', placeholder = '',
}: {
  label: string; hint?: string; value: string; onChange: (v: string) => void
  type?: string; placeholder?: string
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-400 mb-1">{label}</label>
      {hint && <p className="text-xs text-gray-600 mb-1.5">{hint}</p>}
      <input
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500"
      />
    </div>
  )
}

// ─── Server card ─────────────────────────────────────────────────────────────

function ServerCard({
  server, onEdit, onDelete, onToggle,
}: {
  server: RemoteServer
  onEdit: () => void
  onDelete: () => void
  onToggle: () => void
}) {
  const enabled = server.enabled === 1
  const [checking, setChecking] = useState(false)
  const [checkPath, setCheckPath] = useState('')
  const [checkResult, setCheckResult] = useState<{ message: string; writable: boolean; stat: string } | null>(null)

  async function doCheckWrite() {
    setChecking(true)
    setCheckResult(null)
    try {
      const r = await api.remote.checkWrite(server.id, checkPath.trim() || undefined)
      setCheckResult({ message: r.message, writable: r.writable, stat: r.stat })
    } catch (e: any) {
      setCheckResult({ message: `Error: ${e.message ?? e}`, writable: false, stat: '' })
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className={`bg-gray-900 border rounded-xl px-5 py-4 ${
      enabled ? 'border-indigo-700' : 'border-gray-800'
    }`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-medium text-white">{server.label}</span>
            <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
              enabled ? 'bg-green-900/50 text-green-400' : 'bg-gray-800 text-gray-500'
            }`}>
              {enabled ? 'Active' : 'Disabled'}
            </span>
          </div>
          <p className="text-xs text-gray-500 font-mono">
            {server.user}@{server.host}:{server.port}
          </p>
          <div className="mt-2 text-xs text-gray-600 space-y-0.5">
            <div>
              <span className="text-gray-500">Local:  </span>
              <span className="font-mono">{server.local_path_prefix}</span>
            </div>
            <div>
              <span className="text-gray-500">Remote: </span>
              <span className="font-mono">{server.remote_path_prefix}</span>
            </div>
            <div>
              <span className="text-gray-500">Workers: </span>
              {server.writeback_concurrency}
            </div>
          </div>

          {checkResult && (
            <div className={`mt-3 rounded-lg px-3 py-2 text-xs ${
              checkResult.writable
                ? 'bg-green-900/30 border border-green-800 text-green-300'
                : 'bg-red-900/30 border border-red-800 text-red-300'
            }`}>
              <div>{checkResult.message}</div>
              {checkResult.stat && (
                <pre className="mt-1 text-gray-500 text-[10px] whitespace-pre-wrap">{checkResult.stat}</pre>
              )}
              {!checkResult.writable && (
                <div className="mt-1 text-yellow-400 text-[10px]">
                  If files are read-only, run on remote: <code className="font-mono">chmod -R +w '{server.remote_path_prefix}'</code>
                </div>
              )}
            </div>
          )}

          {/* Path input for targeted file check */}
          <div className="mt-3 flex gap-2 items-center">
            <input
              type="text"
              value={checkPath}
              onChange={e => setCheckPath(e.target.value)}
              placeholder={`${server.remote_path_prefix}/... (leave blank for prefix dir)`}
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 placeholder-gray-600 font-mono"
            />
          </div>
        </div>
        <div className="flex flex-col gap-2 shrink-0">
          <button
            onClick={onToggle}
            className={`text-xs px-2.5 py-1 rounded-lg border transition-colors ${
              enabled
                ? 'border-gray-600 text-gray-400 hover:bg-gray-800'
                : 'border-indigo-600 text-indigo-400 hover:bg-indigo-900/30'
            }`}
          >
            {enabled ? 'Disable' : 'Enable'}
          </button>
          <button
            onClick={doCheckWrite}
            disabled={checking}
            className="text-xs px-2.5 py-1 rounded-lg border border-yellow-800 text-yellow-400 hover:bg-yellow-900/20 disabled:opacity-40 transition-colors"
          >
            {checking ? 'Checking…' : 'Check Write'}
          </button>
          <button
            onClick={onEdit}
            className="text-xs px-2.5 py-1 rounded-lg border border-gray-700 text-gray-400 hover:bg-gray-800 transition-colors"
          >
            Edit
          </button>
          <button
            onClick={onDelete}
            className="text-xs px-2.5 py-1 rounded-lg border border-red-900 text-red-400 hover:bg-red-900/20 transition-colors"
          >
            Remove
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Wizard modal ─────────────────────────────────────────────────────────────

interface WizardState {
  label: string
  host: string
  port: string
  user: string
  sshKeyPath: string
  publicKey: string
  password: string
  localPrefix: string
  remotePrefix: string
  samplePath: string
  concurrency: string
  editingId: number | null
}

const BLANK: WizardState = {
  label: 'Remote Server',
  host: '', port: '22', user: '', sshKeyPath: '', publicKey: '',
  password: '', localPrefix: '', remotePrefix: '', samplePath: '',
  concurrency: '4', editingId: null,
}

function Wizard({
  initial,
  onClose,
  onSaved,
}: {
  initial: WizardState
  onClose: () => void
  onSaved: () => void
}) {
  const [step, setStep] = useState<StepId>(1)
  const [w, setW] = useState<WizardState>(initial)
  const set = (k: keyof WizardState) => (v: string) => setW(prev => ({ ...prev, [k]: v }))

  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  function clearResult() { setResult(null) }

  // ── Step 1 → Step 2: generate SSH key ────────────────────────────────────
  async function handleGenerateKey() {
    if (!w.host.trim()) { setResult({ ok: false, msg: 'Enter a hostname first.' }); return }
    setBusy(true); clearResult()
    try {
      const res = await api.remote.generateKey(w.host.trim())
      setW(prev => ({ ...prev, sshKeyPath: res.ssh_key_path, publicKey: res.public_key }))
      setResult({ ok: true, msg: res.already_existed ? 'Existing VIP key found and reused.' : 'New SSH key generated.' })
    } catch (e: unknown) {
      setResult({ ok: false, msg: (e as Error).message ?? 'Key generation failed.' })
    } finally { setBusy(false) }
  }

  // ── Step 2: auto-deploy via password ─────────────────────────────────────
  async function handleDeployKey() {
    if (!w.password) { setResult({ ok: false, msg: 'Enter the remote password.' }); return }
    setBusy(true); clearResult()
    try {
      const res = await api.remote.deployKey({
        host: w.host.trim(), port: Number(w.port), user: w.user.trim(), password: w.password,
      })
      setResult({ ok: true, msg: res.message })
      setW(prev => ({ ...prev, password: '' }))  // discard password immediately
    } catch (e: unknown) {
      setResult({ ok: false, msg: (e as Error).message ?? 'Key deployment failed.' })
    } finally { setBusy(false) }
  }

  // ── Step 3: test SSH ──────────────────────────────────────────────────────
  async function handleTestSSH() {
    setBusy(true); clearResult()
    try {
      const res = await api.remote.testSSH({
        host: w.host.trim(), port: Number(w.port), user: w.user.trim(), ssh_key_path: w.sshKeyPath,
      })
      setResult({ ok: true, msg: res.message })
    } catch (e: unknown) {
      setResult({ ok: false, msg: (e as Error).message ?? 'SSH test failed.' })
    } finally { setBusy(false) }
  }

  // ── Step 4: test ExifTool ─────────────────────────────────────────────────
  async function handleTestExiftool() {
    setBusy(true); clearResult()
    try {
      const res = await api.remote.testExiftool({
        host: w.host.trim(), port: Number(w.port), user: w.user.trim(), ssh_key_path: w.sshKeyPath,
      })
      setResult({ ok: true, msg: res.message })
    } catch (e: unknown) {
      setResult({ ok: false, msg: (e as Error).message ?? 'ExifTool test failed.' })
    } finally { setBusy(false) }
  }

  // ── Step 5: test path mapping ─────────────────────────────────────────────
  async function handleTestPath() {
    if (!w.samplePath) { setResult({ ok: false, msg: 'Enter a sample file path.' }); return }
    setBusy(true); clearResult()
    try {
      const res = await api.remote.testPath({
        host: w.host.trim(), port: Number(w.port), user: w.user.trim(), ssh_key_path: w.sshKeyPath,
        local_path_prefix: w.localPrefix.trim(), remote_path_prefix: w.remotePrefix.trim(),
        sample_local_path: w.samplePath.trim(),
      })
      setResult({
        ok: res.found,
        msg: res.message + (res.found ? '' : `\n→ Translated to: ${res.remote_path}`),
      })
    } catch (e: unknown) {
      setResult({ ok: false, msg: (e as Error).message ?? 'Path test failed.' })
    } finally { setBusy(false) }
  }

  // ── Step 6: save ─────────────────────────────────────────────────────────
  async function handleSave() {
    setBusy(true); clearResult()
    const cfg: RemoteServerConfig = {
      label: w.label.trim() || 'Remote Server',
      host: w.host.trim(),
      port: Number(w.port) || 22,
      user: w.user.trim(),
      ssh_key_path: w.sshKeyPath,
      local_path_prefix: w.localPrefix.trim(),
      remote_path_prefix: w.remotePrefix.trim(),
      writeback_concurrency: Number(w.concurrency) || 4,
      enabled: false,
    }
    try {
      if (w.editingId != null) {
        await api.remote.update(w.editingId, cfg)
      } else {
        await api.remote.create(cfg)
      }
      onSaved()
    } catch (e: unknown) {
      setResult({ ok: false, msg: (e as Error).message ?? 'Save failed.' })
    } finally { setBusy(false) }
  }

  function copyPubkey() {
    navigator.clipboard.writeText(w.publicKey).then(() => {
      setCopied(true)
      if (copyTimer.current) clearTimeout(copyTimer.current)
      copyTimer.current = setTimeout(() => setCopied(false), 2000)
    })
  }

  const canNext1 = w.host.trim() && w.user.trim() && w.label.trim()
  const canNext2 = !!w.sshKeyPath
  const canNext5 = w.localPrefix.trim() && w.remotePrefix.trim()

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-950 border border-gray-800 rounded-2xl shadow-2xl w-full max-w-lg flex flex-col max-h-[90vh]">

        {/* Header */}
        <div className="flex items-center justify-between px-6 pt-5 pb-4 border-b border-gray-800">
          <div>
            <h2 className="text-white font-semibold">
              {w.editingId != null ? 'Edit Remote Server' : 'Add Remote Server'}
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Step {step} of {STEPS.length} — {STEPS.find(s => s.id === step)?.title}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-white text-xl leading-none">×</button>
        </div>

        {/* Step indicator */}
        <div className="flex px-6 pt-4 gap-1.5">
          {STEPS.map(s => (
            <div
              key={s.id}
              className={`h-1 flex-1 rounded-full transition-colors ${
                s.id < step ? 'bg-indigo-500' : s.id === step ? 'bg-indigo-400' : 'bg-gray-800'
              }`}
            />
          ))}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">

          {/* ── Step 1: Server details ─────────────────────────────────── */}
          {step === 1 && (
            <>
              <p className="text-xs text-gray-500">
                Enter the SSH connection details for the Mac that physically hosts your photo library.
              </p>
              <Field label="Label" value={w.label} onChange={set('label')}
                placeholder="e.g. Photo Server" />
              <Field label="Hostname or IP" value={w.host} onChange={set('host')}
                placeholder="e.g. mac-server.local or 192.168.1.10"
                hint="The hostname or IP of the remote Mac visible on your LAN." />
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <Field label="SSH Username" value={w.user} onChange={set('user')}
                    placeholder="e.g. ak" />
                </div>
                <Field label="SSH Port" value={w.port} onChange={set('port')}
                  type="number" placeholder="22" />
              </div>
            </>
          )}

          {/* ── Step 2: SSH key ────────────────────────────────────────── */}
          {step === 2 && (
            <>
              <p className="text-xs text-gray-500">
                VIP uses a dedicated SSH key so it never touches your personal SSH config.
                Generate it once — then authorize it on the remote Mac either automatically
                (needs the remote password once) or manually.
              </p>

              <button
                onClick={handleGenerateKey}
                disabled={busy}
                className="w-full bg-indigo-700 hover:bg-indigo-600 disabled:opacity-40 text-white text-sm rounded-lg px-4 py-2 font-medium transition-colors"
              >
                {busy ? 'Generating…' : 'Generate SSH key for this server'}
              </button>

              {w.publicKey && (
                <>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="text-xs font-medium text-gray-400">Public key</label>
                      <button
                        onClick={copyPubkey}
                        className="text-xs text-indigo-400 hover:text-indigo-300"
                      >
                        {copied ? '✓ Copied' : 'Copy'}
                      </button>
                    </div>
                    <div className="bg-gray-900 rounded-lg px-3 py-2 text-xs font-mono text-gray-400 break-all leading-relaxed border border-gray-800">
                      {w.publicKey}
                    </div>
                  </div>

                  <div className="border border-gray-800 rounded-xl p-4 space-y-3">
                    <p className="text-xs font-medium text-gray-300">Option A — Manual (recommended)</p>
                    <p className="text-xs text-gray-500">
                      On the remote Mac, open Terminal and run:
                    </p>
                    <div className="bg-gray-900 rounded-lg px-3 py-2 text-xs font-mono text-indigo-300 border border-gray-800">
                      mkdir -p ~/.ssh && echo '{w.publicKey}' &gt;&gt; ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
                    </div>
                  </div>

                  <div className="border border-gray-800 rounded-xl p-4 space-y-3">
                    <p className="text-xs font-medium text-gray-300">Option B — Auto-deploy (needs sshpass)</p>
                    <p className="text-xs text-gray-500">
                      Enter the remote password once. It is used to run ssh-copy-id and is never stored.
                    </p>
                    <Field label="Remote password" value={w.password} onChange={set('password')}
                      type="password" placeholder="Remote Mac login password" />
                    <button
                      onClick={handleDeployKey}
                      disabled={busy || !w.password}
                      className="w-full bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-200 text-sm rounded-lg px-4 py-2 font-medium transition-colors border border-gray-700"
                    >
                      {busy ? 'Deploying…' : 'Auto-deploy key to remote'}
                    </button>
                  </div>
                </>
              )}

              {result && <Pill ok={result.ok} msg={result.msg} />}
            </>
          )}

          {/* ── Step 3: Test SSH ───────────────────────────────────────── */}
          {step === 3 && (
            <>
              <p className="text-xs text-gray-500">
                Verify that VIP can connect to the remote Mac using the SSH key —
                no password required at this point.
              </p>
              <div className="bg-gray-900 rounded-lg px-4 py-3 text-xs font-mono text-gray-400 border border-gray-800">
                ssh -i {w.sshKeyPath} {w.user}@{w.host} -p {w.port}
              </div>
              <button
                onClick={handleTestSSH}
                disabled={busy}
                className="w-full bg-indigo-700 hover:bg-indigo-600 disabled:opacity-40 text-white text-sm rounded-lg px-4 py-2 font-medium transition-colors"
              >
                {busy ? 'Testing…' : 'Test SSH connection'}
              </button>
              {result && <Pill ok={result.ok} msg={result.msg} />}
            </>
          )}

          {/* ── Step 4: Verify ExifTool ────────────────────────────────── */}
          {step === 4 && (
            <>
              <p className="text-xs text-gray-500">
                Check that ExifTool is installed on the remote Mac.
                If it is not, connect to the remote Mac and run:{' '}
                <code className="text-indigo-400 font-mono">brew install exiftool</code>
              </p>
              <button
                onClick={handleTestExiftool}
                disabled={busy}
                className="w-full bg-indigo-700 hover:bg-indigo-600 disabled:opacity-40 text-white text-sm rounded-lg px-4 py-2 font-medium transition-colors"
              >
                {busy ? 'Checking…' : 'Verify ExifTool on remote'}
              </button>
              {result && <Pill ok={result.ok} msg={result.msg} />}
            </>
          )}

          {/* ── Step 5: Path mapping ───────────────────────────────────── */}
          {step === 5 && (
            <>
              <p className="text-xs text-gray-500">
                Files appear at different paths on each Mac.
                Tell VIP how to translate between them.
              </p>
              <Field
                label="Local path prefix"
                value={w.localPrefix} onChange={set('localPrefix')}
                placeholder="/Volumes/PhotoServer"
                hint="How this Mac accesses the remote drive (the mount point)."
              />
              <Field
                label="Remote path prefix"
                value={w.remotePrefix} onChange={set('remotePrefix')}
                placeholder="/Users/ak/Photos"
                hint="The actual path on the remote Mac where the same files live."
              />
              <div>
                <Field
                  label="Sample file path (optional — tests the mapping)"
                  value={w.samplePath} onChange={set('samplePath')}
                  placeholder="/Volumes/PhotoServer/2024/IMG_8691.DNG"
                  hint="Paste any file path that VIP already knows about to verify translation."
                />
                <button
                  onClick={handleTestPath}
                  disabled={busy || !w.samplePath.trim() || !canNext5}
                  className="mt-2 text-xs bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-lg px-3 py-1.5 border border-gray-700 transition-colors"
                >
                  {busy ? 'Testing…' : 'Test path translation'}
                </button>
              </div>
              {result && <Pill ok={result.ok} msg={result.msg} />}
            </>
          )}

          {/* ── Step 6: Review & save ──────────────────────────────────── */}
          {step === 6 && (
            <>
              <p className="text-xs text-gray-500">
                Review your configuration and save. The server will be added as{' '}
                <strong className="text-gray-300">disabled</strong> — enable it from the card
                once you are satisfied.
              </p>

              <div>
                <label className="block text-xs font-medium text-gray-400 mb-1">
                  Parallel writeback workers
                </label>
                <p className="text-xs text-gray-600 mb-1.5">
                  How many files to write simultaneously via SSH. 4 is a good default for
                  Gigabit LAN. Raise if the remote Mac's SSD can handle more concurrent writes.
                </p>
                <div className="flex items-center gap-3">
                  <input
                    type="range" min={1} max={16} step={1}
                    value={w.concurrency}
                    onChange={e => set('concurrency')(e.target.value)}
                    className="flex-1 accent-indigo-500"
                  />
                  <span className="text-sm text-white w-6 text-right">{w.concurrency}</span>
                </div>
              </div>

              <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-3 space-y-1.5 text-xs text-gray-400">
                <div><span className="text-gray-600 w-24 inline-block">Label</span> {w.label}</div>
                <div><span className="text-gray-600 w-24 inline-block">Host</span> {w.user}@{w.host}:{w.port}</div>
                <div><span className="text-gray-600 w-24 inline-block">SSH key</span> <span className="font-mono">{w.sshKeyPath}</span></div>
                <div><span className="text-gray-600 w-24 inline-block">Local prefix</span> <span className="font-mono">{w.localPrefix}</span></div>
                <div><span className="text-gray-600 w-24 inline-block">Remote prefix</span> <span className="font-mono">{w.remotePrefix}</span></div>
                <div><span className="text-gray-600 w-24 inline-block">Workers</span> {w.concurrency}</div>
              </div>

              {result && <Pill ok={result.ok} msg={result.msg} />}
            </>
          )}
        </div>

        {/* Footer navigation */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-800">
          <button
            onClick={() => { clearResult(); setStep(s => (s > 1 ? (s - 1) as StepId : s)) }}
            disabled={step === 1}
            className="text-sm text-gray-400 hover:text-white disabled:opacity-30 transition-colors"
          >
            ← Back
          </button>
          <div className="flex gap-2">
            {step < 6 ? (
              <button
                onClick={() => { clearResult(); setStep(s => (s + 1) as StepId) }}
                disabled={
                  (step === 1 && !canNext1) ||
                  (step === 2 && !canNext2) ||
                  (step === 5 && !canNext5)
                }
                className="text-sm bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg px-5 py-1.5 font-medium transition-colors"
              >
                Next →
              </button>
            ) : (
              <button
                onClick={handleSave}
                disabled={busy}
                className="text-sm bg-green-700 hover:bg-green-600 disabled:opacity-40 text-white rounded-lg px-5 py-1.5 font-medium transition-colors"
              >
                {busy ? 'Saving…' : w.editingId != null ? 'Save changes' : 'Add server'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── Main panel ──────────────────────────────────────────────────────────────

export default function RemoteServersPanel() {
  const [servers, setServers] = useState<RemoteServer[]>([])
  const [loading, setLoading] = useState(true)
  const [showWizard, setShowWizard] = useState(false)
  const [wizardInitial, setWizardInitial] = useState<WizardState>(BLANK)
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null)

  async function load() {
    setLoading(true)
    try { setServers(await api.remote.list()) } catch { /* ignore */ }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  function openAddWizard() {
    setWizardInitial(BLANK)
    setShowWizard(true)
  }

  function openEditWizard(s: RemoteServer) {
    setWizardInitial({
      label: s.label,
      host: s.host,
      port: String(s.port),
      user: s.user,
      sshKeyPath: s.ssh_key_path,
      publicKey: '',
      password: '',
      localPrefix: s.local_path_prefix,
      remotePrefix: s.remote_path_prefix,
      samplePath: '',
      concurrency: String(s.writeback_concurrency),
      editingId: s.id,
    })
    setShowWizard(true)
  }

  async function handleDelete(id: number) {
    try { await api.remote.delete(id); await load() } catch {/* ignore */}
    setDeleteConfirm(null)
  }

  async function handleToggle(id: number) {
    try { await api.remote.toggle(id); await load() } catch {/* ignore */}
  }

  return (
    <section className="mt-10">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-medium text-gray-400 uppercase tracking-wider">
          Remote Write Servers
        </h2>
        <button
          onClick={openAddWizard}
          className="text-xs bg-indigo-700 hover:bg-indigo-600 text-white rounded-lg px-3 py-1.5 font-medium transition-colors"
        >
          + Add server
        </button>
      </div>

      <p className="text-xs text-gray-600 mb-4">
        Run ExifTool on a remote Mac that hosts your photo library via SSH, eliminating the
        need to transfer large RAW files over the network during writeback.
        Files are written at local SSD speed; only ~3 KB of metadata is sent per file.
      </p>

      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : servers.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 border-dashed rounded-xl px-5 py-8 text-center">
          <p className="text-sm text-gray-500 mb-1">No remote servers configured</p>
          <p className="text-xs text-gray-600">
            Add a server to speed up writeback for libraries stored on a remote Mac or NAS.
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {servers.map(s => (
            <ServerCard
              key={s.id}
              server={s}
              onEdit={() => openEditWizard(s)}
              onDelete={() => setDeleteConfirm(s.id)}
              onToggle={() => handleToggle(s.id)}
            />
          ))}
        </div>
      )}

      {/* Wizard modal */}
      {showWizard && (
        <Wizard
          initial={wizardInitial}
          onClose={() => { setShowWizard(false); load() }}
          onSaved={() => { setShowWizard(false); load() }}
        />
      )}

      {/* Delete confirmation */}
      {deleteConfirm != null && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-6 max-w-sm w-full shadow-2xl">
            <p className="text-white font-semibold mb-2">Remove this server?</p>
            <p className="text-gray-400 text-sm mb-5">
              The SSH key file on disk is not deleted — only the VIP configuration is removed.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setDeleteConfirm(null)}
                className="text-sm text-gray-400 hover:text-white px-4 py-1.5 rounded-lg border border-gray-700 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => handleDelete(deleteConfirm)}
                className="text-sm bg-red-700 hover:bg-red-600 text-white px-4 py-1.5 rounded-lg font-medium transition-colors"
              >
                Remove
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
