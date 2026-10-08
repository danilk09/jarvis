import { useState, useEffect, useRef, useCallback } from 'react';
import { Routes, Route, NavLink } from 'react-router-dom';
import ParticleOrb, { OrbState, SpeechEnvelope } from './ParticleOrb';
import FilePanel, { JarvisFile } from './FilePanel';
import InputPanel from './InputPanel';
import ActivityFeed, { LogEntry } from './ActivityFeed';
import MusicCard, { MusicState } from './MusicCard';
import WorkspaceManager from './WorkspaceManager';
import './App.css';

const PHRASES: Record<OrbState, string> = {
  idle:      'Say “Jarvis” or click the orb',
  activated: 'Listening…',
  thinking:  'Working on it…',
  speaking:  'Speaking',
  error:     'Something went wrong',
};

const STATE_LABELS: Record<OrbState, string> = {
  idle:      'STANDBY',
  activated: 'LISTENING',
  thinking:  'THINKING',
  speaking:  'SPEAKING',
  error:     'ERROR',
};

type Tab = 'activity' | 'output' | 'input';

// Shown when the backend isn't running (e.g. `npm start` on its own)
const DEMO_CYCLE: { state: OrbState; duration: number }[] = [
  { state: 'idle',      duration: 3200 },
  { state: 'activated', duration: 1800 },
  { state: 'thinking',  duration: 1400 },
  { state: 'speaking',  duration: 3600 },
  { state: 'idle',      duration: 2000 },
];

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

function loadTab(): Tab {
  try {
    const t = localStorage.getItem('jarvis.tab');
    if (t === 'activity' || t === 'output' || t === 'input') return t;
  } catch { /* storage unavailable */ }
  return 'activity';
}

export function Header({ serverOk }: { serverOk: boolean }) {
  const clock = useClock();
  return (
    <header className="header">
      <div className="brand">
        <span className="brandMark" />
        <span className="brandName">J.A.R.V.I.S</span>
      </div>
      <nav className="nav">
        <NavLink to="/" end className={({ isActive }) => `navLink ${isActive ? 'navLinkActive' : ''}`}>Dashboard</NavLink>
        <NavLink to="/workspaces" className={({ isActive }) => `navLink ${isActive ? 'navLinkActive' : ''}`}>Workspaces</NavLink>
      </nav>
      <div className="headerRight">
        <span className={`pill ${serverOk ? 'pillOk' : 'pillBad'}`}>
          <span className="pillDot" />{serverOk ? 'ONLINE' : 'OFFLINE'}
        </span>
        <span className="clock">{clock}</span>
      </div>
    </header>
  );
}

