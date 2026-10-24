import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useJarvis } from '../JarvisStatus';
import { useStage, stageApi, Highlight, Panel } from '../stage/StageContext';
import WebPanel from '../stage/panels/WebPanel';
import SummaryPanel from '../stage/panels/SummaryPanel';
import NotePanel from '../stage/panels/NotePanel';
import ImagePanel from '../stage/panels/ImagePanel';
import FileEditorPanel from '../stage/panels/FileEditorPanel';
import MapPanel from '../stage/panels/MapPanel';
import PlacesPanel from '../stage/panels/PlacesPanel';
import CodePanel from '../stage/panels/CodePanel';
import type { PanelProps } from '../stage/panels/types';
import * as voice from './phoneAudio';
import { useRecorder, listMics, chooseMic, Mic } from './useRecorder';
import '../stage/Stage.css';
import './Phone.css';

// The phone page: talk or type to Jarvis, hear and read everything he says (also what he
// says on his own: Claude Code finished, a reminder), answer his questions, and see the
// Stage. Commands go to POST /phone-command (/phone-image with a photo); what happens
// comes back on GET /api/client/events (core/turns.py) and the Stage's own event stream.

const MAX_SIDE = 1568;     // Jarvis shrinks photos to this anyway: smaller uploads on mobile data

const STATE_LABEL: Record<string, string> = {
  idle: 'STANDBY', activated: 'WAITING', thinking: 'THINKING', speaking: 'SPEAKING', error: 'ERROR',
};

const KIND_NAME: Record<string, string> = {
  page: 'PAGE', summary: 'KEY POINTS', file: 'FILE', image: 'IMAGE', note: 'NOTE', map: 'GLOBE',
  places: 'PLACES', code: 'CLAUDE CODE',
};

interface Listen { question: string; until: number; source: string | null }

function setting(key: string, fallback: boolean) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v === '1'; } catch { return fallback; }
}
function saveSetting(key: string, on: boolean) {
  try { localStorage.setItem(key, on ? '1' : '0'); } catch { /* storage unavailable */ }
}

// ── Live events: Jarvis's lines (spoken here), questions waiting for an answer ──
function useClientEvents(speakLine: (id: number, text: string, mine: boolean) => void) {
  const [listen, setListen] = useState<Listen | null>(null);
  const [connected, setConnected] = useState(false);
  const speak = useRef(speakLine);
  speak.current = speakLine;

  useEffect(() => {
    const es = new EventSource('/api/client/events');
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = ev => {
      let msg: any;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'line' || msg.type === 'user' || msg.type === 'state') window.dispatchEvent(new Event('jarvis:poke'));
      const forMe = (l: Listen | null) => (l && (l.source === 'phone' || !l.source) ? l : null);
      if (msg.type === 'hello') setListen(forMe(msg.listen));
      else if (msg.type === 'listen') setListen(forMe(msg));
      else if (msg.type === 'listen_end') setListen(null);
      // The phone's own turns, and whatever Jarvis says between turns; not replies at the PC
      else if (msg.type === 'line') speak.current(msg.id, msg.text, msg.source === 'phone' || !msg.source);
    };
    return () => es.close();
  }, []);

  return { listen, connected };
}

// ── Stage panels, stacked ────────────────────────────────────────────────────
function PanelContent(props: PanelProps) {
  switch (props.panel.kind) {
    case 'page':    return <WebPanel {...props} />;
    case 'summary': return <SummaryPanel {...props} />;
    case 'note':    return <NotePanel {...props} />;
    case 'image':   return <ImagePanel {...props} />;
    case 'file':    return <FileEditorPanel {...props} />;
    case 'map':     return <MapPanel {...props} />;
    case 'places':  return <PlacesPanel {...props} />;
    case 'code':    return <CodePanel {...props} />;
    default:        return <div className="panelEmpty">Unknown panel</div>;
  }
}

