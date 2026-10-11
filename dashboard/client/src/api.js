import { useEffect, useRef, useState } from 'react';

export async function api(path, opts) {
  const res = await fetch(`/api${path}`, opts && { ...opts, headers: { 'content-type': 'application/json' }, body: opts.body && JSON.stringify(opts.body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/** Fetch `path` once on mount (and whenever `deps` change). Returns [data, error, reload]. */
export function useFetch(path, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    if (!path) { setData(null); return undefined; }
    api(path).then((d) => live && (setData(d), setError(null))).catch((e) => live && setError(e.message));
    return () => { live = false; };
  }, [path, tick, ...deps]); // eslint-disable-line react-hooks/exhaustive-deps
  return [data, error, () => setTick((t) => t + 1)];
}

/** Poll `path` every `ms`; keeps the last good value and reports the latest error. */
export function usePoll(path, ms, enabled = true) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let live = true, timer;
    const go = async () => {
      try { const d = await api(path); if (live) { setData(d); setError(null); } }
      catch (e) { if (live) setError(e.message); }
      if (live) timer = setTimeout(go, ms);
    };
    go();
    return () => { live = false; clearTimeout(timer); };
  }, [path, ms, enabled]);
  return [data, error];
}

/** Element width, kept up to date (for SVG charts). */
export function useWidth() {
  const ref = useRef(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    if (!ref.current) return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.floor(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export const pct = (v, d = 0) => (v == null ? '-' : `${(v * 100).toFixed(d)}%`);
export const ms = (v) => (v == null ? '-' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
export const clock = (t) => new Date(t).toLocaleTimeString([], { hour12: false });
export const shortId = (id) => id?.replace(/^\d{8}-/, '').replace(/^(\d{2})(\d{2})(\d{2})-/, '$1:$2:$3 ');
