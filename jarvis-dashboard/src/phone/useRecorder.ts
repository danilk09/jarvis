import { useCallback, useEffect, useRef, useState } from 'react';
import { context } from './phoneAudio';

// Recording a command on the phone.
//
// Which mic: AirPods (and other Bluetooth headsets) record through the narrow-band
// headset link, which Whisper hears much worse than the phone's own mic, and recording
// through them also drops your music to call quality. So the phone's built-in mic is the
// default; the ⚙ sheet lets you pick another, remembered in localStorage.
//
// It stops by itself after a pause once you've spoken (or tap to stop), up to MAX_S. Started
// with { onset } (Jarvis asked you something), it gives up quietly if you don't start talking
// within that many seconds.

const KEY = 'jarvis.mic';
const FOUND = 'jarvis.mic.found';   // the phone's own mic, once found: opened directly next time
const MAX_S = 20;
const SILENCE_S = 1.3;
const HEADSET = /airpods|bluetooth|headset|beats|buds|hands-?free|hfp/i;
const BUILT_IN = /iphone|ipad|built-?in|internal|default|phone|microphone/i;

export interface Mic { id: string; label: string }

function stored(): string {
  try { return localStorage.getItem(KEY) || 'auto'; } catch { return 'auto'; }
}

export function chooseMic(id: string) {
  try { localStorage.setItem(KEY, id); } catch { /* storage unavailable */ }
}

export async function listMics(): Promise<Mic[]> {
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter(d => d.kind === 'audioinput')
              .map((d, n) => ({ id: d.deviceId, label: d.label || `Microphone ${n + 1}` }));
  } catch {
    return [];
  }
}

function found(): string {
  try { return localStorage.getItem(FOUND) || ''; } catch { return ''; }
}

/** The phone's own mic among the visible ones (iOS shows names only once the page has used the mic). */
async function builtIn(): Promise<string | undefined> {
  const mics = await listMics();
  const m = mics.find(x => x.label && !HEADSET.test(x.label) && BUILT_IN.test(x.label))
         || mics.find(x => x.label && !HEADSET.test(x.label));
  return m?.id || undefined;
}

/** The mic to open: your choice, else the phone's own mic remembered from last time. No device
 *  list needed first: iOS hides it until the mic has been used, which used to mean opening the
 *  default mic (the AirPods), then closing it and opening the right one: two slow opens. */
function pick(): string | undefined {
  const choice = stored();
  return choice !== 'auto' ? choice : (found() || undefined);
}

export function useRecorder(onDone: (audio: Blob, mic: string, ms: number) => void) {
  const [recording, setRecording] = useState(false);
  const [starting, setStarting] = useState(false);   // tapped, mic still opening
  const [level, setLevel] = useState(0);
  const [error, setError] = useState('');
  const rec = useRef<MediaRecorder | null>(null);
  const stopTimer = useRef<() => void>(() => {});
  const done = useRef(onDone);
  done.current = onDone;
  const discard = useRef(false);

  const stop = useCallback(() => {
    if (rec.current && rec.current.state !== 'inactive') rec.current.stop();
  }, []);

  const start = useCallback(async (opts: { onset?: number } = {}) => {
    if (rec.current && rec.current.state !== 'inactive') return;
    setError('');
    setStarting(true);
    discard.current = false;
    const tapped = performance.now();
    let stream: MediaStream;
    try {
      const id = pick();
      const audio: MediaTrackConstraints = { channelCount: 1 };
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: id ? { ...audio, deviceId: { exact: id } } : audio });
      } catch (e: any) {
        if (!id || (e?.name !== 'OverconstrainedError' && e?.name !== 'NotFoundError')) throw e;
        try { localStorage.removeItem(FOUND); } catch { /* ignore */ }
        stream = await navigator.mediaDevices.getUserMedia({ audio });   // that mic is gone
      }
      // Only ever once per phone: the default was a headset, and the phone's own mic is now
      // visible. Remember it (opened directly from now on); switch to it if it's cheap to.
      if (stored() === 'auto' && !found()) {
        const own = await builtIn();
        const now = stream.getAudioTracks()[0]?.getSettings().deviceId;
        if (own) {
          try { localStorage.setItem(FOUND, own); } catch { /* ignore */ }
          if (own !== now) {
            stream.getTracks().forEach(t => t.stop());
            stream = await navigator.mediaDevices.getUserMedia({ audio: { ...audio, deviceId: { exact: own } } });
          }
        }
      }
    } catch {
      setStarting(false);
      if (!opts.onset) setError('Microphone access denied');   // (an automatic start may just not be allowed)
      return;
    }
    const openMs = Math.round(performance.now() - tapped);
    const track = stream.getAudioTracks()[0];
    const micLabel = track?.label || '';
    const chunks: Blob[] = [];
    const r = new MediaRecorder(stream);
    rec.current = r;
    r.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
    r.onstop = () => {
      stopTimer.current();
      stream.getTracks().forEach(t => t.stop());
      setRecording(false);
      setLevel(0);
      rec.current = null;
      const blob = new Blob(chunks, { type: r.mimeType || 'audio/mp4' });
      if (blob.size > 0 && !discard.current) done.current(blob, micLabel, openMs);
    };
    r.start();
    setStarting(false);
    setRecording(true);

    // Auto-stop: after you've spoken, SILENCE_S of quiet ends it
    const ctx = context();
    const started = performance.now();
    let raf = 0, spoke = false, quietSince = 0, floor = 0.01;
    let src: MediaStreamAudioSourceNode | null = null;
    if (ctx && ctx.state === 'running') {
      src = ctx.createMediaStreamSource(stream);
      const an = ctx.createAnalyser();
      an.fftSize = 1024;
      src.connect(an);
      const buf = new Float32Array(an.fftSize);
      const tick = () => {
        an.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        const rms = Math.sqrt(sum / buf.length);
        const t = (performance.now() - started) / 1000;
        if (t < 0.4) floor = Math.max(floor, rms);          // the room, before you start
        const loud = rms > Math.max(0.02, floor * 2.5);
        setLevel(Math.min(1, rms * 8));
        if (loud) { spoke = true; quietSince = 0; }
        else if (spoke && !quietSince) quietSince = t;
        if (!spoke && opts.onset && t > opts.onset) { discard.current = true; stop(); return; }
        if ((spoke && quietSince && t - quietSince > SILENCE_S) || t > MAX_S) { stop(); return; }
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    } else {
      const t = setTimeout(stop, MAX_S * 1000);
      raf = -1;
      stopTimer.current = () => clearTimeout(t);
    }
    if (raf !== -1) stopTimer.current = () => { cancelAnimationFrame(raf); src?.disconnect(); };
  }, [stop]);

  /** Stop without sending. */
  const cancel = useCallback(() => { discard.current = true; stop(); }, [stop]);

  useEffect(() => () => stop(), [stop]);

  return { recording, starting, level, error, start, stop, cancel };
}
