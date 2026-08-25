/** Layout primitives shared by both pages. Small on purpose. */
import type { ReactNode } from 'react';

import { ApiError } from './api';

export function Panel({
  title,
  actions,
  children,
  flush = false,
  className = '',
}: {
  title?: ReactNode;
  /** Pushed to the right of the title bar: buttons, counts, badges. */
  actions?: ReactNode;
  children: ReactNode;
  /** Drop the inner padding when the body is a full-bleed table or viewer. */
  flush?: boolean;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2>{title}</h2>}
          {actions && (
            <>
              <span className="spacer" />
              {actions}
            </>
          )}
        </header>
      )}
      <div className={flush ? '' : 'panel-body'}>{children}</div>
    </section>
  );
}

export function PageHead({ title, lede }: { title: ReactNode; lede?: ReactNode }) {
  return (
    <header className="page-head">
      <h1>{title}</h1>
      {lede && <p className="lede">{lede}</p>}
    </header>
  );
}

export type BadgeTone = 'neutral' | 'ok' | 'info' | 'warn' | 'danger' | 'accent';

export function Badge({
  tone = 'neutral',
  children,
  title,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

/** One labelled figure — the summary chips over a conversion result. */
export function Chip({ k, v, title }: { k: string; v: ReactNode; title?: string }) {
  return (
    <span className="chip" title={title}>
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </span>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row" style={{ gap: 'var(--s-2)' }}>
      <span className="spinner" aria-hidden="true" />
      {label && <span className="muted">{label}</span>}
    </span>
  );
}

export function EmptyState({ title, body }: { title: string; body?: ReactNode }) {
  return (
    <div className="state">
      <div className="state-title">{title}</div>
      {body && <div>{body}</div>}
    </div>
  );
}

/**
 * One error surface for both pages. A 422 needs_ocr is the service *declining to guess* at a
 * scan with no recogniser configured — that is the product working, so it wears the calm
 * blue, never the red.
 */
export function ErrorNotice({ error }: { error: unknown }) {
  const api = error instanceof ApiError ? error : null;
  if (api?.needsOcr) {
    return (
      <div className="notice tone-info" role="status">
        <div className="notice-title">needs recognition</div>
        <div className="muted">
          This document has no readable text layer and no OCR endpoint is configured on this
          deployment, so the service refused to guess. {api.message}
        </div>
      </div>
    );
  }
  const message =
    api?.message ?? (error instanceof Error ? error.message : 'something went wrong');
  return (
    <div className="notice" role="alert">
      <div className="notice-title">conversion failed{api ? ` (HTTP ${api.status})` : ''}</div>
      <div className="muted">{message}</div>
    </div>
  );
}
