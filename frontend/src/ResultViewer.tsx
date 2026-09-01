/**
 * The PMD viewer — one component, used by Convert (fresh result) and History (fetched back).
 *
 * Four tabs:
 *   Markdown     the stored PMD 2.0 bytes exactly as-is, in monospace — fenced ```text
 *                canvas blocks must never re-wrap, so nothing here wraps; anchor comments
 *                stay visible but de-emphasised (dim, they are addresses not prose);
 *   Tree         the doctree artifact (structure only, invariant I5 — no document text);
 *   Arrangement  the advisory arrange pass's audit artifact, or an honest empty state;
 *   Meta         provenance: provider, shas (short, click-to-copy), passes manifest,
 *                timing, and the download buttons for .md / tree.json / tree.md.
 */
import { useMemo, useState, type ReactNode } from 'react';

import {
  downloadText,
  getTreeMarkdown,
  getTreeRaw,
} from './api';
import ArrangementView from './ArrangementView';
import { Chip, ErrorNotice, Panel, ShaChip } from './components';
import TreeView from './TreeView';

export interface ViewerMeta {
  pages?: number | null;
  blocks?: number | null;
  tables?: number | null;
  marks?: number | null;
  keyValues?: number | null;
  chars?: number | null;
  source?: string;
  provider?: string;
  filename?: string | null;
  createdAt?: string | null;
  ms?: number | null;
  sha256Markdown?: string | null;
  sha256Input?: string | null;
  /** Doctree meta — surfaced when the conversions API returns it (SPEC-DOCTREE-1 §6.1). */
  treeStatus?: string | null;
  treeSource?: string | null;
  treeNodes?: number | null;
  sha256Tree?: string | null;
  sha256TreeMarkdown?: string | null;
  passes?: Record<string, string> | string | null;
}

