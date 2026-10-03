"use strict";
const fs = require('node:fs');
const {execute} = require('./core.cjs');
try {
  const request = JSON.parse(fs.readFileSync(0, 'utf8'));
  process.stdout.write(JSON.stringify(execute(request.name, request.args, request.protected)));
} catch (error) {
  process.stdout.write(JSON.stringify({error: String(error.message), infrastructure: !!error.infrastructure}));
}
