/* Schema Field Mapper UI.
 *
 * Preact + htm loaded as ES modules, so there is no build step and the whole
 * frontend is two files a reviewer can read. The interesting parts:
 *
 *  - The mapping graph draws one wire per decision, coloured and weighted by
 *    confidence, and animates each wire in as its `mapping` event arrives, so
 *    the pipeline's progress is the loading state rather than a spinner.
 *  - Selecting a field shows its full provenance: the candidate shortlist with
 *    per-component scores, every model pass it went through, and the transform
 *    executed against a real row.
 *  - The constraint panel restates the assignment's rule as live pass/fail
 *    checks computed from the run's own prompt manifests.
 */

import { h, render } from "https://esm.sh/preact@10.23.1";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "https://esm.sh/preact@10.23.1/hooks";
import htm from "https://esm.sh/htm@3.1.1";

const html = htm.bind(h);

/* ------------------------------------------------------------------ utils */

const BANDS = { high: 0.9, medium: 0.8 };
const band = (c) => (c >= BANDS.high ? "high" : c >= BANDS.medium ? "medium" : "review");
const usd = (n) => `$${(n ?? 0).toFixed(4)}`;
const pct = (n) => `${Math.round((n ?? 0) * 100)}%`;
const num = (n) => (n ?? 0).toLocaleString();
const clock = () => new Date().toLocaleTimeString([], { hour12: false });

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json();
}

/** Parse an SSE byte stream into {event, data} objects. */
async function* sseStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      let event = "message";
      const dataLines = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) {
        try {
          yield { event, data: JSON.parse(dataLines.join("\n")) };
        } catch (err) {
          console.warn("unparseable SSE payload", err);
        }
      }
    }
  }
}

/* ------------------------------------------------------------- components */

function Meter({ label, value, sub, tone }) {
  return html`<div class=${`meter ${tone || ""}`}>
    <div class="k">${label}</div>
    <div class="v">${value}${sub ? html`<small> ${sub}</small>` : null}</div>
  </div>`;
}

function Bar({ value, tone }) {
  return html`<div class=${`bar ${tone || ""}`}>
    <i style=${{ width: `${Math.max(2, Math.min(100, value * 100))}%` }}></i>
  </div>`;
}

/**
 * Wires live inside the middle canvas only. Y comes from the real source/dest
 * rows; X is the canvas left/right edge. That keeps every curve fully visible
 * instead of hiding under opaque schema columns (which looked like a broken
 * gap in the middle of the mapping).
 */
function WireLayer({ segments, selected, onSelect }) {
  return html`<svg class="wires" aria-hidden="true">
    ${segments.map(({ key, wire, x1, y1, x2, y2 }) => {
      const tone = band(wire.confidence ?? 0);
      const active = selected === wire.source_field;
      const dim = selected && !active;
      const span = Math.max(x2 - x1, 40);
      const reach = Math.min(span * 0.55, Math.max(span * 0.35, 48));
      return html`<path
        key=${key}
        class=${`wire ${tone} ${active ? "active" : ""} ${dim ? "dim" : ""}`}
        d=${`M ${x1} ${y1} C ${x1 + reach} ${y1}, ${x2 - reach} ${y2}, ${x2} ${y2}`}
        stroke-width=${active ? 2.8 : 1.45}
        onClick=${() => onSelect(wire.source_field)}
        style="cursor:pointer"
      />`;
    })}
    ${segments
      .filter(({ wire }) => selected === wire.source_field && wire.type_transform)
      .map(({ key, wire, x1, y1, x2, y2 }) => html`<text
        key=${`t${key}`}
        class="wire-label"
        x=${(x1 + x2) / 2}
        y=${(y1 + y2) / 2 - 8}
        text-anchor="middle"
      >${wire.type_transform}</text>`)}
  </svg>`;
}

/* ------------------------------------------------------------ detail tabs */

