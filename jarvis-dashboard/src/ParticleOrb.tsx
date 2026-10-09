import { useEffect, useRef } from 'react';

// 2-D canvas particle orb. Particles live on a 3-D spherical shell displaced by
// Perlin noise, are rotated (auto-spin + a tilt that follows the mouse) and
// projected with perspective. Each state has its own colour/speed targets reached
// through per-parameter damped springs, so state changes feel layered.
//
// Speech: Jarvis renders every sentence silently to compute a 16-band spectral
// envelope (core/tts.py). It is replayed here against the wall clock, so the orb
// moves with the actual audio like an equaliser: each particle is pushed outward by
// the band that matches its elevation on screen (lows at the equator, highs at the
// poles), and a radial bar ring shows the same bands. Without an envelope (phone,
// older backend) word-boundary beats drive a synthetic version.
//
// Interaction: the cursor repels particles and tilts the orb; clicking sends a
// shockwave through the shell and activates Jarvis.

// Overall brightness of the orb's colours (1 = full). Only colour — motion is untouched.
const GLOW = 0.72;

export type OrbState = 'idle' | 'activated' | 'thinking' | 'speaking' | 'error';

export interface SpeechEnvelope {
  id: number;
  start: number;            // epoch ms when the audio started
  end: number | null;       // set if the speech was cut off
  frameMs: number;
  bands: number[][];        // [frame][band], 0-99
  level: number[];          // [frame], 0-99
}

interface Props {
  state: OrbState;
  beat?: number;
  speech?: SpeechEnvelope | null;
  onActivate?: () => void;
  /** Small toolbar orb: fewer particles, no HUD arcs. */
  compact?: boolean;
  /** Stop drawing (e.g. while its page is hidden). */
  paused?: boolean;
}

const NB          = 16;     // spectral bands
const LATENCY_MS  = 70;     // audio output latency: shift visuals to match what you hear
const REF         = 440;    // geometry is authored for a 440 px box and scaled to fit
const R0          = 118;    // base shell radius in reference units
const RING_BARS   = 72;

/* ── Perlin noise ──────────────────────────────────────────────────────────── */
function makeNoise3D() {
  const perm = new Uint8Array(512);
  for (let i = 0; i < 256; i++) perm[i] = perm[i + 256] = (Math.random() * 256) | 0;
  const fade = (t: number) => t * t * t * (t * (t * 6 - 15) + 10);
  const lerp  = (a: number, b: number, t: number) => a + t * (b - a);
  const grad  = (h: number, x: number, y: number, z: number) => {
    h &= 15;
    return ((h & 1) ? -(h < 8 ? x : y) : (h < 8 ? x : y))
         + ((h & 2) ? -(h < 4 ? y : (h === 12 || h === 14 ? x : z))
                     : (h < 4 ? y : (h === 12 || h === 14 ? x : z)));
  };
  return (x: number, y: number, z: number) => {
    const X = Math.floor(x) & 255, Y = Math.floor(y) & 255, Z = Math.floor(z) & 255;
    x -= Math.floor(x); y -= Math.floor(y); z -= Math.floor(z);
    const u = fade(x), v = fade(y), w = fade(z);
    const A = perm[X] + Y, AA = perm[A] + Z, AB = perm[A+1] + Z;
    const B = perm[X+1] + Y, BA = perm[B] + Z, BB = perm[B+1] + Z;
    return lerp(
      lerp(lerp(grad(perm[AA],x,y,z),grad(perm[BA],x-1,y,z),u),
           lerp(grad(perm[AB],x,y-1,z),grad(perm[BB],x-1,y-1,z),u),v),
      lerp(lerp(grad(perm[AA+1],x,y,z-1),grad(perm[BA+1],x-1,y,z-1),u),
           lerp(grad(perm[AB+1],x,y-1,z-1),grad(perm[BB+1],x-1,y-1,z-1),u),v),w);
  };
}

