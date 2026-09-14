"""只读演示服务，使用真实 MCP stdio；stdout 仅用于协议通信。"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('car-agent-demo')


@mcp.tool()
def add(a: int, b: int) -> str:
    """计算两个整数的和。"""
    return str(a + b)


@mcp.tool()
def get_version() -> str:
    """返回演示服务版本，不代表 Agent 或模型版本。"""
    return 'car-agent-demo 1.0'


if __name__ == '__main__':
    mcp.run(transport='stdio')
