import { useEffect, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, get, post } from '../../lib/api'
import './auth.css'

interface SetupInfo { name: string; username: string; qr_svg: string; secret: string }

/** The one-time link an admin sends: scan the QR code, choose a password, confirm the first code, then save the backup codes (shown once). */
export function SetupPage() {
  const { token = '' } = useParams()
  const [info, setInfo] = useState<SetupInfo | null>(null)
  const [bad, setBad] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [code, setCode] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [codes, setCodes] = useState<string[] | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    get<SetupInfo>(`/api/auth/setup/${encodeURIComponent(token)}`).then(setInfo).catch((e: unknown) => setBad(e instanceof ApiError ? e.message : 'Ask your admin for a new link.'))
  }, [token])

  async function submit(ev: FormEvent) {
    ev.preventDefault()
    setErr('')
    if (pw !== pw2) { setErr('The two passwords are not the same.'); return }
    setBusy(true)
    try {
      const r = await post<{ backup_codes: string[] }>(`/api/auth/setup/${encodeURIComponent(token)}`, { password: pw, totp: code.trim() })
      setPw(''); setPw2('')
      setCodes(r.backup_codes)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Setup failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-page">
      <main className="auth-card narrow">
        {codes ? (
          <div className="stack">
            <h1>You are all set</h1>
            <div className="alert warn"><b>Save these backup codes now.</b> They are shown only once. Each works one time if you lose your phone. Keep them somewhere safe.</div>
            <div className="codes">{codes.map((c) => <span key={c}>{c}</span>)}</div>
            <button className="btn" onClick={() => void navigator.clipboard.writeText(codes.join('\n')).then(() => setCopied(true))}>{copied ? 'Copied' : 'Copy codes'}</button>
            <Link className="btn primary big" to="/login">Continue to sign in</Link>
          </div>
        ) : bad ? (
          <div className="stack"><h1>This link does not work</h1><p className="sub">{bad}</p><Link to="/login">Go to sign in</Link></div>
        ) : !info ? (
          <div className="skeleton" style={{ minHeight: 320 }} />
        ) : (
          <form className="stack" onSubmit={submit} autoComplete="off">
            <h1>Welcome, {info.name}</h1>
            <p className="sub">Username: <b>{info.username}</b>. Three quick steps:</p>
            <ol className="steps"><li>Scan this QR code with Google or Microsoft Authenticator.</li><li>Choose a password (at least 12 characters).</li><li>Type the 6-digit code your app shows now.</li></ol>
            <div className="qr" dangerouslySetInnerHTML={{ __html: info.qr_svg }} />
            <p className="sub">Cannot scan? Enter this key in the app instead: <span className="mono secret">{info.secret}</span></p>
            <label className="field">Password<input type="password" autoComplete="new-password" minLength={12} required value={pw} onChange={(e) => setPw(e.target.value)} /></label>
            <label className="field">Repeat password<input type="password" autoComplete="new-password" minLength={12} required value={pw2} onChange={(e) => setPw2(e.target.value)} /></label>
            <label className="field">6-digit code from the app<input inputMode="numeric" autoComplete="one-time-code" maxLength={6} pattern="\d{6}" required value={code} onChange={(e) => setCode(e.target.value)} /></label>
            {err && <div className="alert" role="alert">{err}</div>}
            <button className="btn primary big" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Finish setup'}</button>
          </form>
        )}
      </main>
    </div>
  )
}