/** `<!-- @<page> [x0,y0,x1,y1] <tag> -->` — the grammar from docs/SPEC-PMD.md. */
const ANCHOR_RE = /<!--\s*@(\d+)\s*\[(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]\s*(.*?)\s*-->/g;

function countAnchors(markdown: string): number {
  return markdown.match(ANCHOR_RE)?.length ?? 0;
}

/**
 * The PMD source, byte-faithful: monospace, `white-space: pre` (canvas rows in the fenced
 * ```text blocks must NEVER re-wrap — the columns are the content), anchors dimmed.
 */
function PmdSource({ markdown }: { markdown: string }) {
  const parts = useMemo(() => {
    const out: Array<{ anchor: boolean; text: string }> = [];
    let last = 0;
    for (const m of markdown.matchAll(ANCHOR_RE)) {
      const at = m.index ?? 0;
      if (at > last) out.push({ anchor: false, text: markdown.slice(last, at) });
      out.push({ anchor: true, text: m[0] });
      last = at + m[0].length;
    }
    if (last < markdown.length) out.push({ anchor: false, text: markdown.slice(last) });
    return out;
  }, [markdown]);

  return (
    <pre className="pmd-source">
      {parts.map((p, i) =>
        p.anchor ? (
          <span key={i} className="pmd-anchor">
            {p.text}
          </span>
        ) : (
          p.text
        ),
      )}
    </pre>
  );
}

/** The passes manifest as chips; tolerates the raw JSON string an older row might carry. */
function PassChips({ passes }: { passes: Record<string, string> | string }) {
  let entries: Array<[string, string]>;
  if (typeof passes === 'string') {
    try {
      entries = Object.entries(JSON.parse(passes) as Record<string, string>);
    } catch {
      return <span className="mono faint">{passes}</span>;
    }
  } else {
    entries = Object.entries(passes);
  }
  entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return (
    <>
      {entries.map(([k, v]) => (
        <Chip key={k} k={k} v={String(v)} />
      ))}
    </>
  );
}

function MetaTab({
  meta,
  markdown,
  conversionId,
}: {
  meta?: ViewerMeta;
  markdown: string;
  conversionId?: string;
}) {
  const [downloadError, setDownloadError] = useState<unknown>(null);
  const m = meta ?? {};
  const shortId = conversionId ? conversionId.slice(0, 8) : 'result';

  const fetchAndSave = (
    fetcher: (id: string) => Promise<string>,
    name: string,
    type: string,
  ) => {
    if (!conversionId) return;
    setDownloadError(null);
    fetcher(conversionId)
      .then((text) => downloadText(name, text, type))
      .catch(setDownloadError);
  };

  return (
    <div className="stack" style={{ padding: 'var(--s-3) var(--s-4)' }}>
      <div className="row">
        {m.source && <Chip k="source" v={m.source} />}
        {m.provider && <Chip k="provider" v={m.provider} />}
        {m.filename && <Chip k="file" v={m.filename} />}
        {m.pages != null && <Chip k="pages" v={m.pages} />}
        {m.blocks != null && <Chip k="blocks" v={m.blocks} />}
        {m.tables != null && <Chip k="tables" v={m.tables} />}
        {m.marks != null && <Chip k="marks" v={m.marks} />}
        {m.keyValues != null && <Chip k="key values" v={m.keyValues} />}
        {m.chars != null && <Chip k="chars" v={m.chars.toLocaleString('en-US')} />}
        {m.ms != null && <Chip k="took" v={`${m.ms.toLocaleString('en-US')} ms`} />}
        {m.createdAt && <Chip k="created" v={m.createdAt} />}
      </div>

      <div className="row">
        {m.sha256Markdown && <ShaChip k="sha md" sha={m.sha256Markdown} />}
        {m.sha256Input && <ShaChip k="sha input" sha={m.sha256Input} />}
        {m.sha256Tree && <ShaChip k="sha tree" sha={m.sha256Tree} />}
        {m.sha256TreeMarkdown && <ShaChip k="sha tree.md" sha={m.sha256TreeMarkdown} />}
        {m.treeStatus != null && <Chip k="tree" v={m.treeStatus} />}
        {m.treeSource != null && <Chip k="tree source" v={m.treeSource} />}
        {m.treeNodes != null && <Chip k="tree nodes" v={m.treeNodes} />}
      </div>

      {m.passes != null && (
        <div className="row">
          <span className="label">passes</span>
          <PassChips passes={m.passes} />
        </div>
      )}

      <div className="row">
        <span className="label">download</span>
        <button
          className="btn btn-sm"
          onClick={() => downloadText(`${shortId}.md`, markdown, 'text/markdown')}
        >
          .md
        </button>
        {conversionId != null && m.sha256Tree != null && (
          <button
            className="btn btn-sm"
            onClick={() => fetchAndSave(getTreeRaw, `${shortId}.tree.json`, 'application/json')}
          >
            tree.json
          </button>
        )}
        {conversionId != null && m.sha256TreeMarkdown != null && (
          <button
            className="btn btn-sm"
            onClick={() => fetchAndSave(getTreeMarkdown, `${shortId}.tree.md`, 'text/markdown')}
          >
            tree.md
          </button>
        )}
      </div>
      {downloadError != null && <ErrorNotice error={downloadError} />}
    </div>
  );
}

const TABS = ['Markdown', 'Tree', 'Arrangement', 'Meta'] as const;
type Tab = (typeof TABS)[number];

export default function ResultViewer({
  markdown,
  meta,
  title = 'result',
  conversionId,
}: {
  markdown: string;
  meta?: ViewerMeta;
  title?: ReactNode;
  /**
   * The stored conversion's id — enables the Tree and Arrangement tabs, which fetch their
   * artifacts from the conversions API on first open. Without an id (nothing stored to
   * address) those tabs are simply not offered.
   */
  conversionId?: string;
}) {
  const [tab, setTab] = useState<Tab>('Markdown');
  const anchorCount = useMemo(() => countAnchors(markdown), [markdown]);
  const tabs = conversionId ? TABS : TABS.filter((t) => t !== 'Tree' && t !== 'Arrangement');

  return (
    <Panel title={title} flush actions={<span className="faint mono">{anchorCount} anchors</span>}>
      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            className={`tab ${tab === t ? 'active' : ''}`}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'Markdown' && <PmdSource markdown={markdown} />}
      {tab === 'Tree' && conversionId != null && <TreeView conversionId={conversionId} />}
      {tab === 'Arrangement' && conversionId != null && (
        <ArrangementView conversionId={conversionId} />
      )}
      {tab === 'Meta' && <MetaTab meta={meta} markdown={markdown} conversionId={conversionId} />}
    </Panel>
  );
}
