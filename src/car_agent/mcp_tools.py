"""真实 stdio MCP：连接发现、动态分发及宿主策略；导入不启动进程。"""
import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sys
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def external_name(server, tool):
    name = 'mcp__' + re.sub(r'[^a-zA-Z0-9_-]', '_', server) + '__' + re.sub(r'[^a-zA-Z0-9_-]', '_', tool)
    if len(name) > 64:
        raise ValueError('MCP 工具名超过 64 字符。')
    return name


class StdioConnection:
    """每个连接由一个线程内的同一个 async task 管理，避免跨 task 关闭 SDK 上下文。"""

    def __init__(self, params, timeout=30):
        self.params, self.timeout = params, timeout
        self.ready = Future()
        self.loop = self.queue = self.task = None
        self.thread = threading.Thread(target=self._thread_main, daemon=True, name='mcp-stdio')
        self.thread.start()
        try:
            self.tools = self.ready.result(timeout=timeout + 5)
        except BaseException:
            self.close()
            raise

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError(f'MCP 连接失败：{type(exc).__name__}'))

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        self.queue = asyncio.Queue()
        active = None
        try:
            # stderr 写到普通临时文件，避免后台 server 的日志破坏交互提示符或进入协议 stdout。
            import tempfile
            with tempfile.TemporaryFile(mode='w+') as errors:
                async with stdio_client(self.params, errlog=errors) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                        async with asyncio.timeout(self.timeout):
                            await session.initialize()
                            tools, cursor, seen = [], None, set()
                            while True:
                                page = await session.list_tools(cursor=cursor)
                                tools.extend(tool.model_dump(by_alias=True, exclude_none=True) for tool in page.tools)
                                if len(tools) > 128:
                                    raise ValueError('单个 MCP 服务最多允许 128 个工具。')
                                cursor = page.nextCursor
                                if not cursor:
                                    break
                                if cursor in seen:
                                    raise ValueError('MCP 工具分页游标重复。')
                                seen.add(cursor)
                        self.ready.set_result(tools)
                        while True:
                            request = await self.queue.get()
                            if request is None:
                                return
                            name, args, active = request
                            try:
                                async with asyncio.timeout(self.timeout):
                                    result = await session.call_tool(name, arguments=args)
                                if not active.done():
                                    active.set_result(result)
                            except BaseException as exc:
                                if not active.done():
                                    active.set_exception(RuntimeError(f'MCP 调用失败：{type(exc).__name__}；执行状态可能未知，请勿盲目重试写操作。'))
                                if not isinstance(exc, Exception):
                                    raise
                            finally:
                                active = None
        finally:
            error = RuntimeError('MCP 连接已关闭；未确认的操作执行状态未知。')
            if active is not None and not active.done():
                active.set_exception(error)
            while not self.queue.empty():
                request = self.queue.get_nowait()
                if request is not None and not request[2].done():
                    request[2].set_exception(error)

    def call(self, name, args):
        if not self.thread.is_alive() or self.loop is None or self.loop.is_closed():
            raise RuntimeError('MCP 连接已断开，请重新 connect_mcp。')
        future = Future()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (name, args, future))
        try:
            return future.result(timeout=self.timeout + 5)
        except (FutureTimeout, KeyboardInterrupt):
            self.close()
            raise

    def close(self):
        if self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
            except RuntimeError:
                pass
        self.thread.join(timeout=1)
        if self.thread.is_alive() and self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass
            self.thread.join(timeout=5)


def result_text(result):
    parts = []
    for block in result.content:
        if block.type == 'text':
            parts.append(block.text)
        elif block.type == 'resource' and getattr(block.resource, 'text', None) is not None:
            parts.append(block.resource.text)
        else:
            parts.append(f'[当前仅支持文本 MCP 结果，已省略 {block.type} 内容]')
    if getattr(result, 'structuredContent', None) is not None:
        parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
    output = '\n'.join(parts).strip() or '(MCP 工具返回空结果)'
    if result.isError:
        raise RuntimeError(output)
    return output


