# Runtime provenance

The compatible Read/Write/Edit/Glob/Grep interface and line slicing / exact
replacement approach were adapted from `tool_runtime.js` in
`https://github.com/HalilCan/headless-chatgpt`, local source
`/home/rahul/headless-chatgpt/tool_runtime.js`.

Source SHA-256: `2656231abfead752d494a9601a1ec1d8120525770651d448e858f34a196118ef`.
The source checkout has no `.git` directory, so no commit identity was available.
Its `package.json` identifies `headless-chatgpt` version `2.0.0`, author
`HalilCan`, and license `MIT`. No separate LICENSE file was present locally.

Only the standalone file-tool concepts/interface are retained. Browser/server
dependencies, HTTP endpoints, process-global fake agent/state implementations,
shell-interpolated ripgrep commands and unsandboxed execution are not included.
This runtime uses only Node built-ins and argv-based ripgrep. Python owns the
mandatory bubblewrap boundary and bounded evidence capture. Notebook cell edits,
atomic file writes, path restrictions and process isolation are implemented here.
This is not the official Claude Code product.
