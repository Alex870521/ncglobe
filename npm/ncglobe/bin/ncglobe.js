#!/usr/bin/env node
// ncglobe 的 npm 入口:找出這台電腦平台對應的執行檔套件(ncglobe-<平台>-<架構>),原樣轉交參數。
// 各平台的執行檔放在各自的套件裡(optionalDependencies + os/cpu),npm 只會下載符合的那一個。
'use strict';
const { spawn } = require('node:child_process');
const path = require('node:path');

const key = `${process.platform}-${process.arch}`;
let exe;
try {
  const dir = path.dirname(require.resolve(`ncglobe-${key}/package.json`));
  exe = path.join(dir, 'ncglobe', process.platform === 'win32' ? 'ncglobe.exe' : 'ncglobe');
} catch {
  console.error(`ncglobe has no build for ${key} (available: darwin-arm64, darwin-x64, win32-x64).\n` +
                `ncglobe 還沒有 ${key} 的執行檔;可以改用原始碼安裝:https://github.com/Alex870521/ncglobe#install`);
  process.exit(1);
}
const child = spawn(exe, process.argv.slice(2), { stdio: 'inherit' });
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => child.kill(sig));
child.on('error', (e) => { console.error(`無法啟動 ${exe}: ${e.message}`); process.exit(1); });
child.on('exit', (code, signal) => (signal ? process.kill(process.pid, signal) : process.exit(code ?? 0)));
