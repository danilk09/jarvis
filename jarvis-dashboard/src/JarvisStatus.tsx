import { createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode } from 'react';
import type { OrbState, SpeechEnvelope } from './ParticleOrb';
import type { JarvisFile } from './FilePanel';
import type { LogEntry } from './ActivityFeed';
import type { MusicState } from './MusicCard';

// Polls GET /status for the whole app, so the Dashboard and the Stage share one
// view of Jarvis (state, captions, speech envelope for the orbs, music, files).

export interface JarvisStatus {
  orbState: OrbState;
  transcript: string;
  files: JarvisFile[];
  log: LogEntry[];
  serverOk: boolean;
  speechBeat: number;
  speech: SpeechEnvelope | null;
  music: MusicState;
  activate: () => Promise<boolean>;
}

const StatusContext = createContext<JarvisStatus | null>(null);

export function useJarvis() {
  const ctx = useContext(StatusContext);
  if (!ctx) throw new Error('useJarvis outside JarvisStatusProvider');
  return ctx;
}

// Shown when the backend isn't running (e.g. `npm start` on its own)
const DEMO_CYCLE: { state: OrbState; duration: number }[] = [
  { state: 'idle',      duration: 3200 },
  { state: 'activated', duration: 1800 },
  { state: 'thinking',  duration: 1400 },
  { state: 'speaking',  duration: 3600 },
  { state: 'idle',      duration: 2000 },
];

export function JarvisStatusProvider({ children }: { children: ReactNode }) {
  const [orbState,   setOrbState]   = useState<OrbState>('idle');
  const [transcript, setTranscript] = useState('');
  const [files,      setFiles]      = useState<JarvisFile[]>([]);
  const [log,        setLog]        = useState<LogEntry[]>([]);
  const [serverOk,   setServerOk]   = useState(false);
  const [speechBeat, setSpeechBeat] = useState(0);
  const [speech,     setSpeech]     = useState<SpeechEnvelope | null>(null);
  const [music,      setMusic]      = useState<MusicState>({ playing: false, paused: false, currentSong: '', history: [] });
  // Refs, not state: poll() is created once on mount, so state here would be a stale closure
  const fileCount = useRef(-1);
  const speechId  = useRef(0);
  const logSig    = useRef('');
  const musicSig  = useRef('');
  const filesSig  = useRef('');
  const demoRef   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const demoBeat  = useRef<ReturnType<typeof setInterval> | null>(null);
  const demoIdx   = useRef(0);

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
          // Same count but edited content (saved from the Stage) also arrives here
          const sig = data.files.map((f: JarvisFile) => f.name + f.size).join('|');
          if (sig !== filesSig.current) { filesSig.current = sig; setFiles(data.files); }
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
        const sig = last ? `${lg.length}|${last.time}|${last.text}|${data.log_rev ?? 0}` : '';
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
      // Fast enough to pick up speech envelopes promptly; back off when the window is hidden
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
      return !!(await res.json()).ok;
    } catch {
      return false;
    }
  }, []);

  return (
    <StatusContext.Provider value={{ orbState, transcript, files, log, serverOk, speechBeat, speech, music, activate }}>
      {children}
    </StatusContext.Provider>
  );
}
