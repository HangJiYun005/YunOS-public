const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');

function readHost(root) {
  const config = JSON.parse(fs.readFileSync(path.join(root, 'config_private', 'primary_host.json'), 'utf8'));
  if (config.schema_version !== 1 || config.role !== 'primary' ||
      config.host_name.toLowerCase() !== os.hostname().toLowerCase() ||
      path.resolve(config.project_root).toLowerCase() !== path.resolve(root).toLowerCase() ||
      config.web_host !== '127.0.0.1' || config.service_mode !== 'real-readonly' ||
      config.database_sync_enabled !== false || config.provider_enabled !== false ||
      !Number.isInteger(config.web_port) || config.web_port < 1024 || config.web_port > 65535) {
    throw new Error('本机主机配置不匹配，请检查主机部署配置。');
  }
  return { ...config, origin: `http://127.0.0.1:${config.web_port}` };
}

function validStatus(status, port) {
  return status?.service_mode === 'real-readonly' && status.readonly === true &&
    status.bound_host === '127.0.0.1' && status.bound_port === port &&
    status.can_execute === false && status.execution_authority_present === false &&
    Number.isInteger(status.process_id) && status.process_id > 0;
}

function probe(config) {
  return new Promise((resolve, reject) => {
    const req = http.get(`${config.origin}/api/status`, { timeout: 3000 }, (res) => {
      if (res.statusCode !== 200) {
        res.resume(); reject(new Error('服务端口已被其他程序使用。')); return;
      }
      let body = '';
      res.on('data', chunk => {
        body += chunk;
        if (body.length > 1024 * 1024) req.destroy(new Error('服务状态响应异常。'));
      });
      res.on('error', reject);
      res.on('end', () => {
        try {
          const status = JSON.parse(body);
          if (!validStatus(status, config.web_port)) throw new Error('该端口不是预期的 YunOS 主机服务。');
          resolve(status);
        } catch (error) { reject(error); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('检查本机服务超时。')));
    req.on('error', error => error.code === 'ECONNREFUSED' ? resolve(null) : reject(error));
  });
}

module.exports = { readHost, validStatus, probe };
