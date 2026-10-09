import { useEffect, useRef, useState } from 'react';
import { setAnchor, toAnchor } from '../anchors';
import type { PanelProps } from './types';

// An image Jarvis made or was given. The image is fitted into the panel inside a box of
// exactly its displayed size, so regions Jarvis points at (fractions of the image) line
// up. Double-click toggles actual size.

export default function ImagePanel({ panel, highlight }: PanelProps) {
  const { src, caption } = panel.data as { src?: string; caption?: string };
  const areaRef = useRef<HTMLDivElement>(null);
  const regionRef = useRef<HTMLDivElement>(null);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [area, setArea] = useState({ w: 0, h: 0 });
  const [zoomed, setZoomed] = useState(false);
  const [showCaption, setShowCaption] = useState(true);

  useEffect(() => {
    const el = areaRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setArea({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  let box = { w: 0, h: 0 };
  if (natural && area.w && area.h) {
    const scale = zoomed ? 1 : Math.min(area.w / natural.w, area.h / natural.h, 1.5);
    box = { w: natural.w * scale, h: natural.h * scale };
  }

  const region = highlight?.target.region;
  const hlId = highlight?.id ?? '';
  const hasRegion = !!region && region.length === 4;
  useEffect(() => {
    if (!hasRegion || !hlId) return;
    return setAnchor(hlId, () => toAnchor(regionRef.current?.getBoundingClientRect()));
  }, [hlId, hasRegion]);

  return (
    <div className="imagePanel">
      <div ref={areaRef} className={`imageArea ${zoomed ? 'imageAreaZoomed' : ''}`}>
        {src && (
          <div className="imageBox" style={{ width: box.w || undefined, height: box.h || undefined }}>
            <img
              src={src}
              alt={panel.title}
              draggable={false}
              onLoad={e => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
              onDoubleClick={() => setZoomed(z => !z)}
              title="Double-click to toggle actual size"
            />
            {region && region.length === 4 && (
              <div
                ref={regionRef}
                className="imageRegion"
                style={{ left: `${region[0] * 100}%`, top: `${region[1] * 100}%`,
                         width: `${region[2] * 100}%`, height: `${region[3] * 100}%` }}
              >
                {highlight?.label && <span className="imageRegionTag">{highlight.label}</span>}
              </div>
            )}
          </div>
        )}
      </div>
      {caption && (
        <button className={`imageCaption ${showCaption ? '' : 'imageCaptionCollapsed'}`}
                onClick={() => setShowCaption(s => !s)} title="Show / hide caption">
          {caption}
        </button>
      )}
    </div>
  );
}