function DecisionPanel({ decision, mapping, rows, table, collection }) {
  const [preview, setPreview] = useState(null);
  const [previewError, setPreviewError] = useState(null);

  const runPreview = useCallback(async () => {
    if (!mapping || !rows.length) return;
    setPreviewError(null);
    try {
      const response = await fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          table,
          collection,
          row: rows[0],
          mappings: [
            {
              source_field: mapping.source_field,
              destination_field: mapping.destination_field,
            },
          ],
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      setPreview(await response.json());
    } catch (err) {
      setPreviewError(String(err));
    }
  }, [mapping, rows, table, collection]);

  useEffect(() => {
    setPreview(null);
    setPreviewError(null);
  }, [mapping?.source_field]);

  if (!mapping && !decision) {
    return html`<p class="note">
      Select a source column, a destination path, or a wire to see how that decision was reached.
    </p>`;
  }

  const passes = decision?.passes || [];
  const candidates = decision?.candidates || [];
  const tone = mapping ? band(mapping.confidence) : "review";

  return html`<div class="split">
    <div class="stack">
      <div>
        <h3 class="section">Decision</h3>
        <dl class="kv">
          <dt>Source</dt><dd><code>${table}.${mapping?.source_field || decision?.source_field}</code></dd>
          <dt>Destination</dt>
          <dd>
            ${mapping
              ? html`<code>${mapping.destination_field}</code>`
              : html`<span class="badge fail">unmapped</span>`}
          </dd>
          ${mapping &&
          html`<dt>Type transform</dt><dd><span class="pill">${mapping.type_transform}</span></dd>`}
          ${mapping &&
          html`<dt>Confidence</dt>
            <dd style="display:flex;align-items:center;gap:8px">
              <strong>${mapping.confidence.toFixed(2)}</strong>
              <span class="badge ${tone === "high" ? "ok" : tone === "medium" ? "warn" : "fail"}">${tone}</span>
              ${decision && html`<span class="note" style="margin:0">model said ${decision.model_confidence?.toFixed(2)}</span>`}
            </dd>`}
          <dt>Reasoning</dt><dd>${mapping?.reasoning || decision?.reasoning || "-"}</dd>
          <dt>Notes</dt>
          <dd>${mapping?.notes || html`<span class="note" style="margin:0">none needed</span>`}</dd>
          ${decision?.decided_by &&
          html`<dt>Decided by</dt><dd><span class="pill">${decision.decided_by}</span>
            ${decision.repaired ? html` <span class="badge warn">repaired</span>` : null}
            ${decision.tie_broken ? html` <span class="badge warn">tie-broken</span>` : null}
          </dd>`}
          ${decision?.forced_null &&
          html`<dt>Why unmapped</dt><dd>${decision.forced_null}</dd>`}
          ${decision?.knowledge_snippets?.length &&
          html`<dt>Conventions used</dt>
            <dd>${decision.knowledge_snippets.map((id) => html`<span class="pill" style="margin-right:4px">${id}</span>`)}</dd>`}
        </dl>
      </div>

      ${passes.length > 0 &&
      html`<div>
        <h3 class="section">Model passes (${passes.length})</h3>
        <table class="grid">
          <thead><tr><th>Pass</th><th>Model</th><th>Chose</th><th>Conf</th></tr></thead>
          <tbody>
            ${passes.map(
              (p, i) => html`<tr key=${i}>
                <td>${p.pass}</td>
                <td class="mono" style="font-size:10.5px">${(p.model || "").split(".").pop()}</td>
                <td class="mono">${p.destination_field || "null"}</td>
                <td>${(p.confidence ?? 0).toFixed(2)}</td>
              </tr>`
            )}
          </tbody>
        </table>
      </div>`}

      <div>
        <h3 class="section">Transform on a real row</h3>
        <button onClick=${runPreview} disabled=${!mapping || !rows.length}>
          Run this transform
        </button>
        ${previewError && html`<div class="error-box" style="margin-top:8px">${previewError}</div>`}
        ${preview &&
        html`<div style="margin-top:8px">
          <pre class="json">${JSON.stringify(preview.document, null, 2)}</pre>
          ${Object.entries(preview.annotations).map(
            ([path, info]) => html`<p class="note" key=${path}>
              <code>${path}</code> via <span class="pill">${info.rule}</span>
              ${info.manual ? html` <span class="badge warn">manual step</span>` : null}
              ${info.detail ? html`<br />${info.detail}` : null}
            </p>`
          )}
        </div>`}
      </div>
    </div>

    <div>
      <h3 class="section">
        Candidate shortlist ${candidates.length ? `(${candidates.length} offered by retrieval)` : ""}
      </h3>
      ${candidates.length
        ? html`<table class="grid">
            <thead>
              <tr>
                <th>Path</th><th>Total</th><th>Lexical</th><th>Type</th>
                <th>Key</th><th>Comment</th><th>Fuzzy</th>
              </tr>
            </thead>
            <tbody>
              ${candidates.map(
                (c) => html`<tr key=${c.path} class=${c.path === mapping?.destination_field ? "on" : ""}>
                  <td class="mono">${c.path}</td>
                  <td><strong>${c.total.toFixed(3)}</strong></td>
                  <td>${html`<${Bar} value=${c.lexical} tone="source" />`}</td>
                  <td>${html`<${Bar} value=${c.type_compat} />`}</td>
                  <td>${html`<${Bar} value=${c.key_role} />`}</td>
                  <td>${html`<${Bar} value=${c.comment} />`}</td>
                  <td>${html`<${Bar} value=${c.fuzzy} />`}</td>
                </tr>`
              )}
            </tbody>
          </table>
          <p class="note">
            Retrieval is deterministic and free: these scores come from token overlap, type
            compatibility, key role, and comment agreement. The model never sees them, so its
            judgement stays independent of the ranking.
          </p>`
        : html`<p class="note">No candidate data for this field in this run.</p>`}
    </div>
  </div>`;
}

function ConstraintPanel({ report }) {
  if (!report) return html`<p class="note">Run the pipeline to compute the constraint proof.</p>`;
  const c = report.constraint;
  const checks = [
    {
      ok: c.prompts_containing_both_full_schemas === 0,
      label: "No prompt contained both full schemas",
      detail: `${c.total_llm_calls} calls inspected, ${c.prompts_containing_both_full_schemas} violations`,
    },
    {
      ok: c.max_source_tables_in_one_prompt <= 1,
      label: "Each prompt reasoned about at most one source table",
      detail: `max ${c.max_source_tables_in_one_prompt} of ${c.total_source_tables} tables`,
    },
    {
      ok: c.max_typed_source_fields_in_one_prompt < c.total_source_fields,
      label: "No prompt carried the full source schema with types",
      detail: `max ${c.max_typed_source_fields_in_one_prompt} of ${c.total_source_fields} typed fields`,
    },
    {
      ok: c.max_destination_paths_in_one_prompt < c.total_destination_paths,
      label: "Destination exposure was a candidate shortlist, not the schema",
      detail: `max ${c.max_destination_paths_in_one_prompt} of ${c.total_destination_paths} paths`,
    },
    {
      ok: c.max_mappings_from_one_call < report.coverage.source_fields_mapped,
      label: "No single call produced the finished mapping",
      detail: `largest response held ${c.max_mappings_from_one_call} of ${report.coverage.source_fields_mapped} mappings`,
    },
  ];

  return html`<div class="split">
    <div>
      <h3 class="section">Constraint assertions</h3>
      ${checks.map(
        (check, i) => html`<div class=${`check ${check.ok ? "pass" : "fail"}`} key=${i}>
          <span class="mark">${check.ok ? "PASS" : "FAIL"}</span>
          <span>
            ${check.label}
            <div class="detail">${check.detail}</div>
          </span>
        </div>`
      )}
      <p class="note">
        Every request records a manifest of what it was allowed to see. These checks are computed
        from those manifests, and the test suite re-asserts them against the recorded prompt text.
      </p>
    </div>
    <div>
      <h3 class="section">Every prompt in this run</h3>
      <table class="grid">
        <thead>
          <tr><th>Stage</th><th>Detail</th><th>Tables</th><th>Fields</th><th>Paths</th><th>Tokens</th></tr>
        </thead>
        <tbody>
          ${c.prompts.map(
            (p, i) => html`<tr key=${i}>
              <td>${p.stage}</td>
              <td><span class="pill">${p.detail_level}</span></td>
              <td>${p.source_table_count}</td>
              <td>${p.source_field_count}</td>
              <td>${p.destination_path_count}</td>
              <td>${num(p.input_tokens)}</td>
            </tr>`
          )}
        </tbody>
      </table>
      <p class="note">
        The largest prompt is ${num(c.max_input_tokens_in_one_prompt)} input tokens, against
        ${num(c.both_schemas_counterfactual_tokens)} to paste both raw schemas. Decomposition here is
        about bounding what each decision depends on, not about making prompts smaller: a Stage 3
        prompt is larger precisely because each field carries its candidates with types, comments,
        and the conventions retrieved for it.
      </p>
    </div>
  </div>`;
}

