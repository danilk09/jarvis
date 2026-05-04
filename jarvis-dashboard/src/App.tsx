import { useState, useEffect, useRef, useCallback } from 'react';
import { Routes, Route, Link } from 'react-router-dom';
import ParticleOrb from './ParticleOrb';
import FilePanel, { JarvisFile } from './FilePanel';
import InputPanel from './InputPanel';
import WorkspaceManager from './WorkspaceManager';
import { getAuth, setAuth, clearAuth, authedFetch } from './auth';
import './App.css';

// ── Types ─────────────────────────────────────────────────────────────────────
type OrbState = 'idle' | 'activated' | 'thinking' | 'speaking' | 'error';

const PHRASES: Record<OrbState, string> = {
  idle:      'Waiting for wake word',
  activated: 'Listening...',
  thinking:  'Processing command',
  speaking:  'Speaking response',
  error:     'Connection lost',
};

const STATE_LABELS: Record<OrbState, string> = {
  idle:      'STANDBY',
  activated: 'LISTENING',
  thinking:  'THINKING',
  speaking:  'SPEAKING',
  error:     'ERROR',
};

const STATE_COLORS: Record<OrbState, string> = {
  idle:      '#1a5e32',
  activated: '#0abfa0',
  thinking:  '#6060eb',
  speaking:  '#14e65f',
  error:     '#e63232',
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function useHexTicker() {
  const [hex, setHex] = useState('0x3F8A2C');
  useEffect(() => {
    const id = setInterval(() => {
      setHex('0x' + Math.floor(Math.random() * 0xFFFFFF).toString(16).toUpperCase().padStart(6, '0'));
    }, 800);
    return () => clearInterval(id);
  }, []);
  return hex;
}

function useClock() {
  const [time, setTime] = useState('');
  useEffect(() => {
    const tick = () => setTime(new Date().toLocaleTimeString('en-US', { hour12: false }));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return time;
}

function StatusDot({ active, pulsing, error, label }: {
  active?: boolean; pulsing?: boolean; error?: boolean; label: string;
}) {
  const cls = ['dot', active && 'dotActive', pulsing && 'dotPulsing', error && 'dotError']
    .filter(Boolean).join(' ');
  return (
    <div className="statusItem">
      <div className={cls} />
      <span className="mono dimmed">{label}</span>
    </div>
  );
}

type MusicState = {
  playing: boolean; paused: boolean; currentSong: string; history: string[];
};

function MusicPlayer({ music }: { music: MusicState }) {
  const sendControl = async (command: string, extra?: object) => {
    await authedFetch('/api/music/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command, ...extra }),
    });
  };
  if (!music.playing && !music.paused && !music.currentSong) return null;
  return (
    <div className="musicBar">
      <button className="musicBtn" title="Previous" disabled={music.history.length === 0}
        onClick={() => sendControl('prev', { n: 1 })}>⏮</button>
      <button className="musicBtn" title={music.paused ? 'Resume' : 'Pause'}
        onClick={() => sendControl('toggle_pause')}>{music.paused ? '▶' : '⏸'}</button>
      <button className="musicBtn" title="Skip" onClick={() => sendControl('skip')}>⏭</button>
      <span className="musicBarTitle" title={music.currentSong}>{music.currentSong || '—'}</span>
    </div>
  );
}

// ── Login screen ──────────────────────────────────────────────────────────────
function LoginScreen({ onAuth }: { onAuth: (token: string, username: string) => void }) {
  const [username, setUsername] = useState('');
  const [error,    setError]    = useState('');
  const [busy,     setBusy]     = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim()) return;
    setBusy(true);
    setError('');
    try {
      const r = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim().toLowerCase() }),
      });
      const d = await r.json();
      if (!r.ok) { setError(d.error ?? 'Sign in failed.'); setBusy(false); return; }
      setAuth(d.token, d.username);
      onAuth(d.token, d.username);
    } catch {
      setError('Network error — is the server running?');
    }
    setBusy(false);
  };

  return (
    <div className="loginWrap">
      <div className="loginCard">
        <div className="loginLogo">J A R V I S</div>
        <div className="loginSubtitle">ADVANCED AI INTERFACE</div>
        <form onSubmit={submit} className="loginForm">
          <input
            className="loginInput"
            type="text"
            placeholder="Username"
            autoCapitalize="none"
            autoComplete="username"
            value={username}
            onChange={e => setUsername(e.target.value)}
            disabled={busy}
          />
          {error && <div className="loginError">{error}</div>}
          <button className="loginBtn" type="submit" disabled={busy || !username.trim()}>
            {busy ? '···' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  );
}