/* ── Particle ─────────────────────────────────────────────────────────────── */
interface P {
  theta: number; phi: number;
  baseR: number;
  speed: number;
  n0: number; n1: number;
  size: number; alpha: number;
  layer: 0 | 1;             // 0 = shell, 1 = outer wisps
  ox: number; oy: number;   // screen-space offset from mouse / shockwaves (spring back to 0)
  vx: number; vy: number;
}

/* ── Per-state visual targets ─────────────────────────────────────────────── */
interface VisTarget {
  energy:   number;
  rotSpeed: number;
  noiseSpd: number;
  waveAmp:  number;
  r: number; g: number; b: number;
}

const TARGETS: Record<OrbState, VisTarget> = {
  idle:      { energy: 0.00, rotSpeed: 0.30, noiseSpd: 0.05, waveAmp:  8, r: 15,  g: 125, b:  60 },
  activated: { energy: 0.52, rotSpeed: 0.85, noiseSpd: 0.13, waveAmp: 22, r: 10,  g: 200, b: 165 },
  thinking:  { energy: 0.44, rotSpeed: 1.60, noiseSpd: 0.24, waveAmp: 18, r: 80,  g: 100, b: 245 },
  speaking:  { energy: 0.62, rotSpeed: 0.65, noiseSpd: 0.11, waveAmp: 16, r: 25,  g: 235, b: 105 },
  error:     { energy: 0.28, rotSpeed: 2.20, noiseSpd: 0.32, waveAmp: 14, r: 235, g:  55, b:  50 },
};

// ω₀ = sqrt(stiffness), ζ = damping / (2·ω₀). ζ < 1 → underdamped (overshoots).
function springStep(cur: number, vel: number, tgt: number, dt: number,
                    stiffness: number, damping: number): [number, number] {
  const vel2 = vel + (stiffness * (tgt - cur) - damping * vel) * dt;
  return [cur + vel2 * dt, vel2];
}

interface SpringState extends VisTarget {
  energyV: number; rotSpeedV: number; noiseSpdV: number; waveAmpV: number;
  rV: number; gV: number; bV: number;
}

interface Shock { x: number; y: number; t0: number }

/** Band level at elevation u (0 = equator, 1 = pole), linearly interpolated. */
function bandAt(lvl: Float32Array, u: number) {
  const f = u * (NB - 1);
  const i = Math.min(NB - 2, f | 0);
  return lvl[i] + (lvl[i + 1] - lvl[i]) * (f - i);
}