class MCPManager:
    def __init__(self, path):
        self.path = Path(path)
        self.servers = self._load()
        self.connections = {}
        self.origins = {}
        self.definitions = {}
        self.lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) != {'servers'} or not isinstance(data['servers'], dict):
            raise ValueError('mcp_servers.json 必须包含 servers 字典。')
        if len(data['servers']) > 8:
            raise ValueError('最多配置 8 个 MCP 服务。')
        for name, entry in data['servers'].items():
            if not name or not isinstance(entry, dict) or not isinstance(entry.get('command'), str) or not entry['command']:
                raise ValueError('MCP 服务名和 command 必须非空。')
            for key in ('args', 'env_vars'):
                values = entry.get(key, [])
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ValueError(f'MCP {key} 必须为字符串数组。')
            policies = entry.get('tool_policy', {})
            if not isinstance(policies, dict) or any(v not in ('allow', 'confirm', 'deny') for v in policies.values()):
                raise ValueError('MCP tool_policy 必须为 allow/confirm/deny 映射。')
            if entry.get('connect_policy', 'confirm') not in ('allow', 'confirm', 'deny'):
                raise ValueError('无效 connect_policy。')
        return data['servers']

    def catalog(self):
        return ', '.join(self.servers) or '无（尚未配置 mcp_servers.json）'

    def policy(self, name, args):
        if name == 'connect_mcp':
            entry = self.servers.get(args.get('name'))
            return entry.get('connect_policy', 'confirm') if entry else 'deny'
        origin = self.origins.get(name)
        if not origin:
            return 'deny'
        server, raw = origin
        return self.servers[server].get('tool_policy', {}).get(raw, 'confirm')

    def approval_target(self, name, args):
        if name == 'connect_mcp':
            entry = self.servers.get(args.get('name'), {})
            return {'server': args.get('name'), 'command': entry.get('command'), 'args': entry.get('args', [])}
        return args

    def connect(self, name):
        with self.lock:
            if name not in self.servers:
                raise ValueError(f'未知 MCP 服务；可用服务：{self.catalog()}')
            existing = self.connections.get(name)
            if existing and existing.thread.is_alive():
                return f'MCP 服务 {name} 已连接。'
            if existing:
                existing.close()
                self._remove(name)
            entry = self.servers[name]
            command = sys.executable if entry['command'] == '${PYTHON}' else entry['command']
            params = StdioServerParameters(command=command, args=entry.get('args', []),
                                          cwd=str(self.path.parent),
                                          env={key: os.environ[key] for key in entry.get('env_vars', []) if key in os.environ})
            connection = StdioConnection(params)
            try:
                definitions, origins = {}, {}
                for tool in connection.tools:
                    raw = tool['name']
                    prefixed = external_name(name, raw)
                    if prefixed in self.origins or prefixed in origins:
                        raise ValueError('MCP 工具名规范化后冲突。')
                    schema = tool.get('inputSchema', {})
                    if schema.get('type') != 'object':
                        raise ValueError('MCP 工具输入必须是 object schema。')
                    origins[prefixed] = (name, raw)
                    definitions[prefixed] = {'name': prefixed, 'description': tool.get('description', ''), 'input_schema': schema}
                self.connections[name] = connection
                self.origins.update(origins)
                self.definitions.update(definitions)
            except BaseException:
                connection.close()
                raise
            return f'MCP 服务 {name} 已连接；下一轮可用工具：' + (', '.join(definitions) or '无')

    def _remove(self, server):
        self.connections.pop(server, None)
        for name, origin in list(self.origins.items()):
            if origin[0] == server:
                del self.origins[name]
                del self.definitions[name]

    def call(self, name, args):
        server, raw = self.origins[name]
        return result_text(self.connections[server].call(raw, args))

    def handler(self, name):
        def invoke(**args):
            return self.call(name, args)
        return invoke

    def assemble(self, schemas, handlers):
        with self.lock:
            # 每轮从基础池重新组装，连接后的工具不累积重复。
            tools, dispatch = deepcopy(schemas), dict(handlers)
            names = {tool['name'] for tool in tools} | set(dispatch)
            for name, schema in self.definitions.items():
                if name in names:
                    raise ValueError('MCP 工具名与已有工具冲突。')
                tools.append(deepcopy(schema))
                dispatch[name] = self.handler(name)
            return tools, dispatch

    def close(self):
        with self.lock:
            for connection in self.connections.values():
                connection.close()
            self.connections.clear()
            self.origins.clear()
            self.definitions.clear()


MANAGER = None
CONNECT_TOOL = {'name': 'connect_mcp', 'description': '按宿主配置的名称连接本地 MCP 服务并发现工具；下一轮才能使用新工具。',
                'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name'], 'additionalProperties': False}}


def connect_mcp(name):
    if MANAGER is None:
        raise RuntimeError('MCP 管理器未启动。')
    return MANAGER.connect(name)
