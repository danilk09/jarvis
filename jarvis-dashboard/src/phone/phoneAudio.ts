// Jarvis's voice on the phone: each line arrives as Piper speech (GET /api/say/<id>) and is
// played through Web Audio, one after another.
//
// iOS only lets a page make sound after a tap, so the first tap anywhere "unlocks" an
// AudioContext (unlock()). Once unlocked it can play whenever the page is open; while the
// phone is locked or the page is in the background nothing plays (the line still shows,
// and ntfy notifications cover that case).
//
// navigator.audioSession (Safari 16.4+): "transient" ducks your music app for the line
// instead of stopping it, like navigation prompts do.

type Listener = (speaking: boolean) => void;

let ctx: AudioContext | null = null;
let current: AudioBufferSourceNode | null = null;
let playing = false;
const queue: Promise<AudioBuffer | null>[] = [];
const listeners = new Set<Listener>();

function session(type: string) {
  const s = (navigator as any).audioSession;
  if (s) { try { s.type = type; } catch { /* unsupported type */ } }
}

export function unlock() {
  if (!ctx) {
    const AC = window.AudioContext || (window as any).webkitAudioContext;
    if (!AC) return;
    ctx = new AC();
  }
  if (ctx.state !== 'running') ctx.resume().catch(() => {});
  // iOS also wants a sound started inside the tap
  const b = ctx.createBuffer(1, 1, 22050);
  const s = ctx.createBufferSource();
  s.buffer = b;
  s.connect(ctx.destination);
  s.start(0);
}

export function ready() {
  return !!ctx && ctx.state === 'running';
}

export function context() {
  return ctx;
}

export function onSpeaking(fn: Listener) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

function setPlaying(p: boolean) {
  playing = p;
  listeners.forEach(fn => fn(p));
}

async function load(url: string): Promise<AudioBuffer | null> {
  try {
    const res = await fetch(url);
    if (!res.ok || !ctx) return null;
    const data = await res.arrayBuffer();
    return await ctx.decodeAudioData(data);
  } catch {
    return null;
  }
}

/** Queue a line's audio. Downloading starts right away, so the next line is ready when this one ends. */
export function say(url: string) {
  if (!ready()) return false;
  queue.push(load(url));
  if (!playing) next();
  return true;
}

async function next() {
  const pending = queue.shift();
  if (!pending || !ctx) {
    setPlaying(false);
    session('auto');
    return;
  }
  setPlaying(true);
  const buffer = await pending;
  if (!buffer || !ctx) { next(); return; }
  session('transient');
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  src.onended = () => { if (current === src) { current = null; next(); } };
  current = src;
  src.start(0);
}

/** Stop talking now and drop anything queued (you started speaking, or tapped stop). */
export function hush() {
  queue.length = 0;
  const src = current;
  current = null;
  if (src) { try { src.stop(); } catch { /* already ended */ } }
  setPlaying(false);
  session('auto');
}
