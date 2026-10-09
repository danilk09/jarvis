import { RefObject, useEffect, useRef } from 'react';
import { getAnchor } from './anchors';
import type { Highlight } from './StageContext';

// A beam of light from the Stage's orb to whatever Jarvis is pointing at. Every frame
// it asks the active highlight's panel where the mark is (see anchors.ts), so it follows
// scrolling, dragging and the globe turning. It fades out after a while.

const VISIBLE_FOR = 12;   // seconds
const FADE = 3;

export default function StageBeam({ active, originRef, paused }: {
  active: Highlight | null;
  originRef: RefObject<HTMLDivElement | null>;
  paused: boolean;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const pathRef = useRef<SVGPathElement>(null);
  const glowRef = useRef<SVGPathElement>(null);
  const dotRef = useRef<SVGCircleElement>(null);
  const ringRef = useRef<SVGCircleElement>(null);
  const activeRef = useRef(active);
  activeRef.current = active;

  useEffect(() => {
    if (paused) return;
    let raf = 0;
    const tick = () => {
      raf = requestAnimationFrame(tick);
      const svg = svgRef.current;
      const h = activeRef.current;
      if (!svg) return;
      const age = h ? Date.now() / 1000 - h.time : Infinity;
      const a = h && age < VISIBLE_FOR ? getAnchor(h.id) : null;
      const o = originRef.current?.getBoundingClientRect();
      if (!a || !o) { svg.style.opacity = '0'; return; }

      const ox = o.left + o.width / 2, oy = o.top + o.height / 2;
      // aim at the nearest point of the mark, nudged inside it
      const tx = Math.min(Math.max(ox, a.x + 6), a.x + Math.max(a.w - 6, 6));
      const ty = Math.min(Math.max(oy, a.y + 4), a.y + Math.max(a.h - 4, 4));
      const d = `M ${ox} ${oy} C ${ox + (tx - ox) * 0.55} ${oy}, ${tx} ${oy + (ty - oy) * 0.45}, ${tx} ${ty}`;
      pathRef.current?.setAttribute('d', d);
      glowRef.current?.setAttribute('d', d);
      dotRef.current?.setAttribute('cx', String(tx));
      dotRef.current?.setAttribute('cy', String(ty));
      ringRef.current?.setAttribute('cx', String(tx));
      ringRef.current?.setAttribute('cy', String(ty));
      const fade = age > VISIBLE_FOR - FADE ? (VISIBLE_FOR - age) / FADE : Math.min(1, age * 3 + 0.2);
      svg.style.opacity = String(Math.max(0, Math.min(1, fade)));
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [originRef, paused]);

  return (
    <svg ref={svgRef} className="stageBeam" aria-hidden>
      <defs>
        <linearGradient id="beamGrad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#3dff8f" stopOpacity="0.15" />
          <stop offset="1" stopColor="#a8ffd0" stopOpacity="0.95" />
        </linearGradient>
      </defs>
      <path ref={glowRef} className="beamGlow" />
      <path ref={pathRef} className="beamLine" />
      <circle ref={ringRef} className="beamRing" r="10" />
      <circle ref={dotRef} className="beamDot" r="3.5" />
    </svg>
  );
}
