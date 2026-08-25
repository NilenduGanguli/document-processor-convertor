/**
 * App shell: nav, health pill, error boundary, two routes.
 *   /convert  run a document or payload through and read the PMD   (default)
 *   /history  the ledger of everything stored
 */
import { Component, useEffect, useState, type ReactNode } from 'react';
import { Navigate, NavLink, Route, Routes } from 'react-router-dom';

import { health, type HealthResponse } from './api';
import { Badge } from './components';
import Convert from './pages/Convert';
import History from './pages/History';

/** One page throwing must not take the console with it. */
class Boundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="page">
          <div className="notice" role="alert">
            <div className="notice-title">this page crashed</div>
            <div className="muted">
              {this.state.error.message} — the nav above still works.{' '}
              <button className="btn btn-sm" onClick={() => this.setState({ error: null })}>
                retry
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function HealthPill() {
  const [info, setInfo] = useState<HealthResponse | null>(null);
  const [down, setDown] = useState(false);

  useEffect(() => {
    let live = true;
    const poll = () =>
      health()
        .then((h) => live && (setInfo(h), setDown(false)))
        .catch(() => live && setDown(true));
    poll();
    const timer = window.setInterval(poll, 30_000);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, []);

  if (down) return <Badge tone="danger">service unreachable</Badge>;
  if (!info) return null;
  return (
    <Badge tone="ok" title={`${info.service} v${info.version}`}>
      {info.status}
    </Badge>
  );
}

const TABS: Array<[string, string, string]> = [
  ['/convert', 'Convert', 'run a document through and read the PMD'],
  ['/history', 'History', 'everything this service has stored'],
];

export default function App() {
  return (
    <div className="app">
      <nav className="nav">
        <NavLink to="/convert" className="nav-brand">
          <span>Document Processor</span>
          <span className="sub">positional markdown</span>
        </NavLink>
        <div className="nav-links">
          {TABS.map(([to, label, title]) => (
            <NavLink
              key={to}
              to={to}
              title={title}
              className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}
            >
              {label}
            </NavLink>
          ))}
        </div>
        <div className="nav-right">
          <HealthPill />
        </div>
      </nav>

      <Boundary>
        <Routes>
          <Route path="/" element={<Navigate to="/convert" replace />} />
          <Route path="/convert" element={<Convert />} />
          <Route path="/history" element={<History />} />
          <Route path="*" element={<Navigate to="/convert" replace />} />
        </Routes>
      </Boundary>
    </div>
  );
}
