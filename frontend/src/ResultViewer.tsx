/**
 * The PMD viewer — one component, used by Convert (fresh result) and History (fetched back).
 *
 * Three tabs, three honesty levels of the same bytes:
 *   Rendered  what a downstream consumer's markdown renderer sees (anchors are HTML
 *             comments, so they vanish — that is the format working);
 *   Raw PMD   the exact stored bytes, anchors visible;
 *   Anchors   just the geometry, parsed into a table: page, rect, tag.
 */
import { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { Chip, EmptyState, Panel } from './components';

export interface ViewerMeta {
  pages?: number | null;
  blocks?: number | null;
  tables?: number | null;
  chars?: number | null;
  source?: string;
  provider?: string;
  ms?: number | null;
}

interface Anchor {
  page: number;
  rect: [number, number, number, number];
  tag: string;
}

/** `<!-- @<page> [x0,y0,x1,y1] <tag> -->` — the grammar from docs/SPEC-PMD.md. */
const ANCHOR_RE = /<!--\s*@(\d+)\s*\[(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]\s*(.*?)\s*-->/g;

function parseAnchors(markdown: string): Anchor[] {
  const out: Anchor[] = [];
  for (const m of markdown.matchAll(ANCHOR_RE)) {
    out.push({
      page: Number(m[1]),
      rect: [Number(m[2]), Number(m[3]), Number(m[4]), Number(m[5])],
      tag: m[6] || 'p',
    });
  }
  return out;
}

/** The front matter is provenance, not document content; the chips already carry it. */
function stripFrontMatter(markdown: string): string {
  if (!markdown.startsWith('---\n')) return markdown;
  const end = markdown.indexOf('\n---\n', 4);
  return end < 0 ? markdown : markdown.slice(end + 5);
}

const TABS = ['Rendered', 'Raw PMD', 'Anchors'] as const;
type Tab = (typeof TABS)[number];

export default function ResultViewer({
  markdown,
  meta,
  title = 'result',
}: {
  markdown: string;
  meta?: ViewerMeta;
  title?: string;
}) {
  const [tab, setTab] = useState<Tab>('Rendered');
  const anchors = useMemo(() => parseAnchors(markdown), [markdown]);
  const rendered = useMemo(() => stripFrontMatter(markdown), [markdown]);

  const chips = meta && (
    <div className="row" style={{ padding: 'var(--s-3) var(--s-4)' }}>
      {meta.pages != null && <Chip k="pages" v={meta.pages} />}
      {meta.blocks != null && <Chip k="blocks" v={meta.blocks} />}
      {meta.tables != null && <Chip k="tables" v={meta.tables} />}
      {meta.chars != null && <Chip k="chars" v={meta.chars.toLocaleString('en-US')} />}
      {meta.source && <Chip k="source" v={meta.source} />}
      {meta.provider && <Chip k="provider" v={meta.provider} />}
      {meta.ms != null && <Chip k="took" v={`${meta.ms} ms`} />}
    </div>
  );

  return (
    <Panel title={title} flush actions={<span className="faint mono">{anchors.length} anchors</span>}>
      {chips}
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
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

      {tab === 'Rendered' && (
        <div className="md">
          {/* skipHtml: anchors are HTML comments and MUST vanish when rendered —
              that is the format working. Raw PMD is the tab where they show. */}
          <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
            {rendered}
          </ReactMarkdown>
        </div>
      )}

      {tab === 'Raw PMD' && <pre className="raw-pmd">{markdown}</pre>}

      {tab === 'Anchors' &&
        (anchors.length === 0 ? (
          <EmptyState
            title="no anchors"
            body="No element in this document carried geometry — no bbox, no anchor."
          />
        ) : (
          <div className="scroll-x">
            <table className="grid">
              <thead>
                <tr>
                  <th>#</th>
                  <th>page</th>
                  <th>rect [x0, y0, x1, y1]</th>
                  <th>tag</th>
                </tr>
              </thead>
              <tbody>
                {anchors.map((a, i) => (
                  <tr key={i}>
                    <td className="faint tabular">{i + 1}</td>
                    <td className="tabular">{a.page}</td>
                    <td className="mono tabular">[{a.rect.join(', ')}]</td>
                    <td className="mono">{a.tag}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </Panel>
  );
}
