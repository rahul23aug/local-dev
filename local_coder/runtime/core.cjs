"use strict";
// Compatible file-tool adaptation: see PROVENANCE.md.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const ROOT = '/workspace';
const MAX_FILE = 256 * 1024;
const MAX_TEXT = 6000;

function safePath(value) {
  if (typeof value !== 'string' || !value) throw Error('A workspace path is required');
  const parts = value.split('/');
  if (parts.some(p => p === '..' || p === 'node_modules' || p === '__pycache__' || (p.startsWith('.') && p !== '.'))) throw Error('Forbidden path');
  const target = path.resolve(ROOT, value);
  if (target !== ROOT && !target.startsWith(ROOT + '/')) throw Error('Path escapes workspace');
  let current = ROOT;
  for (const part of path.relative(ROOT, target).split('/').filter(Boolean)) {
    current = path.join(current, part);
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) throw Error('Symlinks are forbidden');
  }
  return target;
}

function read(target) {
  const fd = fs.openSync(target, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile()) throw Error('Not a regular file');
    if (info.size > MAX_FILE) throw Error('File exceeds 256 KiB limit');
    return fs.readFileSync(fd, 'utf8');
  } finally { fs.closeSync(fd); }
}

function atomicWrite(target, content, protectedFiles) {
  const relative = path.relative(ROOT, target);
  if (protectedFiles.includes(relative)) throw Error('Acceptance files are read-only');
  if (typeof content !== 'string') throw Error('content must be a string');
  if (Buffer.byteLength(content) > MAX_FILE) throw Error('Result exceeds 256 KiB limit');
  if (fs.existsSync(target) && fs.statSync(target).size > MAX_FILE) throw Error('File exceeds 256 KiB limit');
  fs.mkdirSync(path.dirname(target), {recursive: true});
  safePath(relative);
  const temporary = path.join(path.dirname(target), '.native-' + crypto.randomUUID());
  try {
    const fd = fs.openSync(temporary, 'wx', fs.existsSync(target) ? fs.statSync(target).mode & 0o777 : 0o644);
    try { fs.writeFileSync(fd, content); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    safePath(relative);
    fs.renameSync(temporary, target);
  } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  return {changed: relative, file_path: relative, sha256: crypto.createHash('sha256').update(content).digest('hex')};
}

function search(name, args) {
  const cwd = safePath(args.path || '.');
  if (typeof args.pattern !== 'string' || !args.pattern || args.pattern.length > 4096) throw Error('pattern must be a nonempty string, at most 4096 characters');
  const options = ['--color', 'never', '--sort', 'path', '--glob', '!**/.*', '--glob', '!**/node_modules/**', '--glob', '!**/__pycache__/**'];
  const mode = args.output_mode === undefined ? 'content' : args.output_mode;
  let headLimit = 300;
  if (name === 'Glob') options.push('--files', '--glob', args.pattern);
  else {
    if (!['content', 'files_with_matches', 'count'].includes(mode)) throw Error('Invalid output_mode');
    for (const option of ['-i', '-n', 'multiline']) {
      if (args[option] !== undefined && typeof args[option] !== 'boolean') throw Error(option + ' must be boolean');
    }
    if (args.head_limit !== undefined) {
      if (!Number.isInteger(args.head_limit) || args.head_limit < 0) throw Error('head_limit must be a nonnegative integer');
      headLimit = args.head_limit === 0 ? 300 : Math.min(300, args.head_limit);
    }
    options.push('--max-filesize', String(MAX_FILE));
    if (args['-i']) options.push('--ignore-case');
    if (args.multiline) options.push('--multiline', '--multiline-dotall');
    if (mode === 'files_with_matches') options.push('--files-with-matches');
    else if (mode === 'count') options.push('--count');
    else options.push('--json', '--max-count', '301');
    if (args.glob !== undefined) {
      if (typeof args.glob !== 'string') throw Error('glob must be a string');
      options.push('--glob', args.glob);
    }
    options.push('--regexp', args.pattern);
  }
  options.push('--', cwd);
  const child = spawnSync('rg', options, {encoding: 'utf8', timeout: 5000, maxBuffer: 1024 * 1024, shell: false});
  if (child.error) { child.error.infrastructure = child.error.code === 'ENOENT'; throw child.error; }
  if (![0, 1].includes(child.status)) throw Error((child.stderr || 'Search failed').slice(0, MAX_TEXT));
  const raw = (child.stdout || '').trimEnd().split('\n').filter(Boolean);
  let lines = [];
  let counts = [];
  const relative = filename => path.relative(ROOT, safePath(filename));
  for (const line of raw) {
    try {
      if (name === 'Glob' || mode === 'files_with_matches') lines.push(relative(line));
      else if (mode === 'count') {
        const match = line.match(/^(.*):(\d+)$/);
        if (!match) continue;
        const file_path = relative(match[1]);
        const count = Number(match[2]);
        counts.push({file_path, count});
        lines.push(file_path + ':' + count);
      } else {
        const event = JSON.parse(line);
        if (event.type !== 'match' || !event.data.path.text || event.data.lines.text === undefined) continue;
        const filename = relative(event.data.path.text);
        const text = event.data.lines.text.replace(/\r?\n$/, '');
        const prefix = args['-n'] === false ? filename + ':' : filename + ':' + event.data.line_number + ':';
        lines.push(prefix + text);
      }
    } catch (_) { /* Reject inaccessible paths and non-text/binary events. */ }
  }
  let truncated = lines.length > headLimit || Buffer.byteLength(lines.join('\n')) > MAX_TEXT;
  lines = lines.slice(0, headLimit);
  let budget = MAX_TEXT;
  const bounded = [];
  for (const line of lines) {
    const length = Buffer.byteLength(line) + 1;
    if (length > budget) { truncated = true; break; }
    bounded.push(line);
    budget -= length;
  }
  lines = bounded;
  if (name === 'Glob') return {files: lines, truncated};
  const result = {matches: lines, content: lines.join('\n'), truncated, output_mode: mode};
  if (mode === 'files_with_matches') result.files = lines;
  if (mode === 'count') result.counts = counts.slice(0, lines.length);
  return result;
}

function notebook(args, target, protectedFiles) {
  const document = JSON.parse(read(target));
  if (!document || !Array.isArray(document.cells) || document.nbformat !== 4) throw Error('Expected a version 4 notebook');
  const mode = args.edit_mode || 'replace';
  if (!['replace', 'insert', 'delete'].includes(mode)) throw Error('Invalid edit_mode');
  if (args.cell_id !== undefined && args.cell_number !== undefined) throw Error('Choose cell_id or cell_number');
  let index;
  if (args.cell_id !== undefined) {
    const found = document.cells.map((cell, i) => cell.id === args.cell_id ? i : -1).filter(i => i >= 0);
    if (found.length !== 1) throw Error('cell_id must match exactly one cell');
    index = found[0];
    if (mode === 'insert') index += 1;
  } else index = args.cell_number === undefined && mode === 'insert' ? document.cells.length : args.cell_number;
  if (!Number.isInteger(index) || index < 0 || index >= document.cells.length + (mode === 'insert' ? 1 : 0)) throw Error('Invalid zero-based cell_number');
  if (mode === 'delete') document.cells.splice(index, 1);
  else {
    if (typeof args.new_source !== 'string') throw Error('new_source must be a string');
    const type = args.cell_type || (mode === 'replace' ? document.cells[index].cell_type : 'code');
    if (!['code', 'markdown', 'raw'].includes(type)) throw Error('Invalid cell_type');
    const cell = mode === 'replace' ? {...document.cells[index]} : {id: crypto.randomUUID(), metadata: {}};
    cell.cell_type = type;
    cell.source = args.new_source.match(/[^\n]*\n|[^\n]+$/g) || [];
    if (type === 'code') { cell.outputs = []; cell.execution_count = null; }
    else { delete cell.outputs; delete cell.execution_count; }
    if (mode === 'insert') document.cells.splice(index, 0, cell);
    else document.cells[index] = cell;
  }
  return {...atomicWrite(target, JSON.stringify(document, null, 2) + '\n', protectedFiles), cell_number: index, edit_mode: mode};
}

function execute(name, args, protectedFiles = []) {
  if (name === 'Glob' || name === 'Grep') return search(name, args);
  const target = safePath(args.file_path);
  const relative = path.relative(ROOT, target);
  if (name === 'Read') {
    const offset = args.offset === undefined ? 1 : args.offset;
    const limit = args.limit === undefined ? 80 : args.limit;
    if (!Number.isInteger(offset) || offset < 1 || !Number.isInteger(limit) || limit < 1 || limit > 1000) throw Error('Invalid offset or limit');
    const lines = read(target).split(/\r?\n/);
    if (lines[lines.length - 1] === '') lines.pop();
    const text = lines.slice(offset - 1, offset - 1 + limit).join('\n');
    const content = Buffer.from(text).subarray(0, MAX_TEXT).toString('utf8');
    return {file_path: relative, content, offset, total_lines: lines.length, truncated: Buffer.byteLength(text) > MAX_TEXT || offset - 1 + limit < lines.length};
  }
  if (name === 'Write') return atomicWrite(target, args.content, protectedFiles);
  if (name === 'Edit') {
    if (typeof args.old_string !== 'string' || !args.old_string || typeof args.new_string !== 'string') throw Error('Edit requires nonempty old_string and string new_string');
    if (args.replace_all !== undefined && typeof args.replace_all !== 'boolean') throw Error('replace_all must be boolean');
    const before = read(target);
    const count = before.split(args.old_string).length - 1;
    if (!count || (!args.replace_all && count !== 1)) throw Error('old_string must match exactly once unless replace_all');
    const after = args.replace_all ? before.split(args.old_string).join(args.new_string) : before.replace(args.old_string, () => args.new_string);
    return {...atomicWrite(target, after, protectedFiles), replacements: args.replace_all ? count : 1};
  }
  if (name === 'NotebookEdit') return notebook(args, target, protectedFiles);
  throw Error('Unknown native tool');
}

module.exports = {execute};
