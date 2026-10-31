// Offline only: stdin is observed inline font/text; no browser or network access.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const crypto = require('node:crypto');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const sandbox = {window: {TextDecoder, TextEncoder}, console: {log() {}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, 'TyprMd5.js'), 'utf8'), sandbox, {timeout: 5000});
const table = JSON.parse(fs.readFileSync(path.join(__dirname, 'table.json'), 'utf8'));
const bytes = Buffer.from(input.font_base64, 'base64');
if (!bytes.length || bytes.length > 2000000) throw Error('invalid font size');
const font = sandbox.Typr.parse(Uint8Array.from(bytes).buffer)[0];
const mapping = {}, unresolved = [];
for (const ch of [...new Set(input.text)]) {
  const gid = sandbox.Typr.U.codeToGlyph(font, ch.codePointAt(0));
  if (!gid) continue; // Characters absent from this custom font use normal fallback.
  const shape = sandbox.Typr.U.glyphToPath(font, gid);
  const hash = crypto.createHash('md5').update(JSON.stringify(shape)).digest('hex');
  const codepoint = table[hash.slice(-8)];
  if (Number.isInteger(Number(codepoint)) && Number(codepoint) > 0 && Number(codepoint) <= 0x10ffff) {
    mapping[ch] = String.fromCodePoint(Number(codepoint));
  } else unresolved.push(ch);
}
process.stdout.write(JSON.stringify({mapping, unresolved, font_sha256: crypto.createHash('sha256').update(bytes).digest('hex')}));
