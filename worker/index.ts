import { catalog } from '../shared/catalog';
import modelInventory from '../shared/model_inventory.json';
import { latestUpdate, publishUpdate, UpdateError } from './updates';
import { version as APP_VERSION } from '../package.json';

interface Env { DB: D1Database; ASSETS: Fetcher; ADMIN_PASSWORD: string; SESSION_SECRET: string; UPDATES_SIGNING_KEY?: string }
type Row = Record<string, any>;
class HttpError extends Error { constructor(public status: number, message: string) { super(message); } }
const now = () => new Date().toISOString();
const json = (data: unknown, status = 200) => Response.json(data, { status });
const id = () => crypto.randomUUID();
const encoder = new TextEncoder();
const writeTypes = new Set(['users.upsert', 'device.time.sync', 'device.restart']);
const writeSql = "'users.upsert','device.time.sync','device.restart'";
const hex = (a: ArrayBuffer) => [...new Uint8Array(a)].map(x => x.toString(16).padStart(2, '0')).join('');
const sha = async (s: string) => hex(await crypto.subtle.digest('SHA-256', encoder.encode(s)));
async function hmac(secret: string, text: string) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return hex(await crypto.subtle.sign('HMAC', key, encoder.encode(text)));
}
function same(a: string, b: string) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
function text(value: unknown, field: string, max = 100) {
  if (typeof value !== 'string' || !value.trim() || value.length > max || /[\u0000-\u001f]/.test(value)) throw new HttpError(400, `${field} geçersiz.`);
  return value.trim();
}
function integer(value: unknown, field: string, min: number, max: number) {
  if (!Number.isInteger(value) || (value as number) < min || (value as number) > max) throw new HttpError(400, `${field} geçersiz.`);
  return value as number;
}
function privateIp(value: unknown) {
  const ip = text(value, 'IP', 15);
  const p = ip.split('.');
  if (p.length !== 4 || p.some(x => !/^(0|[1-9]\d{0,2})$/.test(x) || +x > 255)) throw new HttpError(400, 'Geçerli bir IPv4 adresi girin.');
  const n = p.map(Number);
  if (!(n[0] === 10 || n[0] === 172 && n[1] >= 16 && n[1] <= 31 || n[0] === 192 && n[1] === 168)) throw new HttpError(400, 'Yalnızca özel yerel ağ IPv4 adresleri kullanılabilir.');
  return ip;
}
async function body(request: Request): Promise<Row> {
  if (!request.headers.get('content-type')?.includes('application/json')) throw new HttpError(415, 'JSON içerik gerekiyor.');
  // Read the stream with a cap, including chunked requests without Content-Length.
  const reader = request.body?.getReader();
  if (!reader) throw new HttpError(400, 'İstek gövdesi eksik.');
  let size = 0;
  const chunks: Uint8Array[] = [];
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 400_000) { await reader.cancel(); throw new HttpError(413, 'İstek çok büyük.'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const c of chunks) { bytes.set(c, offset); offset += c.byteLength; }
  try {
    const b: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (!b || Array.isArray(b) || typeof b !== 'object') throw new Error();
    return b as Row;
  } catch { throw new HttpError(400, 'Geçersiz JSON.'); }
}
async function audit(env: Env, actor: string, action: string, target: string) {
  return env.DB.prepare('INSERT INTO audit(created_at,actor,action,target) VALUES(?,?,?,?)').bind(now(), actor, action, target).run();
}
async function session(request: Request, env: Env) {
  const token = request.headers.get('cookie')?.split(';').map(x => x.trim()).find(x => x.startsWith('pmk_session='))?.slice(12);
  if (!token || token.length > 200) throw new HttpError(401, 'Oturum açın.');
  const parts = token.split('.');
  if (parts.length !== 3 || !/^\d+$/.test(parts[1]) || +parts[1] < Date.now() || !same(parts[2], await hmac(env.SESSION_SECRET, `${parts[0]}.${parts[1]}`))) throw new HttpError(401, 'Oturum süresi doldu.');
  if (!await env.DB.prepare('SELECT id FROM sessions WHERE id=? AND expires_at>?').bind(parts[0], Date.now()).first()) throw new HttpError(401, 'Oturum kapatılmış.');
  return parts[0];
}
function checkOrigin(request: Request) {
  const origin = request.headers.get('origin');
  if (origin && origin !== new URL(request.url).origin) throw new HttpError(403, 'Kaynak doğrulaması başarısız.');
  if (request.headers.get('sec-fetch-site') === 'cross-site') throw new HttpError(403, 'Siteler arası istek reddedildi.');
}
async function gatewayAuth(request: Request, env: Env) {
  const token = request.headers.get('authorization')?.match(/^Bearer ([a-f0-9]{64})$/)?.[1];
  if (!token) throw new HttpError(401, 'Bağlantı anahtarı gerekiyor.');
  const g = await env.DB.prepare('SELECT id FROM gateways WHERE token_hash=? AND revoked_at IS NULL').bind(await sha(token)).first<Row>();
  if (!g) throw new HttpError(401, 'Bağlantı anahtarı geçersiz veya iptal edilmiş.');
  await env.DB.prepare('UPDATE gateways SET last_seen=? WHERE id=? AND (last_seen IS NULL OR last_seen<?)').bind(now(), g.id, new Date(Date.now() - 30_000).toISOString()).run();
  return g.id as string;
}
async function gatewayRoute(request: Request, env: Env, path: string) {
  if (request.method !== 'POST') throw new HttpError(405, 'POST gerekiyor.');
  const gatewayId = await gatewayAuth(request, env);
  const b = await body(request);
  if (path === '/api/gateway/poll') {
    // Never automatically repeat a write after a lost response: its outcome is uncertain.
    await env.DB.prepare(`UPDATE jobs SET status=CASE WHEN type IN (${writeSql}) THEN 'uncertain' ELSE 'failed' END,
      completed_at=?, error='Bağlantı süresi doldu; cihazdaki sonucu kontrol edin.'
      WHERE gateway_id=? AND status='running' AND lease_until < ?`).bind(now(), gatewayId, now()).run();
    const scheduledId = id(), timestamp = now();
    // Manual work has priority. Schedule at most one due device per idle gateway.
    await env.DB.batch([
      env.DB.prepare(`INSERT INTO jobs(id,gateway_id,device_id,type,params,created_at,request_id,request_hash,origin)
        SELECT ?,gateway_id,id,'attendance.read','{}',?,?,?,'scheduled' FROM devices
        WHERE gateway_id=? AND sync_enabled=1 AND (next_sync_at IS NULL OR next_sync_at<=?)
        AND NOT EXISTS(SELECT 1 FROM jobs WHERE gateway_id=? AND status IN ('queued','running'))
        ORDER BY COALESCE(next_sync_at,created_at),id LIMIT 1`)
        .bind(scheduledId, timestamp, `scheduled:${scheduledId}`, scheduledId, gatewayId, timestamp, gatewayId),
      env.DB.prepare(`UPDATE devices SET next_sync_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+' || sync_interval_seconds || ' seconds')
        WHERE id=(SELECT device_id FROM jobs WHERE id=?)`).bind(scheduledId)
    ]);
    const claim = id();
    const job = await env.DB.prepare(`UPDATE jobs SET status='running', attempts=attempts+1, claim_token=?, lease_until=?
      WHERE id=(SELECT id FROM jobs WHERE gateway_id=? AND status='queued' ORDER BY created_at,id LIMIT 1)
      AND status='queued' AND NOT EXISTS(SELECT 1 FROM jobs WHERE gateway_id=? AND status='running')
      RETURNING *`).bind(claim, new Date(Date.now() + 600_000).toISOString(), gatewayId, gatewayId).first<Row>();
    if (!job) return json({ job: null });
    const device = job.device_id ? await env.DB.prepare('SELECT id,ip,port,driver FROM devices WHERE id=? AND gateway_id=?').bind(job.device_id, gatewayId).first() : null;
    const params = JSON.parse(job.params);
    if (job.type === 'device.time.sync') params.serverTime = now();
    return json({ job: { id: job.id, type: job.type, params, origin: job.origin, claimToken: claim, device } });
  }
  const match = path.match(/^\/api\/gateway\/jobs\/([\w-]+)\/result$/);
  if (!match) throw new HttpError(404, 'Uç nokta bulunamadı.');
  const job = await env.DB.prepare('SELECT * FROM jobs WHERE id=? AND gateway_id=?').bind(match[1], gatewayId).first<Row>();
  if (!job || job.claim_token !== b.claimToken) throw new HttpError(404, 'Görev bulunamadı.');
  if (job.status === 'succeeded' && b.final === true) return json({ ok: true, duplicate: true });
  if (job.status !== 'running' || job.lease_until < now()) throw new HttpError(409, 'Görevin çalışma süresi doldu.');
  if (typeof b.final !== 'boolean') throw new HttpError(400, 'final alanı gerekiyor.');
  const statements: D1PreparedStatement[] = [];
  const timestamp = now();
  if (b.error !== undefined) {
    const error = text(b.error, 'Hata', 300);
    const status = writeTypes.has(job.type) ? 'uncertain' : 'failed';
    statements.push(env.DB.prepare('UPDATE jobs SET status=?,error=?,completed_at=? WHERE id=?').bind(status, error, timestamp, job.id));
    if (job.device_id) statements.push(env.DB.prepare('UPDATE devices SET last_error=? WHERE id=?').bind(error, job.device_id));
    if (job.device_id && job.type === 'attendance.read') statements.push(env.DB.prepare(`UPDATE devices SET sync_failures=MIN(sync_failures+1,6),
      next_sync_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+' || MIN(MAX(sync_interval_seconds*3,60),3600) || ' seconds') WHERE id=?`).bind(job.device_id));
    statements.push(env.DB.prepare('INSERT INTO audit(created_at,actor,action,target) VALUES(?,?,?,?)').bind(timestamp, gatewayId, `job.${status}`, job.id));
    await env.DB.batch(statements);
    return json({ ok: true });
  }
  const records = b.records ?? [];
  // Keep each invocation below the D1 Free plan's 50-query allowance, including auth and metadata.
  if (!Array.isArray(records) || records.length > 40) throw new HttpError(400, 'Bir pakette en fazla 40 kayıt gönderilebilir.');
  if (records.length && !['users.read', 'attendance.read'].includes(job.type)) throw new HttpError(400, 'Bu görev kayıt paketi kabul etmez.');
  // Validate the entire batch before writing any of it. Upserts make retries idempotent.
  for (const r of records) {
    if (!r || typeof r !== 'object') throw new HttpError(400, 'Geçersiz kayıt.');
    if (job.type === 'users.read') {
      const uid = integer(r.uid, 'UID', 1, 65535);
      const userId = text(r.userId, 'Kullanıcı kimliği', 24);
      const name = typeof r.name === 'string' && r.name.length <= 100 ? r.name : '';
      statements.push(env.DB.prepare(`INSERT INTO device_users(device_id,uid,user_id,name,card,privilege,synced_at) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(device_id,uid) DO UPDATE SET user_id=excluded.user_id,name=excluded.name,card=excluded.card,privilege=excluded.privilege,synced_at=excluded.synced_at`)
        .bind(job.device_id, uid, userId, name, integer(r.card ?? 0, 'Kart', 0, 4294967295), integer(r.privilege ?? 0, 'Yetki', 0, 255), timestamp));
    } else if (job.type === 'attendance.read') {
      const userId = text(r.userId, 'Kullanıcı kimliği', 24);
      const recordedAt = text(r.recordedAt, 'Zaman', 40);
      if (!/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(recordedAt) || !Number.isFinite(Date.parse(recordedAt))) throw new HttpError(400, 'Saat dilimi içeren bir zaman gerekiyor.');
      const utc = new Date(recordedAt).toISOString();
      const status = integer(r.status, 'Durum', 0, 255), punch = integer(r.punch, 'Geçiş', 0, 255);
      const recordId = await sha(JSON.stringify([job.device_id, userId, utc, status, punch]));
      statements.push(env.DB.prepare('INSERT OR IGNORE INTO attendance(id,device_id,user_id,recorded_at,device_local_time,timezone,status,punch,synced_at) VALUES(?,?,?,?,?,?,?,?,?)')
        .bind(recordId, job.device_id, userId, utc, text(r.localTime, 'Yerel zaman', 40), text(r.timezone, 'Saat dilimi', 80), status, punch, timestamp));
    }
  }
  if (b.final) {
    const summary = b.summary ?? {};
    if (typeof summary !== 'object' || summary === null || Array.isArray(summary) || JSON.stringify(summary).length > 50000) throw new HttpError(400, 'Geçersiz özet.');
    statements.push(env.DB.prepare("UPDATE jobs SET status='succeeded',result=?,completed_at=? WHERE id=?").bind(JSON.stringify(summary), timestamp, job.id));
    if (job.device_id) {
      statements.push(env.DB.prepare('UPDATE devices SET last_contact=?,last_error=NULL WHERE id=?').bind(timestamp, job.device_id));
      if (job.type === 'device.info') statements.push(env.DB.prepare('UPDATE devices SET info=? WHERE id=?').bind(JSON.stringify(summary), job.device_id));
      if (job.type === 'device.settings.read' || job.type === 'device.time.sync') statements.push(env.DB.prepare('UPDATE devices SET settings=? WHERE id=?').bind(JSON.stringify(summary), job.device_id));
      if (job.type === 'attendance.read') statements.push(env.DB.prepare(`UPDATE devices SET last_sync_at=?,sync_failures=0,
        next_sync_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+' || sync_interval_seconds || ' seconds') WHERE id=?`).bind(timestamp, job.device_id));
    }
    statements.push(env.DB.prepare('INSERT INTO audit(created_at,actor,action,target) VALUES(?,?,?,?)').bind(timestamp, gatewayId, 'job.succeeded', job.id));
  } else statements.push(env.DB.prepare('UPDATE jobs SET lease_until=? WHERE id=?').bind(new Date(Date.now() + 600_000).toISOString(), job.id));
  await env.DB.batch(statements);
  return json({ ok: true });
}