function CostPanel({ report }) {
  if (!report?.cost?.by_stage) return html`<p class="note">Run the pipeline to see token spend.</p>`;
  const cost = report.cost;
  const stages = Object.entries(cost.by_stage);
  const whatIf = Object.entries(cost.what_if_single_model || {}).sort((a, b) => a[1] - b[1]);

  return html`<div class="split">
    <div>
      <h3 class="section">
        Spend by stage ${cost.billed === false ? html`<span class="badge offline">recorded, not billed</span>` : null}
      </h3>
      <table class="grid">
        <thead><tr><th>Stage</th><th>Calls</th><th>In</th><th>Out</th><th>USD</th></tr></thead>
        <tbody>
          ${stages.map(
            ([stage, s]) => html`<tr key=${stage}>
              <td>${stage}</td>
              <td>${s.calls}</td>
              <td>${num(s.input_tokens)}</td>
              <td>${num(s.output_tokens)}</td>
              <td>${usd(s.usd)}</td>
            </tr>`
          )}
          <tr>
            <td><strong>total</strong></td>
            <td><strong>${cost.billable_calls + cost.cache_hits}</strong></td>
            <td><strong>${num(cost.total_input_tokens)}</strong></td>
            <td><strong>${num(cost.total_output_tokens)}</strong></td>
            <td><strong>${usd(cost.total_usd)}</strong></td>
          </tr>
        </tbody>
      </table>
      <p class="note">
        ${usd(cost.cost_per_mapped_field)} per mapped field.
        ${cost.cache_hits} of ${cost.billable_calls + cost.cache_hits} calls were served from cache.
        Token counts are what Bedrock reported, never estimates.
      </p>
    </div>
    <div>
      <h3 class="section">What one model for everything would have cost</h3>
      <table class="grid">
        <thead><tr><th>Model</th><th>USD</th><th>vs this run</th></tr></thead>
        <tbody>
          ${whatIf.map(([model, amount]) => {
            const delta = amount - cost.total_usd;
            return html`<tr key=${model}>
              <td class="mono" style="font-size:10.5px">${model.replace(/^us\./, "")}</td>
              <td>${usd(amount)}</td>
              <td style=${{ color: delta > 0 ? "var(--review)" : "var(--high)" }}>
                ${delta >= 0 ? "+" : ""}${usd(delta)}
              </td>
            </tr>`;
          })}
        </tbody>
      </table>
      <p class="note">
        This run's measured token counts repriced against every registered model. The cascade sends
        the cheap model first and escalates only low-confidence fields, which is where the saving
        comes from.
      </p>
    </div>
  </div>`;
}

function QualityPanel({ report, mapping }) {
  if (!report) return html`<p class="note">Run the pipeline to see coverage and quality.</p>`;
  const coverage = report.coverage;
  const quality = report.quality;
  const diagnostics = report.diagnostics;
  const bands = quality.confidence_histogram;
  const total = coverage.source_fields_mapped || 1;

  return html`<div class="split">
    <div class="stack">
      <div>
        <h3 class="section">Coverage</h3>
        <dl class="kv">
          <dt>Source fields</dt>
          <dd>${coverage.source_fields_mapped} mapped, ${coverage.source_fields_unmapped} unmapped,
            ${coverage.accounted_source_fields}/${coverage.source_fields_total} accounted for</dd>
          <dt>Destination paths</dt>
          <dd>${coverage.destination_paths_targeted}/${coverage.destination_paths_total} targeted</dd>
          <dt>Second pass</dt>
          <dd>${pct(quality.escalation_rate)} escalated, ${quality.reflection_count} reflected</dd>
          <dt>Repairs</dt>
          <dd>${quality.repaired} repaired, ${quality.tie_broken} tie-broken</dd>
        </dl>
      </div>
      <div>
        <h3 class="section">Confidence distribution</h3>
        <table class="grid">
          <tbody>
            ${[["high", bands.high], ["medium", bands.medium], ["review", bands.review]].map(
              ([name, count]) => html`<tr key=${name}>
                <td style="width:70px">${name}</td>
                <td style="width:40px">${count}</td>
                <td>${html`<${Bar} value=${count / total} tone=${name} />`}</td>
              </tr>`
            )}
          </tbody>
        </table>
        <p class="note">Mean ${quality.mean_confidence}. Confidence blends the model's certainty
          with how decisively retrieval agreed, and is capped where a human must write the transform.</p>
      </div>
    </div>
    <div class="stack">
      <div>
        <h3 class="section">Validation</h3>
        <div class=${`check ${diagnostics.ok ? "pass" : "fail"}`}>
          <span class="mark">${diagnostics.ok ? "PASS" : "FAIL"}</span>
          <span>Contract, coverage, and destination paths
            <div class="detail">
              ${diagnostics.schema_violations.length} contract violations,
              ${diagnostics.coverage_errors.length} coverage errors,
              ${(diagnostics.unresolved_paths || []).length} invented paths surviving
            </div>
          </span>
        </div>
        ${(diagnostics.hallucinated_paths_caught || []).length > 0 &&
        html`<p class="note">Caught and repaired: ${diagnostics.hallucinated_paths_caught.join(", ")}</p>`}
        ${(diagnostics.notes_corrections || []).length > 0 &&
        html`<div>
          <h3 class="section" style="margin-top:10px">Notes corrected</h3>
          ${diagnostics.notes_corrections.map(
            (correction, i) => html`<p class="note" key=${i}>
              <code>${correction.source_field}</code> claimed
              “${correction.rejected}” which is not how that transform works, so it was
              replaced.
            </p>`
          )}
        </div>`}
        ${diagnostics.coverage_errors.map((e, i) => html`<div class="error-box" key=${i}>${e}</div>`)}
      </div>
      <div>
        <h3 class="section">Unmapped, with reasons</h3>
        ${Object.entries(coverage.unmapped_source_explanations || {}).map(
          ([field, why]) => html`<p class="note" key=${field}>
            <code>${field}</code> — ${why}
          </p>`
        )}
        ${mapping &&
        html`<p class="note" style="margin-top:8px">
          ${Object.keys(coverage.unmapped_destination_explanations || {}).length} destination paths
          have no source column; most are denormalized copies populated by a join at migration time.
        </p>`}
      </div>
    </div>
  </div>`;
}

