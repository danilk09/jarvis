import { useEffect, useRef } from 'react';

export interface LogEntry {
  role: 'user' | 'jarvis';
  text: string;
  time: string;
}

export default function ActivityFeed({ log }: { log: LogEntry[] }) {
  const feedRef = useRef<HTMLDivElement>(null);

  // Scroll only the feed itself — scrollIntoView would also scroll the page on narrow screens
  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [log]);

  if (log.length === 0) {
    return (
      <div className="feedEmpty">
        <div className="feedEmptyIcon">◌</div>
        <div>No conversation yet</div>
        <div className="feedEmptyHint">Say “Jarvis” or click the orb</div>
      </div>
    );
  }

  return (
    <div ref={feedRef} className="feed">
      {log.map((m, i) => (
        <div key={i} className={`msg ${m.role === 'user' ? 'msgUser' : 'msgJarvis'}`}>
          <div className="msgMeta">
            <span>{m.role === 'user' ? 'YOU' : 'JARVIS'}</span>
            <span>{m.time}</span>
          </div>
          <div className="msgText">{m.text}</div>
        </div>
      ))}
    </div>
  );
}
