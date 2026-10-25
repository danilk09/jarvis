import { useCallback, useEffect, useRef, useState } from 'react';
import { useJarvis } from '../JarvisStatus';
import { useStage, Panel } from '../stage/StageContext';
import PhoneStage from './PhoneStage';
import { dayLabel, useHistory, Day } from '../history';
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

interface Listen { question: string; until: number; source: string | null; to?: string[] }

/** Meant for the phone: the brain's "to" (M4: at the PC when you're there, else here);
 *  older brains: the phone's own turns and Jarvis's own lines. */
function forPhone(ev: { source?: string | null; to?: string[] } | null) {
  if (!ev) return false;
  return ev.to ? ev.to.includes('phone') : ev.source === 'phone' || !ev.source;
}

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
      const forMe = (l: Listen | null) => (forPhone(l) ? l : null);
      if (msg.type === 'hello') setListen(forMe(msg.listen));
      else if (msg.type === 'listen') setListen(forMe(msg));
      else if (msg.type === 'listen_end') setListen(null);
      else if (msg.type === 'line') speak.current(msg.id, msg.text, forPhone(msg) && !msg.spoken);
    };
    return () => es.close();
  }, []);

  return { listen, connected };
}

// ── Settings sheet: voice on/off, which mic ──────────────────────────────────
function Settings({ onClose, speakOn, setSpeakOn, autoListen, setAutoListen }: {
  onClose: () => void; speakOn: boolean; setSpeakOn: (v: boolean) => void;
  autoListen: boolean; setAutoListen: (v: boolean) => void;
}) {
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
        <label className="phRow">
          <span>When Jarvis asks something, listen for my answer</span>
          <input type="checkbox" checked={autoListen} onChange={e => setAutoListen(e.target.checked)} />
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

// ── History: pick a day ──────────────────────────────────────────────────────
function DaySheet({ days, onPick, onClose }: { days: Day[]; onPick: (day: string) => void; onClose: () => void }) {
  return (
    <div className="phSheetBack" onClick={onClose}>
      <div className="phSheet" onClick={e => e.stopPropagation()}>
        <div className="phSheetTitle">EARLIER CONVERSATIONS</div>
        {days.length === 0 && <div className="phHint">Nothing saved yet.</div>}
        {days.map(d => (
          <button key={d.day} className="phDayRow" onClick={() => onPick(d.day)}>
            <span>{dayLabel(d.day)}</span>
            <span className="phDayCount">{d.count} lines</span>
          </button>
        ))}
        <button className="phPill phPillPrimary" onClick={onClose}>Close</button>
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
  const [autoListen, setAutoListen] = useState(() => setting('jarvis.autolisten', true));
  const [typing, setTyping] = useState(false);
  const [voiceReady, setVoiceReady] = useState(voice.ready());
  const [speaking, setSpeaking] = useState(false);
  const [settings, setSettings] = useState(false);
  const [toast, setToast] = useState<Panel | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [power, setPower] = useState('');
  const history = useHistory();
  const [daysOpen, setDaysOpen] = useState(false);
  const lineIds = useRef(new Map<string, number>());     // text → line id, to replay a reply
  const feedRef = useRef<HTMLDivElement>(null);
  const photoRef = useRef<HTMLInputElement>(null);
  const speakOnRef = useRef(speakOn);
  speakOnRef.current = speakOn;

  const clearFocus = useCallback(() => setFocus(null), []);
  const setAutoListenOn = (v: boolean) => { setAutoListen(v); saveSetting('jarvis.autolisten', v); };
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
    if (el && tab === 'chat') el.scrollTop = history.day ? 0 : el.scrollHeight;
  }, [log.length, tab, listen, orbState, history.day, history.entries]);

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

  const send = useCallback(async (opts: { text?: string; audio?: Blob; mic?: string; micMs?: number }) => {
    voice.hush();
    history.open(null);                                     // back to now
    const form = new FormData();
    const typed = (opts.text || '').trim();
    if (typed) form.append('text', typed);
    if (opts.audio) form.append('audio', opts.audio, /mp4|m4a|aac/.test(opts.audio.type) ? 'command.mp4' : 'command.webm');
    if (opts.mic) form.append('mic', opts.mic);
    if (opts.micMs) form.append('mic_ms', String(opts.micMs));   // how long the mic took to open (logged)
    if (photo) {
      form.append('image', photo.blob, 'photo.jpg');
      if (await post('/phone-image', form)) { URL.revokeObjectURL(photo.url); setPhoto(null); setText(''); }
      return;
    }
    if (!typed && !opts.audio) return;
    if (await post('/phone-command', form)) setText('');
  }, [photo, post, history.open]);   // eslint-disable-line react-hooks/exhaustive-deps

  const rec = useRecorder((audio, mic, micMs) => send({ audio, mic, micMs, text: photo ? text : '' }));

  // Jarvis asked something here: once he's finished saying it, open the mic for the answer
  // (gives up after a few quiet seconds; the answer bar stays for tapping or typing)
  const askedAt = useRef(0);
  const { recording, start: startRec } = rec;
  useEffect(() => {
    if (!listen || !autoListen || document.hidden || typing || recording || sending) return;
    if (askedAt.current === listen.until) return;
    if (speaking) return;                                   // wait for the question to finish
    const t = setTimeout(() => { askedAt.current = listen.until; startRec({ onset: 6 }); },
                         speakOn && voiceReady ? 250 : 900);
    return () => clearTimeout(t);
  }, [listen, autoListen, speaking, typing, sending, speakOn, voiceReady, recording, startRec]);

  const onMic = () => {
    if (rec.recording) { rec.stop(); return; }
    if (rec.starting) return;
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
  const shownLog = history.day ? history.entries : log;
  const busy = sending || rec.recording;
  const shownState = speaking ? 'speaking' : listen ? 'activated' : orbState;

  return (
    <div className="phone">
      <header className="phHead">
        <span className="brandMark" />
        <span className="phBrand">JARVIS</span>
        <span className={`phState phState-${shownState}`}><span className="phDot" />{connected && serverOk ? STATE_LABEL[shownState] || shownState : 'OFFLINE'}</span>
        <span className="phSpacer" />
        <button className={`phIconBtn ${speakOn ? 'phOn' : ''}`} aria-label="Speak replies" onClick={() => setSpeakOn(!speakOn)}>
          {speakOn ? '🔊' : '🔇'}
        </button>
        <button className="phIconBtn" aria-label="Earlier conversations"
                onClick={() => { history.loadDays(); setDaysOpen(true); }}>🕘</button>
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
          {history.day && (
            <div className="phPast">
              <span>{dayLabel(history.day)}</span>
              <button className="phPill" onClick={() => history.open(null)}>Back to now</button>
            </div>
          )}
          {shownLog.length === 0 && <div className="phEmpty">
            {history.day ? (history.loading ? 'Loading…' : 'Nothing that day.') : 'Tap the mic and talk to Jarvis.'}
          </div>}
          {shownLog.map((m, i) => (
            <div key={`${m.day}-${m.time}-${i}`} className="phMsgGroup">
              {m.day && m.day !== shownLog[i - 1]?.day && (i > 0 || shownLog.some(x => x.day !== m.day)) &&
                <div className="phDayDivider">{dayLabel(m.day)}</div>}
              <div className={`phMsg phMsg-${m.role}`} onClick={m.role === 'jarvis' ? () => replay(m.text) : undefined}>
                <div className="phMsgText">{m.text}</div>
                <div className="phMsgTime">{m.time.slice(0, 5)}{m.role === 'user' && m.source === 'pc' ? ' · PC' : ''}</div>
              </div>
            </div>
          ))}
          {!history.day && orbState === 'thinking' && !listen && <div className="phMsg phMsg-jarvis phTyping"><span /><span /><span /></div>}
        </div>
                <div className="phStageWrap" style={{ display: tab === 'stage' ? undefined : 'none' }}>
          <PhoneStage hidden={tab !== 'stage'} focus={focus} onFocused={clearFocus} />
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

      {/* talking, photos and typing belong to the chat (the Stage gets the whole screen) */}
      {tab === 'chat' && <footer className={`phDock ${rec.recording ? 'phDockRec' : ''}`}>
        {photo && (
          <div className="phPhoto">
            <img src={photo.url} alt="To send" />
            <span>Tap the mic to ask about it, or just send it</span>
            {!rec.recording && <button className="phPill phPillGo" disabled={busy} onClick={() => send({ text })}>Send</button>}
            <button className="phIconBtn" aria-label="Remove photo" onClick={() => { URL.revokeObjectURL(photo.url); setPhoto(null); }}>✕</button>
          </div>
        )}
        {(status || rec.error) && <div className="phStatus">{rec.error || status}</div>}
        {typing && (
          <div className="phTypeRow">
            <textarea className="phInput" rows={1} value={text} autoFocus
                      placeholder={listen ? 'Type your answer…' : photo ? 'Ask about the photo…' : 'Message Jarvis…'}
                      enterKeyHint="send" onChange={e => setText(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!busy) send({ text }); } }} />
            <button className="phSend" disabled={busy || !text.trim()} onClick={() => send({ text })} aria-label="Send">➤</button>
          </div>
        )}
        <div className="phDockRow">
          <button className="phSideBtn" aria-label="Photo" onClick={() => photoRef.current?.click()}>📷</button>
          <input ref={photoRef} type="file" accept="image/*" hidden onChange={onPick} />
          <div className="phMicWrap">
            <button className={`phBigMic ${rec.recording ? 'phBigMicOn' : ''} ${rec.starting ? 'phBigMicStarting' : ''} ${listen && !rec.recording ? 'phBigMicAsk' : ''}`}
                    disabled={sending} onClick={onMic} aria-label={rec.recording ? 'Stop' : 'Talk to Jarvis'}
                    style={{ ['--lvl' as string]: rec.level.toFixed(2) }}>
              {rec.recording ? <span className="phStopSq" /> : <MicIcon />}
            </button>
            <div className="phMicLabel">
              {rec.recording ? 'Listening… tap when done' : rec.starting ? 'Opening mic…' : sending ? 'Sending…' : listen ? 'Tap to answer'
                : photo ? 'Tap to ask about the photo' : 'Tap to talk'}
            </div>
          </div>
          <button className={`phSideBtn ${typing ? 'phSideOn' : ''}`} aria-label="Type instead" onClick={() => setTyping(!typing)}>⌨</button>
        </div>
      </footer>}

      {daysOpen && <DaySheet days={history.days} onClose={() => setDaysOpen(false)}
                             onPick={d => { setDaysOpen(false); setTab('chat'); history.open(d); }} />}
      {settings && <Settings onClose={() => setSettings(false)} speakOn={speakOn} setSpeakOn={setSpeakOn}
                             autoListen={autoListen} setAutoListen={setAutoListenOn} />}
    </div>
  );
}

function MicIcon() {
  return (
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
    </svg>
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