// ── Sleep overlay ─────────────────────────────────────────────────────────────
function SleepOverlay({ message, onWake }: { message: string; onWake: () => void }) {
  return (
    <div className="sleepOverlay">
      <div className="sleepCard">
        <div className="sleepIcon">◑</div>
        <div className="sleepMessage">{message}</div>
        <button className="sleepWakeBtn" onClick={onWake}>Wake Jarvis</button>
      </div>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  // ── Auth ───────────────────────────────────────────────────────────────────
  const [auth, setAuthState] = useState(getAuth());

  const handleAuth = (token: string, username: string) => {
    setAuthState({ token, username });
  };

  const handleLogout = async () => {
    await authedFetch('/api/logout', { method: 'POST' });
    clearAuth();
    setAuthState(null);
  };

  if (!auth) {
    return <LoginScreen onAuth={handleAuth} />;
  }

  return <Dashboard username={auth.username} token={auth.token} onLogout={handleLogout} />;
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
function Dashboard({ username, token, onLogout }: {
  username: string; token: string; onLogout: () => void;
}) {
  const [orbState,   setOrbState]   = useState<OrbState>('idle');
  const [transcript, setTranscript] = useState('—');
  const [files,      setFiles]      = useState<JarvisFile[]>([]);
  const [serverOk,   setServerOk]   = useState(false);
  const [fileCount,  setFileCount]  = useState(0);
  const [speechBeat, setSpeechBeat] = useState(0);
  const [panelWidth, setPanelWidth] = useState(360);
  const [rightTab,   setRightTab]   = useState<'output' | 'input'>('output');
  const [music,      setMusic]      = useState<MusicState>({ playing: false, paused: false, currentSong: '', history: [] });

  // Wake word / recording state
  const [recording,      setRecording]      = useState(false);
  const [wakeWordActive, setWakeWordActive] = useState(false);
  const [sleepMsg,       setSleepMsg]       = useState('');

  const wsRef           = useRef<WebSocket | null>(null);
  const mediaRecRef     = useRef<MediaRecorder | null>(null);
  const chunksRef       = useRef<Blob[]>([]);
  const dragging        = useRef(false);
  const speechSynthRef  = useRef<SpeechSynthesisUtterance | null>(null);
  const recognitionRef  = useRef<any>(null);
  const demoRef         = useRef<ReturnType<typeof setTimeout> | null>(null);
  const demoIdx         = useRef(0);
  const clock           = useClock();
  const hex             = useHexTicker();

  // ── Resize handle ────────────────────────────────────────────────────────
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const shell = document.querySelector('.shell') as HTMLElement;
      if (!shell) return;
      const newWidth = shell.getBoundingClientRect().right - e.clientX;
      setPanelWidth(Math.max(220, Math.min(1200, newWidth)));
    };
    const onUp = () => { dragging.current = false; document.body.style.cursor = ''; };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };
  }, []);

  // ── Demo cycle when server not connected ──────────────────────────────────
  const DEMO_CYCLE: { state: OrbState; duration: number }[] = [
    { state: 'idle',      duration: 3200 },
    { state: 'activated', duration: 1800 },
    { state: 'thinking',  duration: 1400 },
    { state: 'speaking',  duration: 3000 },
    { state: 'idle',      duration: 2000 },
  ];

  const runDemo = useCallback(() => {
    const step = DEMO_CYCLE[demoIdx.current % DEMO_CYCLE.length];
    setOrbState(step.state);
    demoIdx.current++;
    demoRef.current = setTimeout(runDemo, step.duration);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Poll /status for orb state ────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    const demoTimer = setTimeout(() => { if (alive && !serverOk) runDemo(); }, 2000);

    async function poll() {
      if (!alive) return;
      try {
        const res  = await authedFetch('/status', { signal: AbortSignal.timeout(1800) });
        if (res.status === 401) { /* token expired — handled by WebSocket reconnect */ return; }
        const data = await res.json();
        if (demoRef.current) { clearTimeout(demoRef.current); demoRef.current = null; }
        setServerOk(true);
        if (!recording) setOrbState(data.state as OrbState);
        if (data.transcript) setTranscript(data.transcript);
        if (data.speech_beat !== undefined) setSpeechBeat(data.speech_beat);
        if (data.files?.length !== fileCount) {
          setFiles(data.files ?? []);
          setFileCount(data.files?.length ?? 0);
        }
        setMusic({
          playing:     !!data.music_playing,
          paused:      !!data.music_paused,
          currentSong: data.current_song ?? '',
          history:     data.song_history ?? [],
        });
      } catch {
        setServerOk(false);
      }
      if (alive) setTimeout(poll, 500);
    }
    poll();

    return () => {
      alive = false;
      clearTimeout(demoTimer);
      if (demoRef.current) clearTimeout(demoRef.current);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording]);

  // ── WebSocket connection ──────────────────────────────────────────────────
  const speakText = useCallback((text: string) => {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    const utt = new SpeechSynthesisUtterance(text);
    utt.rate = 1.05;
    utt.onstart  = () => setOrbState('speaking');
    utt.onend    = () => setOrbState('idle');
    utt.onerror  = () => setOrbState('idle');
    speechSynthRef.current = utt;
    window.speechSynthesis.speak(utt);
  }, []);

  const handleClientActions = useCallback((actions: any[]) => {
    for (const a of actions) {
      if (a.type === 'open_url') {
        window.open(a.url, '_blank', 'noopener,noreferrer');
      } else if (a.type === 'download_file') {
        const blob = new Blob([a.content], { type: 'text/plain' });
        const url  = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href     = url;
        link.download = a.filename;
        link.click();
        URL.revokeObjectURL(url);
      }
    }
  }, []);

  const connectWs = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState <= WebSocket.OPEN) return;
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws    = new WebSocket(`${proto}//${window.location.host}/ws`);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'auth', token }));
    };

    ws.onmessage = (evt) => {
      let msg: any;
      try { msg = JSON.parse(evt.data); } catch { return; }

      switch (msg.type) {
        case 'auth_ok':
          setServerOk(true);
          break;
        case 'error':
          console.warn('Jarvis WS error:', msg.message);
          break;
        case 'sleeping':
          setSleepMsg(msg.message);
          break;
        case 'awake':
          setSleepMsg('');
          break;
        case 'state':
          if (!recording) setOrbState(msg.state as OrbState);
          break;
        case 'transcript':
          setTranscript(msg.text);
          setOrbState('thinking');
          break;
        case 'response':
          handleClientActions(msg.client_actions ?? []);
          speakText(msg.text);
          break;
        default:
          break;
      }
    };

    ws.onclose = () => {
      setServerOk(false);
      setTimeout(connectWs, 3000);
    };
  }, [token, recording, speakText, handleClientActions]);

  useEffect(() => {
    connectWs();
    return () => {
      wsRef.current?.close();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Wake word via Web Speech API ──────────────────────────────────────────
  const startRecording = useCallback(async () => {
    if (recording) return;
    try {
      const stream   = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = e => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      recorder.onstop = () => {
        stream.getTracks().forEach(t => t.stop());
        const blob   = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
        const reader = new FileReader();
        reader.onload = () => {
          const b64 = (reader.result as string).split(',')[1];
          wsRef.current?.send(JSON.stringify({
            type: 'command', audio: b64, mime: recorder.mimeType || 'audio/webm',
          }));
        };
        reader.readAsDataURL(blob);
        setRecording(false);
        setOrbState('thinking');
      };
      recorder.start();
      mediaRecRef.current = recorder;
      setRecording(true);
      setOrbState('activated');
    } catch (err) {
      console.error('Mic error:', err);
    }
  }, [recording]);

  const stopRecording = useCallback(() => {
    if (mediaRecRef.current && mediaRecRef.current.state !== 'inactive') {
      mediaRecRef.current.stop();
    }
  }, []);

  // Auto-stop after 8 seconds
  useEffect(() => {
    if (!recording) return;
    const t = setTimeout(stopRecording, 8000);
    return () => clearTimeout(t);
  }, [recording, stopRecording]);

  useEffect(() => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) return;

    const rec   = new SR();
    rec.continuous      = true;
    rec.interimResults  = true;
    rec.lang            = 'en-US';
    recognitionRef.current = rec;

    rec.onresult = (event: any) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const text = event.results[i][0].transcript.toLowerCase();
        if (text.includes('jarvis') && !recording) {
          setWakeWordActive(true);
          startRecording();
          setTimeout(() => setWakeWordActive(false), 2000);
        }
      }
    };

    rec.onend = () => { try { rec.start(); } catch {} };

    try { rec.start(); } catch {}

    return () => {
      try { rec.stop(); } catch {}
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleWakeOvr = () => {
    wsRef.current?.send(JSON.stringify({ type: 'wake' }));
    setSleepMsg('');
  };

  // ── Render ────────────────────────────────────────────────────────────────
  const stateColor = STATE_COLORS[orbState];

  const dashboard = (
    <div className="shell">
      <div className="scanlines" />

      <header className="header">
        <div className="logo">
          <span className="logoMain">JARVIS</span>
          <span className="logoSub">ADVANCED AI INTERFACE</span>
        </div>
        <MusicPlayer music={music} />
        <div className="statusBar">
          <StatusDot active={serverOk} label="SERVER" />
          <StatusDot
            active={orbState === 'speaking' || orbState === 'activated'}
            pulsing={orbState === 'speaking'}
            error={orbState === 'error'}
            label={STATE_LABELS[orbState]}
          />
          <StatusDot active label="AI ONLINE" />
          <span className="mono dimmed">{hex}</span>
          <span className="mono">{clock}</span>
          <span className="mono dimmed" title="Logged in as">{username}</span>
          <Link to="/workspaces" className="wsLink">Workspaces</Link>
          <button className="logoutBtn" onClick={onLogout} title="Sign out">⏻</button>
        </div>
      </header>

      <div className="body" style={{ gridTemplateColumns: `1fr 5px ${panelWidth}px` }}>
        <main className="orbPanel">
          <div className="cornerTL" />
          <div className="cornerBR" />
          <div className="gridLines" />

          <div className="orbWrap">
            <ParticleOrb state={orbState} beat={speechBeat} />
          </div>

          <div className="stateLabel" style={{ color: stateColor }}>
            {STATE_LABELS[orbState]}
          </div>
          <div className="statePhrase">
            {recording ? 'Recording — tap to stop' : PHRASES[orbState]}
            {wakeWordActive && !recording && <span className="wakeFlash"> · wake word!</span>}
          </div>

          {/* Mic button */}
          <button
            className={`micBtn${recording ? ' micBtnActive' : ''}`}
            title={recording ? 'Stop recording' : 'Hold to speak (or say "Jarvis")'}
            onClick={recording ? stopRecording : startRecording}
          >
            {recording ? '⏹' : '🎤'}
          </button>

          <div className="transcriptBox">
            <div className="transcriptLabel">LAST COMMAND</div>
            <div className="transcriptText">{transcript}</div>
          </div>
        </main>

        <div
          className="resizeHandle"
          onMouseDown={() => { dragging.current = true; document.body.style.cursor = 'col-resize'; }}
        />
        <div className="rightPanel">
          <div className="panelTabs">
            <button
              className={`panelTab ${rightTab === 'output' ? 'panelTabActive' : ''}`}
              onClick={() => setRightTab('output')}
            >OUTPUT</button>
            <button
              className={`panelTab ${rightTab === 'input' ? 'panelTabActive' : ''}`}
              onClick={() => setRightTab('input')}
            >INPUT</button>
          </div>
          {rightTab === 'output'
            ? <FilePanel files={files} style={{ borderLeft: 'none', flex: 1, minHeight: 0 }} />
            : <InputPanel />
          }
        </div>
      </div>

      {sleepMsg && <SleepOverlay message={sleepMsg} onWake={handleWakeOvr} />}
    </div>
  );

  return (
    <Routes>
      <Route path="/"           element={dashboard} />
      <Route path="/workspaces" element={<WorkspaceManager />} />
    </Routes>
  );
}
