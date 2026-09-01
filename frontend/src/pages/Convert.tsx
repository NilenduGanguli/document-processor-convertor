/**
 * Convert — the working page. Two ways in, one viewer out:
 *   File        drag/drop or pick a PDF/image, sent as base64;
 *   Paste JSON  an Azure Read / Azure analyze / DES OCR payload someone already has.
 * Both call POST /api/v1/convert with echo=true so the markdown comes straight back.
 */
import { useCallback, useRef, useState, type DragEvent } from 'react';

import {
  JSON_KINDS,
  convertFile,
  convertJson,
  fileToBase64,
  type ConvertResponse,
  type JsonKind,
} from '../api';
import { Badge, ErrorNotice, PageHead, Panel, Spinner } from '../components';
import ResultViewer from '../ResultViewer';

type Mode = 'file' | 'json';

export default function Convert() {
  const [mode, setMode] = useState<Mode>('file');
  const [docId, setDocId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<ConvertResponse | null>(null);

  const run = useCallback(async (work: Promise<ConvertResponse>) => {
    setBusy(true);
    setError(null);
    try {
      setResult(await work);
    } catch (e) {
      setResult(null);
      setError(e);
    } finally {
      setBusy(false);
    }
  }, []);

  /* ------------------------------------------------------------- file mode */

  const [over, setOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const takeFile = useCallback(
    async (file: File | undefined | null) => {
      if (!file || busy) return;
      const base64 = await fileToBase64(file);
      await run(convertFile({ name: file.name, base64 }, docId.trim()));
    },
    [busy, docId, run],
  );

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    void takeFile(e.dataTransfer.files?.[0]);
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
    void run(convertJson(kind, payload, docId.trim()));
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
                File
              </button>
              <button
                className={`btn btn-sm ${mode === 'json' ? 'btn-primary' : 'btn-ghost'}`}
                onClick={() => setMode('json')}
              >
                Paste JSON
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
                  className={`dropzone ${over ? 'over' : ''}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => fileInput.current?.click()}
                  onKeyDown={(e) => e.key === 'Enter' && fileInput.current?.click()}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setOver(true);
                  }}
                  onDragLeave={() => setOver(false)}
                  onDrop={onDrop}
                >
                  <span className="big">drop a document here</span>
                  <span>PDF or image — or click to pick a file</span>
                  <span className="faint">
                    a PDF with a text layer is read locally; scans go to OCR if this deployment
                    has one
                  </span>
                </div>
                <input
                  ref={fileInput}
                  type="file"
                  accept=".pdf,image/*,application/pdf"
                  style={{ display: 'none' }}
                  onChange={(e) => {
                    void takeFile(e.target.files?.[0]);
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
                    disabled={busy || !jsonText.trim()}
                    onClick={submitJson}
                  >
                    convert
                  </button>
                </div>
              </>
            )}

            {busy && <Spinner label="converting…" />}
            {error != null && <ErrorNotice error={error} />}
          </div>
        </Panel>

        {result?.markdown != null && (
          <ResultViewer
            markdown={result.markdown}
            title={result.doc_id ? `result — ${result.doc_id}` : 'result'}
            conversionId={result.id}
            meta={{
              pages: result.pages,
              blocks: result.blocks,
              tables: result.tables,
              chars: result.chars,
              source: result.source,
              provider: result.provider,
              ms: result.ms,
              treeStatus: result.tree_status,
              treeNodes: result.tree_nodes,
              sha256Tree: result.sha256_tree,
            }}
          />
        )}
      </div>
    </div>
  );
}
