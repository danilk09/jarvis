import { useCallback, useEffect, useRef, useState } from 'react';
import { context } from './phoneAudio';

// Recording a command on the phone.
//
// Which mic: AirPods (and other Bluetooth headsets) record through the narrow-band
// headset link, which Whisper hears much worse than the phone's own mic, and recording
// through them also drops your music to call quality. So the phone's built-in mic is the
// default; the ⚙ sheet lets you pick another, remembered in localStorage.
//
// It stops by itself after a pause once you've spoken (or tap to stop), up to MAX_S.

const KEY = 'jarvis.mic';
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

/** The mic to use: your choice, else the phone's own (not a headset), else the default. */
async function pick(): Promise<string | undefined> {
  const mics = await listMics();
  const choice = stored();
  if (choice !== 'auto' && mics.some(m => m.id === choice)) return choice;
  const builtIn = mics.find(m => m.label && !HEADSET.test(m.label) && BUILT_IN.test(m.label))
               || mics.find(m => m.label && !HEADSET.test(m.label));
  return builtIn?.id || undefined;
}

export function useRecorder(onDone: (audio: Blob, mic: string) => void) {
  const [recording, setRecording] = useState(false);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState('');
  const rec = useRef<MediaRecorder | null>(null);
  const stopTimer = useRef<() => void>(() => {});
  const done = useRef(onDone);
  done.current = onDone;

  const stop = useCallback(() => {
    if (rec.current && rec.current.state !== 'inactive') rec.current.stop();
  }, []);

  const start = useCallback(async () => {
    if (rec.current && rec.current.state !== 'inactive') return;
    setError('');
    let stream: MediaStream;
    try {
      const id = await pick();
      const audio: MediaTrackConstraints = { channelCount: 1 };
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: id ? { ...audio, deviceId: { exact: id } } : audio });
      } catch (e: any) {
        if (!id || e?.name !== 'OverconstrainedError') throw e;
        stream = await navigator.mediaDevices.getUserMedia({ audio });   // that mic is gone
      }
      // The very first time, mic names are hidden until you allow access: now that they're
      // visible, switch to the phone's own mic if the default was a headset
      if (!id) {
        const better = await pick();
        if (better && better !== stream.getAudioTracks()[0]?.getSettings().deviceId) {
          stream.getTracks().forEach(t => t.stop());
          stream = await navigator.mediaDevices.getUserMedia({ audio: { ...audio, deviceId: { exact: better } } });
        }
      }
    } catch {
      setError('Microphone access denied');
      return;
    }
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
      if (blob.size > 0) done.current(blob, micLabel);
    };
    r.start();
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

  useEffect(() => () => stop(), [stop]);

  return { recording, level, error, start, stop };
}
