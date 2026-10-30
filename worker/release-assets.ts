/** Stream immutable update chunks so deployment requests stay small. */
export async function releaseAsset(request: Request, assets: Fetcher): Promise<Response> {
  const direct = await assets.fetch(request);
  if (direct.status !== 404) return direct;
  await direct.body?.cancel();
  const url = new URL(request.url);
  const match = url.pathname.match(/^\/updates\/files\/((?:Parmakizi|ParmakiziUpdater)-\d+\.\d+\.\d+-([a-f0-9]{12}))\.exe$/);
  if (!match) return new Response('Not found', { status: 404 });
  const prefix = `/updates/chunks/${match[1]}`;
  const index = await assets.fetch(new Request(new URL(prefix + '/index.json', url)));
  if (index.status !== 200) return new Response('Not found', { status: 404 });
  const plan = await index.json<{ size: number; sha256: string; parts: number; chunkSize: number; format: number }>();
  if (plan.format !== 1 || !Number.isInteger(plan.size) || plan.size < 1 || plan.size > 25 * 1024 * 1024 || plan.chunkSize !== 1024 * 1024 ||
      plan.parts !== Math.ceil(plan.size / plan.chunkSize) || !/^[a-f0-9]{64}$/.test(plan.sha256) || !plan.sha256.startsWith(match[2])) {
    return new Response('Invalid update asset', { status: 502 });
  }
  const headers = { 'Content-Type': 'application/octet-stream', 'Content-Length': String(plan.size), 'X-Parmakizi-SHA256': plan.sha256 };
  if (request.method === 'HEAD') return new Response(null, { headers });
  let part = 0, seen = 0;
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        while (true) {
          if (!reader) {
            if (part === plan.parts) { controller.close(); return; }
            const response = await assets.fetch(new Request(new URL(prefix + '/' + String(part).padStart(6, '0') + '.bin', url)));
            if (response.status !== 200 || !response.body) throw new Error('Update part missing');
            reader = response.body.getReader();
            seen = 0;
          }
          const value = await reader.read();
          const expected = Math.min(plan.chunkSize, plan.size - part * plan.chunkSize);
          if (value.done) {
            if (seen !== expected) throw new Error('Update part truncated');
            reader = null;
            part++;
            continue;
          }
          seen += value.value.byteLength;
          if (seen > expected) throw new Error('Update part oversized');
          controller.enqueue(value.value);
          return;
        }
      } catch (error) {
        await reader?.cancel().catch(() => {});
        controller.error(error);
      }
    },
    async cancel() { await reader?.cancel(); }
  });
  return new Response(stream, { headers });
}