export default function ParticleOrb({ state, beat = 0, speech = null, onActivate, compact = false, paused = false }: Props) {
  const wrapRef     = useRef<HTMLDivElement>(null);
  const canvasRef   = useRef<HTMLCanvasElement>(null);
  const stateRef    = useRef(state);
  const speechRef   = useRef(speech);
  const activateRef = useRef(onActivate);
  const beatRef     = useRef(0);
  const prevBeatRef = useRef(beat);
  const pointer     = useRef({ x: 0, y: 0, inside: false });
  const shocks      = useRef<Shock[]>([]);
  const pausedRef   = useRef(paused);
  const compactRef  = useRef(compact);

  useEffect(() => { stateRef.current = state; }, [state]);
  useEffect(() => { speechRef.current = speech; }, [speech]);
  useEffect(() => { activateRef.current = onActivate; }, [onActivate]);
  useEffect(() => { pausedRef.current = paused; }, [paused]);

  useEffect(() => {
    const delta = Math.min(beat - prevBeatRef.current, 4);
    prevBeatRef.current = beat;
    if (delta > 0) beatRef.current = Math.min(beatRef.current + 0.40 * delta, 1.0);
  }, [beat]);

  useEffect(() => {
    const canvas = canvasRef.current!;
    const wrap   = wrapRef.current!;
    const ctx    = canvas.getContext('2d')!;
    const noise  = makeNoise3D();
    let size = REF, dpr = 1;

    const resize = () => {
      const r = wrap.getBoundingClientRect();
      size = Math.max(compactRef.current ? 40 : 160, Math.min(r.width, r.height, 720));
      dpr  = Math.min(window.devicePixelRatio || 1, 2);
      canvas.style.width = canvas.style.height = `${size}px`;
      canvas.width = canvas.height = Math.round(size * dpr);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);

    const TOTAL = compactRef.current ? 520 : 1_500;
    const pts: P[] = [];
    for (let i = 0; i < TOTAL; i++) {
      const layer: 0 | 1 = i < TOTAL * 0.72 ? 0 : 1;
      pts.push({
        theta:  Math.acos(2 * Math.random() - 1),
        phi:    Math.random() * Math.PI * 2,
        baseR:  layer === 0 ? R0 - 12 + Math.random() * 18 : R0 - 4 + Math.random() * 36,
        speed:  0.02 + Math.random() * 0.06,
        n0:     Math.random() * 100,
        n1:     Math.random() * 100,
        size:   layer === 0 ? .6 + Math.random() * 1.4 : .4 + Math.random() * .8,
        alpha:  .3 + Math.random() * .7,
        layer,
        ox: 0, oy: 0, vx: 0, vy: 0,
      });
    }

    const sp: SpringState = { ...TARGETS.idle, energyV: 0, rotSpeedV: 0, noiseSpdV: 0,
                              waveAmpV: 0, rV: 0, gV: 0, bV: 0 };
    const lvl    = new Float32Array(NB);   // smoothed band levels, 0-1
    const lvlTgt = new Float32Array(NB);
    let hover = 0, yaw = 0, pitch = 0, flowT = 0, t = 0, ringRot = 0;
    let prev = performance.now();
    let raf  = 0;

    function speechTargets(): boolean {
      const env = speechRef.current;
      if (!env || !env.bands.length) return false;
      const now = Date.now();
      if (env.end && now > env.end) return false;
      const pos = (now - env.start - LATENCY_MS) / env.frameMs;
      const i = Math.floor(pos);
      if (i < 0 || i >= env.bands.length - 1) return false;
      const f = pos - i, a = env.bands[i], b = env.bands[i + 1];
      for (let k = 0; k < NB; k++) lvlTgt[k] = (a[k] + (b[k] - a[k]) * f) / 99;
      return true;
    }

    function frame(now: number) {
      raf = requestAnimationFrame(frame);
      const dt = Math.min((now - prev) / 1000, 0.05);
      prev = now;
      if (pausedRef.current) return;
      t += dt;

      const S  = size / REF;
      const CX = size / 2, CY = size / 2;

      /* ── state springs ─────────────────────────────────────────────────── */
      const tgt = TARGETS[stateRef.current];
      [sp.energy,   sp.energyV]   = springStep(sp.energy,   sp.energyV,   tgt.energy,   dt, 22, 7.5);
      [sp.rotSpeed, sp.rotSpeedV] = springStep(sp.rotSpeed, sp.rotSpeedV, tgt.rotSpeed, dt, 18, 7.0);
      [sp.noiseSpd, sp.noiseSpdV] = springStep(sp.noiseSpd, sp.noiseSpdV, tgt.noiseSpd, dt, 14, 6.5);
      [sp.waveAmp,  sp.waveAmpV]  = springStep(sp.waveAmp,  sp.waveAmpV,  tgt.waveAmp,  dt, 20, 7.5);
      [sp.r, sp.rV] = springStep(sp.r, sp.rV, tgt.r, dt, 9, 5.5);
      [sp.g, sp.gV] = springStep(sp.g, sp.gV, tgt.g, dt, 9, 5.5);
      [sp.b, sp.bV] = springStep(sp.b, sp.bV, tgt.b, dt, 9, 5.5);

      beatRef.current = Math.max(0, beatRef.current - 0.9 * dt);
      const bv = beatRef.current;

      /* ── speech levels: real envelope, else beat-driven stand-in ────────── */
      if (!speechTargets()) {
        const speakingNow = stateRef.current === 'speaking';
        for (let k = 0; k < NB; k++) {
          lvlTgt[k] = speakingNow
            ? Math.max(0, bv * (0.55 + 0.6 * noise(k * 0.45, t * 4.0, 7.7)) * (1 - k / NB * 0.5))
            : 0;
        }
      }
      let loud = 0;
      for (let k = 0; k < NB; k++) {
        const rising = lvlTgt[k] > lvl[k];
        // fast attack, slower release — the same ballistics as an equaliser display
        lvl[k] += (lvlTgt[k] - lvl[k]) * (1 - Math.exp(-dt * (rising ? 30 : 9)));
        loud += lvl[k];
      }
      loud /= NB;

      /* ── pointer: hover, tilt toward the cursor ─────────────────────────── */
      const ptr = pointer.current;
      hover += ((ptr.inside ? 1 : 0) - hover) * (1 - Math.exp(-dt * 6));
      const yawT   = ptr.inside ? ((ptr.x - CX) / size) * 1.1 : 0;
      const pitchT = ptr.inside ? ((ptr.y - CY) / size) * 0.9 : 0;
      yaw   += (yawT - yaw) * (1 - Math.exp(-dt * 3));
      pitch += (pitchT - pitch) * (1 - Math.exp(-dt * 3));
      const tilt = 0.32 + pitch;
      const cP = Math.cos(tilt), sP = Math.sin(tilt);
      const cY = Math.cos(yaw),  sY = Math.sin(yaw);

      flowT   += dt * (sp.noiseSpd + loud * 0.45);
      ringRot += dt * (0.08 + sp.rotSpeed * 0.12);
      const breath = Math.sin(t * Math.PI * 2 * 0.15) * 6 * (1 - sp.energy * 0.5);
      const e  = sp.energy;
      const cr = (sp.r * GLOW) | 0, cg = (sp.g * GLOW) | 0, cb = (sp.b * GLOW) | 0;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      /* ── fade the previous frame instead of clearing: flowing trails ────── */
      ctx.globalCompositeOperation = 'destination-out';
      ctx.fillStyle = `rgba(0,0,0,${0.42 - loud * 0.2 - e * 0.08})`;
      ctx.fillRect(0, 0, size, size);
      ctx.globalCompositeOperation = 'source-over';

      /* halo */
      const halo = ctx.createRadialGradient(CX, CY, 80 * S, CX, CY, 215 * S);
      halo.addColorStop(0,   `rgba(${cr},${cg},${cb},${.03 + e * .06 + loud * .10})`);
      halo.addColorStop(.55, `rgba(${cr},${cg},${cb},${.008 + e * .02})`);
      halo.addColorStop(1,   'rgba(0,0,0,0)');
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(CX, CY, 215 * S, 0, Math.PI * 2); ctx.fill();

      /* ── shockwaves from clicks ────────────────────────────────────────── */
      shocks.current = shocks.current.filter(s => now - s.t0 < 1100);
      const waves = shocks.current.map(s => {
        const age = (now - s.t0) / 1000;
        return { x: s.x, y: s.y, r: age * 520 * S, k: 1 - age / 1.1 };
      });

      /* ── particles ─────────────────────────────────────────────────────── */
      ctx.globalCompositeOperation = 'lighter';
      const repelR  = 100 * S;
      const flowAmp = .45 * (1 + e) + loud * 0.55;

      for (const p of pts) {
        p.phi += p.speed * sp.rotSpeed * (1 + loud * 0.9) * dt;

        const st = Math.sin(p.theta);
        const nx = noise(st * Math.cos(p.phi) * 1.2 + p.n0, st * Math.sin(p.phi) * 1.2 + p.n1, flowT);
        const ny = noise(Math.cos(p.theta) * .8 + p.n1, Math.sin(p.phi) * .8 + p.n0, flowT * 0.7 + 5.3);

        const wT = p.theta + nx * flowAmp;
        const wP = p.phi   + ny * flowAmp;

        // a word-beat ripple that travels from the top pole downward
        let ripple = 0;
        if (bv > 0.02) {
          const d = p.theta - (1 - bv) * Math.PI;
          ripple = bv * Math.exp(-d * d * 2.5) * 14;
        }
        const r = p.baseR + nx * sp.waveAmp + breath + ripple;

        // sphere (y up) → tilt about X → yaw about Y → perspective
        const sT = Math.sin(wT);
        const x0 = r * sT * Math.cos(wP);
        const y0 = r * Math.cos(wT);
        const z0 = r * sT * Math.sin(wP);
        const y1 = y0 * cP - z0 * sP;
        const z1 = y0 * sP + z0 * cP;
        const x2 = x0 * cY + z1 * sY;
        const z2 = -x0 * sY + z1 * cY;
        const persp = 560 / (560 - z2);
        let sx = CX + x2 * persp * S;
        let sy = CY - y1 * persp * S;
        const depth = (z2 / Math.max(r, 1) + 1) * .5;   // 1 = facing the viewer

        // equaliser push: the band for this particle's on-screen elevation
        const dx = sx - CX, dy = sy - CY;
        const len = Math.hypot(dx, dy) || 1;
        const u   = Math.asin(Math.min(1, Math.abs(dy) / len)) / (Math.PI / 2);
        const lv  = bandAt(lvl, u);
        const push = lv * (22 + 30 * depth) * S * (p.layer === 1 ? 1.5 : 1);
        sx += dx / len * push;
        sy += dy / len * push;

        // pointer repulsion and shockwave impulses act on a damped screen-space offset
        if (hover > 0.01) {
          const mx = sx + p.ox - ptr.x, my = sy + p.oy - ptr.y;
          const d2 = mx * mx + my * my;
          if (d2 < repelR * repelR) {
            const d = Math.sqrt(d2) || 1;
            const f = (1 - d / repelR) ** 2 * 2600 * S * hover * dt;
            p.vx += mx / d * f; p.vy += my / d * f;
          }
        }
        for (const w of waves) {
          const mx = sx - w.x, my = sy - w.y;
          const d = Math.hypot(mx, my) || 1;
          const band = Math.abs(d - w.r);
          if (band < 26 * S) {
            const f = (1 - band / (26 * S)) * 900 * S * w.k * dt;
            p.vx += mx / d * f; p.vy += my / d * f;
          }
        }
        p.vx += (-p.ox * 34 - p.vx * 6.5) * dt;
        p.vy += (-p.oy * 34 - p.vy * 6.5) * dt;
        p.ox += p.vx * dt; p.oy += p.vy * dt;
        sx += p.ox; sy += p.oy;

        const shell  = Math.max(0, 1 - Math.abs(r - R0) / 50);
        const bright = shell * .6 + depth * .4;
        const spec   = (bright * bright * 80 + lv * 110) * GLOW;
        const rC = Math.min(255, (cr * .12 + bright * cr * .88 + spec + e * 40 * GLOW) | 0);
        const gC = Math.min(255, (cg * .10 + bright * cg * .90 + spec) | 0);
        const bC = Math.min(255, (cb * .10 + bright * cb * .90 + spec) | 0);

        const a  = p.alpha * (.12 + depth * .88) * (.36 + e * .5 + lv * .4) * (p.layer === 0 ? .85 : .4);
        const sz = p.size * (.6 + depth * .7) * (1 + e * .3 + lv * .6) * Math.max(.7, S);
        if (a < 0.02 || sz < 0.15) continue;

        ctx.globalAlpha = Math.min(1, a);
        ctx.fillStyle   = `rgb(${rC},${gC},${bC})`;
        ctx.beginPath(); ctx.arc(sx, sy, sz, 0, Math.PI * 2); ctx.fill();
        if (depth > .55 && (shell > .5 || lv > .45)) {
          ctx.globalAlpha = a * .14 * Math.max(shell, lv);
          ctx.beginPath(); ctx.arc(sx, sy, sz * 3.6, 0, Math.PI * 2); ctx.fill();
        }
      }

      /* ── radial equaliser ring ─────────────────────────────────────────── */
      const ringR = (R0 + 52) * S;
      ctx.lineCap = 'round';
      ctx.lineWidth = Math.max(1.2, 2.2 * S);
      for (let k = 0; k < RING_BARS; k++) {
        const ang = (k / RING_BARS) * Math.PI * 2;
        const lv  = bandAt(lvl, Math.abs(Math.sin(ang)));
        const len = (3 + lv * 40) * S;
        const c = Math.cos(ang), s = Math.sin(ang);
        ctx.globalAlpha = .14 + e * .12 + lv * .7;
        ctx.strokeStyle = `rgb(${Math.min(255, cr + lv * 140 * GLOW) | 0},${Math.min(255, cg + lv * 60 * GLOW) | 0},${Math.min(255, cb + lv * 120 * GLOW) | 0})`;
        ctx.beginPath();
        ctx.moveTo(CX + c * ringR, CY + s * ringR);
        ctx.lineTo(CX + c * (ringR + len), CY + s * (ringR + len));
        ctx.stroke();
      }

      /* ── HUD arcs (counter-rotating) ───────────────────────────────────── */
      ctx.lineWidth = 1;
      ctx.strokeStyle = `rgb(${cr},${cg},${cb})`;
      const hudR = (R0 + 104) * S;
      for (let i = 0; i < (compactRef.current ? 0 : 3); i++) {
        const a0 = ringRot * (i % 2 ? -1 : 1.4) + i * 2.1;
        ctx.globalAlpha = .16 + e * .14 + hover * .1;
        ctx.beginPath(); ctx.arc(CX, CY, hudR + i * 7 * S, a0, a0 + 0.9 + i * 0.35); ctx.stroke();
      }

      /* shockwave rings */
      for (const w of waves) {
        ctx.globalAlpha = w.k * .5;
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(w.x, w.y, w.r, 0, Math.PI * 2); ctx.stroke();
      }

      /* ── inner void + rim light ────────────────────────────────────────── */
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 1;
      const voidR = (R0 - 16) * S;
      const v = ctx.createRadialGradient(CX, CY, 0, CX, CY, voidR);
      v.addColorStop(0,   `rgba(2,8,6,${.9 - e * .15 - loud * .25})`);
      v.addColorStop(.6,  `rgba(2,8,6,${.7 - e * .1 - loud * .2})`);
      v.addColorStop(1,   'rgba(2,8,6,0)');
      ctx.fillStyle = v;
      ctx.beginPath(); ctx.arc(CX, CY, voidR, 0, Math.PI * 2); ctx.fill();

      // core glow that swells with loudness
      if (loud > 0.02 || e > 0.3) {
        const core = ctx.createRadialGradient(CX, CY, 0, CX, CY, voidR * .9);
        core.addColorStop(0, `rgba(${cr},${cg},${cb},${loud * .35 + e * .04})`);
        core.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.globalCompositeOperation = 'lighter';
        ctx.fillStyle = core;
        ctx.beginPath(); ctx.arc(CX, CY, voidR * .9, 0, Math.PI * 2); ctx.fill();
        ctx.globalCompositeOperation = 'source-over';
      }
    }

    raf = requestAnimationFrame(frame);
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, []);

  const toLocal = (e: React.MouseEvent) => {
    const r = canvasRef.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  return (
    <div ref={wrapRef} className="orbCanvasWrap">
      <canvas
        ref={canvasRef}
        title="Click to talk to Jarvis"
        style={{ display: 'block', cursor: 'pointer', touchAction: 'none' }}
        onPointerMove={e => { pointer.current = { ...toLocal(e), inside: true }; }}
        onPointerLeave={() => { pointer.current.inside = false; }}
        onClick={e => {
          const { x, y } = toLocal(e);
          shocks.current.push({ x, y, t0: performance.now() });
          activateRef.current?.();
        }}
      />
    </div>
  );
}