function Dashboard() {
  const [orbState,   setOrbState]   = useState<OrbState>('idle');
  const [transcript, setTranscript] = useState('');
  const [files,      setFiles]      = useState<JarvisFile[]>([]);
  const [log,        setLog]        = useState<LogEntry[]>([]);
  const [serverOk,   setServerOk]   = useState(false);
  const [speechBeat, setSpeechBeat] = useState(0);
  const [speech,     setSpeech]     = useState<SpeechEnvelope | null>(null);
  const [panelWidth, setPanelWidth] = useState(380);
  const [tab,        setTabState]   = useState<Tab>(loadTab);
  const [busyHint,   setBusyHint]   = useState(false);
  const [music,      setMusic]      = useState<MusicState>({ playing: false, paused: false, currentSong: '', history: [] });
  // Refs, not state: poll() is created once on mount, so state here would be a stale closure
  const fileCount  = useRef(-1);
  const speechId   = useRef(0);
  const logSig     = useRef('');
  const musicSig   = useRef('');
  const demoRef    = useRef<ReturnType<typeof setTimeout> | null>(null);
  const demoBeat   = useRef<ReturnType<typeof setInterval> | null>(null);
  const demoIdx    = useRef(0);
  const dragging   = useRef(false);
  const bodyRef    = useRef<HTMLDivElement>(null);

  const setTab = (t: Tab) => {
    setTabState(t);
    try { localStorage.setItem('jarvis.tab', t); } catch { /* ignore */ }
  };

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current || !bodyRef.current) return;
      const w = bodyRef.current.getBoundingClientRect().right - e.clientX;
      setPanelWidth(Math.max(280, Math.min(900, w)));
    };
    const onUp = () => { dragging.current = false; document.body.style.cursor = ''; };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };
  }, []);

  const stopDemo = () => {
    if (demoRef.current)  { clearTimeout(demoRef.current);  demoRef.current = null; }
    if (demoBeat.current) { clearInterval(demoBeat.current); demoBeat.current = null; }
  };

  const runDemo = useCallback(() => {
    const step = DEMO_CYCLE[demoIdx.current % DEMO_CYCLE.length];
    setOrbState(step.state);
    if (demoBeat.current) { clearInterval(demoBeat.current); demoBeat.current = null; }
    if (step.state === 'speaking') demoBeat.current = setInterval(() => setSpeechBeat(b => b + 1), 260);
    demoIdx.current++;
    demoRef.current = setTimeout(runDemo, step.duration);
  }, []);

  useEffect(() => {
    let alive = true;
    let online = false;
    const demoTimer = setTimeout(() => { if (alive && !online) runDemo(); }, 2000);

    async function loadSpeech(meta: { id: number; start: number; end: number | null; frame_ms: number }) {
      try {
        const res = await fetch(`/api/speech/${meta.id}`);
        if (!res.ok) return;
        const env = await res.json();
        setSpeech({ id: meta.id, start: meta.start, end: meta.end, frameMs: meta.frame_ms,
                    bands: env.bands, level: env.level });
      } catch { /* the orb falls back to word beats */ }
    }

    async function poll() {
      if (!alive) return;
      try {
        const res  = await fetch(`/status?files=${fileCount.current}`, { signal: AbortSignal.timeout(1800) });
        const data = await res.json();
        if (!online) { online = true; stopDemo(); }
        setServerOk(true);
        setOrbState(data.state as OrbState);
        if (data.transcript) setTranscript(data.transcript);
        if (data.speech_beat !== undefined) setSpeechBeat(data.speech_beat);
        if (data.files) {
          setFiles(data.files);
          fileCount.current = data.files.length;
        }
        const sp = data.speech;
        if (sp && sp.id !== speechId.current) {
          speechId.current = sp.id;
          loadSpeech(sp);
        } else if (sp?.end) {
          setSpeech(s => (s && s.id === sp.id && !s.end ? { ...s, end: sp.end } : s));
        }
        const lg: LogEntry[] = data.log ?? [];
        const last = lg[lg.length - 1];
        const sig = last ? `${lg.length}|${last.time}|${last.text}` : '';
        if (sig !== logSig.current) { logSig.current = sig; setLog(lg); }
        const m: MusicState = {
          playing:     !!data.music_playing,
          paused:      !!data.music_paused,
          currentSong: data.current_song ?? '',
          history:     data.song_history ?? [],
        };
        const mSig = JSON.stringify(m);
        if (mSig !== musicSig.current) { musicSig.current = mSig; setMusic(m); }
      } catch {
        setServerOk(false);
      }
      // Fast enough to pick up speech envelopes promptly; back off when the tab is hidden
      if (alive) setTimeout(poll, document.hidden ? 3000 : 150);
    }
    poll();

    return () => {
      alive = false;
      clearTimeout(demoTimer);
      stopDemo();
    };
  }, [runDemo]);

  const activate = useCallback(async () => {
    try {
      const res = await fetch('/api/activate', { method: 'POST' });
      const data = await res.json();
      if (!data.ok) {
        setBusyHint(true);
        setTimeout(() => setBusyHint(false), 1800);
      }
    } catch { /* offline */ }
  }, []);

  const lastJarvis = [...log].reverse().find(m => m.role === 'jarvis');
  const caption = orbState === 'speaking' && lastJarvis ? lastJarvis.text : transcript;
  const captionLabel = orbState === 'speaking' && lastJarvis ? 'JARVIS' : 'LAST COMMAND';

  return (
    <div className="shell">
      <Header serverOk={serverOk} />

      <div ref={bodyRef} className="body" style={{ ['--panel-w' as string]: `${panelWidth}px` }}>
        <main className={`stage stage-${orbState}`}>
          <div className="stageGrid" />
          <div className="orbArea">
            <ParticleOrb state={orbState} beat={speechBeat} speech={speech} onActivate={activate} />
          </div>

          <div className="stateBlock">
            <div className="stateChip"><span className="stateChipDot" />{STATE_LABELS[orbState]}</div>
            <div className="statePhrase">{busyHint ? 'Busy — try again in a moment' : PHRASES[orbState]}</div>
          </div>

          <div className="caption">
            <div className="captionLabel">{captionLabel}</div>
            <div className="captionText">{caption || '—'}</div>
          </div>

          <MusicCard music={music} />
        </main>

        <div
          className="resizeHandle"
          onMouseDown={() => { dragging.current = true; document.body.style.cursor = 'col-resize'; }}
        />

        <aside className="sidebar">
          <div className="tabs" role="tablist">
            {([['activity', 'ACTIVITY', log.length], ['output', 'OUTPUT', files.length], ['input', 'INPUT', null]] as const)
              .map(([id, label, count]) => (
                <button key={id} role="tab" aria-selected={tab === id}
                        className={`tab ${tab === id ? 'tabActive' : ''}`} onClick={() => setTab(id)}>
                  {label}{count ? <span className="tabCount">{count}</span> : null}
                </button>
              ))}
          </div>
          <div className="tabBody">
            {tab === 'activity' && <ActivityFeed log={log} />}
            {tab === 'output'   && <FilePanel files={files} />}
            {tab === 'input'    && <InputPanel />}
          </div>
        </aside>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/workspaces" element={<WorkspaceManager />} />
    </Routes>
  );
}
