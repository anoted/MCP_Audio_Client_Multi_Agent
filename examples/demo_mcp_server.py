"""Tiny stdio MCP server for testing the registration UI and tool-call flow.

Register it in the web UI as:
    name:      demo
    transport: stdio
    command:   python examples/demo_mcp_server.py
"""
import ast
import operator
import random
from datetime import datetime

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError(f"Unsupported expression element: {ast.dump(node)}")


@mcp.tool()
def get_current_time() -> str:
    """Get the current local date and time."""
    return datetime.now().strftime("%A, %B %d %Y, %I:%M:%S %p")


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression, e.g. '3 * (4 + 5) ** 2'."""
    result = _eval(ast.parse(expression, mode="eval"))
    return str(result)


@mcp.tool()
def roll_dice(sides: int = 6, count: int = 1) -> str:
    """Roll `count` dice with `sides` sides each and return the results."""
    rolls = [random.randint(1, max(2, sides)) for _ in range(max(1, min(count, 20)))]
    return f"rolls={rolls} total={sum(rolls)}"


if __name__ == "__main__":
    mcp.run()  # stdio transport