function StageList({ hidden, focus }: { hidden: boolean; focus: string | null }) {
  const { stage } = useStage();
  const refs = useRef(new Map<string, HTMLDivElement>());
  const latest = useMemo(() => {
    const byPanel = new Map<string, Highlight>();
    for (const h of stage.highlights) byPanel.set(h.panel, h);
    return byPanel;
  }, [stage.highlights]);
  // numbered like the PC's Stage ("close panel 2"); newest at the top
  const numbered = stage.panels.map((p, n) => ({ p, n: n + 1 }));
  const shown = [...numbered].sort((a, b) => b.p.created - a.p.created);

  useEffect(() => {
    if (focus && !hidden) refs.current.get(focus)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [focus, hidden]);

  if (!stage.panels.length) {
    return <div className="phEmpty">Nothing on the Stage. Ask Jarvis to show you something: a map, a page, your agenda…</div>;
  }
  return (
    <div className="phStage">
      {shown.map(({ p, n }) => (
        <div key={p.id} ref={el => { if (el) refs.current.set(p.id, el); else refs.current.delete(p.id); }}
             className={`phCard phCard-${p.kind} panel panel-${p.kind} ${latest.has(p.id) ? 'panelMarked' : ''}`}>
          <div className="phCardHead">
            <span className="panelNum">{n}</span>
            <span className="panelKind">{KIND_NAME[p.kind]}</span>
            <span className="phCardTitle">{p.title}</span>
            <button className="phIconBtn" aria-label="Close" onClick={() => stageApi.post(`panel/${p.id}/close`)}>✕</button>
          </div>
          <div className="panelBody phCardBody">
            <PanelContent panel={p} highlight={latest.get(p.id) || null} hidden={hidden}
                          all={stage.panels as Panel[]} highlights={stage.highlights} />
          </div>
        </div>
      ))}
      {stage.undo && <button className="phPill phUndo" onClick={() => stageApi.post('restore')}>Undo: {stage.undo}</button>}
    </div>
  );
}

// ── Settings sheet: voice on/off, which mic ──────────────────────────────────
function Settings({ onClose, speakOn, setSpeakOn }: { onClose: () => void; speakOn: boolean; setSpeakOn: (v: boolean) => void }) {
  const [mics, setMics] = useState<Mic[]>([]);
  const [choice, setChoice] = useState(() => { try { return localStorage.getItem('jarvis.mic') || 'auto'; } catch { return 'auto'; } });
  useEffect(() => { listMics().then(setMics); }, []);
  const pick = (id: string) => { setChoice(id); chooseMic(id); };
  const named = mics.some(m => !/^Microphone \d+$/.test(m.label));
  return (
    <div className="phSheetBack" onClick={onClose}>
      <div className="phSheet" onClick={e => e.stopPropagation()}>
        <div className="phSheetTitle">SETTINGS</div>
        <label className="phRow">
          <span>Speak replies out loud</span>
          <input type="checkbox" checked={speakOn} onChange={e => setSpeakOn(e.target.checked)} />
        </label>
        <div className="phSheetLabel">MICROPHONE</div>
        <label className="phRow">
          <span>Phone's own mic (recommended)</span>
          <input type="radio" name="mic" checked={choice === 'auto'} onChange={() => pick('auto')} />
        </label>
        {mics.map(m => (
          <label key={m.id} className="phRow">
            <span>{m.label}</span>
            <input type="radio" name="mic" checked={choice === m.id} onChange={() => pick(m.id)} />
          </label>
        ))}
        {!named && <div className="phHint">Mic names appear after you've recorded once.</div>}
        <div className="phHint">
          AirPods record through the low-quality headset link, so Jarvis hears them worse. With the phone's own mic
          your AirPods keep playing at full quality.
          <br /><br />
          To stop Safari asking for the mic every time: tap <b>aA</b> in the address bar → Website Settings →
          Microphone → <b>Allow</b>. Added to your Home Screen: iPhone Settings → Apps → Safari → Microphone → Allow.
        </div>
        <button className="phPill phPillPrimary" onClick={onClose}>Done</button>
      </div>
    </div>
  );
}

// ── The page ──────────────────────────────────────────────────────────────────
export default function PhonePage() {
  const { orbState, log, serverOk } = useJarvis();
  const { stage } = useStage();
  const [tab, setTab] = useState<'chat' | 'stage'>('chat');
  const [text, setText] = useState('');
  const [status, setStatus] = useState('');
  const [sending, setSending] = useState(false);
  const [photo, setPhoto] = useState<{ blob: Blob; url: string } | null>(null);
  const [speakOn, setSpeakOnState] = useState(() => setting('jarvis.speak', true));
  const [voiceReady, setVoiceReady] = useState(voice.ready());
  const [speaking, setSpeaking] = useState(false);
  const [settings, setSettings] = useState(false);
  const [toast, setToast] = useState<Panel | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [power, setPower] = useState('');
  const lineIds = useRef(new Map<string, number>());     // text → line id, to replay a reply
  const feedRef = useRef<HTMLDivElement>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const libraryRef = useRef<HTMLInputElement>(null);
  const speakOnRef = useRef(speakOn);
  speakOnRef.current = speakOn;

  const setSpeakOn = (v: boolean) => { setSpeakOnState(v); saveSetting('jarvis.speak', v); if (!v) voice.hush(); };

  useEffect(() => voice.onSpeaking(setSpeaking), []);

  // The browser bar matches the phone's blue theme (the dashboard's is green)
  useEffect(() => {
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', '#0a0a0f');
    document.body.style.background = '#0a0a0f';
  }, []);

  // The first tap anywhere unlocks sound (iOS), and again after the page was in the background
  useEffect(() => {
    const unlock = () => { voice.unlock(); setTimeout(() => setVoiceReady(voice.ready()), 50); };
    const vis = () => { if (!document.hidden) setTimeout(() => setVoiceReady(voice.ready()), 300); };
    document.addEventListener('pointerdown', unlock, true);
    document.addEventListener('visibilitychange', vis);
    return () => { document.removeEventListener('pointerdown', unlock, true); document.removeEventListener('visibilitychange', vis); };
  }, []);

  const { listen, connected } = useClientEvents((id, line, mine) => {
    lineIds.current.set(line, id);
    if (mine && speakOnRef.current && !document.hidden) voice.say(`/api/say/${id}`);
  });

  // Pi only: the power switch (scripts/pi/control.py, port 5152) stays up while Jarvis is off
  useEffect(() => {
    const url = `${window.location.protocol}//${window.location.hostname}:5152/`;
    fetch(url, { mode: 'no-cors', cache: 'no-store' }).then(() => setPower(url)).catch(() => {});
  }, []);

  // Something new on the Stage: say so in the chat, one tap to see it
  const known = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!stage.rev) return;              // not loaded yet: the first state is what was already there
    const ids = new Set(stage.panels.map(p => p.id));
    if (known.current) {
      const fresh = stage.panels.filter(p => !known.current!.has(p.id));
      if (fresh.length) setToast(fresh[fresh.length - 1]);
    }
    known.current = ids;
  }, [stage.panels, stage.rev]);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 9000);
    return () => clearTimeout(t);
  }, [toast]);

  // Keep the newest message in view
  useEffect(() => {
    const el = feedRef.current;
    if (el && tab === 'chat') el.scrollTop = el.scrollHeight;
  }, [log.length, tab, listen, orbState]);

  const post = useCallback(async (url: string, form: FormData) => {
    setSending(true);
    setStatus('Sending…');
    try {
      const res = await fetch(url, { method: 'POST', body: form });
      const data = await res.json().catch(() => ({}));
      setStatus(res.ok ? '' : (data.error === 'no speech detected' ? "I didn't hear anything." : `Error: ${data.error || res.status}`));
      return res.ok;
    } catch {
      setStatus('No connection. Is Tailscale on?');
      return false;
    } finally {
      setSending(false);
    }
  }, []);

  const send = useCallback(async (opts: { text?: string; audio?: Blob; mic?: string }) => {
    voice.hush();
    const form = new FormData();
    const typed = (opts.text || '').trim();
    if (typed) form.append('text', typed);
    if (opts.audio) form.append('audio', opts.audio, /mp4|m4a|aac/.test(opts.audio.type) ? 'command.mp4' : 'command.webm');
    if (opts.mic) form.append('mic', opts.mic);
    if (photo) {
      form.append('image', photo.blob, 'photo.jpg');
      if (await post('/phone-image', form)) { URL.revokeObjectURL(photo.url); setPhoto(null); setText(''); }
      return;
    }
    if (!typed && !opts.audio) return;
    if (await post('/phone-command', form)) setText('');
  }, [photo, post]);

  const rec = useRecorder((audio, mic) => send({ audio, mic, text: photo ? text : '' }));

  const onMic = () => {
    if (rec.recording) { rec.stop(); return; }
    voice.hush();
    rec.start();
  };

  const loadPhoto = (file: File) => {
    setStatus('Preparing photo…');
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(img.naturalWidth * scale);
      canvas.height = Math.round(img.naturalHeight * scale);
      canvas.getContext('2d')!.drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      canvas.toBlob(blob => {
        if (!blob) { setStatus("Couldn't read that photo."); return; }
        setPhoto({ blob, url: URL.createObjectURL(blob) });
        setStatus('');
      }, 'image/jpeg', 0.85);
    };
    img.onerror = () => { URL.revokeObjectURL(url); setStatus("Couldn't open that photo."); };
    img.src = url;
  };
  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = '';
    if (f) loadPhoto(f);
  };

  const replay = (line: string) => {
    voice.unlock();
    const id = lineIds.current.get(line);
    voice.hush();
    if (id && voice.say(`/api/say/${id}`)) return;
    if ('speechSynthesis' in window) {      // an older line: the phone's own voice
      speechSynthesis.cancel();
      speechSynthesis.speak(new SpeechSynthesisUtterance(line));
    }
  };

  const quick = listen ? quickReplies(listen.question) : [];
  const busy = sending || rec.recording;
  const shownState = speaking ? 'speaking' : listen ? 'activated' : orbState;

  return (
    <div className="phone">
      <header className="phHead">
        <span className="brandMark" />
        <span className="phBrand">J.A.R.V.I.S</span>
        <span className={`phState phState-${shownState}`}><span className="phDot" />{connected && serverOk ? STATE_LABEL[shownState] || shownState : 'OFFLINE'}</span>
        <span className="phSpacer" />
        <button className={`phIconBtn ${speakOn ? 'phOn' : ''}`} aria-label="Speak replies" onClick={() => setSpeakOn(!speakOn)}>
          {speakOn ? '🔊' : '🔇'}
        </button>
        <button className="phIconBtn" aria-label="Settings" onClick={() => setSettings(true)}>⚙</button>
        {power && <a className="phIconBtn" href={power} aria-label="Power">⏻</a>}
      </header>

      <nav className="phTabs">
        <button className={`phTab ${tab === 'chat' ? 'phTabOn' : ''}`} onClick={() => setTab('chat')}>CHAT</button>
        <button className={`phTab ${tab === 'stage' ? 'phTabOn' : ''}`} onClick={() => setTab('stage')}>
          STAGE{stage.panels.length > 0 && <span className="navCount">{stage.panels.length}</span>}
        </button>
      </nav>

      {speakOn && !voiceReady && (
        <button className="phBanner" onClick={() => { voice.unlock(); setTimeout(() => setVoiceReady(voice.ready()), 50); }}>
          Tap to turn on Jarvis's voice
        </button>
      )}

      <main className="phMain">
        <div ref={feedRef} className="phFeed" style={{ display: tab === 'chat' ? undefined : 'none' }}>
          {log.length === 0 && <div className="phEmpty">Say or type something to Jarvis.</div>}
          {log.map((m, i) => (
            <div key={`${m.time}-${i}`} className={`phMsg phMsg-${m.role}`}
                 onClick={m.role === 'jarvis' ? () => replay(m.text) : undefined}>
              <div className="phMsgText">{m.text}</div>
              <div className="phMsgTime">{m.time.slice(0, 5)}</div>
            </div>
          ))}
          {orbState === 'thinking' && !listen && <div className="phMsg phMsg-jarvis phTyping"><span /><span /><span /></div>}
        </div>
        {/* always mounted: the globe and the editor keep their state on the chat tab */}
        <div className="phStageWrap" style={{ display: tab === 'stage' ? undefined : 'none' }}>
          <StageList hidden={tab !== 'stage'} focus={focus} />
        </div>
      </main>

      {toast && tab === 'chat' && (
        <button className="phToast" onClick={() => { setTab('stage'); setFocus(toast.id); setToast(null); }}>
          On the Stage: <b>{toast.title}</b> — View
        </button>
      )}

      {listen && (
        <div className="phAnswer">
          <div className="phAnswerQ">{listen.question ? `Jarvis is waiting for your answer: “${listen.question}”` : 'Jarvis is waiting for your answer'}</div>
          {quick.length > 0 && (
            <div className="phQuick">
              {quick.map(q => <button key={q} className="phPill" disabled={busy} onClick={() => send({ text: q })}>{q}</button>)}
            </div>
          )}
        </div>
      )}

      {photo && (
        <div className="phPhoto">
          <img src={photo.url} alt="To send" />
          <span>Ask about it, or just send it</span>
          <button className="phIconBtn" aria-label="Remove photo" onClick={() => { URL.revokeObjectURL(photo.url); setPhoto(null); }}>✕</button>
        </div>
      )}

      {(status || rec.error) && <div className="phStatus">{rec.error || status}</div>}

      <footer className="phComposer">
        <button className="phIconBtn" aria-label="Camera" onClick={() => cameraRef.current?.click()}>📷</button>
        <button className="phIconBtn" aria-label="Photos" onClick={() => libraryRef.current?.click()}>🖼</button>
        <input ref={cameraRef} type="file" accept="image/*" capture="environment" hidden onChange={onPick} />
        <input ref={libraryRef} type="file" accept="image/*" hidden onChange={onPick} />
        <textarea className="phInput" rows={1} value={text} placeholder={listen ? 'Type your answer…' : photo ? 'Ask about the photo…' : 'Message Jarvis…'}
                  enterKeyHint="send" onChange={e => setText(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!busy) send({ text }); } }} />
        {text.trim() || (photo && !rec.recording)
          ? <button className="phSend" disabled={busy} onClick={() => send({ text })} aria-label="Send">➤</button>
          : <button className={`phMic ${rec.recording ? 'phMicOn' : ''}`} disabled={sending} onClick={onMic}
                    aria-label={rec.recording ? 'Stop recording' : 'Speak'}
                    style={{ ['--lvl' as string]: rec.level.toFixed(2) }}>
              {rec.recording ? '■' : '🎙'}
            </button>}
      </footer>

      {settings && <Settings onClose={() => setSettings(false)} speakOn={speakOn} setSpeakOn={setSpeakOn} />}
    </div>
  );
}

/** One-tap answers for a question Jarvis is waiting on. */
function quickReplies(question: string): string[] {
  const q = question.toLowerCase();
  if (/should i let it|allow|permission|needs your ok/.test(q)) return ['Allow it', 'No', 'Never mind'];
  if (/go ahead|build it|shall i|should i|do you want|want me to|is that right|correct\?|okay\?|ok\?/.test(q))
    return ['Yes', 'No', 'Never mind'];
  return ['Never mind'];
}
