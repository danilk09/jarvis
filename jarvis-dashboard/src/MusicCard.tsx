import { useState } from 'react';

export type MusicState = {
  playing: boolean;
  paused: boolean;
  currentSong: string;
  history: string[];      // oldest first
};

const sendControl = (command: string, extra?: object) =>
  fetch('/api/music/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, ...extra }),
  }).catch(() => {});

export default function MusicCard({ music }: { music: MusicState }) {
  const [showHistory, setShowHistory] = useState(false);
  if (!music.playing && !music.paused && !music.currentSong) return null;

  const live = music.playing && !music.paused;
  return (
    <div className="music">
      <div className="musicMain">
        <div className={`eqBars ${live ? 'eqLive' : ''}`} aria-hidden>
          <span /><span /><span /><span />
        </div>
        <div className="musicInfo">
          <div className="musicLabel">{music.paused ? 'PAUSED' : 'NOW PLAYING'}</div>
          <div className="musicTitle" title={music.currentSong}>{music.currentSong || '—'}</div>
        </div>
        <div className="musicControls">
          <button className="iconBtn" title="Previous" disabled={!music.history.length}
                  onClick={() => sendControl('prev', { n: 1 })}>⏮</button>
          <button className="iconBtn iconBtnMain" title={music.paused ? 'Resume' : 'Pause'}
                  onClick={() => sendControl('toggle_pause')}>{music.paused ? '▶' : '⏸'}</button>
          <button className="iconBtn" title="Skip" onClick={() => sendControl('skip')}>⏭</button>
          <button className="iconBtn" title="Stop" onClick={() => sendControl('stop')}>■</button>
          <button className={`iconBtn ${showHistory ? 'iconBtnOn' : ''}`} title="Recently played"
                  disabled={!music.history.length} onClick={() => setShowHistory(s => !s)}>☰</button>
        </div>
      </div>
      {showHistory && music.history.length > 0 && (
        <ol className="musicHistory">
          {[...music.history].reverse().map((title, i) => (
            <li key={`${title}-${i}`}>
              <button onClick={() => sendControl('prev', { n: i + 1 })} title="Play again">
                <span className="musicHistoryIdx">{i + 1}</span>{title}
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
