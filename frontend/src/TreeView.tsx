/**
 * The Tree tab — the stored doctree.json (SPEC-DOCTREE-1 §2.2) rendered read-only.
 *
 * Three parts, in honesty order:
 *   header   the passes manifest + counters — what the builder actually ran and claimed,
 *            shown BEFORE the pretty tree so a flat fallback can't masquerade as structure;
 *   tree     collapsible nodes: kind badge, path, page, char count; flow edges listed under
 *            their source node ("continues -> <path> (score N)") — annotations, never reorder;
 *   absent   a calm state for 404: no tree is a normal outcome (tree_mode=off, an older
 *            conversion, a declined build), not an error surface.
 *
 * The artifact carries zero document strings (invariant I5), so this view can only ever show
 * structure — which is the point: it is safe to render anything the endpoint returns.
 */
import { useEffect, useMemo, useState } from 'react';

import {
  ApiError,
  getTree,
  isDocTree,
  treeStatusFrom,
  type DocTree,
  type FlowEdge,
  type TreeNode,
} from './api';
import { Badge, Chip, EmptyState, ErrorNotice, Spinner, type BadgeTone } from './components';

/* ------------------------------------------------------------ kind looks */

/** Tone + short tag per node kind — closed set from §2.1; unknown kinds get neutral. */
const KIND_LOOK: Record<string, [BadgeTone, string]> = {
  document: ['neutral', 'doc'],
  body: ['neutral', 'body'],
  furniture: ['neutral', 'furniture'],
  section: ['accent', 'section'],
  flow_group: ['info', 'flow group'],
  frame: ['info', 'frame'],
  heading: ['accent', 'heading'],
  paragraph: ['neutral', 'para'],
  footnote: ['neutral', 'footnote'],
  table: ['info', 'table'],
  figure: ['info', 'figure'],
  caption: ['info', 'caption'],
  kv_group: ['warn', 'kv group'],
  kv_pair: ['warn', 'kv'],
  list_group: ['neutral', 'list'],
  list_item: ['neutral', 'item'],
  mark: ['neutral', 'mark'],
};

function kindLook(kind: string): [BadgeTone, string] {
  return KIND_LOOK[kind] ?? ['neutral', kind];
}

/* --------------------------------------------------------------- one row */

