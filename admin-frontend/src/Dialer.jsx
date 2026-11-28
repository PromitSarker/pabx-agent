import React, { useEffect, useState, useCallback } from 'react';
import { Plus, Trash2, PhoneCall } from 'lucide-react';
import { API_BASE_URL, authFetch, getApiError } from './api';

const BATCH_KEY = 'dialBatchId';
const small = { color: 'var(--text-muted)', fontSize: '0.85rem' };

// Same rule as the server (and the SIP bridge): returns the number as 0XXXXXXXXXX or null.
function normalize(raw) {
  let d = String(raw || '').replace(/[\s\-().]/g, '');
  if (!/^\+?\d{7,15}$/.test(d)) return null;
  d = d.replace(/^\+/, '');
  if (d.startsWith('00')) d = d.slice(2);
  let national;
  if (d.startsWith('880')) national = d.slice(3);
  else if (d.startsWith('0')) national = d.slice(1);
  else if (/^1[3-9]\d{8}$/.test(d)) national = d;
  else return null;
  return /^[1-9]\d{7,10}$/.test(national) ? `0${national}` : null;
}

let nextId = 1;
const newRow = (number = '') => ({ id: nextId++, number, name: '' });

const OUTCOMES = {
  interested: 'Interested', not_interested: 'Not interested', call_later: 'Call later', do_not_call: 'Do not call',
  wrong_number: 'Wrong number', answered_questions: 'Answered questions', no_response: 'No response', technical_problem: 'Technical problem',
};

function statusOf(it) {
  if (it.state === 'queued') return ['Waiting in line', 'var(--text-muted)'];
  if (it.state === 'waiting') return ['Waiting for a free line', 'var(--text-muted)'];
  if (it.state === 'cancelled') return ['Cancelled', 'var(--text-muted)'];
  if (it.state === 'failed') return ['Failed', 'var(--danger)'];
  switch (it.call_status) {
    case 'queued': return ['Ringing', 'var(--secondary-color)'];
    case 'answered': return ['Talking', 'var(--success)'];
    case 'finished': return ['Done', 'var(--success)'];
    case 'failed': return ['Not answered', 'var(--danger)'];
    case 'lost': return ['Lost', 'var(--danger)'];
    default: return ['Dialing', 'var(--secondary-color)'];
  }
}

