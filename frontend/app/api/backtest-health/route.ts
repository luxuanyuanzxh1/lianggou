const services = {
  quant: "http://127.0.0.1:8767/health",
  lab: "http://127.0.0.1:8502/_stcore/health",
} as const;

export async function GET() {
  const entries = await Promise.all(Object.entries(services).map(async ([id, url]) => {
    const started = Date.now();
    try {
      const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(2500) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return [id, { status: "online", latency: Date.now() - started }] as const;
    } catch (reason) {
      return [id, { status: "offline", detail: reason instanceof Error ? reason.message : "无法连接" }] as const;
    }
  }));
  return Response.json({ services: Object.fromEntries(entries), checked_at: new Date().toISOString() }, { headers: { "Cache-Control": "no-store" } });
}
