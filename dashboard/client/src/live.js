import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';

const KEEP_MS = 15 * 60 * 1000;

/** Storefront health: polls once a second and accumulates a 1 s series. */
export function useHealth(enabled) {
  const [snap, setSnap] = useState(null);
  const [series, setSeries] = useState([]);
  const [offline, setOffline] = useState(false);
  const lastT = useRef(0);
  useEffect(() => {
    if (!enabled) return undefined;
    let live = true, timer;
    const go = async () => {
      try {
        const s = await api(`/health?since=${lastT.current || Date.now() - 180000}`);
        if (!live) return;
        setSnap(s); setOffline(false);
        if (s.series.length) {
          lastT.current = s.series[s.series.length - 1].t + 1000;
          setSeries((old) => {
            const have = new Set(old.map((p) => p.t));
            const merged = [...old, ...s.series.filter((p) => !have.has(p.t))];
            return merged.filter((p) => p.t > Date.now() - KEEP_MS);
          });
        }
      } catch { if (live) setOffline(true); }
      if (live) timer = setTimeout(go, 1000);
    };
    go();
    return () => { live = false; clearTimeout(timer); };
  }, [enabled]);
  return { snap, series, offline };
}

/** The currently running (or last) agent job + the timeline markers derived from its output. */
export function useJob() {
  const [job, setJob] = useState(null);
  const [lines, setLines] = useState([]);
  const [markers, setMarkers] = useState([]);
  const [error, setError] = useState(null);
  const cur = useRef({ id: null, from: 0 });

  useEffect(() => {
    let live = true, timer;
    // re-attach after a page reload: pick up the running (or last) job from the server
    api('/jobs/latest?from=0').then((j) => {
      if (!live || cur.current.id || !j?.id) return;
      cur.current = { id: j.id, from: 0 };
    }).catch(() => {});
    const go = async () => {
      const { id, from } = cur.current;
      if (id) {
        try {
          const j = await api(`/jobs/${id}?from=${from}`);
          if (live && cur.current.id === id && cur.current.from === from) {
            cur.current.from = j.total;
            if (j.lines.length) {
              const now = Date.now();
              const ms = [];
              for (const line of j.lines) {
                if (/INJECTED/.test(line)) ms.push({ x: now, label: 'fault injected', tone: 'bad' });
                else if (/^\d\d:\d\d:\d\d ABORTED/.test(line)) ms.push({ x: now, label: 'guard abort', tone: 'bad' });
                else if (/cleaned up/.test(line)) ms.push({ x: now, label: 'fault removed', tone: 'good' });
                else if (/steady state restored/.test(line)) ms.push({ x: now, label: 'recovered', tone: 'good' });
              }
              setLines((l) => [...l, ...j.lines]);
              if (ms.length) setMarkers((m) => [...m, ...ms]);
            }
            const { lines: _l, ...meta } = j;
            setJob(meta);
          }
        } catch (e) { if (live) setError(e.message); }
      }
      if (live) timer = setTimeout(go, 800);
    };
    go();
    return () => { live = false; clearTimeout(timer); };
  }, []);

  const start = useCallback(async (path, body) => {
    setError(null);
    try {
      const { id } = await api(path, { method: 'POST', body });
      cur.current = { id, from: 0 };
      setJob({ id, status: 'running', startedAt: Date.now(), meta: body });
      setLines([]); setMarkers([]);
      return id;
    } catch (e) { setError(e.message); return null; }
  }, []);

  const abort = useCallback(async () => {
    try { const r = await api('/abort', { method: 'POST' }); setLines((l) => [...l, `[dashboard] kill switch: ${r.output}`]); }
    catch (e) { setError(e.message); }
  }, []);

  return { job, lines, markers, error, start, abort };
}
