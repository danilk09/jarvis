import { useEffect, useRef, useCallback } from 'react';

// Pure canvas implementation — no external lib needed, ships with zero extra deps.
// 2 200 particles on a sphere surface displaced by 3-D Perlin noise → wispy nebula.

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
  speed: number;
  n0: number; n1: number;           // noise offsets
  size: number; alpha: number;
  layer: 0 | 1;                     // 0 = shell, 1 = wisps
}

const TARGET: Record<OrbState, number> = {
  idle: 0, activated: 0.6, thinking: 0.45, speaking: 0.70, error: 0.25,
};

export default function ParticleOrb({ state, beat = 0 }: Props) {
  const canvasRef  = useRef<HTMLCanvasElement>(null);
  const stateRef   = useRef(state);
  const rafRef     = useRef(0);
  const eRef       = useRef(0);     // base energy 0-1 tracking state target
  const beatRef    = useRef(0);     // extra energy burst from word beats, decays to 0
  const prevBeatRef = useRef(0);    // last seen beat count for delta calc

  useEffect(() => { stateRef.current = state; }, [state]);

  // Each new beat value: compute how many words fired since last poll and kick energy
  useEffect(() => {
    const delta = Math.min(beat - prevBeatRef.current, 4); // cap at 4 missed beats
    prevBeatRef.current = beat;
    if (delta > 0) {
      beatRef.current = Math.min(beatRef.current + 0.30 * delta, 0.32);
    }
  }, [beat]);

  const init = useCallback(() => {
    const canvas = canvasRef.current!;
    const ctx    = canvas.getContext('2d')!;
    const SZ     = 420;
    canvas.width = canvas.height = SZ;
    const CX = SZ / 2; const CY = SZ / 2;
    const noise = makeNoise3D();

    /* build particles */
    const TOTAL = 1_400;
    const pts: P[] = [];
    for (let i = 0; i < TOTAL; i++) {
      const layer: 0|1 = i < TOTAL * 0.72 ? 0 : 1;
      pts.push({
        theta:  Math.acos(2 * Math.random() - 1),
        phi:    Math.random() * Math.PI * 2,
        baseR:  layer === 0 ? 108 + Math.random() * 20 : 116 + Math.random() * 38,
        dr:     (Math.random() - .5) * 7,
        speed:  0.00025 + Math.random() * 0.0005,
        n0:     Math.random() * 100,
        n1:     Math.random() * 100,
        size:   layer === 0 ? .6 + Math.random() * 1.5 : .4 + Math.random() * .9,
        alpha:  .3 + Math.random() * .7,
        layer,
      });
    }

    let prev = performance.now();

    function frame(now: number) {
      rafRef.current = requestAnimationFrame(frame);
      const dt = Math.min((now - prev) / 1000, 0.05);
      prev = now;
      const t  = now * 0.001;

      // smooth base energy tracks state target
      const target = TARGET[stateRef.current];
      const spd    = eRef.current < target ? 2.0 : 1.4;
      eRef.current += (target - eRef.current) * spd * dt;

      // word-beat pulse decays — 0.30 / 1.6 ≈ 0.19s full decay, rhythmic gap between words
      beatRef.current = Math.max(0, beatRef.current - 1.6 * dt);

      const e = Math.min(1, eRef.current + beatRef.current);

      ctx.clearRect(0, 0, SZ, SZ);

      /* outer glow halo */
      const halo = ctx.createRadialGradient(CX, CY, 90, CX, CY, 200);
      halo.addColorStop(0,   `rgba(20,180,80,${.04 + e * .10})`);
      halo.addColorStop(.55, `rgba(10,100,45,${.015 + e * .04})`);
      halo.addColorStop(1,   'rgba(0,0,0,0)');
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(CX, CY, 200, 0, Math.PI * 2); ctx.fill();

      /* particles */
      for (const p of pts) {
        p.phi += p.speed * (1 + e * 2.5);

        const nx = noise(
          Math.sin(p.theta) * Math.cos(p.phi) * 1.2 + p.n0,
          Math.sin(p.theta) * Math.sin(p.phi) * 1.2 + p.n1,
          t * (.12 + e * .28));
        const ny = noise(
          Math.cos(p.theta) * .8 + p.n1,
          Math.sin(p.phi)   * .8 + p.n0,
          t * (.08 + e * .20) + 5.3);

        const wAmp = 16 + e * 42;
        const wT   = p.theta + nx * .55 * (1 + e * 1.1);
        const wP   = p.phi   + ny * .55 * (1 + e * 1.1);
        const wR   = p.baseR + nx * wAmp + p.dr;

        /* sphere → 3D */
        const sinT = Math.sin(wT), cosT = Math.cos(wT);
        const sinP = Math.sin(wP), cosP = Math.cos(wP);
        const x3 = wR * sinT * cosP;
        const y3 = wR * sinT * sinP;
        const z3 = wR * cosT;

        /* slight tilt so it looks like the reference image */
        const tilt = 0.22;
        const ry   = y3 * Math.cos(tilt) - z3 * Math.sin(tilt);
        const rz   = y3 * Math.sin(tilt) + z3 * Math.cos(tilt);

        const sx = CX + x3 * 0.84 + ry * 0.16;
        const sy = CY + rz * 0.80;

        const depth  = (rz / Math.max(wR, 1) + 1) * .5;   // 0=back 1=front
        const shell  = Math.max(0, 1 - Math.abs(wR - 130) / 50);
        const bright = shell * .7 + depth * .3;

        /* green-white color */
        const rC = Math.min(255, p.layer === 0 ? (25  + bright * 130 + e * 65) | 0 : (8  + bright * 55 + e * 28) | 0);
        const gC = Math.min(255, p.layer === 0 ? (155 + bright * 100) | 0             : (95 + bright * 85)         | 0);
        const bC = Math.min(255, p.layer === 0 ? (55  + bright * 75)  | 0             : (45 + bright * 55)         | 0);

        const a   = p.alpha * (.15 + depth * .85) * (.5 + e * .5) * (p.layer === 0 ? 1 : .5);
        const sz  = p.size * (.65 + depth * .65) * (1 + e * .5);
        if (a < 0.02 || sz < 0.15) continue;

        ctx.globalAlpha = a;
        ctx.fillStyle   = `rgb(${rC},${gC},${bC})`;
        ctx.beginPath(); ctx.arc(sx, sy, sz, 0, Math.PI * 2); ctx.fill();

        /* soft halo on bright surface particles — simple circle, no per-particle gradient */
        if (shell > .5 && depth > .4) {
          ctx.globalAlpha = a * .18 * shell;
          ctx.fillStyle   = `rgb(${rC},${gC},${bC})`;
          ctx.beginPath(); ctx.arc(sx, sy, sz * 3.5, 0, Math.PI * 2); ctx.fill();
        }
      }

      ctx.globalAlpha = 1;

      /* inner void — hollow dark centre like the reference */
      const void_ = ctx.createRadialGradient(CX, CY, 0, CX, CY, 105);
      void_.addColorStop(0,    `rgba(2,8,4,${.97 - e * .14})`);
      void_.addColorStop(.58,  `rgba(2,8,4,${.84 - e * .10})`);
      void_.addColorStop(.9,   'rgba(2,8,4,0)');
      ctx.fillStyle = void_;
      ctx.beginPath(); ctx.arc(CX, CY, 105, 0, Math.PI * 2); ctx.fill();

      /* rim specular top-left */
      const rim = ctx.createRadialGradient(CX - 62, CY - 72, 8, CX, CY, 148);
      rim.addColorStop(0,  `rgba(190,255,210,${.04 + e * .06})`);
      rim.addColorStop(.4, `rgba(50,220,110,${.01 + e * .025})`);
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
