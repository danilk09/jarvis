import { useState, useEffect, useRef, useCallback } from 'react';
import { Routes, Route, NavLink, useLocation } from 'react-router-dom';
import ParticleOrb, { OrbState } from './ParticleOrb';
import FilePanel from './FilePanel';
import InputPanel from './InputPanel';
import ActivityFeed from './ActivityFeed';
import MusicCard from './MusicCard';
import WorkspaceManager from './WorkspaceManager';
import { JarvisStatusProvider, useJarvis } from './JarvisStatus';
import { StageProvider, useStage } from './stage/StageContext';
import StagePage from './stage/StagePage';
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

function Header() {
  const clock = useClock();
  const { serverOk } = useJarvis();
  const { stage } = useStage();
  const link = ({ isActive }: { isActive: boolean }) => `navLink ${isActive ? 'navLinkActive' : ''}`;
  return (
    <header className="header">
      <div className="brand">
        <span className="brandMark" />
        <span className="brandName">J.A.R.V.I.S</span>
      </div>
      <nav className="nav">
        <NavLink to="/" end className={link}>Dashboard</NavLink>
        <NavLink to="/stage" className={link}>
          Stage{stage.panels.length > 0 && <span className="navCount">{stage.panels.length}</span>}
        </NavLink>
        <NavLink to="/workspaces" className={link}>Workspaces</NavLink>
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
  const { orbState, transcript, files, log, speechBeat, speech, music, activate } = useJarvis();
  const [panelWidth, setPanelWidth] = useState(380);
  const [tab,        setTabState]   = useState<Tab>(loadTab);
  const [busyHint,   setBusyHint]   = useState(false);
  const dragging = useRef(false);
  const bodyRef  = useRef<HTMLDivElement>(null);

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

  const onOrbClick = useCallback(async () => {
    if (!(await activate())) {
      setBusyHint(true);
      setTimeout(() => setBusyHint(false), 1800);
    }
  }, [activate]);

  const lastJarvis = [...log].reverse().find(m => m.role === 'jarvis');
  const caption = orbState === 'speaking' && lastJarvis ? lastJarvis.text : transcript;
  const captionLabel = orbState === 'speaking' && lastJarvis ? 'JARVIS' : 'LAST COMMAND';

  return (
    <div ref={bodyRef} className="body" style={{ ['--panel-w' as string]: `${panelWidth}px` }}>
      <main className={`hero hero-${orbState}`}>
        <div className="heroGrid" />
        <div className="orbArea">
          <ParticleOrb state={orbState} beat={speechBeat} speech={speech} onActivate={onOrbClick} />
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
  );
}

function Shell() {
  const { pathname } = useLocation();
  return (
    <>
      <div className="shell">
        <Header />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/workspaces" element={<WorkspaceManager />} />
          <Route path="*" element={null} />
        </Routes>
      </div>
      {/* Always mounted, at the same place in the tree, so live pages, the editor
          and the globe keep their state while you're on another tab */}
      <StagePage visible={pathname === '/stage'} />
    </>
  );
}

export default function App() {
  return (
    <JarvisStatusProvider>
      <StageProvider>
        <Shell />
      </StageProvider>
    </JarvisStatusProvider>
  );
}
