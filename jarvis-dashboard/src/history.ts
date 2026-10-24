import { useCallback, useState } from 'react';
import type { LogEntry } from './ActivityFeed';

// Past conversations, one day at a time (Phase 3 M5): GET /api/history lists the days that
// have a conversation, GET /api/history/<day> returns one. Both pages use this for their
// day picker; the live feed itself comes from /status.

export interface Day { day: string; count: number }

function todayIso(offset = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** "Today", "Yesterday", "Mon, Oct 19" (and the year when it isn't this one). */
export function dayLabel(day: string) {
  if (day === todayIso()) return 'Today';
  if (day === todayIso(-1)) return 'Yesterday';
  const [y, m, d] = day.split('-').map(Number);
  const date = new Date(y, m - 1, d);
  return date.toLocaleDateString('en-US', {
    weekday: 'short', month: 'short', day: 'numeric',
    ...(y !== new Date().getFullYear() ? { year: 'numeric' } : {}),
  });
}

export function useHistory() {
  const [days, setDays] = useState<Day[]>([]);
  const [day, setDay] = useState<string | null>(null);        // null = now (the live feed)
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);

  const loadDays = useCallback(async () => {
    try {
      const res = await fetch('/api/history');
      setDays((await res.json()).days || []);
    } catch { /* offline: keep what we have */ }
  }, []);

  const open = useCallback(async (d: string | null) => {
    setDay(d);
    if (!d) { setEntries([]); return; }
    setLoading(true);
    try {
      const res = await fetch(`/api/history/${d}`);
      setEntries((await res.json()).entries || []);
    } catch {
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, []);

  return { days, loadDays, day, entries, loading, open };
}