function TimelinePanel({ report, log }) {
  const stages = report?.stages || [];
  const longest = Math.max(1, ...stages.map((s) => s.duration_ms));
  return html`<div class="split">
    <div>
      <h3 class="section">Stage timings</h3>
      ${stages.length
        ? html`<table class="grid">
            <thead><tr><th>Stage</th><th>ms</th><th></th></tr></thead>
            <tbody>
              ${stages.map(
                (s, i) => html`<tr key=${i}>
                  <td>${s.stage}</td>
                  <td>${num(s.duration_ms)}</td>
                  <td>${html`<${Bar} value=${s.duration_ms / longest} />`}</td>
                </tr>`
              )}
            </tbody>
          </table>`
        : html`<p class="note">No stage timings yet.</p>`}
    </div>
    <div>
      <h3 class="section">Event log</h3>
      <div class="log">
        ${log.length
          ? log.map(
              (entry, i) => html`<div class="row" key=${i}>
                <span class="t">${entry.at}</span>
                <span class=${`m ${entry.tone || ""}`}>${entry.message}</span>
              </div>`
            )
          : html`<p class="note">Nothing logged yet.</p>`}
      </div>
    </div>
  </div>`;
}

function JsonPanel({ mapping, runId }) {
  const [copied, setCopied] = useState(false);
  if (!mapping) return html`<p class="note">No mapping document yet.</p>`;
  const text = JSON.stringify(mapping, null, 2);
  return html`<div>
    <div class="row-between" style="margin-bottom:8px">
      <h3 class="section" style="margin:0">
        The deliverable — ${mapping.tables.length} tables,
        ${mapping.tables.reduce((n, t) => n + t.field_mappings.length, 0)} field mappings
      </h3>
      <div style="display:flex;gap:6px">
        <button
          onClick=${async () => {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1600);
          }}
        >${copied ? "Copied" : "Copy JSON"}</button>
        ${runId &&
        html`<a href=${`/api/runs/${runId}/mapping.json`} download>
          <button>Download</button>
        </a>`}
      </div>
    </div>
    <pre class="json">${text}</pre>
  </div>`;
}

const FORMAT_LABEL = {
  mysql_ddl: "MySQL CREATE TABLE",
  mysql_json: "MySQL schema JSON",
  mongo_json: "MongoDB schema JSON",
  mongo_documents: "MongoDB sample documents",
};

/**
 * One side of the input panel: paste, drop a file, pick a file, or load a
 * bundled sample. Parse feedback comes from POST /api/parse, which is free, so
 * a format mistake is caught here instead of by a failed paid run.
 */
function SchemaInput({ side, title, text, onText, samples, onSample, status }) {
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef(null);

  const readFile = useCallback(
    (file) => {
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => onText(String(reader.result), file.name);
      reader.readAsText(file);
    },
    [onText]
  );

  const mine = samples.filter((s) => s.kind === side);

  return html`<div
    class=${`schema-input ${dragging ? "dragging" : ""}`}
    onDragOver=${(e) => {
      e.preventDefault();
      setDragging(true);
    }}
    onDragLeave=${() => setDragging(false)}
    onDrop=${(e) => {
      e.preventDefault();
      setDragging(false);
      readFile(e.dataTransfer.files?.[0]);
    }}
  >
    <div class="row-between" style="margin-bottom:6px">
      <h3 class="section" style="margin:0">${title}</h3>
      <div class="input-actions">
        <select value="" onChange=${(e) => e.target.value && onSample(e.target.value, side)}>
          <option value="">Load sample…</option>
          ${mine.map((s) => html`<option key=${s.name} value=${s.name}>${s.name}</option>`)}
        </select>
        <button onClick=${() => fileRef.current?.click()}>Upload file</button>
        <input
          ref=${fileRef}
          type="file"
          accept=".json,.sql,.txt"
          style="display:none"
          onChange=${(e) => {
            readFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
      </div>
    </div>
    <textarea
      class="schema"
      spellcheck="false"
      placeholder=${`Paste ${side === "source" ? "MySQL DDL or MySQL schema JSON" : "MongoDB schema JSON or sample documents"}, drop a file here, or load a sample.`}
      value=${text}
      onInput=${(e) => onText(e.target.value)}
    ></textarea>
    <div class=${`parse-status ${status?.ok === false ? "bad" : status?.database ? "ok" : ""}`}>
      ${status?.ok === false
        ? html`<span>✕ ${status.error}</span>`
        : status?.database
        ? html`<span>
            ✓ ${FORMAT_LABEL[status.format] || status.format} · <code>${status.database}</code> ·
            ${Object.keys(status.containers || {}).length}
            ${side === "source" ? " tables" : " collections"} ·
            ${status.fields} ${side === "source" ? "columns" : "paths"}
          </span>`
        : html`<span>Using the bundled ${side} schema.</span>`}
    </div>
  </div>`;
}

