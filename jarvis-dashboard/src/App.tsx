import { useState, useEffect, useRef, useCallback } from 'react';
import ParticleOrb from './ParticleOrb';
import FilePanel, { JarvisFile } from './FilePanel';
import './App.css';

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

function useHexTicker() {
  const [hex, setHex] = useState('0x3F8A2C');
  useEffect(() => {
    const id = setInterval(() => {
      setHex('0x' + Math.floor(Math.random() * 0xFFFFFF).toString(16).toUpperCase().padStart(6,'0'));
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

const DEMO_CYCLE: { state: OrbState; duration: number }[] = [
  { state: 'idle',      duration: 3200 },
  { state: 'activated', duration: 1800 },
  { state: 'thinking',  duration: 1400 },
  { state: 'speaking',  duration: 3000 },
  { state: 'idle',      duration: 2000 },
];

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

export default function App() {
  const [orbState,   setOrbState]   = useState<OrbState>('idle');
  const [transcript, setTranscript] = useState('—');
  const [files,      setFiles]      = useState<JarvisFile[]>([]);
  const [serverOk,   setServerOk]   = useState(false);
  const [fileCount,  setFileCount]  = useState(0);
  const [speechBeat, setSpeechBeat] = useState(0);
  const [panelWidth, setPanelWidth] = useState(360);
  const demoRef    = useRef<ReturnType<typeof setTimeout> | null>(null);
  const demoIdx    = useRef(0);
  const dragging   = useRef(false);
  const clock      = useClock();
  const hex        = useHexTicker();

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

  const runDemo = useCallback(() => {
    const step = DEMO_CYCLE[demoIdx.current % DEMO_CYCLE.length];
    setOrbState(step.state);
    demoIdx.current++;
    demoRef.current = setTimeout(runDemo, step.duration);
  }, []);

  useEffect(() => {
    let alive = true;
    const demoTimer = setTimeout(() => { if (alive && !serverOk) runDemo(); }, 2000);

    async function poll() {
      if (!alive) return;
      try {
        const res  = await fetch('http://localhost:5151/status', { signal: AbortSignal.timeout(1800) });
        const data = await res.json();
        if (demoRef.current) { clearTimeout(demoRef.current); demoRef.current = null; }
        setServerOk(true);
        setOrbState(data.state as OrbState);
        if (data.transcript) setTranscript(data.transcript);
        if (data.speech_beat !== undefined) setSpeechBeat(data.speech_beat);
        if (data.files?.length !== fileCount) {
          setFiles(data.files ?? []);
          setFileCount(data.files?.length ?? 0);
        }
      } catch {
        setServerOk(false);
      }
      if (alive) setTimeout(poll, 300);
    }
    poll();

    return () => {
      alive = false;
      clearTimeout(demoTimer);
      if (demoRef.current) clearTimeout(demoRef.current);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const STATE_COLORS: Record<OrbState, string> = {
    idle:      '#1a5e32',
    activated: '#0abfa0',
    thinking:  '#6060eb',
    speaking:  '#14e65f',
    error:     '#e63232',
  };
  const stateColor = STATE_COLORS[orbState];

  return (
    <div className="shell">
      <div className="scanlines" />

      <header className="header">
        <div className="logo">
          <span className="logoMain">JARVIS</span>
          <span className="logoSub">ADVANCED AI INTERFACE</span>
        </div>
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
          <div className="statePhrase">{PHRASES[orbState]}</div>

          <div className="transcriptBox">
            <div className="transcriptLabel">LAST COMMAND</div>
            <div className="transcriptText">{transcript}</div>
          </div>
        </main>

        <div
          className="resizeHandle"
          onMouseDown={() => { dragging.current = true; document.body.style.cursor = 'col-resize'; }}
        />
        <FilePanel files={files} />
      </div>
    </div>
  );
}
