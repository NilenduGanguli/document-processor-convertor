/**
 * History — the ledger. Every conversion the service has done, newest first; clicking a row
 * fetches its stored markdown back out of S3 and opens it in the same viewer Convert uses,
 * so what you review later is what was stored, not what was echoed.
 */
import { useCallback, useEffect, useState } from 'react';

import { getConversion, getMarkdown, listConversions, type ConversionRow } from '../api';
import { Badge, EmptyState, ErrorNotice, PageHead, Panel, Spinner, type BadgeTone } from '../components';
import ResultViewer, { type ViewerMeta } from '../ResultViewer';

const PAGE_SIZE = 50;

function statusTone(status: string): BadgeTone {
  if (status === 'ok' || status === 'done' || status === 'success') return 'ok';
  if (status === 'error' || status === 'failed') return 'danger';
  return 'neutral';
}

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('en-GB');
}

export default function History() {
  const [rows, setRows] = useState<ConversionRow[] | null>(null);
  const [offset, setOffset] = useState(0);
  const [listError, setListError] = useState<unknown>(null);

  const [selected, setSelected] = useState<string | null>(null);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [meta, setMeta] = useState<ViewerMeta | undefined>();
  const [loadingDoc, setLoadingDoc] = useState(false);
  const [docError, setDocError] = useState<unknown>(null);

  const refresh = useCallback(() => {
    setListError(null);
    listConversions(PAGE_SIZE, offset)
      .then(setRows)
      .catch((e) => {
        setRows([]);
        setListError(e);
      });
  }, [offset]);

  useEffect(refresh, [refresh]);

  const open = useCallback(async (row: ConversionRow) => {
    setSelected(row.id);
    setMarkdown(null);
    setDocError(null);
    setLoadingDoc(true);
    setMeta({
      pages: row.pages,
      blocks: row.blocks,
      tables: row.tables_n,
      chars: row.chars,
      source: row.source,
      provider: row.provider,
      ms: row.ms,
    });
    try {
      // The markdown is the point; the detail row is only richer chips. Fetch both, and
      // survive the detail call failing — the bytes still render.
      const [md, detail] = await Promise.allSettled([getMarkdown(row.id), getConversion(row.id)]);
      if (md.status === 'rejected') throw md.reason;
      setMarkdown(md.value);
      if (detail.status === 'fulfilled') {
        const d = detail.value;
        setMeta({
          pages: d.pages,
          blocks: d.blocks,
          tables: d.tables_n,
          chars: d.chars,
          source: d.source,
          provider: d.provider,
          ms: d.ms,
        });
      }
    } catch (e) {
      setDocError(e);
    } finally {
      setLoadingDoc(false);
    }
  }, []);

  return (
    <div className="page">
      <PageHead
        title="History"
        lede="Every conversion this service has stored, newest first. Click a row to fetch its markdown back out of the object store."
      />
      <div className="stack">
        <Panel
          title="conversions"
          flush
          actions={
            <span className="row">
              <span className="faint">{rows ? `${rows.length} shown` : ''}</span>
              <button
                className="btn btn-sm btn-ghost"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                newer
              </button>
              <button
                className="btn btn-sm btn-ghost"
                disabled={!rows || rows.length < PAGE_SIZE}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                older
              </button>
              <button className="btn btn-sm" onClick={refresh}>
                refresh
              </button>
            </span>
          }
        >
          {rows === null ? (
            <div className="state">
              <Spinner label="loading…" />
            </div>
          ) : listError != null ? (
            <div className="panel-body">
              <ErrorNotice error={listError} />
            </div>
          ) : rows.length === 0 ? (
            <EmptyState
              title="nothing converted yet"
              body="Run a document through the Convert tab and it will appear here."
            />
          ) : (
            <div className="scroll-x">
              <table className="grid">
                <thead>
                  <tr>
                    <th>created</th>
                    <th>doc id</th>
                    <th>source</th>
                    <th>provider</th>
                    <th>pages</th>
                    <th>chars</th>
                    <th>status</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr
                      key={row.id}
                      className={`clickable ${row.id === selected ? 'selected' : ''}`}
                      onClick={() => void open(row)}
                    >
                      <td className="nowrap tabular">{when(row.created_at)}</td>
                      <td className="mono">{row.doc_id || <span className="faint">—</span>}</td>
                      <td>{row.source}</td>
                      <td className="mono">{row.provider}</td>
                      <td className="tabular">{row.pages ?? ''}</td>
                      <td className="tabular">{row.chars?.toLocaleString('en-US') ?? ''}</td>
                      <td>
                        <Badge tone={statusTone(row.status)} title={row.error ?? undefined}>
                          {row.status}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {loadingDoc && (
          <Panel title="document">
            <Spinner label="fetching markdown…" />
          </Panel>
        )}
        {docError != null && <ErrorNotice error={docError} />}
        {markdown != null && (
          <ResultViewer markdown={markdown} meta={meta} title={`stored PMD — ${selected}`} />
        )}
      </div>
    </div>
  );
}
