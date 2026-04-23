import { useEffect, useRef, useCallback } from 'react';

// 2-D canvas particle orb. Particles live on a spherical shell displaced by
// 3-D Perlin noise. Each state has distinct colour, rotation speed and noise
// speed. Speech beats cause the whole orb to breathe outward uniformly.

type OrbState = 'idle' | 'activated' | 'thinking' | 'speaking' | 'error';
interface Props { state: OrbState; beat?: number }

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
  baseR: number; dr: number;
  speed: number;       // rad/s — frame-rate independent
  n0: number; n1: number;
  size: number; alpha: number;
  layer: 0 | 1;
}

/* ── Per-state visual targets (all values smoothly interpolated) ─────────── */
interface VisTarget {
  energy:   number;
  rotSpeed: number;   // orbital rotation speed multiplier
  noiseSpd: number;   // noise time evolution speed
  waveAmp:  number;   // max noise displacement (px)
  r: number; g: number; b: number;
}

const TARGETS: Record<OrbState, VisTarget> = {
  idle:      { energy: 0.00, rotSpeed: 0.30, noiseSpd: 0.05, waveAmp:  8, r: 15,  g: 115, b:  50 },
  activated: { energy: 0.52, rotSpeed: 0.85, noiseSpd: 0.13, waveAmp: 22, r: 10,  g: 190, b: 155 },
  thinking:  { energy: 0.44, rotSpeed: 1.60, noiseSpd: 0.24, waveAmp: 18, r: 70,  g:  90, b: 235 },
  speaking:  { energy: 0.62, rotSpeed: 0.65, noiseSpd: 0.11, waveAmp: 20, r: 20,  g: 230, b:  95 },
  error:     { energy: 0.28, rotSpeed: 2.20, noiseSpd: 0.32, waveAmp: 14, r: 230, g:  50, b:  45 },
};

