import React, { useEffect, useState } from 'react';
import { useFetch, usePoll } from './api.js';
import { useHealth, useJob } from './live.js';
import LiveLab from './views/LiveLab.jsx';
import Replay from './views/Replay.jsx';
import Campaign from './views/Campaign.jsx';
import Findings from './views/Findings.jsx';
import Monitor from './views/Monitor.jsx';

const TABS = [
  ['live', 'Live Lab'],
  ['campaign', 'AI Agents'],
  ['monitor', 'Monitor'],
  ['replay', 'Replay'],
  ['findings', 'Findings'],
];

const STATE_LABEL = { healthy: 'Shop healthy', degraded: 'Shop degraded', down: 'Shop DOWN', unknown: 'Connecting' };

export default function App() {
  const [tab, setTabState] = useState(() => (TABS.some(([id]) => id === location.hash.slice(1)) ? location.hash.slice(1) : 'live'));
  const setTab = (t) => { setTabState(t); history.replaceState(null, '', `#${t}`); };
  const [replayRun, setReplayRun] = useState(null);
  const [config] = useFetch('/config');
  const [graph] = useFetch('/graph');
  const hasVm = config?.vm;
  const health = useHealth(true);
  const [pods] = usePoll('/pods', 2000, Boolean(hasVm));
  const [metrics] = usePoll('/metrics', 3000, Boolean(hasVm));
  const jobState = useJob();
  const [mhist, setMhist] = useState([]);
  useEffect(() => {          // 2 minutes of per-service CPU / memory for the Live Lab resource chart
    if (metrics?.services) setMhist((h) => [...h.filter((x) => x.t > Date.now() - 180000), { t: Date.now(), services: metrics.services }]);
  }, [metrics]);

  const state = health.offline ? 'unknown' : health.snap?.state ?? 'unknown';
  const running = jobState.job?.status === 'running';
  const ctx = { config, graph, pods, metrics, mhist, health, jobState, goAgents: () => setTab('campaign') };
  const openRun = (id) => { setReplayRun(id); setTab('replay'); };

  return (
    <div className={`app state-${state}`}>
      {state === 'down' && <div className="crash-banner">SITE DOWN - shoppers cannot use the store</div>}
      <header className="topbar">
        <div className="brand"><span className="logo">⚡</span><div><b>Chaos Lab</b><small>AI-driven chaos engineering</small></div></div>
        <nav>
          {TABS.map(([id, label]) => (
            <button key={id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{label}{id === 'live' && running ? <i className="live-dot" /> : null}</button>
          ))}
        </nav>
        <div className="top-right">
          <span className={`state-pill ${state}`}><i />{STATE_LABEL[state]}</span>
          <span className={`link-pill ${hasVm ? 'on' : ''}`} title={hasVm ? 'VM configured' : 'No infra/vm.env - replay only'}>{hasVm ? 'VM linked' : 'offline mode'}</span>
          <button className="btn kill" onClick={jobState.abort} title="Abort the running campaign and remove every fault">■ Kill switch</button>
        </div>
      </header>

      <main>
        {tab === 'live' && <LiveLab ctx={ctx} />}
        {tab === 'monitor' && <Monitor ctx={ctx} />}
        {tab === 'campaign' && <Campaign ctx={ctx} onOpenRun={openRun} onOpenReport={() => setTab('findings')} goLive={() => setTab('live')} />}
        {tab === 'replay' && <Replay initialRun={replayRun} />}
        {tab === 'findings' && <Findings onOpenRun={openRun} />}
      </main>
    </div>
  );
}
