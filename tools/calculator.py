from __future__ import annotations

import ast
import operator

MAX_EXPRESSION_LENGTH = 256
MAX_EXPONENT = 1000
MAX_BASE = 10000


def _safe_pow(base: float | int, exp: float | int) -> float | int:
    if abs(base) > MAX_BASE or abs(exp) > MAX_EXPONENT:
        raise ValueError("Calculation rejected: base or exponent exceeds safety limit")
    return operator.pow(base, exp)


SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: _safe_pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def calculate_expression(expression: str) -> str:
    if not expression or not isinstance(expression, str):
        return "Error: Expression must be a non-empty string."
    cleaned = expression.strip()
    if len(cleaned) > MAX_EXPRESSION_LENGTH:
        return f"Error: Expression exceeds maximum length of {MAX_EXPRESSION_LENGTH} characters."

    try:
        node = ast.parse(cleaned, mode="eval")
        result = _evaluate(node.body)
        # Format integer results without unnecessary decimal point
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return str(result)
    except ZeroDivisionError:
        return "Error: Division by zero is undefined."
    except OverflowError:
        return "Error: Result exceeded numeric representation limits."
    except (SyntaxError, ValueError, KeyError) as exc:
        return f"Error: {exc}"
    except Exception:
        return "Error: Invalid mathematical expression."


def _evaluate(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")
        return node.value
    if hasattr(ast, "Num") and isinstance(node, getattr(ast, "Num")):
        return getattr(node, "n")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in SAFE_OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        operator_fn = SAFE_OPERATORS[op_type]
        return operator_fn(left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in SAFE_OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        operand = _evaluate(node.operand)
        operator_fn = SAFE_OPERATORS[op_type]
        return operator_fn(operand)
    raise ValueError(f"Unsupported expression construct: {type(node).__name__}")