export default function ParticleOrb({ state, beat = 0 }: Props) {
  const canvasRef   = useRef<HTMLCanvasElement>(null);
  const stateRef    = useRef(state);
  const rafRef      = useRef(0);
  const vis         = useRef<VisTarget>({ ...TARGETS.idle });
  const beatRef     = useRef(0);
  const prevBeatRef = useRef(0);

  useEffect(() => { stateRef.current = state; }, [state]);

  useEffect(() => {
    const delta = Math.min(beat - prevBeatRef.current, 4);
    prevBeatRef.current = beat;
    if (delta > 0) {
      beatRef.current = Math.min(beatRef.current + 0.40 * delta, 1.0);
    }
  }, [beat]);

  const init = useCallback(() => {
    const canvas = canvasRef.current!;
    const ctx    = canvas.getContext('2d')!;
    const SZ     = 420;
    canvas.width = canvas.height = SZ;
    const CX = SZ / 2, CY = SZ / 2;
    const noise = makeNoise3D();

    const TOTAL = 1_400;
    const pts: P[] = [];
    for (let i = 0; i < TOTAL; i++) {
      const layer: 0|1 = i < TOTAL * 0.72 ? 0 : 1;
      pts.push({
        theta:  Math.acos(2 * Math.random() - 1),
        phi:    Math.random() * Math.PI * 2,
        baseR:  layer === 0 ? 106 + Math.random() * 18 : 114 + Math.random() * 36,
        dr:     (Math.random() - .5) * 6,
        speed:  0.02 + Math.random() * 0.06,   // rad/s
        n0:     Math.random() * 100,
        n1:     Math.random() * 100,
        size:   layer === 0 ? .6 + Math.random() * 1.4 : .4 + Math.random() * .8,
        alpha:  .3 + Math.random() * .7,
        layer,
      });
    }

    let prev = performance.now();
    let t    = 0;

    function frame(now: number) {
      rafRef.current = requestAnimationFrame(frame);
      const dt = Math.min((now - prev) / 1000, 0.05);
      prev = now;
      t += dt;

      /* smooth all vis params toward current state target */
      const tgt  = TARGETS[stateRef.current];
      const rate = 2.2 * dt;
      const v    = vis.current;
      v.energy   += (tgt.energy   - v.energy)   * rate;
      v.rotSpeed += (tgt.rotSpeed - v.rotSpeed)  * rate;
      v.noiseSpd += (tgt.noiseSpd - v.noiseSpd)  * rate;
      v.waveAmp  += (tgt.waveAmp  - v.waveAmp)   * rate;
      v.r        += (tgt.r        - v.r)         * rate;
      v.g        += (tgt.g        - v.g)         * rate;
      v.b        += (tgt.b        - v.b)         * rate;

      /* beat decays gently so each word produces a sustained breath */
      beatRef.current = Math.max(0, beatRef.current - 0.75 * dt);
      const bv = beatRef.current;

      const e  = v.energy;
      const cr = v.r | 0, cg = v.g | 0, cb = v.b | 0;

      ctx.clearRect(0, 0, SZ, SZ);

      /* outer glow halo — tinted to state colour */
      const halo = ctx.createRadialGradient(CX, CY, 88, CX, CY, 205);
      halo.addColorStop(0,   `rgba(${cr},${cg},${cb},${.05 + e * .13 + bv * .07})`);
      halo.addColorStop(.55, `rgba(${cr},${cg},${cb},${.012 + e * .04})`);
      halo.addColorStop(1,   'rgba(0,0,0,0)');
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(CX, CY, 205, 0, Math.PI * 2); ctx.fill();

      /* beat ring — glows with each spoken word */
      if (bv > 0.04) {
        const ring = ctx.createRadialGradient(CX, CY, 95, CX, CY, 165);
        ring.addColorStop(0,   'rgba(0,0,0,0)');
        ring.addColorStop(0.4, `rgba(${cr},${cg},${cb},${bv * 0.18})`);
        ring.addColorStop(1,   'rgba(0,0,0,0)');
        ctx.fillStyle = ring;
        ctx.beginPath(); ctx.arc(CX, CY, 165, 0, Math.PI * 2); ctx.fill();
      }

      /* particles */
      for (const p of pts) {
        /* dt-based rotation — consistent across all frame rates */
        p.phi += p.speed * v.rotSpeed * dt;

        const nx = noise(
          Math.sin(p.theta) * Math.cos(p.phi) * 1.2 + p.n0,
          Math.sin(p.theta) * Math.sin(p.phi) * 1.2 + p.n1,
          t * v.noiseSpd);
        const ny = noise(
          Math.cos(p.theta) * .8 + p.n1,
          Math.sin(p.phi)   * .8 + p.n0,
          t * v.noiseSpd * 0.7 + 5.3);

        const wT = p.theta + nx * .45 * (1 + e);
        const wP = p.phi   + ny * .45 * (1 + e);

        /* uniform outward breath — every particle expands together */
        const breathe = bv * 24;
        const wR = p.baseR + nx * v.waveAmp + p.dr + breathe;

        const sinT = Math.sin(wT), cosT = Math.cos(wT);
        const sinP = Math.sin(wP), cosP = Math.cos(wP);
        const x3 = wR * sinT * cosP;
        const y3 = wR * sinT * sinP;
        const z3 = wR * cosT;

        const tilt = 0.22;
        const ry   = y3 * Math.cos(tilt) - z3 * Math.sin(tilt);
        const rz   = y3 * Math.sin(tilt) + z3 * Math.cos(tilt);

        const sx = CX + x3 * 0.84 + ry * 0.16;
        const sy = CY + rz * 0.80;

        const depth  = (rz / Math.max(wR, 1) + 1) * .5;
        const shell  = Math.max(0, 1 - Math.abs(wR - 130) / 50);
        const bright = shell * .7 + depth * .3;

        /* colour: state hue × brightness with white specular at front */
        const spec = bright * bright * 90;
        const rC = Math.min(255, (v.r * .12 + bright * v.r * .88 + spec + e * 45) | 0);
        const gC = Math.min(255, (v.g * .10 + bright * v.g * .90 + spec)           | 0);
        const bC = Math.min(255, (v.b * .10 + bright * v.b * .90 + spec)           | 0);

        /* wisps: slightly cooler and more translucent */
        const rF = p.layer === 1 ? Math.min(255, (rC * .75 + 20) | 0) : rC;
        const gF = p.layer === 1 ? Math.min(255, (gC * .75 + 20) | 0) : gC;
        const bF = p.layer === 1 ? Math.min(255, (bC * .75 + 20) | 0) : bC;

        const a  = p.alpha * (.15 + depth * .85) * (.38 + e * .62) * (p.layer === 0 ? 1 : .45);
        const sz = p.size  * (.65 + depth * .65) * (1 + e * .38);
        if (a < 0.02 || sz < 0.15) continue;

        ctx.globalAlpha = a;
        ctx.fillStyle   = `rgb(${rF},${gF},${bF})`;
        ctx.beginPath(); ctx.arc(sx, sy, sz, 0, Math.PI * 2); ctx.fill();

        if (shell > .5 && depth > .4) {
          ctx.globalAlpha = a * .18 * shell;
          ctx.beginPath(); ctx.arc(sx, sy, sz * 3.5, 0, Math.PI * 2); ctx.fill();
        }
      }

      ctx.globalAlpha = 1;

      /* inner void — hollow dark centre */
      const void_ = ctx.createRadialGradient(CX, CY, 0, CX, CY, 104);
      void_.addColorStop(0,   `rgba(2,6,4,${.97 - e * .13})`);
      void_.addColorStop(.58, `rgba(2,6,4,${.84 - e * .10})`);
      void_.addColorStop(.9,  'rgba(2,6,4,0)');
      ctx.fillStyle = void_;
      ctx.beginPath(); ctx.arc(CX, CY, 104, 0, Math.PI * 2); ctx.fill();

      /* rim specular — top-left highlight */
      const rimR = Math.min(255, cr + 160), rimG = Math.min(255, cg + 160), rimB = Math.min(255, cb + 160);
      const rim = ctx.createRadialGradient(CX - 62, CY - 72, 8, CX, CY, 148);
      rim.addColorStop(0,  `rgba(${rimR},${rimG},${rimB},${.04 + e * .07})`);
      rim.addColorStop(.4, `rgba(${cr},${cg},${cb},${.01 + e * .025})`);
      rim.addColorStop(1,  'rgba(0,0,0,0)');
      ctx.fillStyle = rim;
      ctx.beginPath(); ctx.arc(CX, CY, 148, 0, Math.PI * 2); ctx.fill();
    }

    rafRef.current = requestAnimationFrame(frame);
  }, []);

  useEffect(() => {
    init();
    return () => cancelAnimationFrame(rafRef.current);
  }, [init]);

  return <canvas ref={canvasRef} style={{ width: 420, height: 420, display: 'block' }} />;
}
