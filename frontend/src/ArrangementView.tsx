/**
 * The Arrangement tab — the newest stored arrangement.json rendered read-only.
 *
 * The artifact is the audit trail of the advisory LLM arrange pass (dpc/arrange): what
 * model ran, what it saw (payload_mode), every verdict the verifier issued, and the ops
 * that survived. Three artifact statuses map straight through:
 *   ran        header chips + the verdict table + accepted ops;
 *   skipped    a calm line naming the closed-set reason;
 *   error:*    the boundary failure's class name (never a message — PII rule).
 * A 404 is the honest empty state: no pass was enqueued for this conversion at all.
 */
import { useEffect, useState } from 'react';

import {
  ApiError,
  getArrangement,
  isArrangement,
  type Arrangement,
  type ArrangeVerdict,
  type ArrangeWindow,
} from './api';
import { Badge, Chip, EmptyState, ErrorNotice, ShaChip, Spinner, type BadgeTone } from './components';

function verdictTone(verdict: string): BadgeTone {
  if (verdict === 'ACCEPTED') return 'ok';
  if (verdict === 'ADVISORY') return 'info';
  if (verdict.startsWith('REJECT')) return 'warn';
  return 'neutral';
}

function statusTone(status: string): BadgeTone {
  if (status === 'ran') return 'ok';
  if (status === 'skipped') return 'neutral';
  if (status.startsWith('error')) return 'danger';
  return 'neutral';
}

/** Every verdict across the windows, flattened with its window for the one table. */
function allVerdicts(windows: ArrangeWindow[]): Array<{ w: ArrangeWindow; v: ArrangeVerdict }> {
  const out: Array<{ w: ArrangeWindow; v: ArrangeVerdict }> = [];
  for (const w of windows) for (const v of w.verdicts ?? []) out.push({ w, v });
  return out;
}

function Artifact({ arr }: { arr: Arrangement }) {
  const windows = arr.windows ?? [];
  const verdicts = allVerdicts(windows);
  const skippedWindows = windows.filter((w) => w.skipped != null);
  const accepted = arr.accepted_ops ?? [];

  return (
    <div className="stack" style={{ padding: 'var(--s-3) var(--s-4)' }}>
      <div className="row">
        <Badge tone={statusTone(arr.status)}>{arr.status}</Badge>
        {arr.reason && <Chip k="reason" v={arr.reason} />}
        {arr.model_id && <Chip k="model" v={arr.model_id} />}
        {arr.payload_mode && (
          <Chip
            k="payload"
            v={arr.payload_mode}
            title="what the model was shown — multimodal carries page images, structure does not"
          />
        )}
        {arr.samples != null && <Chip k="samples" v={arr.samples} />}
        {arr.prompt_template_version && <Chip k="prompt" v={arr.prompt_template_version} />}
        {arr.verifier_version && <Chip k="verifier" v={arr.verifier_version} />}
        {arr.sha256_tree && <ShaChip k="tree sha" sha={arr.sha256_tree} />}
        {arr.pmd_sha256 && <ShaChip k="pmd sha" sha={arr.pmd_sha256} />}
      </div>

      {skippedWindows.length > 0 && (
        <div className="row">
          <Badge tone="warn">
            {skippedWindows.length} window{skippedWindows.length === 1 ? '' : 's'} skipped (
            {skippedWindows[0].skipped})
          </Badge>
        </div>
      )}

      {verdicts.length > 0 ? (
        <div className="scroll-x">
          <table className="grid">
            <thead>
              <tr>
                <th>window</th>
                <th>page</th>
                <th>op</th>
                <th>node</th>
                <th>ref</th>
                <th>reason</th>
                <th>votes</th>
                <th>verdict</th>
              </tr>
            </thead>
            <tbody>
              {verdicts.map(({ w, v }, i) => (
                <tr key={i}>
                  <td className="tabular faint">{w.window_ix}</td>
                  <td className="tabular">{w.page ?? ''}</td>
                  <td className="mono">{v.op.op}</td>
                  <td className="mono">{v.op.node}</td>
                  <td className="mono">{v.op.ref ?? ''}</td>
                  <td className="faint">{v.op.reason}</td>
                  <td className="tabular">{v.votes ?? ''}</td>
                  <td>
                    <Badge tone={verdictTone(v.verdict)} title={v.rule ?? undefined}>
                      {v.verdict}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        arr.status === 'ran' && (
          <p className="muted">The pass ran but no window produced a verdict.</p>
        )
      )}

      {accepted.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">accepted ops (canonical application order)</span>
          <ol className="accepted-ops">
            {accepted.map((op, i) => (
              <li key={i} className="mono">
                {op.op} <span className="faint">node</span> {op.node}
                {op.ref != null && (
                  <>
                    {' '}
                    <span className="faint">ref</span> {op.ref}
                  </>
                )}
                {op.reason && <span className="faint"> — {op.reason}</span>}
              </li>
            ))}
          </ol>
        </div>
      )}
      {arr.status === 'ran' && accepted.length === 0 && (
        <p className="muted">No ops were accepted — the heuristic arrangement stands as-is.</p>
      )}
    </div>
  );
}

type ArrState =
  | { phase: 'loading' }
  | { phase: 'ready'; arr: Arrangement }
  | { phase: 'absent' }
  | { phase: 'error'; error: unknown };

export default function ArrangementView({ conversionId }: { conversionId: string }) {
  const [state, setState] = useState<ArrState>({ phase: 'loading' });

  useEffect(() => {
    let live = true;
    setState({ phase: 'loading' });
    getArrangement(conversionId)
      .then((body) => {
        if (!live) return;
        if (isArrangement(body)) setState({ phase: 'ready', arr: body });
        else {
          setState({
            phase: 'error',
            error: new Error('the /arrangement response is not an arrangement artifact'),
          });
        }
      })
      .catch((e: unknown) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 404) setState({ phase: 'absent' });
        else setState({ phase: 'error', error: e });
      });
    return () => {
      live = false;
    };
  }, [conversionId]);

  if (state.phase === 'loading') {
    return (
      <div className="panel-body">
        <Spinner label="fetching arrangement…" />
      </div>
    );
  }
  if (state.phase === 'absent') {
    return (
      <EmptyState
        title="no arrangement stored"
        body="The advisory arrange pass did not run for this conversion — arrange_mode=off, tree_mode below emit, or an older conversion."
      />
    );
  }
  if (state.phase === 'error') {
    return (
      <div className="panel-body">
        <ErrorNotice error={state.error} />
      </div>
    );
  }
  return <Artifact arr={state.arr} />;
}
