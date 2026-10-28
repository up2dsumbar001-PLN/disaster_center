import { NextRequest } from "next/server";

// Fixed destinations: this route cannot be used to fetch arbitrary URLs.
const cache = new Map<string, { at: number; body: string; type: string; status: number }>();
const pending = new Map<string, Promise<Response>>();
const requests: number[] = [];

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams;
  const product = q.get("product");
  let url: string;
  if (product === "nowcast") url = "https://www.bmkg.go.id/alerts/nowcast/id";
  else if (product === "quake") url = "https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json";
  else if (product === "forecast" && /^\d{2}\.\d{2}\.\d{2}\.\d{4}$/.test(q.get("adm4") || ""))
    url = `https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4=${q.get("adm4")}`;
  else if (product === "cap" && /^[a-zA-Z0-9_-]{1,180}\.xml$/.test(q.get("file") || ""))
    url = `https://www.bmkg.go.id/alerts/nowcast/id/${q.get("file")}`;
  else return Response.json({ error: "Produk atau parameter BMKG tidak valid." }, { status: 400 });

  const cached = cache.get(url);
  const reply = (v: NonNullable<typeof cached>) => new Response(v.body, { status: v.status, headers: {
    "Content-Type": v.type, "X-Fetched-At": new Date(v.at).toISOString(),
    "Cache-Control": "private, max-age=30", "X-Content-Type-Options": "nosniff",
  } });
  if (cached && Date.now() - cached.at < (cached.status === 200 ? 60_000 : 30_000)) return reply(cached);
  if (pending.has(url)) return (await pending.get(url)!).clone();
  // Per-instance guard only. Production across replicas needs a shared collector.
  while (requests.length && requests[0] < Date.now() - 60_000) requests.shift();
  if (requests.length >= 40) return Response.json({ error: "Batas konektor tercapai. Coba kembali dalam 60 detik." }, { status: 429, headers: { "Retry-After": "60" } });
  requests.push(Date.now());
  const work = (async () => {
    try {
      const upstream = await fetch(url, { signal: AbortSignal.timeout(15_000), redirect: "manual", headers: { Accept: "application/xml,application/json,text/xml,*/*" } });
      if (!upstream.ok) throw new Error(`BMKG merespons HTTP ${upstream.status}`);
      const body = await upstream.text();
      const isJson = product === "forecast" || product === "quake";
      if (body.length > 6_000_000) throw new Error("Ukuran respons BMKG melebihi batas konektor");
      if (isJson) JSON.parse(body);
      else if (!/<(?:[\w-]+:)?(?:rss|feed|alert)\b/i.test(body)) throw new Error("Format XML BMKG tidak sesuai");
      const value = { at: Date.now(), body, type: isJson ? "application/json; charset=utf-8" : "application/xml; charset=utf-8", status: 200 };
      if (cache.size > 160) cache.delete(cache.keys().next().value!);
      cache.set(url, value);
      return reply(value);
    } catch (e) {
      const value = { at: Date.now(), body: JSON.stringify({ error: e instanceof Error ? e.message : "Koneksi BMKG gagal", source: "BMKG", state: "unavailable" }), type: "application/json", status: 502 };
      cache.set(url, value);
      return reply(value);
    }
  })();
  pending.set(url, work);
  try { return (await work).clone(); } finally { pending.delete(url); }
}
