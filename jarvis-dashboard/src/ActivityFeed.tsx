import { useState } from 'react';

export interface LogEntry {
  role: 'user' | 'jarvis';
  text: string;
  time: string;
  sample?: string;      // id of the recording, when the line came from the mic
  corrected?: boolean;
  confirmed?: boolean;
}

// Tell Jarvis what you actually said (✎) or that it heard right (✓). Both label the
// recording in jarvis_memory/voice/ — benchmark and training data; corrections also
// teach it to fix that mishearing automatically.
function label(sample: string, body: { text: string; confirmed?: boolean }) {
  return fetch('/api/voice/label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sample, ...body }),
  }).catch(() => {});
}

function UserText({ m }: { m: LogEntry }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(m.text);

  if (!m.sample) return <div className="msgText">{m.text}</div>;

  if (editing) {
    const save = () => {
      setEditing(false);
      if (draft.trim() && draft.trim() !== m.text) label(m.sample!, { text: draft.trim() });
    };
    return (
      <input
        className="msgEdit"
        value={draft}
        autoFocus
        onChange={e => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={e => {
          if (e.key === 'Enter') save();
          if (e.key === 'Escape') { setDraft(m.text); setEditing(false); }
        }}
      />
    );
  }

  const done = m.corrected || m.confirmed;
  return (
    <div className="msgText">
      {m.text}
      <span className="msgLabel">
        {done ? (
          <span className="msgLabelDone" title={m.corrected ? 'Corrected' : 'Marked as heard right'}>
            {m.corrected ? '✎' : '✓'}
          </span>
        ) : (
          <>
            <button title="Heard right" onClick={() => label(m.sample!, { text: m.text, confirmed: true })}>✓</button>
            <button title="Correct what Jarvis heard" onClick={() => { setDraft(m.text); setEditing(true); }}>✎</button>
          </>
        )}
      </span>
    </div>
  );
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
          {m.role === 'user' ? <UserText m={m} /> : <div className="msgText">{m.text}</div>}
        </div>
      )).reverse()}
    </div>
  );
}
