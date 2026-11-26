import React, { useEffect, useState, useCallback } from 'react';
import { API_BASE_URL, authFetch, getApiError } from './api';

const n0 = (v) => Number(v || 0).toLocaleString();
const bdt = (v, d = 2) => `৳${Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
const usd = (v, d = 4) => `$${Number(v || 0).toFixed(d)}`;
const when = (s) => (s ? String(s).replace('T', ' ').slice(0, 19) : '-');
const card = { minWidth: 0 };
const small = { color: 'var(--text-muted)', fontSize: '0.85rem' };

function Kpi({ label, value, sub, color }) {
  return (
    <div className="glass-card" style={card}>
      <h3 style={{ color: 'var(--text-muted)', marginBottom: 8, fontSize: '0.9rem' }}>{label}</h3>
      <div style={{ fontSize: '2rem', fontWeight: 'bold', color }}>{value}</div>
      {sub && <div style={small}>{sub}</div>}
    </div>
  );
}

function StatusBadge({ status }) {
  const color = status === 'finished' ? 'var(--success)' : status === 'failed' || status === 'lost' ? 'var(--danger)' : 'var(--text-muted)';
  return <span className="badge" style={{ color }}>{status}</span>;
}

export default function BillingView() {
  const [days, setDays] = useState(30);
  const [includeTest, setIncludeTest] = useState(true);
  const [summary, setSummary] = useState(null);
  const [daily, setDaily] = useState([]);
  const [calls, setCalls] = useState([]);
  const [prices, setPrices] = useState([]);
  const [fx, setFx] = useState('');
  const [open, setOpen] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');
  const [msg, setMsg] = useState('');
  const [loading, setLoading] = useState(true);
  const [newPrice, setNewPrice] = useState({ model: '', modality: 'text', direction: 'in', usd_per_million: '', valid_from: '' });

  const qs = `days=${days}&include_test=${includeTest}`;

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const get = async (path) => {
        const r = await authFetch(`${API_BASE_URL}/admin/billing/${path}`);
        if (!r.ok) throw new Error(await getApiError(r));
        return r.json();
      };
      const [s, d, c, p] = await Promise.all([get(`summary?${qs}`), get(`daily?${qs}`), get(`calls?${qs}&limit=200`), get('prices')]);
      setSummary(s);
      setDaily(d);
      setCalls(c.calls);
      setPrices(p);
      setFx(String(s.usd_to_bdt));
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [qs]);

  useEffect(() => { load(); }, [load]);

  const saveFx = async () => {
    const r = await authFetch(`${API_BASE_URL}/admin/settings`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: 'usd_to_bdt', value: fx }),
    });
    if (!r.ok) { setMsg(`Rate not saved: ${await getApiError(r)}`); return; }
    setMsg('Exchange rate saved.');
    load();
  };

  const exportCsv = async () => {
    const r = await authFetch(`${API_BASE_URL}/admin/billing/calls.csv?${qs}`);
    if (!r.ok) { setMsg(`Export failed: ${await getApiError(r)}`); return; }
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `calls-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const toggle = async (id) => {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    setDetail(null);
    const r = await authFetch(`${API_BASE_URL}/admin/billing/calls/${id}`);
    if (r.ok) setDetail(await r.json());
  };

  const addPrice = async (e) => {
    e.preventDefault();
    const r = await authFetch(`${API_BASE_URL}/admin/billing/prices`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...newPrice, usd_per_million: Number(newPrice.usd_per_million) }),
    });
    setMsg(r.ok ? 'Price saved. Run "Recompute costs" to apply it to past usage.' : `Price not saved: ${await getApiError(r)}`);
    if (r.ok) load();
  };

  const recompute = async () => {
    if (!window.confirm('Re-price ALL stored usage with the current price table? Costs of past usage will change.')) return;
    const r = await authFetch(`${API_BASE_URL}/admin/billing/recompute`, { method: 'POST' });
    setMsg(r.ok ? `Recomputed ${(await r.json()).events} events.` : `Recompute failed: ${await getApiError(r)}`);
    load();
  };

  const s = summary;
  const maxCost = Math.max(0.000001, ...daily.map((d) => d.cost_bdt));

  return (
    <div className="fade-in">
      <div className="page-header">
        <h1 className="page-title">Billing & Cost</h1>
        <p className="page-subtitle">Every Gemini token and every call minute, priced from the dated price table</p>
      </div>

      <div className="glass-card" style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center', marginBottom: 16 }}>
        <label>Period&nbsp;
          <select className="input-field" value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ width: 'auto', display: 'inline-block' }}>
            {[1, 7, 30, 90, 365].map((d) => <option key={d} value={d}>{d === 1 ? 'Today (24h)' : `Last ${d} days`}</option>)}
          </select>
        </label>
        <label><input type="checkbox" checked={includeTest} onChange={(e) => setIncludeTest(e.target.checked)} /> Include test traffic</label>
        <label>USD→BDT&nbsp;
          <input className="input-field" value={fx} onChange={(e) => setFx(e.target.value)} style={{ width: 90, display: 'inline-block' }} />
        </label>
        <button className="btn btn-outline" onClick={saveFx}>Save rate</button>
        <button className="btn btn-primary" onClick={load}>{loading ? 'Loading...' : 'Refresh'}</button>
        <button className="btn btn-outline" onClick={exportCsv}>Export calls CSV</button>
      </div>

      {error && <div className="glass-card" style={{ color: 'var(--danger)', marginBottom: 16 }}>{error}</div>}
      {msg && <div className="glass-card" style={{ marginBottom: 16 }}>{msg}</div>}
      {s && includeTest && (
        <div className="glass-card" style={{ marginBottom: 16, color: 'var(--text-muted)' }}>
          Test traffic is included in these totals (test numbers and test sessions are marked <b>test</b>). Untick the box to see billable usage only.
        </div>
      )}
      {s && s.alerts.length > 0 && (
        <div className="glass-card" style={{ marginBottom: 16, borderLeft: '4px solid var(--secondary-color)' }}>
          <b>Check these</b>
          <ul style={{ margin: '8px 0 0 18px' }}>{s.alerts.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}

      {s && (
        <div className="dashboard-grid">
          <Kpi label="Calls" value={n0(s.calls.by_status.finished || 0)}
               sub={`${n0(s.calls.total)} placed · ${n0(s.calls.by_status.failed || 0)} not answered · ${n0(s.calls.by_status.lost || 0)} lost`} />
          <Kpi label="Billable minutes" value={s.calls.billable_minutes.toFixed(2)}
               sub={`${n0(s.calls.billable_minutes_rounded_up)} min if each call is rounded up · source: Asterisk, else our timer`} />
          <Kpi label="Gemini cost" value={bdt(s.cost.gemini_bdt)} color="var(--success)" sub={`${usd(s.cost.gemini_usd)} at ৳${s.usd_to_bdt}/USD`} />
          <Kpi label="Phone cost per call-minute" value={s.cost.per_call_minute_bdt == null ? '-' : bdt(s.cost.per_call_minute_bdt, 3)}
               sub={s.cost.per_call_minute_usd == null ? '' : `${usd(s.cost.per_call_minute_usd)} · AI only, telecom not included`} />
          <Kpi label="Tokens in / out" value={`${n0(s.tokens.input_text + s.tokens.input_audio + s.tokens.input_image + s.tokens.input_cached)} / ${n0(s.tokens.output_text + s.tokens.output_audio)}`}
               sub={`audio in ${n0(s.tokens.input_audio)} · audio out ${n0(s.tokens.output_audio)}`} />
        </div>
      )}

      <div className="glass-card" style={{ marginTop: 24 }}>
        <h3 style={{ marginBottom: 12 }}>Cost by day</h3>
        <div className="table-container"><table>
          <thead><tr><th>Date</th><th>Calls</th><th>Minutes</th><th>Cost</th><th style={{ width: '40%' }}></th></tr></thead>
          <tbody>
            {daily.filter((d) => d.calls || d.cost_bdt).reverse().map((d) => (
              <tr key={d.date}>
                <td>{d.date}</td><td>{d.calls}</td><td>{d.minutes}</td><td>{bdt(d.cost_bdt)} <span style={small}>({usd(d.cost_usd)})</span></td>
                <td><div style={{ height: 10, borderRadius: 5, background: 'var(--accent-gradient)', width: `${Math.max(2, (d.cost_bdt / maxCost) * 100)}%` }} /></td>
              </tr>
            ))}
            {!daily.some((d) => d.calls || d.cost_bdt) && <tr><td colSpan={5} style={small}>No usage in this period.</td></tr>}
          </tbody>
        </table></div>
      </div>

      {s && (
        <div className="glass-card" style={{ marginTop: 24 }}>
          <h3 style={{ marginBottom: 12 }}>Where the cost comes from</h3>
          <div className="table-container"><table>
            <thead><tr><th>Channel</th><th>Purpose</th><th>Model</th><th>Events</th><th>In text</th><th>In audio</th><th>Out text</th><th>Out audio</th><th>Cost</th></tr></thead>
            <tbody>
              {s.breakdown.map((b, i) => (
                <tr key={i}>
                  <td>{b.channel}</td><td>{b.purpose}</td><td>{b.model}</td><td>{n0(b.events)}</td>
                  <td>{n0(b.input_text + b.input_cached)}</td><td>{n0(b.input_audio)}</td><td>{n0(b.output_text)}</td><td>{n0(b.output_audio)}</td>
                  <td>{bdt(b.cost_bdt, 3)} <span style={small}>({usd(b.cost_usd)})</span>
                    {b.estimated > 0 && <span className="badge" style={{ marginLeft: 6 }}>est.</span>}
                    {b.unpriced > 0 && <span className="badge" style={{ marginLeft: 6, color: 'var(--danger)' }}>unpriced</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>
      )}

      <div className="glass-card" style={{ marginTop: 24 }}>
        <h3 style={{ marginBottom: 12 }}>Calls</h3>
        <div className="table-container"><table>
          <thead><tr><th>Time (UTC)</th><th>Number</th><th>Status</th><th>Outcome</th><th>Our s</th><th>Asterisk s</th><th>Min (rounded up)</th><th>Tokens in/out</th><th>Cost</th><th>৳/min</th></tr></thead>
          <tbody>
            {calls.map((c) => (
              <React.Fragment key={c.call_id}>
                <tr onClick={() => toggle(c.call_id)} style={{ cursor: 'pointer' }}>
                  <td>{when(c.requested_at)}</td>
                  <td>{c.phone_number} {c.is_test ? <span className="badge">test</span> : null}</td>
                  <td><StatusBadge status={c.status} /></td>
                  <td>{c.outcome || '-'}</td>
                  <td>{Math.round(c.talk_seconds)}</td>
                  <td>{c.cdr_billsec == null ? '-' : c.cdr_billsec}</td>
                  <td>{c.billable_minutes_rounded_up}</td>
                  <td>{n0(c.tokens_in)} / {n0(c.tokens_out)}</td>
                  <td>{bdt(c.cost_bdt, 3)}{c.unpriced > 0 && <span className="badge" style={{ marginLeft: 6, color: 'var(--danger)' }}>unpriced</span>}</td>
                  <td>{c.cost_per_minute_bdt == null ? '-' : bdt(c.cost_per_minute_bdt, 2)}</td>
                </tr>
                {open === c.call_id && (
                  <tr><td colSpan={10} style={{ background: 'var(--bg-card-hover)' }}>
                    {!detail ? 'Loading...' : (
                      <div>
                        <div style={small}>Ended: {c.end_reason || '-'} · Conversation: {c.conversation_id} · {c.source === 'legacy' ? 'imported (modality split estimated)' : `${detail.events.length} Gemini turns`}</div>
                        {c.fail_detail && <div style={{ color: 'var(--danger)' }}>{c.fail_detail}</div>}
                        {c.summary && <div style={{ marginTop: 8, whiteSpace: 'pre-line' }}><b>Summary</b><br />{c.summary}</div>}
                        {(() => { let t = []; try { t = JSON.parse(c.todos || '[]'); } catch (e) { /* ignore */ }
                          return t.length > 0 && (
                            <div style={{ marginTop: 8 }}><b>To-do for customer service</b>
                              <ul style={{ margin: '4px 0 0 18px' }}>{t.map((x, i) => <li key={i}>[{x.priority}] {x.action}{x.due ? ` (by ${x.due})` : ''}</li>)}</ul>
                            </div>); })()}
                        {!c.summary && c.status === 'finished' && <div style={small}>No summary yet.</div>}
                        <table style={{ marginTop: 8 }}>
                          <thead><tr><th>#</th><th>Purpose</th><th>In text</th><th>In audio</th><th>Out text</th><th>Out audio</th><th>Cost</th></tr></thead>
                          <tbody>{detail.events.map((e, i) => (
                            <tr key={e.id}><td>{i + 1}</td><td>{e.purpose}</td><td>{n0(e.input_text)}</td><td>{n0(e.input_audio)}</td><td>{n0(e.output_text)}</td><td>{n0(e.output_audio)}</td>
                              <td>{e.cost_usd == null ? <span style={{ color: 'var(--danger)' }}>{e.price_note || 'unpriced'}</span> : `${bdt(e.cost_usd * detail.usd_to_bdt, 3)} (${usd(e.cost_usd, 5)})`}</td></tr>
                          ))}</tbody>
                        </table>
                      </div>
                    )}
                  </td></tr>
                )}
              </React.Fragment>
            ))}
            {calls.length === 0 && <tr><td colSpan={10} style={small}>No calls in this period.</td></tr>}
          </tbody>
        </table></div>
      </div>

      <div className="glass-card" style={{ marginTop: 24 }}>
        <h3 style={{ marginBottom: 4 }}>Price table (USD per 1M tokens)</h3>
        <p style={{ ...small, marginBottom: 12 }}>A price change is a new row with a new start date. Usage is priced with the row in force on the day it happened.</p>
        <div className="table-container"><table>
          <thead><tr><th>Model</th><th>Kind</th><th>Dir</th><th>$ / 1M</th><th>From</th><th>To</th><th>Source</th></tr></thead>
          <tbody>{prices.map((p) => (
            <tr key={p.id}><td>{p.model}</td><td>{p.modality}</td><td>{p.direction}</td><td>{Number(p.usd_per_million)}</td><td>{String(p.valid_from).slice(0, 10)}</td><td>{p.valid_to ? String(p.valid_to).slice(0, 10) : '-'}</td><td style={small}>{p.source}</td></tr>
          ))}</tbody>
        </table></div>
        <form onSubmit={addPrice} style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 16 }}>
          <input className="input-field" placeholder="model" value={newPrice.model} onChange={(e) => setNewPrice({ ...newPrice, model: e.target.value })} style={{ width: 220 }} />
          <select className="input-field" value={newPrice.modality} onChange={(e) => setNewPrice({ ...newPrice, modality: e.target.value })} style={{ width: 100 }}>
            {['text', 'audio', 'image', 'cached'].map((m) => <option key={m}>{m}</option>)}</select>
          <select className="input-field" value={newPrice.direction} onChange={(e) => setNewPrice({ ...newPrice, direction: e.target.value })} style={{ width: 80 }}>
            <option>in</option><option>out</option></select>
          <input className="input-field" placeholder="$ per 1M" value={newPrice.usd_per_million} onChange={(e) => setNewPrice({ ...newPrice, usd_per_million: e.target.value })} style={{ width: 110 }} />
          <input className="input-field" type="date" value={newPrice.valid_from} onChange={(e) => setNewPrice({ ...newPrice, valid_from: e.target.value })} style={{ width: 160 }} />
          <button className="btn btn-primary" type="submit" disabled={!newPrice.model || !newPrice.usd_per_million || !newPrice.valid_from}>Add price</button>
          <button className="btn btn-outline" type="button" onClick={recompute}>Recompute costs</button>
        </form>
      </div>
    </div>
  );
}
