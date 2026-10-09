export interface LogEntry {
  role: 'user' | 'jarvis';
  text: string;
  time: string;
}

// Newest first, so the latest exchange is visible without scrolling
export default function ActivityFeed({ log }: { log: LogEntry[] }) {
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
    <div className="feed">
      {log.map((m, i) => (
        <div key={i} className={`msg ${m.role === 'user' ? 'msgUser' : 'msgJarvis'}`}>
          <div className="msgMeta">
            <span>{m.role === 'user' ? 'YOU' : 'JARVIS'}</span>
            <span>{m.time}</span>
          </div>
          <div className="msgText">{m.text}</div>
        </div>
      )).reverse()}
    </div>
  );
}