function SchemaPanel({ sourceText, destText, onSource, onDest, onReset, samples, onSample, parse }) {
  return html`<div class="stack">
    <p class="note" style="margin:0">
      This is the pipeline's input. Anything you paste, drop, or upload here is what the next run
      maps — formats are detected, so MySQL DDL, MySQL schema JSON, MongoDB schema JSON, and MongoDB
      sample documents all work.
      <button style="margin-left:8px" onClick=${onReset}>Reset to bundled schemas</button>
    </p>
    <div class="split">
      ${html`<${SchemaInput}
        side="source"
        title="Source schema (MySQL)"
        text=${sourceText}
        onText=${onSource}
        samples=${samples}
        onSample=${onSample}
        status=${parse?.source}
      />`}
      ${html`<${SchemaInput}
        side="destination"
        title="Destination schema (MongoDB)"
        text=${destText}
        onText=${onDest}
        samples=${samples}
        onSample=${onSample}
        status=${parse?.destination}
      />`}
    </div>
  </div>`;
}

/* -------------------------------------------------------------- main app */

function App() {
  const [health, setHealth] = useState(null);
  const [models, setModels] = useState([]);
  const [samples, setSamples] = useState([]);
  const [sourceText, setSourceText] = useState("");
  const [destText, setDestText] = useState("");
  const [parse, setParse] = useState(null);
  const [edited, setEdited] = useState(false);

  const [routerModel, setRouterModel] = useState("");
  const [mapperModel, setMapperModel] = useState("");
  const [cheapModel, setCheapModel] = useState("");
  const [cascade, setCascade] = useState(true);
  const [reflection, setReflection] = useState(true);
  const [offline, setOffline] = useState(false);

  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [log, setLog] = useState([]);
  const [schemaInfo, setSchemaInfo] = useState(null);
  const [liveWires, setLiveWires] = useState([]);
  const [mapping, setMapping] = useState(null);
  const [report, setReport] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [runId, setRunId] = useState(null);
  const [routing, setRouting] = useState({});

  const [activeTable, setActiveTable] = useState(null);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState("decision");
  const [collapsed, setCollapsed] = useState(false);
  const [rows, setRows] = useState([]);
  const [segments, setSegments] = useState([]);

  const stageRef = useRef(null);
  const canvasRef = useRef(null);
  const leftRef = useRef(null);
  const rightRef = useRef(null);
  const srcNodes = useRef(new Map());
  const dstNodes = useRef(new Map());
  const frame = useRef(null);
  const syncingScroll = useRef(false);

  const note = useCallback((message, tone) => {
    setLog((prev) => [...prev.slice(-160), { at: clock(), message, tone }]);
  }, []);

  /* initial load */
  useEffect(() => {
    (async () => {
      try {
        const [healthData, modelData, schemaData] = await Promise.all([
          getJSON("/api/health"),
          getJSON("/api/models"),
          getJSON("/api/schemas"),
        ]);
        setHealth(healthData);
        setModels(modelData.models);
        setSamples(schemaData.samples);
        setSourceText(schemaData.default_source);
        setDestText(schemaData.default_destination);
        setRouterModel(healthData.defaults.router_model);
        setMapperModel(healthData.defaults.mapper_model);
        setCheapModel(healthData.defaults.cheap_mapper_model);
        note("Ready. Bundled legacy_hrm and people_platform schemas loaded.", "ok");

        const artifact = await getJSON("/api/latest_artifact");
        if (artifact.mapping) {
          applyResult(artifact.mapping, artifact.report, [], null);
          note(`Loaded the committed artifact from ${artifact.source}.`, "ok");
        }
      } catch (err) {
        setError(String(err));
      }
      try {
        const rowData = await getJSON("/api/sample_rows?table=emp_master");
        setRows(rowData.rows || []);
      } catch {
        /* sample rows are a nicety, not a requirement */
      }
    })();
  }, []);

  const applyResult = useCallback((mappingDoc, reportDoc, decisionList, id) => {
    setMapping(mappingDoc);
    setReport(reportDoc);
    setDecisions(decisionList || []);
    setRunId(id);
    const pairs = {};
    const wires = [];
    for (const table of mappingDoc.tables) {
      pairs[table.source_table] = table.destination_collection;
      for (const m of table.field_mappings) {
        wires.push({
          table: table.source_table,
          source_field: m.source_field,
          destination_field: m.destination_field,
          confidence: m.confidence,
          type_transform: m.type_transform,
        });
      }
    }
    setRouting(pairs);
    setLiveWires(wires);
    setActiveTable((current) => current || mappingDoc.tables[0]?.source_table || null);
  }, []);

  const start = useCallback(async () => {
    setRunning(true);
    setError(null);
    setLiveWires([]);
    setMapping(null);
    setReport(null);
    setDecisions([]);
    setSelected(null);
    setLog([]);
    note(offline ? "Replaying recorded cassettes." : "Starting live run against Bedrock.");

    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_text: sourceText,
          destination_text: destText,
          router_model: routerModel,
          mapper_model: mapperModel,
          cheap_mapper_model: cheapModel,
          enable_cascade: cascade,
          enable_reflection: reflection,
          offline,
        }),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `HTTP ${response.status}`);
      }

      for await (const { event, data } of sseStream(response)) {
        if (event === "hello") {
          setSchemaInfo(data);
          setActiveTable(Object.keys(data.source.tables)[0]);
          note(
            `${data.source.database}: ${data.source.fields} fields into ` +
              `${data.destination.database}: ${data.destination.leaf_paths} paths ` +
              `(${data.models.mapper}).`
          );
        } else if (event === "stage_start") {
          note(`stage ${data.stage}: ${data.label}`);
        } else if (event === "stage_end") {
          note(`stage ${data.stage} finished in ${data.duration_ms}ms`);
        } else if (event === "route") {
          setRouting((prev) => ({ ...prev, [data.table]: data.collection }));
          note(`${data.table} -> ${data.collection} (${data.confidence.toFixed(2)})`, "ok");
        } else if (event === "batch") {
          note(`batch ${data.batch}/${data.of} of ${data.table}: ${data.fields.join(", ")}`);
        } else if (event === "escalate") {
          note(`escalating to the strong model: ${data.fields.join(", ")}`, "warn");
        } else if (event === "reflect") {
          note(`reviewed ${data.source_field}`, "warn");
        } else if (event === "mapping") {
          if (data.destination_field) {
            setLiveWires((prev) => [
              ...prev.filter((w) => w.source_field !== data.source_field),
              {
                table: data.table,
                source_field: data.source_field,
                destination_field: data.destination_field,
                confidence: data.confidence,
              },
            ]);
          } else {
            note(`${data.source_field}: no destination is a genuine match`, "warn");
          }
        } else if (event === "result") {
          applyResult(data.mapping, data.report, data.decisions, data.run_id);
          note(
            `Done: ${data.report.coverage.source_fields_mapped}/` +
              `${data.report.coverage.source_fields_total} mapped, ` +
              `${usd(data.report.cost.total_usd)}, ` +
              `${data.report.diagnostics.ok ? "validation passed" : "VALIDATION FAILED"}.`,
            data.report.diagnostics.ok ? "ok" : "err"
          );
        } else if (event === "error") {
          setError(data.message);
          note(data.message, "err");
        }
      }
    } catch (err) {
      setError(String(err.message || err));
      note(String(err.message || err), "err");
    } finally {
      setRunning(false);
    }
  }, [sourceText, destText, routerModel, mapperModel, cheapModel, cascade, reflection, offline]);

  const loadSample = useCallback(
    async (name, kind) => {
      try {
        const data = await getJSON(`/api/samples/${name}`);
        if (kind === "source") setSourceText(data.text);
        else setDestText(data.text);
        note(`Loaded ${name} as the ${kind} schema.`);
      } catch (err) {
        setError(String(err));
      }
    },
    [note]
  );

  const resetSchemas = useCallback(async () => {
    const data = await getJSON("/api/schemas");
    setSourceText(data.default_source);
    setDestText(data.default_destination);
    setEdited(false);
    note("Restored the bundled schemas.");
  }, [note]);

  const setSource = useCallback(
    (text, filename) => {
      setSourceText(text);
      setEdited(true);
      if (filename) note(`Loaded ${filename} as the source schema.`);
    },
    [note]
  );

  const setDest = useCallback(
    (text, filename) => {
      setDestText(text);
      setEdited(true);
      if (filename) note(`Loaded ${filename} as the destination schema.`);
    },
    [note]
  );

  const openInput = useCallback(() => {
    setTab("input");
    setCollapsed(false);
  }, []);

  /* Validate whatever is in the two editors, debounced. Free endpoint, so the
     user learns their paste is understood before spending a run on it. */
  useEffect(() => {
    if (!sourceText && !destText) return;
    const timer = setTimeout(async () => {
      try {
        const response = await fetch("/api/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_text: sourceText, destination_text: destText }),
        });
        setParse(await response.json());
      } catch {
        /* validation is advisory; the run itself reports parse errors too */
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [sourceText, destText]);

  /* derived view data */
  const tables = useMemo(() => {
    if (schemaInfo) return Object.keys(schemaInfo.source.tables);
    if (mapping) return mapping.tables.map((t) => t.source_table);
    return [];
  }, [schemaInfo, mapping]);

  const collection = routing[activeTable];

  const wires = useMemo(
    () => liveWires.filter((w) => !w.table || w.table === activeTable),
    [liveWires, activeTable]
  );

  const sourceFields = useMemo(() => {
    let fields = [];
    if (!activeTable) return [];
    if (schemaInfo?.source.tables[activeTable]) {
      fields = [...schemaInfo.source.tables[activeTable]];
    } else {
      const table = mapping?.tables.find((t) => t.source_table === activeTable);
      if (!table) return [];
      fields = [...table.field_mappings.map((m) => m.source_field), ...table.unmapped_source_fields];
    }
    if (!wires.length) return fields;
    const mapped = new Set(
      wires.filter((w) => w.destination_field && w.destination_field !== "null").map((w) => w.source_field)
    );
    return [...fields.filter((f) => mapped.has(f)), ...fields.filter((f) => !mapped.has(f))];
  }, [activeTable, schemaInfo, mapping, wires]);

  /* Dest paths follow source-field order so wires run nearly parallel instead
     of crossing into spaghetti. Unmapped destination paths come after. */
  const destPaths = useMemo(() => {
    let all = [];
    if (schemaInfo?.destination.collections[collection]) {
      all = [...schemaInfo.destination.collections[collection]];
    } else {
      const table = mapping?.tables.find((t) => t.source_table === activeTable);
      if (!table) return [];
      all = [
        ...table.field_mappings.map((m) => m.destination_field),
        ...table.unmapped_destination_fields,
      ];
    }
    const bySource = new Map(
      wires
        .filter((w) => w.destination_field && w.destination_field !== "null")
        .map((w) => [w.source_field, w.destination_field])
    );
    const preferred = [];
    const seen = new Set();
    for (const name of sourceFields) {
      const path = bySource.get(name);
      if (path && all.includes(path) && !seen.has(path)) {
        preferred.push(path);
        seen.add(path);
      }
    }
    for (const path of all) {
      if (!seen.has(path)) preferred.push(path);
    }
    return preferred;
  }, [collection, schemaInfo, mapping, activeTable, wires, sourceFields]);

  const activeMapping = useMemo(() => {
    if (!mapping || !selected) return null;
    const table = mapping.tables.find((t) => t.source_table === activeTable);
    return table?.field_mappings.find((m) => m.source_field === selected) || null;
  }, [mapping, selected, activeTable]);

  const activeDecision = useMemo(
    () => decisions.find((d) => d.source_field === `${activeTable}.${selected}`) || null,
    [decisions, activeTable, selected]
  );

  const wireFor = useCallback(
    (name) => wires.find((w) => w.source_field === name),
    [wires]
  );

  /* Wire geometry is measured from the rendered rows rather than recomputed from
     field counts, which keeps every wire anchored to its label through scrolling,
     resizing and the drawer opening. */
  const registerNode = useCallback((store, key, el) => {
    if (el) store.current.set(key, el);
    else store.current.delete(key);
  }, []);

  const measure = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const box = canvas.getBoundingClientRect();
    const topClip = box.top + 4;
    const bottomClip = box.bottom - 4;
    const visible = (clientY) => clientY >= topClip && clientY <= bottomClip;

    const next = [];
    for (const wire of wires) {
      if (!wire.destination_field || wire.destination_field === "null") continue;
      const from = srcNodes.current.get(wire.source_field);
      const to = dstNodes.current.get(wire.destination_field);
      if (!from || !to) continue;
      const a = from.getBoundingClientRect();
      const b = to.getBoundingClientRect();
      const y1 = a.top + a.height / 2;
      const y2 = b.top + b.height / 2;
      if (!visible(y1) || !visible(y2)) continue;
      next.push({
        key: wire.source_field,
        wire,
        x1: 0,
        y1: y1 - box.top,
        x2: box.width,
        y2: y2 - box.top,
      });
    }
    setSegments(next);
  }, [wires]);

  const remeasure = useCallback(() => {
    if (frame.current) cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(measure);
  }, [measure]);

  const onColumnScroll = useCallback(
    (side) => (event) => {
      const other = side === "left" ? rightRef.current : leftRef.current;
      if (!other) {
        remeasure();
        return;
      }
      if (!syncingScroll.current) {
        syncingScroll.current = true;
        other.scrollTop = event.currentTarget.scrollTop;
        requestAnimationFrame(() => {
          syncingScroll.current = false;
        });
      }
      remeasure();
    },
    [remeasure]
  );

  useEffect(() => {
    remeasure();
  }, [remeasure, sourceFields, destPaths, selected, collapsed, tab, activeTable]);

  useEffect(() => {
    const targets = [stageRef.current, canvasRef.current].filter(Boolean);
    if (!targets.length) return;
    const observer = new ResizeObserver(remeasure);
    for (const el of targets) observer.observe(el);
    window.addEventListener("resize", remeasure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", remeasure);
      if (frame.current) cancelAnimationFrame(frame.current);
    };
  }, [remeasure]);

  const totalFields = schemaInfo?.source.fields || report?.coverage.source_fields_total || 0;
  const progress = totalFields ? Math.min(1, liveWires.length / totalFields) : 0;

  return html`<div class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-title">
          <h1>Schema Field Mapper</h1>
          <p class="sub">MySQL → MongoDB, field by field</p>
        </div>
        <div class="badges">
          ${report && html`<span class=${`badge ${report.mode === "live" ? "live" : "offline"}`}>${report.mode}</span>`}
          ${report &&
          html`<span class=${`badge ${report.diagnostics.ok ? "ok" : "fail"}`}>
            ${report.diagnostics.ok ? "valid" : "invalid"}
          </span>`}
        </div>
      </div>

      <div class="controls">
        <div class="input-row">
          <button class="input-chip" onClick=${openInput} title="Paste, upload, or load sample schemas">
            <span class="chip-k">Input</span>
            <span class="chip-v">
              ${parse?.source?.database || "legacy_hrm"} → ${parse?.destination?.database || "people_platform"}
            </span>
            <span class="chip-sub">
              ${parse?.source?.fields ?? schemaInfo?.source.fields ?? "34"} cols ·
              ${parse?.destination?.fields ?? schemaInfo?.destination.leaf_paths ?? "40"} paths ·
              ${edited ? "edited" : "bundled"}
            </span>
          </button>
          ${parse && parse.ok === false && html`<span class="badge fail">input error</span>`}
        </div>
        <div class="model-row">
          <label class="field">
            <span>Router</span>
            <select value=${routerModel} onChange=${(e) => setRouterModel(e.target.value)}>
              ${models.map((m) => html`<option key=${m.id} value=${m.id}>${m.label}</option>`)}
            </select>
          </label>
          <label class="field">
            <span>Mapper</span>
            <select value=${mapperModel} onChange=${(e) => setMapperModel(e.target.value)}>
              ${models.map((m) => html`<option key=${m.id} value=${m.id}>${m.label}</option>`)}
            </select>
          </label>
          <label class="field">
            <span>Cheap pass</span>
            <select value=${cheapModel} onChange=${(e) => setCheapModel(e.target.value)}>
              ${models.map((m) => html`<option key=${m.id} value=${m.id}>${m.label}</option>`)}
            </select>
          </label>
        </div>
        <div class="toggle-row">
          <label class="toggle" title="Cheap model first, strong model only for low-confidence fields">
            <input type="checkbox" checked=${cascade} onChange=${(e) => setCascade(e.target.checked)} />
            cascade
          </label>
          <label class="toggle" title="Re-examine the weakest decisions">
            <input type="checkbox" checked=${reflection} onChange=${(e) => setReflection(e.target.checked)} />
            reflect
          </label>
          <label
            class="toggle"
            title=${health?.offline_available
              ? `Replay ${health.cassette_count} recorded exchanges, no AWS needed`
              : "No cassettes are bundled in this build"}
          >
            <input
              type="checkbox"
              checked=${offline}
              disabled=${!health?.offline_available}
              onChange=${(e) => setOffline(e.target.checked)}
            />
            offline
          </label>
          <button class="primary" onClick=${start} disabled=${running}>
            ${running ? "Running…" : "Run pipeline"}
          </button>
        </div>
      </div>
    </header>

    <div class="meterbar">
      <div class="meters">
        ${html`<${Meter}
          label="Coverage"
          value=${`${report?.coverage.source_fields_mapped ?? liveWires.length}/${totalFields || "—"}`}
          sub="mapped"
          tone=${report && report.coverage.accounted_source_fields === report.coverage.source_fields_total ? "good" : ""}
        />`}
        ${html`<${Meter}
          label="Confidence"
          value=${report?.quality.mean_confidence ?? "—"}
          sub="mean"
          tone=${report ? (report.quality.mean_confidence >= 0.85 ? "good" : "warn") : ""}
        />`}
        ${html`<${Meter}
          label="Cost"
          value=${report ? usd(report.cost.total_usd) : "—"}
          sub=${report?.cost.billed === false ? "recorded" : "this run"}
        />`}
        ${html`<${Meter} label="LLM calls" value=${report?.constraint.total_llm_calls ?? "—"} />`}
        ${html`<${Meter}
          label="Max / prompt"
          value=${report ? `${report.constraint.max_typed_source_fields_in_one_prompt}/${report.constraint.total_source_fields}` : "—"}
          sub="fields"
          tone=${report && report.constraint.max_typed_source_fields_in_one_prompt < report.constraint.total_source_fields ? "good" : ""}
        />`}
        ${html`<${Meter}
          label="Both schemas"
          value=${report?.constraint.prompts_containing_both_full_schemas ?? "—"}
          sub="in one prompt"
          tone=${report && report.constraint.prompts_containing_both_full_schemas === 0 ? "good" : "bad"}
        />`}
        ${html`<${Meter}
          label="Unmapped"
          value=${report ? report.coverage.source_fields_unmapped : "—"}
          sub="declared"
        />`}
        ${html`<${Meter} label="Duration" value=${report ? `${(report.duration_ms / 1000).toFixed(1)}s` : "—"} />`}
      </div>
      <div class=${`progress-track ${running ? "busy" : ""}`}>
        <div class="progress-fill" style=${{ width: `${progress * 100}%` }}></div>
      </div>
    </div>

    <div class="stage" ref=${stageRef}>
      <div class="column left" ref=${leftRef} onScroll=${onColumnScroll("left")}>
        <h2>
          <span>Source</span>
          <span class="who">${schemaInfo?.source.database || mapping?.source || ""}</span>
        </h2>
        ${activeTable &&
        html`<div>
          <div class="group-name" style="color:var(--source)">${activeTable}</div>
          ${sourceFields.map((name) => {
            const wire = wireFor(name);
            const cls = wire ? band(wire.confidence) : running ? "pending" : "unmapped";
            return html`<div
              key=${name}
              ref=${(el) => registerNode(srcNodes, name, el)}
              class=${`node ${selected === name ? "selected" : ""}`}
              title=${name}
              onClick=${() => {
                setSelected(name);
                setTab("decision");
              }}
            >
              <span class="label">${name}</span>
              <span class=${`dot ${cls}`}></span>
            </div>`;
          })}
        </div>`}
      </div>

      <div class="middle">
        <div class="tabs-tables">
          ${tables.map(
            (table) => html`<button
              key=${table}
              class=${table === activeTable ? "on" : ""}
              onClick=${() => {
                setActiveTable(table);
                setSelected(null);
              }}
            >${table} → ${routing[table] || "?"}</button>`
          )}
        </div>
        <div class="canvas" ref=${canvasRef}>
          <div class="canvas-title">
            <span>Field mappings</span>
            <span class="canvas-count">${wires.filter((w) => w.destination_field && w.destination_field !== "null").length} wires</span>
          </div>
          ${html`<${WireLayer}
            segments=${segments}
            selected=${selected}
            onSelect=${(name) => {
              setSelected(name);
              setTab("decision");
            }}
          />`}
          ${!wires.length &&
          html`<div class="empty">
            <div>
              <div>
                No mappings yet for
                <span class="mono">${activeTable || "?"} → ${collection || "?"}</span>
              </div>
              <div style="margin-top:6px">
                Press <strong>Run pipeline</strong>, or load the committed artifact.
              </div>
            </div>
          </div>`}
          ${!!wires.length &&
          html`<div class="legend">
            <span class="high"><i></i>high ≥ 0.90</span>
            <span class="medium"><i></i>medium ≥ 0.80</span>
            <span class="review"><i></i>review ${"<"} 0.80</span>
            <span class="none"><i style="background:transparent;border:1px solid var(--review)"></i>unmapped</span>
          </div>`}
        </div>
      </div>

      <div class="column right" ref=${rightRef} onScroll=${onColumnScroll("right")}>
        <h2>
          <span>Destination</span>
          <span class="who">${schemaInfo?.destination.database || mapping?.destination || ""}</span>
        </h2>
        <div class="group-name" style="color:var(--dest)">${collection || "—"}</div>
        ${destPaths.map((path) => {
          const wire = wires.find((w) => w.destination_field === path);
          return html`<div
            key=${path}
            ref=${(el) => registerNode(dstNodes, path, el)}
            class=${`node ${wire && selected === wire.source_field ? "selected" : ""} ${wire ? "" : "dim"}`}
            title=${path}
            onClick=${() => wire && setSelected(wire.source_field)}
          >
            <span class=${`dot ${wire ? band(wire.confidence) : "unmapped"}`}></span>
            <span class="label">${path}</span>
          </div>`;
        })}
      </div>
    </div>

    <div class=${`drawer ${collapsed ? "collapsed" : ""}`}>
      <div class="tabs">
        ${[
          ["input", "Input data"],
          ["decision", "Decision"],
          ["constraint", "Constraint proof"],
          ["quality", "Coverage & quality"],
          ["cost", "Cost"],
          ["timeline", "Timeline"],
          ["json", "Mapping JSON"],
        ].map(
          ([key, label]) => html`<button
            key=${key}
            class=${tab === key ? "on" : ""}
            onClick=${() => {
              setTab(key);
              setCollapsed(false);
            }}
          >${label}</button>`
        )}
        <span class="spacer"></span>
        <button onClick=${() => setCollapsed(!collapsed)}>${collapsed ? "Expand" : "Collapse"}</button>
      </div>
      ${!collapsed &&
      html`<div class="drawer-body">
        ${error && html`<div class="error-box"><strong>Run failed.</strong> ${error}</div>`}
        ${tab === "decision" &&
        html`<${DecisionPanel}
          decision=${activeDecision}
          mapping=${activeMapping}
          rows=${rows}
          table=${activeTable}
          collection=${collection}
        />`}
        ${tab === "constraint" && html`<${ConstraintPanel} report=${report} />`}
        ${tab === "quality" && html`<${QualityPanel} report=${report} mapping=${mapping} />`}
        ${tab === "cost" && html`<${CostPanel} report=${report} />`}
        ${tab === "timeline" && html`<${TimelinePanel} report=${report} log=${log} />`}
        ${tab === "json" && html`<${JsonPanel} mapping=${mapping} runId=${runId} />`}
        ${tab === "input" &&
        html`<${SchemaPanel}
          sourceText=${sourceText}
          destText=${destText}
          onSource=${setSource}
          onDest=${setDest}
          onReset=${resetSchemas}
          samples=${samples}
          onSample=${loadSample}
          parse=${parse}
        />`}
      </div>`}
    </div>
  </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
