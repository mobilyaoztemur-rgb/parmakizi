export interface UpdateEnv { DB: D1Database; ASSETS: Fetcher; UPDATES_SIGNING_KEY?: string }
export class UpdateError extends Error { constructor(public status: number, message: string) { super(message); } }
export function versionCode(version: string) {
  if (!/^(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})$/.test(version)) throw new UpdateError(400, 'Geçersiz sürüm numarası.');
  const [a, b, c] = version.split('.').map(Number);
  return a * 1_000_000 + b * 1_000 + c;
}
export async function latestUpdate(env: UpdateEnv) {
  const row = await env.DB.prepare('SELECT payload,signature FROM releases ORDER BY version_code DESC LIMIT 1').first<{ payload: string; signature: string }>();
  return Response.json(row ? { payload: row.payload, signature: row.signature } : { update: null });
}
export async function publishUpdate(request: Request, env: UpdateEnv, data: Record<string, any>) {
  if (typeof data.version !== 'string') throw new UpdateError(400, 'Sürüm numarası gerekiyor.');
  const code = versionCode(data.version);
  if (!Array.isArray(data.files) || data.files.length !== 2) throw new UpdateError(400, 'Uygulama ve güncelleyici dosyaları gerekiyor.');
  const files = [];
  for (const name of ['Parmakizi.exe', 'ParmakiziUpdater.exe']) {
    const file = data.files.find((f: any) => f?.name === name);
    if (!file || !/^[a-f0-9]{64}$/.test(file.sha256 ?? '') || !Number.isInteger(file.size) || file.size < 1 || file.size > 25 * 1024 * 1024) throw new UpdateError(400, 'Dosya özeti veya boyutu geçersiz.');
    const path = `/updates/files/${name.slice(0, -4)}-${data.version}-${file.sha256.slice(0, 12)}.exe`;
    if (file.path !== path) throw new UpdateError(400, 'Güncelleme dosyası yolu geçersiz.');
    const asset = await env.ASSETS.fetch(new Request(new URL(path, request.url)));
    if (asset.status !== 200 || !asset.body) throw new UpdateError(409, 'Önce doğru dosyaları Cloudflare’a yayımlayın.');
    // Asset HEAD responses do not always expose Content-Length. Count the stream
    // without buffering an executable into the Worker's memory.
    const reader = asset.body.getReader();
    let size = 0;
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > file.size) { await reader.cancel(); break; }
    }
    if (size !== file.size) throw new UpdateError(409, 'Cloudflare dosyasının boyutu bildirilen değerle eşleşmiyor.');
    files.push({ name, path, size: file.size, sha256: file.sha256 });
  }
  const existing = await env.DB.prepare('SELECT payload,signature FROM releases WHERE version=?').bind(data.version).first<{ payload: string; signature: string }>();
  if (existing) {
    if (JSON.stringify(JSON.parse(existing.payload).files) !== JSON.stringify(files)) throw new UpdateError(409, 'Bu sürüm farklı dosyalarla zaten yayımlanmış. Yeni sürüm numarası kullanın.');
    return Response.json(existing);
  }
  if (!env.UPDATES_SIGNING_KEY) throw new UpdateError(503, 'Güncelleme imzalama anahtarı yapılandırılmamış.');
  const publishedAt = new Date().toISOString();
  const notes = typeof data.notes === 'string' ? data.notes.slice(0, 3000) : '';
  const payload = JSON.stringify({ version: data.version, publishedAt, notes, files });
  if (new TextEncoder().encode(payload).byteLength > 16000) throw new UpdateError(400, 'Sürüm açıklaması çok uzun.');
  const keyBytes = Uint8Array.from(atob(env.UPDATES_SIGNING_KEY), c => c.charCodeAt(0));
  const key = await crypto.subtle.importKey('pkcs8', keyBytes, 'Ed25519', false, ['sign']);
  const signed = await crypto.subtle.sign('Ed25519', key, new TextEncoder().encode(payload));
  const signature = btoa(String.fromCharCode(...new Uint8Array(signed)));
  const result = await env.DB.prepare(`INSERT INTO releases(version,version_code,payload,signature,published_at)
    SELECT ?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM releases WHERE version_code>=?)`).bind(data.version, code, payload, signature, publishedAt, code).run();
  if (!result.meta.changes) throw new UpdateError(409, 'Daha yeni veya aynı bir sürüm zaten yayımlanmış.');
  return Response.json({ payload, signature }, { status: 201 });
}
