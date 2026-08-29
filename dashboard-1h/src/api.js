const API_BASE = import.meta.env.VITE_API_BASE || "";

export async function fetchHourBotStatus() {
  const res = await fetch(`${API_BASE}/api/1h-bot/status`);
  if (!res.ok) {
    throw new Error(`status ${res.status}`);
  }
  return res.json();
}

export async function postHourBotControl(payload) {
  const res = await fetch(`${API_BASE}/api/1h-bot/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`control ${res.status}`);
  }
  return res.json();
}
