"""Public compatible tool contracts. These are local implementations, not Claude Code."""
import copy


def schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required),
            'additionalProperties': False}


S = {'type': 'string'}
I = {'type': 'integer'}
B = {'type': 'boolean'}
O = {'type': 'object'}
A = {'type': 'array'}
TOOLS = []


def tool(name, description, properties=None, required=()):
    TOOLS.append({'name': name, 'description': description,
                  'input_schema': schema(properties, required)})


tool('Read', 'Read bounded workspace text; offset is one-based.',
     {'file_path': S, 'offset': I, 'limit': I}, ('file_path',))
tool('Write', 'Atomically write a non-protected workspace file.',
     {'file_path': S, 'content': S}, ('file_path', 'content'))
tool('Edit', 'Replace exact text; ambiguity is rejected unless replace_all is true.',
     {'file_path': S, 'old_string': S, 'new_string': S, 'replace_all': B},
     ('file_path', 'old_string', 'new_string'))
tool('Glob', 'Find workspace paths with a glob, without shell interpolation.',
     {'pattern': S, 'path': S}, ('pattern',))
tool('Grep', 'Search workspace text with ripgrep regular expressions.',
     {'pattern': S, 'path': S, 'glob': S, 'output_mode': S, '-i': B, '-n': B,
      'head_limit': I, 'multiline': B}, ('pattern',))
tool('NotebookEdit', 'Edit an actual notebook cell, not a text placeholder.',
     {'notebook_path': S, 'cell_id': S, 'cell_index': I, 'cell_number': I, 'new_source': S,
      'cell_type': S, 'edit_mode': S}, ('notebook_path', 'new_source'))
tool('Bash', 'Run bounded shell in a private workspace/PID/network sandbox.',
     {'command': S, 'cwd': S, 'timeout': I, 'description': S}, ('command',))
tool('PowerShell', 'Run installed PowerShell in the same sandbox.',
     {'command': S, 'cwd': S}, ('command',))
tool('REPL', 'Execute bounded JavaScript in a disposable Node process.',
     {'code': S, 'cwd': S}, ('code',))
tool('Agent', 'Run a bounded child coding loop in a separate copy; never auto-apply its changes.',
     {'prompt': S, 'description': S, 'max_steps': I}, ('prompt',))
tool('Task', 'Legacy alias for the real Agent delegated run.',
     {'prompt': S, 'description': S, 'max_steps': I}, ('prompt',))
tool('TaskCreate', 'Create a durable task with validated dependencies.',
     {'title': S, 'subject': S, 'description': S, 'metadata': O, 'blockedBy': A})
for name in ('TaskGet', 'TaskOutput', 'TaskStop'):
    tool(name, 'Read or update a durable task record.', {'id': S}, ('id',))
tool('TaskList', 'List durable task records.')
tool('TaskUpdate', 'Update task status/metadata/dependencies; cycles are rejected.',
     {'id': S, 'status': S, 'title': S, 'subject': S, 'description': S,
      'metadata': O, 'addBlockedBy': A, 'addBlocks': A}, ('id',))
tool('TeamCreate', 'Create a durable logical team; does not start parallel workers.',
     {'name': S, 'agents': A}, ('name',))
tool('TeamDelete', 'Delete a logical team.', {'name': S, 'id': S})
tool('SendMessage', 'Deliver a durable message to a logical agent mailbox.',
     {'to': S, 'message': S, 'from': S}, ('to', 'message'))
tool('SendUserMessage', 'Record a user-facing message in run feedback.', {'message': S}, ('message',))
tool('TodoWrite', 'Replace durable to-do items.', {'todos': A}, ('todos',))
tool('AskUserQuestion', 'Pause the run until an owner answers a durable question.',
     {'question': S, 'questions': A, 'options': A})
tool('Brief', 'Send a short user-facing progress message.', {'message': S}, ('message',))
tool('Config', 'Read/update presentation preferences only; cannot change owner policies.',
     {'action': S, 'key': S, 'value': S})
for name in ('EnterPlanMode', 'ExitPlanMode'):
    tool(name, 'Toggle enforced read-only planning mode.')
tool('EnterWorktree', 'Create a real private Git worktree in the copied run workspace.', {'name': S})
tool('ExitWorktree', 'Commit and fast-forward adopt a private worktree; refuses unsafe merges.')
tool('CronCreate', 'Schedule a real sandbox command at loop boundaries; no background daemon.',
     {'command': S, 'schedule': S, 'interval_seconds': I, 'repeat': B}, ('command',))
tool('CronList', 'List persisted scheduled jobs.')
tool('CronDelete', 'Delete a persisted scheduled job.', {'id': S}, ('id',))
tool('LSP', 'Query a configured language server using real stdio LSP.',
     {'operation': S, 'action': S, 'file_path': S, 'line': I, 'character': I, 'query': S}, ('file_path',))
tool('ListMcpResourcesTool', 'List actual resources from owner-configured MCP servers.',
     {'server': S, 'cursor': S})
tool('list_mcp_tools', 'Discover tools from configured MCP servers.', {'server': S, 'cursor': S})
tool('read_mcp_resource', 'Read a resource from a configured MCP server.',
     {'server': S, 'uri': S}, ('server', 'uri'))
tool('call_mcp_tool', 'Call an explicitly owner-allowlisted MCP tool.',
     {'server': S, 'name': S, 'arguments': O}, ('server', 'name'))
tool('WebFetch', 'Fetch bounded public HTTP(S) content when owner network policy allows.',
     {'url': S, 'prompt': S}, ('url',))
tool('WebSearch', 'Perform an actual web search when owner network policy allows.', {'query': S}, ('query',))
tool('RemoteTrigger', 'Run an owner-configured argv handler with JSON payload on stdin.',
     {'name': S, 'payload': O}, ('name',))
tool('Skill', 'Load a bundled local-coder workflow instruction.', {'skill': S}, ('skill',))
tool('Sleep', 'Wait at most five seconds; not an unattended scheduler.', {'seconds': {'type': 'number'}})
tool('ToolSearch', 'Discover contracts and truthful capability status; use before unfamiliar tools.',
     {'query': S, 'limit': I}, ('query',))
tool('ReadEvidence', 'Read a bounded page of this run\'s retained output by evidence filename.',
     {'file': S, 'offset': I, 'limit': I}, ('file',))
tool('Verify', 'Run the fixed owner acceptance command; model cannot replace it.')
tool('Finish', 'Request independent audit; only harness can mark COMPLETE.')


def contracts():
    return copy.deepcopy(TOOLS)


def validate(name, args):
    contract = next((t for t in TOOLS if t['name'] == name), None)
    if contract is None: raise ValueError('Unknown tool')
    if not isinstance(args, dict): raise ValueError('Tool arguments must be an object')
    spec = contract['input_schema']
    if set(args) - spec['properties'].keys(): raise ValueError('Unknown tool argument')
    if set(spec['required']) - args.keys(): raise ValueError('Missing required tool argument')
    checks = {'string': lambda x: isinstance(x, str), 'integer': lambda x: type(x) is int,
              'number': lambda x: type(x) in (int, float), 'boolean': lambda x: type(x) is bool,
              'array': lambda x: isinstance(x, list), 'object': lambda x: isinstance(x, dict)}
    for key, value in args.items():
        if not checks[spec['properties'][key]['type']](value):
            raise ValueError('Invalid argument type: ' + key)
    return name
