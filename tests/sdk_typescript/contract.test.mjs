import assert from 'node:assert/strict';
import test from 'node:test';
import OpenAI, { toFile } from 'openai';

const baseURL = process.env.GATEWAY_FIXTURE_URL;
assert.ok(baseURL, 'Run through the Python TCP fixture harness');
const client = new OpenAI({ baseURL, apiKey: process.env.GATEWAY_FIXTURE_KEY, maxRetries: 0 });
const other = new OpenAI({ baseURL, apiKey: process.env.GATEWAY_FIXTURE_OTHER_KEY, maxRetries: 0 });
const operation = (id) => ({ headers: { 'Idempotency-Key': 'typescript-' + id } });

test('models and SDK typed errors', async () => {
  assert.ok((await client.models.list()).data.some(m => m.id === 'auto'));
  assert.equal((await client.models.retrieve('text-embedding-3-small')).id, 'text-embedding-3-small');
  await assert.rejects(client.models.retrieve('missing'), e => e instanceof OpenAI.NotFoundError && e.code === 'model_not_found' && Boolean(e.requestID));
  const revoked = new OpenAI({ baseURL, apiKey: process.env.GATEWAY_FIXTURE_REVOKED_KEY, maxRetries: 0 });
  await assert.rejects(revoked.models.list(), e => e instanceof OpenAI.AuthenticationError && e.code === 'invalid_api_key');
});

test('default retries after lost response preserve one logical operation', async () => {
  let injected = false;
  const retrying = new OpenAI({ baseURL, apiKey: process.env.GATEWAY_FIXTURE_KEY,
    fetch: async (...args) => {
      const response = await fetch(...args);
      if (!injected && response.ok && String(args[0]).includes('/chat/completions')) {
        injected = true;
        await response.arrayBuffer();
        return new Response(JSON.stringify({ error: { message: 'fixture proxy loss', type: 'server_error', code: 'fixture_503', param: null } }),
          { status: 503, headers: { 'content-type': 'application/json' } });
      }
      return response;
    } });
  const body = { model: 'auto:fast', messages: [{ role: 'user', content: 'Say hello' }] };
  const result = await retrying.chat.completions.create(body, operation('retry'));
  const replay = await client.chat.completions.create(body, operation('retry'));
  assert.equal(result.id, replay.id);
  assert.equal(result.choices[0].message.content, ' hello world ');
  await assert.rejects(client.chat.completions.create({ ...body, temperature: 0 }, operation('retry')),
    e => e instanceof OpenAI.ConflictError && e.code === 'idempotency_conflict');
});

test('chat stream helper accumulates a measured final completion', async () => {
  const stream = client.chat.completions.stream({ model: 'auto:fast', messages: [{ role: 'user', content: 'Say hello' }],
    stream_options: { include_usage: true } }, operation('chat-helper'));
  const completion = await stream.finalChatCompletion();
  assert.equal(completion.choices[0].message.content, ' hello world ');
  assert.equal(completion.usage.total_tokens, 5);
});

test('live stream helper receives assistant role for every choice', async () => {
  for (const n of [2, 4]) {
    const stream = client.chat.completions.stream({ model: 'auto:fast', n,
      messages: [{ role: 'user', content: 'Say hello' }], stream_options: { include_usage: true } }, operation('choices-' + n));
    const final = await stream.finalChatCompletion();
    assert.equal(final.choices.length, n);
    for (const choice of final.choices) {
      assert.equal(choice.message.role, 'assistant');
      assert.equal(choice.message.content, ' hello world ');
    }
    assert.equal(final.usage.total_tokens, n * 5);
  }
});

test('responses defaults, storage ownership, history and stream helper', async () => {
  const body = { model: 'auto:fast', input: 'Say hello' };
  const stored = await client.responses.create(body, operation('response-store'));
  assert.equal(stored.store, true);
  assert.equal((await client.responses.retrieve(stored.id)).output_text, ' hello world ');
  await assert.rejects(other.responses.retrieve(stored.id), e => e instanceof OpenAI.NotFoundError);
  const next = await client.responses.create({ ...body, previous_response_id: stored.id }, operation('response-next'));
  assert.equal(next.previous_response_id, stored.id);
  const stream = client.responses.stream(body, operation('response-helper'));
  const final = await stream.finalResponse();
  assert.equal(final.status, 'completed');
  assert.equal(final.output_text, ' hello world ');
  assert.equal((await client.responses.delete(stored.id)).deleted, true);
  await assert.rejects(client.responses.retrieve(stored.id), e => e instanceof OpenAI.NotFoundError);
});

test('files multipart, auto pagination, byte download and isolation', async () => {
  const created = [];
  try {
    for (let i=0; i<3; i++) created.push(await client.files.create({ purpose: 'user_data', file: await toFile(Buffer.from('hello-' + i), 'sdk-' + i + '.txt') }));
    const ids = [];
    for await (const file of client.files.list({ limit: 1, order: 'asc' })) ids.push(file.id);
    assert.equal(new Set(ids).size, 3);
    for (const file of created) {
      assert.ok(ids.includes(file.id));
      assert.equal((await client.files.retrieve(file.id)).bytes, 7);
      assert.equal((await client.files.content(file.id)).status, 200);
      assert.equal((await (await client.files.content(file.id)).arrayBuffer()).byteLength, 7);
      await assert.rejects(other.files.content(file.id), e => e instanceof OpenAI.NotFoundError);
    }
  } finally {
    for (const file of created) assert.equal((await client.files.delete(file.id)).deleted, true);
  }
});

test('embedding float, explicit base64 and replay contract', async () => {
  for (const encoding_format of ['float', 'base64']) {
    const body = { model: 'text-embedding-3-small', input: ['one','two'], dimensions: 2, encoding_format };
    const result = await client.embeddings.create(body, operation('embedding-' + encoding_format));
    const replay = await client.embeddings.create(body, operation('embedding-' + encoding_format));
    assert.deepEqual(result, replay);
    assert.deepEqual(result.data.map(v => v.index), [0,1]);
    assert.equal(result.usage.total_tokens, 3);
    if (encoding_format === 'float') assert.equal(result.data[0].embedding.length, 2);
    else assert.equal(Buffer.from(result.data[0].embedding, 'base64').byteLength, 8);
  }
});

test('abort a stream and retry returns indeterminate without another execution', async () => {
  const body = { model: 'auto:fast', messages: [{ role: 'user', content: 'Say hello' }], stream: true };
  const stream = await client.chat.completions.create(body, operation('abort'));
  for await (const chunk of stream) {
    if (chunk.choices[0]?.delta?.content) { stream.controller.abort(); break; }
  }
  await new Promise(resolve => setTimeout(resolve, 100));
  await assert.rejects(client.chat.completions.create(body, operation('abort')),
    e => e instanceof OpenAI.ConflictError && e.code === 'request_outcome_unknown');
});