function NodeRow({
  tree,
  node,
  depth,
  edgesBySrc,
  seen,
}: {
  tree: DocTree;
  node: TreeNode;
  depth: number;
  edgesBySrc: Map<number, FlowEdge[]>;
  seen: Set<number>;
}) {
  // Sections and above start open; leaves deep in a long document start closed so a
  // 500-node tree is scannable. Purely presentational — no data hides permanently.
  const [open, setOpen] = useState(depth < 3);

  const kids = (node.children ?? []).filter(
    (i) => Number.isInteger(i) && i >= 0 && i < tree.nodes.length && !seen.has(i),
  );
  const [tone, tag] = kindLook(node.kind);
  const chars = node.metrics?.char_count;
  const edges = edgesBySrc.get(node.id) ?? [];

  return (
    <li className="tree-node">
      <div className="tree-row">
        {kids.length > 0 ? (
          <button
            className="tree-toggle"
            aria-expanded={open}
            title={open ? 'collapse' : 'expand'}
            onClick={() => setOpen(!open)}
          >
            {open ? '▾' : '▸'}
          </button>
        ) : (
          <span className="tree-toggle leaf" aria-hidden="true">
            {'·'}
          </span>
        )}
        <Badge tone={tone}>
          {tag}
          {node.level != null ? ` ${node.level}` : ''}
        </Badge>
        <span className="mono tree-path" title={`node ${node.id}`}>
          {node.path}
        </span>
        {node.page != null && <span className="faint tabular">p{node.page}</span>}
        {chars != null && <span className="faint tabular">{chars.toLocaleString('en-US')} ch</span>}
        {node.figure_id != null && <span className="mono faint">{node.figure_id}</span>}
        {node.table_ix != null && <span className="faint">table #{node.table_ix}</span>}
        {node.kv_ix != null && <span className="faint">kv #{node.kv_ix}</span>}
        {node.mark_ix != null && <span className="faint">mark #{node.mark_ix}</span>}
        {node.prov?.provider_role != null && (
          <span className="faint mono" title="verbatim provider role (unknown to the builder)">
            role={node.prov.provider_role}
          </span>
        )}
      </div>

      {edges.map((e) => {
        const dst = tree.nodes[e.dst];
        return (
          <div
            key={`${e.src}-${e.dst}`}
            className="tree-flow"
            title={e.evidence?.length ? `evidence: ${e.evidence.join(', ')}` : undefined}
          >
            {e.kind} {'→'} <span className="mono">{dst ? dst.path : `node ${e.dst}`}</span>
            {e.score != null && <span className="faint"> (score {e.score})</span>}
          </div>
        );
      })}

      {open && kids.length > 0 && (
        <ul className="tree-kids">
          {kids.map((i) => (
            <NodeRow
              key={i}
              tree={tree}
              node={tree.nodes[i]}
              depth={depth + 1}
              edgesBySrc={edgesBySrc}
              seen={new Set(seen).add(node.id)}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

/* --------------------------------------------------------- honesty header */

function HonestyHeader({ tree }: { tree: DocTree }) {
  const counters = tree.counters ?? {};
  const orderTies = tree.report?.order_ties?.length ?? 0;
  const fallbackPages = tree.report?.coverage_fallback_pages?.length ?? 0;
  const declinedPages = tree.report?.declined_pages?.length ?? 0;
  return (
    <div className="tree-header">
      <div className="row">
        {tree.builder && (
          <Chip k="builder" v={tree.builder} title="the stale-artifact tell — semver of the heuristics that built this tree" />
        )}
        {tree.view_sha256 && (
          <Chip k="view" v={tree.view_sha256.slice(0, 12)} title={tree.view_sha256} />
        )}
        {Object.entries(counters)
          .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
          .map(([k, v]) => (
            <Chip key={k} k={k} v={v} />
          ))}
      </div>
      {tree.passes && (
        <div className="row">
          <span className="label">passes</span>
          {Object.entries(tree.passes)
            .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
            .map(([k, v]) => (
              <Chip key={k} k={k} v={v} />
            ))}
        </div>
      )}
      {(orderTies > 0 || fallbackPages > 0 || declinedPages > 0) && (
        <div className="row">
          <span className="label">report</span>
          {orderTies > 0 && <Badge tone="warn">{orderTies} order ties</Badge>}
          {fallbackPages > 0 && <Badge tone="warn">{fallbackPages} coverage-fallback pages</Badge>}
          {declinedPages > 0 && <Badge tone="warn">{declinedPages} declined pages</Badge>}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- the view */

type TreeState =
  | { phase: 'loading' }
  | { phase: 'ready'; tree: DocTree }
  | { phase: 'absent'; treeStatus: string | null }
  | { phase: 'error'; error: unknown };

export default function TreeView({ conversionId }: { conversionId: string }) {
  const [state, setState] = useState<TreeState>({ phase: 'loading' });

  useEffect(() => {
    let live = true;
    setState({ phase: 'loading' });
    getTree(conversionId)
      .then((body) => {
        if (!live) return;
        if (isDocTree(body)) setState({ phase: 'ready', tree: body });
        else setState({ phase: 'error', error: new Error('the /tree response is not a doctree artifact') });
      })
      .catch((e: unknown) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 404) {
          setState({ phase: 'absent', treeStatus: treeStatusFrom(e) });
        } else {
          setState({ phase: 'error', error: e });
        }
      });
    return () => {
      live = false;
    };
  }, [conversionId]);

  if (state.phase === 'loading') {
    return (
      <div className="panel-body">
        <Spinner label="fetching tree…" />
      </div>
    );
  }

  if (state.phase === 'absent') {
    return (
      <EmptyState
        title="tree not built"
        body={
          <>
            This conversion has no stored doctree — tree_mode=off or an older conversion.
            {state.treeStatus && (
              <>
                {' '}
                <span className="mono">tree_status={state.treeStatus}</span>
              </>
            )}
          </>
        }
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

  return <TreeArtifact tree={state.tree} />;
}

/**
 * The pure half: render an already-fetched doctree. Split from the fetching wrapper so it
 * can be exercised against a typed mock without a live endpoint.
 */
export function TreeArtifact({ tree }: { tree: DocTree }) {
  const edgesBySrc = useMemo(() => {
    const by = new Map<number, FlowEdge[]>();
    for (const e of tree.flow ?? []) {
      if (!Number.isInteger(e.src) || !Number.isInteger(e.dst)) continue;
      const list = by.get(e.src) ?? [];
      list.push(e);
      by.set(e.src, list);
    }
    // Stable listing under each source: by destination id — the pre-order ordinal.
    for (const list of by.values()) list.sort((a, b) => a.dst - b.dst);
    return by;
  }, [tree]);

  // The root is the single document node — I2 says node 0, but find() survives a
  // malformed artifact instead of rendering garbage from a wrong index.
  const root = useMemo(
    () => tree.nodes.find((n) => n && n.kind === 'document') ?? tree.nodes[0] ?? null,
    [tree],
  );

  if (!root) {
    return <EmptyState title="empty tree" body="The artifact parsed but carries no nodes." />;
  }

  return (
    <div className="tree-view">
      <HonestyHeader tree={tree} />
      <ul className="tree-root">
        <NodeRow tree={tree} node={root} depth={0} edgesBySrc={edgesBySrc} seen={new Set()} />
      </ul>
    </div>
  );
}
