import { createClient } from "npm:@supabase/supabase-js@2";

const HEARTBEAT_ID = 1;

function secretsMatch(expected: string, provided: string): boolean {
  const a = new TextEncoder().encode(expected);
  const b = new TextEncoder().encode(provided);
  if (a.length === 0 || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  return diff === 0;
}

function json(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return json({ ok: false, error: "method not allowed" }, 405);
  }

  const expected = Deno.env.get("HEARTBEAT_SECRET") ?? "";
  if (!expected) {
    return json({ ok: false, error: "heartbeat secret is not configured" }, 500);
  }

  const provided = req.headers.get("x-heartbeat-secret") ?? "";
  if (!secretsMatch(expected, provided)) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }

  const url = Deno.env.get("SUPABASE_URL") ?? "";
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
  if (!url || !serviceKey) {
    return json({ ok: false, error: "supabase credentials are not configured" }, 500);
  }

  const supabase = createClient(url, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data, error } = await supabase
    .from("system_heartbeat")
    .upsert(
      { id: HEARTBEAT_ID, last_ping: new Date().toISOString() },
      { onConflict: "id" },
    )
    .select("id, last_ping")
    .single();

  if (error || !data) {
    return json({ ok: false, error: "update failed" }, 500);
  }

  return json({ ok: true, id: data.id, last_ping: data.last_ping }, 200);
});
