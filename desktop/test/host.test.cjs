const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { validStatus, probe } = require('../host.cjs');
const ready = { service_mode: 'real-readonly', readonly: true, bound_host: '127.0.0.1', bound_port: 18790, can_execute: false, execution_authority_present: false, process_id: 123 };
test('accept only the intended read-only service', () => {
  assert.equal(validStatus(ready, 18790), true);
  for (const change of [{ readonly: false }, { can_execute: true }, { execution_authority_present: true }, { bound_port: 8765 }, { service_mode: 'runtime-admin' }]) {
    assert.equal(validStatus({ ...ready, ...change }, 18790), false);
  }
});
test('do not treat another HTTP service as a stopped host', async () => {
  const server = http.createServer((_req, res) => { res.writeHead(404); res.end(); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await assert.rejects(probe({ origin: `http://127.0.0.1:${server.address().port}` }), /其他程序/); }
  finally { await new Promise(resolve => server.close(resolve)); }
});
