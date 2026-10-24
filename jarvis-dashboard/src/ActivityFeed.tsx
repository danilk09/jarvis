import { ReactElement, useEffect, useState } from 'react';
import { dayLabel, useHistory } from './history';

export interface LogEntry {
  role: 'user' | 'jarvis';
  text: string;
  time: string;
  day?: string;         // YYYY-MM-DD (older entries may not have it)
  source?: string;      // where the command came from: phone / pc
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

// Newest first, so the latest exchange is visible without scrolling. A day picker at the top
// shows any earlier day's conversation (M5; read-only).
export default function ActivityFeed({ log }: { log: LogEntry[] }) {
  const history = useHistory();
  const { loadDays } = history;
  useEffect(() => { loadDays(); }, [loadDays, log.length]);
  const past = history.day !== null;
  const shown = past ? history.entries : log;

  const picker = (
    <div className="feedDays">
      <select value={history.day ?? ''} onChange={e => history.open(e.target.value || null)}>
        <option value="">Now</option>
        {history.days.map(d => <option key={d.day} value={d.day}>{dayLabel(d.day)} · {d.count}</option>)}
      </select>
      {past && <button onClick={() => history.open(null)}>Back to now</button>}
    </div>
  );

  if (shown.length === 0) {
    return (
      <>
        {picker}
        <div className="feedEmpty">
          <div className="feedEmptyIcon">◌</div>
          <div>{past ? (history.loading ? 'Loading…' : 'Nothing that day') : 'No conversation yet'}</div>
          {!past && <div className="feedEmptyHint">Say “Jarvis” or click the orb</div>}
        </div>
      </>
    );
  }

  // newest first, with the day's name above each day's block (when there's more than one day,
  // or it's an earlier day)
  const rev = [...shown].reverse();
  const labelled = past || rev.some(x => x.day && x.day !== rev[0].day);
  const items: ReactElement[] = [];
  rev.forEach((m, i) => {
    if (labelled && m.day && m.day !== rev[i - 1]?.day) {
      items.push(<div key={`d${i}`} className="feedDay">{dayLabel(m.day)}</div>);
    }
    items.push(
      <div key={i} className={`msg ${m.role === 'user' ? 'msgUser' : 'msgJarvis'}`}>
        <div className="msgMeta">
          <span>{m.role === 'user' ? (m.source === 'phone' ? 'YOU · PHONE' : 'YOU') : 'JARVIS'}</span>
          <span>{m.time}</span>
        </div>
        {m.role === 'user' && !past ? <UserText m={m} /> : <div className="msgText">{m.text}</div>}
      </div>,
    );
  });

  return (
    <>
      {picker}
      <div className="feed">{items}</div>
    </>
  );
}
