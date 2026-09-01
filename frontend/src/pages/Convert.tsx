/**
 * Convert — the working page. Upload-first:
 *   Upload         drag/drop or pick a file, streamed as multipart to POST /api/v1/process
 *                  (the primary path — a person holds a file, not base64);
 *   Provider JSON  an Azure Read / Azure analyze / DES OCR payload someone already has,
 *                  posted to /api/v1/convert (the advanced paste modes).
 * /process never echoes the markdown, so the page fetches it back by id once the row
 * exists — which also means what you see is what was STORED, same as History.
 */
import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react';

import {
  ACCEPT,
  JSON_KINDS,
  convertJson,
  getMarkdown,
  processFile,
  type ConvertResponse,
  type JsonKind,
} from '../api';
import { Badge, ErrorNotice, PageHead, Panel, Spinner } from '../components';
import ResultViewer, { type ViewerMeta } from '../ResultViewer';

type Mode = 'file' | 'json';

function metaOf(r: ConvertResponse): ViewerMeta {
  return {
    pages: r.pages,
    blocks: r.blocks,
    tables: r.tables,
    marks: r.marks,
    keyValues: r.key_values,
    chars: r.chars,
    source: r.source,
    provider: r.provider,
    ms: r.ms,
    sha256Markdown: r.sha256_markdown,
    treeStatus: r.tree_status,
    treeSource: r.tree_source,
    treeNodes: r.tree_nodes,
    sha256Tree: r.sha256_tree,
    sha256TreeMarkdown: r.sha256_tree_markdown,
    passes: r.passes,
  };
}

/** "converting…  1m 24s" — long OCR round trips need visible proof of life. */
function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const s = Math.max(0, Math.floor((now - since) / 1000));
  const text = s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
  return <span className="faint tabular">{text}</span>;
}

export default function Convert() {
  const [mode, setMode] = useState<Mode>('file');
  const [docId, setDocId] = useState('');
  const [busy, setBusy] = useState<{ label: string; since: number } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<{
    id: string;
    markdown: string;
    meta: ViewerMeta;
    docId: string;
  } | null>(null);

  const run = useCallback(
    async (label: string, work: () => Promise<{ response: ConvertResponse; markdown: string }>) => {
      setBusy({ label, since: Date.now() });
      setError(null);
      try {
        const { response, markdown } = await work();
        setResult({
          id: response.id,
          markdown,
          meta: metaOf(response),
          docId: response.doc_id,
        });
      } catch (e) {
        setResult(null);
        setError(e);
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  /* ------------------------------------------------------------- file mode */

  const [over, setOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const takeFile = useCallback(
    (file: File | undefined | null) => {
      if (!file || busy) return;
      void run(`converting ${file.name}`, async () => {
        const response = await processFile(file, docId.trim());
        // /process stores but never echoes — pull the stored bytes back by id. A conversion
        // that took minutes of OCR must never be thrown away because this READBACK blipped:
        // the row exists, so render the id and meta with a note instead of only an error.
        let markdown = '';
        try {
          markdown = await getMarkdown(response.id);
        } catch {
          markdown =
            `<!-- conversion ${response.id} stored; fetching its markdown failed — ` +
            'retry from History -->';
        }
        return { response, markdown };
      });
    },
    [busy, docId, run],
  );

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    takeFile(e.dataTransfer.files?.[0]);
  };

  /* ------------------------------------------------------------- json mode */

  const [kind, setKind] = useState<JsonKind>('azure_layout');
  const [jsonText, setJsonText] = useState('');

  const submitJson = () => {
    let payload: unknown;
    try {
      payload = JSON.parse(jsonText);
    } catch {
      setError(new Error('that is not valid JSON — fix it and try again'));
      return;
    }
    void run('converting payload', async () => {
      const response = await convertJson(kind, payload, docId.trim());
      return { response, markdown: response.markdown ?? (await getMarkdown(response.id)) };
    });
  };

  /* ------------------------------------------------------------------- ui */

  return (
    <div className="page">
      <PageHead
        title="Convert"
        lede="Turn a document — or a provider payload you already hold — into positional markdown: plain GFM with an anchor comment carrying each element's page and rectangle."
      />
      <div className="stack">
        <Panel
          title="input"
          actions={
            <span className="row">
              <button
                className={`btn btn-sm ${mode === 'file' ? 'btn-primary' : 'btn-ghost'}`}
                onClick={() => setMode('file')}
              >
                Upload
              </button>
              <button
                className={`btn btn-sm ${mode === 'json' ? 'btn-primary' : 'btn-ghost'}`}
                onClick={() => setMode('json')}
              >
                Provider JSON
              </button>
            </span>
          }
        >
          <div className="stack">
            <label className="row" style={{ gap: 'var(--s-2)' }}>
              <span className="label">doc id</span>
              <input
                type="text"
                value={docId}
                placeholder="optional — carried into the front matter"
                onChange={(e) => setDocId(e.target.value)}
                style={{ width: 320 }}
              />
            </label>

            {mode === 'file' ? (
              <>
                <div
                  className={`dropzone ${over ? 'over' : ''} ${busy ? 'busy' : ''}`}
                  role="button"
                  tabIndex={0}
                  aria-disabled={busy != null}
                  onClick={() => !busy && fileInput.current?.click()}
                  onKeyDown={(e) => e.key === 'Enter' && !busy && fileInput.current?.click()}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setOver(true);
                  }}
                  onDragLeave={() => setOver(false)}
                  onDrop={onDrop}
                >
                  <span className="big">drop a document here</span>
                  <span>or click to pick a file</span>
                  <span className="faint">
                    PDF · image (PNG/JPEG/TIFF/BMP/HEIC) · XLSX · DOCX · PPTX · HTML · TXT ·
                    CSV · MD · LOG · EML
                  </span>
                  <span className="faint">
                    every PDF and image is read by Document Intelligence — that round
                    trip can take minutes on large scans; leave this tab open
                  </span>
                </div>
                <input
                  ref={fileInput}
                  type="file"
                  accept={ACCEPT}
                  style={{ display: 'none' }}
                  onChange={(e) => {
                    takeFile(e.target.files?.[0]);
                    e.target.value = '';
                  }}
                />
              </>
            ) : (
              <>
                <label className="row" style={{ gap: 'var(--s-2)' }}>
                  <span className="label">kind</span>
                  <select value={kind} onChange={(e) => setKind(e.target.value as JsonKind)}>
                    {JSON_KINDS.map(([value, , label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                  <Badge tone="neutral">no bytes leave this origin</Badge>
                </label>
                <textarea
                  rows={12}
                  value={jsonText}
                  spellCheck={false}
                  placeholder='paste the provider payload, e.g. {"analyzeResult": {...}}'
                  onChange={(e) => setJsonText(e.target.value)}
                />
                <div className="row">
                  <button
                    className="btn btn-primary"
                    disabled={busy != null || !jsonText.trim()}
                    onClick={submitJson}
                  >
                    convert
                  </button>
                </div>
              </>
            )}

            {busy && (
              <div className="row" role="status">
                <Spinner label={`${busy.label}…`} />
                <Elapsed since={busy.since} />
                <span className="faint">
                  OCR round trips can take minutes — leave this tab open
                </span>
              </div>
            )}
            {error != null && <ErrorNotice error={error} />}
          </div>
        </Panel>

        {result != null && (
          <ResultViewer
            markdown={result.markdown}
            title={result.docId ? `result — ${result.docId}` : 'result'}
            conversionId={result.id}
            meta={result.meta}
          />
        )}
      </div>
    </div>
  );
}
