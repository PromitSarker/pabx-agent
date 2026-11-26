import React, { useState } from 'react';
import { API_BASE_URL, TOKEN_KEY, getApiError } from './api';

export default function LoginView({ onLogin }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = await fetch(`${API_BASE_URL}/admin/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (!res.ok) throw new Error(await getApiError(res));
      const data = await res.json();
      localStorage.setItem(TOKEN_KEY, data.token);
      onLogin();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <form className="glass-card" onSubmit={submit} style={{ width: 360, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <img src="/logo.png" alt="upay Logo" style={{ height: 56, alignSelf: 'center' }} />
        <h2 style={{ textAlign: 'center' }}>Admin login</h2>
        <input
          className="input-field"
          type="password"
          placeholder="Admin password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <div style={{ color: 'var(--danger)', fontSize: '0.9rem' }}>{error}</div>}
        <button className="btn btn-primary" type="submit" disabled={busy || !password}>
          {busy ? 'Checking...' : 'Log in'}
        </button>
      </form>
    </div>
  );
}