export default function DialerView() {
  const [rows, setRows] = useState([newRow()]);
  const [batch, setBatch] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const url = (p = '') => `${API_BASE_URL}/admin/dial${p}`;

  const refresh = useCallback(async (id) => {
    const r = await authFetch(url(`/${id}`));
    if (r.status === 404) {
      localStorage.removeItem(BATCH_KEY);
      setBatch(null);
      setError('That call list is no longer available (the server was restarted). Finished calls are under Billing / Cost -> Calls.');
      return null;
    }
    if (!r.ok) return null;
    const b = await r.json();
    setBatch(b);
    return b;
  }, []);

  // pick up the last batch after a page reload
  useEffect(() => {
    const id = localStorage.getItem(BATCH_KEY);
    if (id) refresh(id);
  }, [refresh]);

  // poll while calls are still going
  useEffect(() => {
    if (!batch || batch.done) return undefined;
    const t = setInterval(() => refresh(batch.batch_id), 3000);
    return () => clearInterval(t);
  }, [batch, refresh]);

  const setRow = (id, patch) => setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const addRow = () => setRows((rs) => [...rs, newRow()]);
  const removeRow = (id) => setRows((rs) => (rs.length === 1 ? [newRow()] : rs.filter((r) => r.id !== id)));

  // several numbers pasted into one box (comma / space / new line separated) become one row each
  const onPaste = (id, e) => {
    const parts = e.clipboardData.getData('text').split(/[\n,;\t ]+/).map((s) => s.trim()).filter(Boolean);
    if (parts.length < 2) return;
    e.preventDefault();
    setRows((rs) => rs.flatMap((r) => (r.id === id ? [{ ...r, number: parts[0] }, ...parts.slice(1).map((p) => newRow(p))] : [r])));
  };

  const filled = rows.filter((r) => r.number.trim());
  const rowProblem = (r) => {
    if (!r.number.trim()) return '';
    const n = normalize(r.number);
    if (!n) return 'Not a valid number';
    if (rows.some((o) => o.id !== r.id && o.number.trim() && normalize(o.number) === n && o.id < r.id)) return 'Already in the list';
    return '';
  };
  const hasProblem = filled.some((r) => rowProblem(r));
  const canCall = filled.length > 0 && !hasProblem && !busy && !(batch && !batch.done);

  const call = async () => {
    setError('');
    if (!window.confirm(`Call ${filled.length} number${filled.length > 1 ? 's' : ''} now?\n\nReal phone calls will be placed and each one costs money.`)) return;
    setBusy(true);
    try {
      const r = await authFetch(url(), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ numbers: filled.map((x) => ({ number: x.number.trim(), name: x.name.trim() })) }),
      });
      if (!r.ok) { setError(await getApiError(r)); return; }
      const b = await r.json();
      localStorage.setItem(BATCH_KEY, b.batch_id);
      setBatch(b);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    const r = await authFetch(url(`/${batch.batch_id}/cancel`), { method: 'POST' });
    if (r.ok) setBatch(await r.json());
  };

  const reset = () => {
    localStorage.removeItem(BATCH_KEY);
    setBatch(null);
    setRows([newRow()]);
    setError('');
  };

  const items = batch ? batch.items : [];
  const count = (f) => items.filter(f).length;
  const waiting = count((i) => i.state === 'queued' || i.state === 'waiting');

  return (
    <div className="fade-in">
      <div className="page-header">
        <h1 className="page-title">Make Calls</h1>
        <p className="page-subtitle">Call one person, or add more numbers and call them all. Several calls run at the same time; the rest wait for a free line.</p>
      </div>

      {!batch && (
        <div className="glass-card">
          <h3 style={{ marginBottom: 12 }}>Numbers to call</h3>
          {rows.map((r, idx) => {
            const problem = rowProblem(r);
            return (
              <div key={r.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 10, flexWrap: 'wrap' }}>
                <span style={{ ...small, width: 22, paddingTop: 12 }}>{idx + 1}.</span>
                <div>
                  <input className="input-field" placeholder="01XXXXXXXXX" value={r.number} inputMode="tel" style={{ width: 200, borderColor: problem ? 'var(--danger)' : undefined }}
                    onChange={(e) => setRow(r.id, { number: e.target.value })} onPaste={(e) => onPaste(r.id, e)}
                    onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addRow(); } }} />
                  {problem && <div style={{ color: 'var(--danger)', fontSize: '0.8rem', marginTop: 2 }}>{problem}</div>}
                </div>
                <input className="input-field" placeholder="Name (optional)" value={r.name} maxLength={60} style={{ width: 200 }}
                  onChange={(e) => setRow(r.id, { name: e.target.value })} />
                <button className="btn btn-outline" type="button" title="Remove" onClick={() => removeRow(r.id)}><Trash2 size={16} /></button>
              </div>
            );
          })}
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 8, flexWrap: 'wrap' }}>
            <button className="btn btn-outline" type="button" onClick={addRow}><Plus size={16} /> Add number</button>
            <button className="btn btn-primary" type="button" disabled={!canCall} onClick={call}>
              <PhoneCall size={16} /> {busy ? 'Starting...' : filled.length > 1 ? `Call ${filled.length} numbers` : 'Call'}
            </button>
            <span style={small}>Tip: paste several numbers at once (separated by commas, spaces or new lines) and they become separate rows.</span>
          </div>
        </div>
      )}

      {error && <div className="glass-card" style={{ color: 'var(--danger)', marginTop: 16 }}>{error}</div>}

      {batch && (
        <div className="glass-card" style={{ marginTop: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
            <h3>
              {batch.done ? 'All calls finished' : 'Calling...'}{' '}
              <span style={small}>
                {count((i) => i.call_status === 'finished')} done · {count((i) => i.call_status === 'answered')} talking · {count((i) => i.call_status === 'queued')} ringing
                · {waiting} waiting · {count((i) => i.call_status === 'failed' || i.call_status === 'lost' || i.state === 'failed')} not completed
              </span>
            </h3>
            <div style={{ display: 'flex', gap: 8 }}>
              {waiting > 0 && !batch.cancelled && <button className="btn btn-outline" onClick={cancel}>Cancel the {waiting} waiting</button>}
              {batch.done && <button className="btn btn-primary" onClick={reset}>New calls</button>}
            </div>
          </div>
          <div className="table-container"><table>
            <thead><tr><th>#</th><th>Number</th><th>Name</th><th>Status</th><th>Result</th><th>Talk time</th></tr></thead>
            <tbody>
              {items.map((it) => {
                const [label, color] = statusOf(it);
                return (
                  <tr key={it.index}>
                    <td>{it.index + 1}</td>
                    <td>{it.number}</td>
                    <td>{it.name || '-'}</td>
                    <td><span className="badge" style={{ color }}>{label}</span></td>
                    <td>{it.error ? <span style={{ color: 'var(--danger)' }}>{it.error}</span> : (OUTCOMES[it.outcome] || it.outcome || (it.call_status === 'failed' || it.call_status === 'lost' ? it.detail || '-' : '-'))}</td>
                    <td>{it.talk_seconds != null ? `${Math.round(it.talk_seconds)} s` : '-'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
          <p style={{ ...small, marginTop: 8 }}>Full details and costs of each call are under Billing / Cost, in the Calls table.</p>
        </div>
      )}
    </div>
  );
}