async function api(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url), path = url.pathname, method = request.method;
  if (path === '/api/health' && method === 'GET') {
    await env.DB.prepare('SELECT 1 FROM gateways LIMIT 1').first();
    return json({ ok: true, service: 'parmakizi', version: APP_VERSION });
  }
  if (path === '/api/updates/latest' && method === 'GET') return latestUpdate(env);
  if (!env.ADMIN_PASSWORD || env.ADMIN_PASSWORD.length < 12 || !env.SESSION_SECRET || env.SESSION_SECRET.length < 32) throw new HttpError(503, 'Yönetici parolası ve oturum anahtarı yapılandırılmalı.');
  if (path.startsWith('/api/gateway/')) return gatewayRoute(request, env, path);
  if (method !== 'GET') checkOrigin(request);
  if (path === '/api/auth/login' && method === 'POST') {
    const b = await body(request);
    const ipKey = await hmac(env.SESSION_SECRET, request.headers.get('cf-connecting-ip') ?? 'local');
    const window = Math.floor(Date.now() / 300_000);
    const limit = await env.DB.prepare(`INSERT INTO login_limits(key,window,attempts) VALUES(?,?,1)
      ON CONFLICT(key) DO UPDATE SET window=excluded.window,attempts=CASE WHEN login_limits.window=excluded.window THEN login_limits.attempts+1 ELSE 1 END RETURNING attempts`).bind(ipKey, window).first<Row>();
    if (!limit || limit.attempts > 10) throw new HttpError(429, 'Çok fazla deneme. Beş dakika sonra tekrar deneyin.');
    if (typeof b.password !== 'string' || !same(await sha(b.password), await sha(env.ADMIN_PASSWORD))) throw new HttpError(401, 'Parola hatalı.');
    const sessionId = id(), expiresAt = Date.now() + 43_200_000;
    const payload = `${sessionId}.${expiresAt}`;
    await env.DB.batch([
      env.DB.prepare('INSERT INTO sessions(id,expires_at,created_at) VALUES(?,?,?)').bind(sessionId, expiresAt, now()),
      env.DB.prepare('DELETE FROM sessions WHERE expires_at<?').bind(Date.now()),
      env.DB.prepare('DELETE FROM login_limits WHERE window<?').bind(window - 1)
    ]);
    const response = json({ ok: true });
    response.headers.set('Set-Cookie', `pmk_session=${payload}.${await hmac(env.SESSION_SECRET, payload)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200${url.protocol === 'https:' ? '; Secure' : ''}`);
    await audit(env, 'admin', 'auth.login', 'session');
    return response;
  }
  const sessionId = await session(request, env);
  if (path === '/api/updates/publish' && method === 'POST') {
    const response = await publishUpdate(request, env, await body(request));
    if (response.status === 201) await audit(env, 'admin', 'update.publish', 'release');
    return response;
  }
  if (path === '/api/auth/logout' && method === 'POST') {
    await env.DB.prepare('DELETE FROM sessions WHERE id=?').bind(sessionId).run();
    await audit(env, 'admin', 'auth.logout', 'session');
    const response = json({ ok: true });
    response.headers.set('Set-Cookie', `pmk_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0${url.protocol === 'https:' ? '; Secure' : ''}`);
    return response;
  }
  if (path === '/api/me' && method === 'GET') return json({ role: 'admin' });
  if (path === '/api/catalog' && method === 'GET') return json({ catalog, models: modelInventory });
  if (path === '/api/overview' && method === 'GET') {
    const [devices, gateways, jobs, counts] = await env.DB.batch([
      env.DB.prepare('SELECT d.*,g.name AS gateway_name FROM devices d JOIN gateways g ON d.gateway_id=g.id ORDER BY d.created_at DESC'),
      env.DB.prepare('SELECT id,name,created_at,last_seen,revoked_at FROM gateways ORDER BY created_at DESC'),
      env.DB.prepare('SELECT j.id,j.device_id,j.type,j.status,j.created_at,j.completed_at,j.error,j.result,d.name AS device_name,g.name AS gateway_name FROM jobs j LEFT JOIN devices d ON j.device_id=d.id JOIN gateways g ON j.gateway_id=g.id ORDER BY j.created_at DESC LIMIT 100'),
      env.DB.prepare('SELECT (SELECT COUNT(*) FROM device_users) AS users,(SELECT COUNT(*) FROM attendance) AS attendance')
    ]);
    return json({ devices: devices.results, gateways: gateways.results, jobs: jobs.results, counts: counts.results[0] });
  }
  if (path === '/api/gateways' && method === 'POST') {
    const b = await body(request);
    const gatewayId = id(), token = hex(crypto.getRandomValues(new Uint8Array(32)).buffer);
    const name = text(b.name, 'Ad');
    await env.DB.batch([
      env.DB.prepare('INSERT INTO gateways(id,name,token_hash,created_at) VALUES(?,?,?,?)').bind(gatewayId, name, await sha(token), now()),
      env.DB.prepare('INSERT INTO audit(created_at,actor,action,target) VALUES(?,?,?,?)').bind(now(), 'admin', 'gateway.create', gatewayId)
    ]);
    return json({ id: gatewayId, token, name }, 201);
  }
  const revoke = path.match(/^\/api\/gateways\/([\w-]+)\/revoke$/);
  if (revoke && method === 'POST') {
    const result = await env.DB.prepare('UPDATE gateways SET revoked_at=? WHERE id=? AND revoked_at IS NULL').bind(now(), revoke[1]).run();
    if (!result.meta.changes) throw new HttpError(404, 'Bağlantı noktası bulunamadı.');
    await env.DB.prepare(`UPDATE jobs SET status=CASE WHEN status='running' AND type IN (${writeSql}) THEN 'uncertain' ELSE 'failed' END,completed_at=?,error='Bağlantı anahtarı iptal edildi.' WHERE gateway_id=? AND status IN ('queued','running')`).bind(now(), revoke[1]).run();
    await audit(env, 'admin', 'gateway.revoke', revoke[1]);
    return json({ ok: true });
  }
  if (path === '/api/devices' && method === 'POST') {
    const b = await body(request);
    if (!['zkteco', 'simulator'].includes(b.driver)) throw new HttpError(400, 'Bu protokol için çalışan adaptör henüz yok.');
    const gatewayId = text(b.gatewayId, 'Bağlantı noktası');
    if (!await env.DB.prepare('SELECT id FROM gateways WHERE id=? AND revoked_at IS NULL').bind(gatewayId).first()) throw new HttpError(400, 'Aktif bir bağlantı noktası seçin.');
    const deviceId = id();
    try {
      await env.DB.batch([
        env.DB.prepare('INSERT INTO devices(id,gateway_id,name,brand,model,ip,port,driver,created_at,sync_enabled) VALUES(?,?,?,?,?,?,?,?,?,?)')
          .bind(deviceId, gatewayId, text(b.name, 'Ad'), text(b.brand, 'Marka', 60), typeof b.model === 'string' ? b.model.slice(0, 100) : '', privateIp(b.ip), integer(b.port, 'Port', 1, 65535), b.driver, now(), b.syncEnabled === true ? 1 : 0),
        env.DB.prepare('INSERT INTO audit(created_at,actor,action,target) VALUES(?,?,?,?)').bind(now(), 'admin', 'device.create', deviceId)
      ]);
    } catch (error) {
      if (String(error).includes('UNIQUE constraint')) throw new HttpError(409, 'Bu IP ve port zaten kayıtlı.');
      throw error;
    }
    return json({ id: deviceId }, 201);
  }
  const syncRoute = path.match(/^\/api\/devices\/([\w-]+)\/sync$/);
  if (syncRoute && method === 'PATCH') {
    const b = await body(request);
    if (typeof b.enabled !== 'boolean') throw new HttpError(400, 'Otomatik aktarım durumu gerekiyor.');
    const interval = integer(b.intervalSeconds, 'Aktarım aralığı', 30, 86400);
    const result = await env.DB.batch([
      env.DB.prepare('UPDATE devices SET sync_enabled=?,sync_interval_seconds=?,next_sync_at=NULL WHERE id=?').bind(b.enabled ? 1 : 0, interval, syncRoute[1]),
      env.DB.prepare("UPDATE jobs SET status='failed',completed_at=?,error='Otomatik aktarım durduruldu.' WHERE device_id=? AND origin='scheduled' AND status='queued' AND ?=0").bind(now(), syncRoute[1], b.enabled ? 1 : 0)
    ]);
    if (!result[0].meta.changes) throw new HttpError(404, 'Cihaz bulunamadı.');
    await audit(env, 'admin', 'device.sync.update', syncRoute[1]);
    return json({ ok: true });
  }
  const deviceRoute = path.match(/^\/api\/devices\/([\w-]+)$/);
  if (deviceRoute && (method === 'PATCH' || method === 'DELETE')) {
    const deviceId = deviceRoute[1];
    const device = await env.DB.prepare('SELECT * FROM devices WHERE id=?').bind(deviceId).first<Row>();
    if (!device) throw new HttpError(404, 'Cihaz bulunamadı.');
    if (method === 'PATCH') {
      const b = await body(request);
      const ip = privateIp(b.ip), port = integer(b.port, 'Port', 1, 65535);
      const moved = ip !== device.ip || port !== device.port;
      let result: D1Result;
      try {
        result = await env.DB.prepare(`UPDATE devices SET name=?,brand=?,model=?,ip=?,port=?,
          last_contact=CASE WHEN ? THEN NULL ELSE last_contact END,
          last_error=CASE WHEN ? THEN NULL ELSE last_error END,info=CASE WHEN ? THEN '{}' ELSE info END
          WHERE id=? AND NOT EXISTS(SELECT 1 FROM jobs WHERE device_id=? AND status IN ('queued','running'))`)
          .bind(text(b.name, 'Ad'), text(b.brand, 'Marka', 60), typeof b.model === 'string' ? b.model.slice(0, 100) : '', ip, port,
            moved ? 1 : 0, moved ? 1 : 0, moved ? 1 : 0, deviceId, deviceId).run();
      } catch (error) {
        if (String(error).includes('UNIQUE constraint')) throw new HttpError(409, 'Bu IP ve port zaten kayıtlı.');
        throw error;
      }
      if (!result.meta.changes) throw new HttpError(409, 'Cihazın bekleyen veya çalışan işlemi var; işlem bittikten sonra düzenleyin.');
      await audit(env, 'admin', 'device.update', deviceId);
      return json({ ok: true });
    }
    // Each statement checks the same condition inside D1's atomic batch. A queued or
    // running command blocks removal, including a command added just before this batch.
    const guard = "NOT EXISTS(SELECT 1 FROM jobs WHERE device_id=? AND status IN ('queued','running'))";
    const result = await env.DB.batch([
      env.DB.prepare(`DELETE FROM attendance WHERE device_id=? AND ${guard}`).bind(deviceId, deviceId),
      env.DB.prepare(`DELETE FROM device_users WHERE device_id=? AND ${guard}`).bind(deviceId, deviceId),
      env.DB.prepare(`DELETE FROM jobs WHERE device_id=? AND ${guard}`).bind(deviceId, deviceId),
      env.DB.prepare(`DELETE FROM devices WHERE id=? AND ${guard}`).bind(deviceId, deviceId)
    ]);
    if (!result[3].meta.changes) throw new HttpError(409, 'Cihazın bekleyen veya çalışan işlemi var; işlem bitmeden kaldırılamaz.');
    await audit(env, 'admin', 'device.delete', deviceId);
    return json({ ok: true });
  }
  if (path === '/api/jobs' && method === 'POST') {
    const b = await body(request);
    const requestId = text(b.requestId, 'İstek kimliği', 80);
    const types = ['device.info', 'users.read', 'attendance.read', 'users.upsert', 'discover', 'device.settings.read', 'device.time.sync', 'device.restart'];
    if (!types.includes(b.type)) throw new HttpError(400, 'Görev türü desteklenmiyor.');
    let device: Row | null = null, gatewayId: string;
    const params: Row = {};
    if (b.type === 'discover') {
      gatewayId = text(b.gatewayId, 'Bağlantı noktası');
      const cidr = text(b.cidr, 'Alt ağ', 18), [ip, mask] = cidr.split('/');
      privateIp(ip);
      if (!/^\d+$/.test(mask ?? '') || +mask < 24 || +mask > 30) throw new HttpError(400, 'Keşif için /24 ile /30 arasında bir alt ağ girin.');
      params.cidr = cidr;
      params.port = integer(b.port ?? 4370, 'Port', 1, 65535);
    } else {
      device = await env.DB.prepare('SELECT * FROM devices WHERE id=?').bind(text(b.deviceId, 'Cihaz')).first<Row>();
      if (!device) throw new HttpError(404, 'Cihaz bulunamadı.');
      gatewayId = device.gateway_id;
      if (b.type === 'users.upsert') {
        params.uid = integer(b.uid, 'UID', 1, 65535);
        params.userId = text(b.userId, 'Sicil numarası', 9);
        if (!/^\d+$/.test(params.userId)) throw new HttpError(400, 'Sicil numarası en fazla 9 rakam olmalı.');
        params.name = text(b.name, 'Ad soyad', 100);
        if (encoder.encode(params.name).length > 24) throw new HttpError(400, 'Cihaz adı en fazla 24 UTF-8 baytı olmalı.');
        params.card = integer(b.card ?? 0, 'Kart', 0, 4294967295);
      }
    }
    if (!await env.DB.prepare('SELECT id FROM gateways WHERE id=? AND revoked_at IS NULL').bind(gatewayId).first()) throw new HttpError(400, 'Bağlantı noktası iptal edilmiş.');
    const requestHash = await sha(JSON.stringify([gatewayId, device?.id ?? null, b.type, params]));
    const existing = await env.DB.prepare('SELECT id,request_hash FROM jobs WHERE request_id=?').bind(requestId).first<Row>();
    if (existing) {
      if (existing.request_hash !== requestHash) throw new HttpError(409, 'İstek kimliği farklı bir işlemde kullanılmış.');
      return json({ id: existing.id, duplicate: true });
    }
    const active = await env.DB.prepare("SELECT COUNT(*) AS count FROM jobs WHERE gateway_id=? AND status IN ('queued','running')").bind(gatewayId).first<Row>();
    if (active && active.count >= 100) throw new HttpError(429, 'Görev kuyruğu dolu.');
    const jobId = id();
    try {
      await env.DB.batch([
        env.DB.prepare('INSERT INTO jobs(id,gateway_id,device_id,type,params,created_at,request_id,request_hash) VALUES(?,?,?,?,?,?,?,?)').bind(jobId, gatewayId, device?.id ?? null, b.type, JSON.stringify(params), now(), requestId, requestHash),
        env.DB.prepare('INSERT INTO audit(created_at,actor,action,target) VALUES(?,?,?,?)').bind(now(), 'admin', `job.queue.${b.type}`, jobId)
      ]);
    } catch (error) {
      if (!String(error).includes('UNIQUE constraint')) throw error;
      const raced = await env.DB.prepare('SELECT id,request_hash FROM jobs WHERE request_id=?').bind(requestId).first<Row>();
      if (!raced || raced.request_hash !== requestHash) throw new HttpError(409, 'İstek kimliği çakıştı.');
      return json({ id: raced.id, duplicate: true });
    }
    return json({ id: jobId }, 201);
  }
  if ((path === '/api/users' || path === '/api/attendance') && method === 'GET') {
    const deviceId = text(url.searchParams.get('deviceId'), 'Cihaz');
    if (path === '/api/users') {
      const after = Number(url.searchParams.get('after') ?? 0);
      integer(after, 'Sayfa', 0, 65535);
      const data = await env.DB.prepare('SELECT * FROM device_users WHERE device_id=? AND uid>? ORDER BY uid LIMIT 101').bind(deviceId, after).all<Row>();
      return json({ records: data.results.slice(0, 100), next: data.results.length > 100 ? data.results[99].uid : null });
    }
    const cursor = url.searchParams.get('before') ?? '';
    const data = await env.DB.prepare('SELECT * FROM attendance WHERE device_id=? AND (?=\'\' OR recorded_at || id < ?) ORDER BY recorded_at DESC,id DESC LIMIT 101').bind(deviceId, cursor, cursor).all<Row>();
    return json({ records: data.results.slice(0, 100), next: data.results.length > 100 ? data.results[99].recorded_at + data.results[99].id : null });
  }
  if (path === '/api/audit' && method === 'GET') return json(await env.DB.prepare('SELECT * FROM audit ORDER BY id DESC LIMIT 100').all());
  throw new HttpError(404, 'Uç nokta bulunamadı.');
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    let response: Response;
    try {
      const path = new URL(request.url).pathname;
      if (/^\/updates\/files\/(Parmakizi|ParmakiziUpdater)-\d+\.\d+\.\d+-[a-f0-9]{12}\.exe$/.test(path) && ['GET', 'HEAD'].includes(request.method)) {
        if (request.headers.has('authorization')) await gatewayAuth(request, env); else await session(request, env);
        response = await env.ASSETS.fetch(request);
      } else response = path.startsWith('/api/') ? await api(request, env) : json({ service: 'Parmakizi Cloud API', client: 'Windows masaüstü uygulamasını kullanın.' });
    } catch (error) {
      const expected = error instanceof HttpError || error instanceof UpdateError;
      if (!expected) console.error('Request failed', error instanceof Error ? error.message.replace(/Bearer\s+\S+/g, 'Bearer [redacted]') : 'Unknown error');
      response = json({ error: expected ? error.message : 'Sunucu hatası. Yapılandırmayı ve veritabanı geçişlerini kontrol edin.' }, expected ? error.status : 500);
    }
    const headers = new Headers(response.headers);
    headers.set('X-Content-Type-Options', 'nosniff');
    headers.set('Referrer-Policy', 'no-referrer');
    headers.set('X-Frame-Options', 'DENY');
    headers.set('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
    headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
    if (new URL(request.url).protocol === 'https:') headers.set('Strict-Transport-Security', 'max-age=31536000');
    if (new URL(request.url).pathname.startsWith('/api/')) headers.set('Cache-Control', 'no-store');
    if (new URL(request.url).pathname.startsWith('/updates/')) {
      headers.set('Cache-Control', 'private, max-age=3600');
      headers.set('Content-Disposition', 'attachment');
    }
    return new Response(response.body, { status: response.status, headers });
  }
} satisfies ExportedHandler<Env>;
