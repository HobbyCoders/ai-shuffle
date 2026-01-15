"""
jq expression parser.

Supports a subset of jq syntax commonly used in Claude Code plugin hooks:
- Field access: .field, .a.b.c
- Array indexing: .[0], .[-1]
- Array iteration: .[]
- Pipes: . | .field
- Object construction: {a: .b, c: $var}
- String interpolation: "prefix \\(.field) suffix"
- Default values: .field // "default"
- Function calls: select(...), map(...)
- Variables: $name
- Comparisons: ==, !=, <, >, <=, >=
- Logical operators: and, or, not
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Union
from enum import Enum
import re


class NodeType(Enum):
    """AST node types."""

    IDENTITY = "identity"  # .
    FIELD = "field"  # .field
    INDEX = "index"  # .[0]
    ITERATE = "iterate"  # .[]
    PIPE = "pipe"  # a | b
    DEFAULT = "default"  # a // b
    OBJECT = "object"  # {a: b}
    ARRAY = "array"  # [a, b]
    STRING = "string"  # "text" or interpolated
    NUMBER = "number"  # 123
    BOOL = "bool"  # true/false
    NULL = "null"  # null
    VARIABLE = "variable"  # $name
    FUNCTION = "function"  # select(), map()
    COMPARISON = "comparison"  # ==, !=, <, >, <=, >=
    LOGICAL = "logical"  # and, or, not
    NEGATE = "negate"  # -expr


@dataclass
class ASTNode:
    """AST node representation."""

    type: NodeType
    value: Any = None
    children: List["ASTNode"] = field(default_factory=list)

    def __repr__(self) -> str:
        if self.children:
            return f"ASTNode({self.type.value}, {self.value!r}, {self.children})"
        return f"ASTNode({self.type.value}, {self.value!r})"


class ParseError(Exception):
    """Raised when parsing fails."""

    pass


class JQParser:
    """
    Recursive descent parser for jq expressions.

    Grammar (simplified):
        expr       -> pipe
        pipe       -> default ("|" default)*
        default    -> logical ("//" logical)*
        logical    -> comparison (("and" | "or") comparison)*
        comparison -> term (("==" | "!=" | "<" | ">" | "<=" | ">=") term)*
        term       -> factor | "not" term | "-" term
        factor     -> primary postfix*
        postfix    -> "." IDENT | "[" expr "]" | "[]"
        primary    -> "." | "." IDENT | NUMBER | STRING | "true" | "false" | "null"
                    | "$" IDENT | "{" object_pairs "}" | "[" array_items "]"
                    | IDENT "(" args ")" | "(" expr ")"
    """

    def __init__(self, expression: str):
        self.expr = expression
        self.pos = 0
        self.length = len(expression)

    def parse(self) -> ASTNode:
        """Parse the full expression."""
        self._skip_whitespace()
        if self.pos >= self.length:
            return ASTNode(NodeType.IDENTITY)
        result = self._parse_pipe()
        self._skip_whitespace()
        if self.pos < self.length:
            raise ParseError(
                f"Unexpected character at position {self.pos}: {self.expr[self.pos]!r}"
            )
        return result

    def _skip_whitespace(self) -> None:
        """Skip whitespace characters."""
        while self.pos < self.length and self.expr[self.pos] in " \t\n\r":
            self.pos += 1

    def _peek(self, offset: int = 0) -> str:
        """Peek at current character without consuming."""
        pos = self.pos + offset
        if pos < self.length:
            return self.expr[pos]
        return ""

    def _consume(self, expected: Optional[str] = None) -> str:
        """Consume and return current character."""
        if self.pos >= self.length:
            if expected:
                raise ParseError(f"Expected {expected!r} but reached end of input")
            return ""
        char = self.expr[self.pos]
        if expected and char != expected:
            raise ParseError(
                f"Expected {expected!r} but got {char!r} at position {self.pos}"
            )
        self.pos += 1
        return char

    def _match(self, s: str) -> bool:
        """Check if current position matches string."""
        return self.expr[self.pos : self.pos + len(s)] == s

    def _consume_string(self, s: str) -> None:
        """Consume expected string."""
        if not self._match(s):
            raise ParseError(f"Expected {s!r} at position {self.pos}")
        self.pos += len(s)

    def _parse_pipe(self) -> ASTNode:
        """Parse pipe expression: a | b | c"""
        left = self._parse_default()

        while True:
            self._skip_whitespace()
            if self._peek() == "|" and self._peek(1) != "|":  # Not || (future: or)
                self._consume()
                self._skip_whitespace()
                right = self._parse_default()
                left = ASTNode(NodeType.PIPE, children=[left, right])
            else:
                break

        return left

    def _parse_default(self) -> ASTNode:
        """Parse default expression: a // b"""
        left = self._parse_logical()

        while True:
            self._skip_whitespace()
            if self._match("//"):
                self._consume_string("//")
                self._skip_whitespace()
                right = self._parse_logical()
                left = ASTNode(NodeType.DEFAULT, children=[left, right])
            else:
                break

        return left

    def _parse_logical(self) -> ASTNode:
        """Parse logical operators: and, or"""
        left = self._parse_comparison()

        while True:
            self._skip_whitespace()
            if self._match("and") and not self._is_identifier_char(
                self._peek(3)
            ):
                self._consume_string("and")
                self._skip_whitespace()
                right = self._parse_comparison()
                left = ASTNode(NodeType.LOGICAL, "and", [left, right])
            elif self._match("or") and not self._is_identifier_char(
                self._peek(2)
            ):
                self._consume_string("or")
                self._skip_whitespace()
                right = self._parse_comparison()
                left = ASTNode(NodeType.LOGICAL, "or", [left, right])
            else:
                break

        return left

    def _parse_comparison(self) -> ASTNode:
        """Parse comparison operators: ==, !=, <, >, <=, >="""
        left = self._parse_term()

        self._skip_whitespace()
        op = None
        if self._match("=="):
            op = "=="
            self._consume_string("==")
        elif self._match("!="):
            op = "!="
            self._consume_string("!=")
        elif self._match("<="):
            op = "<="
            self._consume_string("<=")
        elif self._match(">="):
            op = ">="
            self._consume_string(">=")
        elif self._peek() == "<":
            op = "<"
            self._consume()
        elif self._peek() == ">":
            op = ">"
            self._consume()

        if op:
            self._skip_whitespace()
            right = self._parse_term()
            return ASTNode(NodeType.COMPARISON, op, [left, right])

        return left

    def _parse_term(self) -> ASTNode:
        """Parse term with unary operators."""
        self._skip_whitespace()

        # Handle 'not' operator
        if self._match("not") and not self._is_identifier_char(self._peek(3)):
            self._consume_string("not")
            self._skip_whitespace()
            operand = self._parse_term()
            return ASTNode(NodeType.LOGICAL, "not", [operand])

        # Handle unary minus
        if self._peek() == "-" and self._peek(1) not in "0123456789.":
            self._consume()
            self._skip_whitespace()
            operand = self._parse_term()
            return ASTNode(NodeType.NEGATE, children=[operand])

        return self._parse_factor()

    def _parse_factor(self) -> ASTNode:
        """Parse factor with postfix operators."""
        node = self._parse_primary()
        return self._parse_postfix(node)

    def _parse_postfix(self, node: ASTNode) -> ASTNode:
        """Parse postfix operations: .field, [n], []"""
        while True:
            self._skip_whitespace()
            if self._peek() == ".":
                # Check for .field access
                if self._is_identifier_start(self._peek(1)):
                    self._consume()
                    field_name = self._parse_identifier()
                    node = ASTNode(NodeType.PIPE, children=[node, ASTNode(NodeType.FIELD, field_name)])
                else:
                    break
            elif self._peek() == "[":
                self._consume()
                self._skip_whitespace()
                if self._peek() == "]":
                    # [] - iterate
                    self._consume()
                    node = ASTNode(NodeType.PIPE, children=[node, ASTNode(NodeType.ITERATE)])
                else:
                    # [expr] - index or slice
                    index_expr = self._parse_pipe()
                    self._skip_whitespace()
                    self._consume("]")
                    node = ASTNode(NodeType.PIPE, children=[node, ASTNode(NodeType.INDEX, children=[index_expr])])
            else:
                break

        return node

    def _parse_primary(self) -> ASTNode:
        """Parse primary expression."""
        self._skip_whitespace()

        char = self._peek()

        # Identity or field access
        if char == ".":
            self._consume()
            if self._is_identifier_start(self._peek()):
                field_name = self._parse_identifier()
                return ASTNode(NodeType.FIELD, field_name)
            elif self._peek() == "[":
                # .[...] or .[]
                self._consume()
                self._skip_whitespace()
                if self._peek() == "]":
                    self._consume()
                    return ASTNode(NodeType.ITERATE)
                else:
                    index_expr = self._parse_pipe()
                    self._skip_whitespace()
                    self._consume("]")
                    return ASTNode(NodeType.INDEX, children=[index_expr])
            else:
                return ASTNode(NodeType.IDENTITY)

        # Number (including negative)
        if char == "-" or char.isdigit():
            return self._parse_number()

        # String
        if char == '"':
            return self._parse_string()

        # Variable
        if char == "$":
            self._consume()
            var_name = self._parse_identifier()
            return ASTNode(NodeType.VARIABLE, var_name)

        # Object construction
        if char == "{":
            return self._parse_object()

        # Array construction
        if char == "[":
            return self._parse_array()

        # Parenthesized expression
        if char == "(":
            self._consume()
            self._skip_whitespace()
            expr = self._parse_pipe()
            self._skip_whitespace()
            self._consume(")")
            return expr

        # Keywords and functions
        if self._is_identifier_start(char):
            name = self._parse_identifier()

            # Boolean literals
            if name == "true":
                return ASTNode(NodeType.BOOL, True)
            if name == "false":
                return ASTNode(NodeType.BOOL, False)
            if name == "null":
                return ASTNode(NodeType.NULL)
            if name == "empty":
                return ASTNode(NodeType.FUNCTION, "empty")

            # Function call
            self._skip_whitespace()
            if self._peek() == "(":
                self._consume()
                self._skip_whitespace()
                args = []
                if self._peek() != ")":
                    args.append(self._parse_pipe())
                    while self._peek() == ";":
                        self._consume()
                        self._skip_whitespace()
                        args.append(self._parse_pipe())
                self._skip_whitespace()
                self._consume(")")
                return ASTNode(NodeType.FUNCTION, name, args)

            # Bare function (no parens)
            return ASTNode(NodeType.FUNCTION, name)

        raise ParseError(f"Unexpected character {char!r} at position {self.pos}")

    def _parse_number(self) -> ASTNode:
        """Parse a number literal."""
        start = self.pos
        if self._peek() == "-":
            self._consume()
        while self._peek().isdigit():
            self._consume()
        if self._peek() == ".":
            self._consume()
            while self._peek().isdigit():
                self._consume()
        if self._peek() in "eE":
            self._consume()
            if self._peek() in "+-":
                self._consume()
            while self._peek().isdigit():
                self._consume()

        num_str = self.expr[start : self.pos]
        try:
            if "." in num_str or "e" in num_str.lower():
                return ASTNode(NodeType.NUMBER, float(num_str))
            else:
                return ASTNode(NodeType.NUMBER, int(num_str))
        except ValueError:
            raise ParseError(f"Invalid number: {num_str}")

    def _parse_string(self) -> ASTNode:
        """Parse a string literal with interpolation support."""
        self._consume('"')
        parts: List[Union[str, ASTNode]] = []
        current_str = ""

        while self.pos < self.length and self._peek() != '"':
            char = self._peek()

            if char == "\\":
                self._consume()
                next_char = self._consume()
                if next_char == "(":
                    # String interpolation: \(expr)
                    if current_str:
                        parts.append(current_str)
                        current_str = ""
                    expr = self._parse_pipe()
                    self._skip_whitespace()
                    self._consume(")")
                    parts.append(expr)
                elif next_char == "n":
                    current_str += "\n"
                elif next_char == "r":
                    current_str += "\r"
                elif next_char == "t":
                    current_str += "\t"
                elif next_char == "\\":
                    current_str += "\\"
                elif next_char == '"':
                    current_str += '"'
                else:
                    current_str += next_char
            else:
                current_str += self._consume()

        self._consume('"')

        if current_str:
            parts.append(current_str)

        # If no interpolation, return simple string
        if len(parts) == 0:
            return ASTNode(NodeType.STRING, "")
        if len(parts) == 1 and isinstance(parts[0], str):
            return ASTNode(NodeType.STRING, parts[0])

        # Return string with interpolation parts
        return ASTNode(NodeType.STRING, parts)

    def _parse_object(self) -> ASTNode:
        """Parse object construction: {a: b, c: d}"""
        self._consume("{")
        self._skip_whitespace()

        pairs: List[tuple] = []

        if self._peek() != "}":
            pairs.append(self._parse_object_pair())

            while True:
                self._skip_whitespace()
                if self._peek() == ",":
                    self._consume()
                    self._skip_whitespace()
                    if self._peek() == "}":
                        break
                    pairs.append(self._parse_object_pair())
                else:
                    break

        self._skip_whitespace()
        self._consume("}")

        return ASTNode(NodeType.OBJECT, pairs)

    def _parse_object_pair(self) -> tuple:
        """Parse a single object key-value pair."""
        self._skip_whitespace()

        # Key can be identifier or string or parenthesized expression
        if self._peek() == '"':
            key_node = self._parse_string()
            key = key_node.value if isinstance(key_node.value, str) else key_node
        elif self._peek() == "(":
            self._consume()
            self._skip_whitespace()
            key = self._parse_pipe()
            self._skip_whitespace()
            self._consume(")")
        else:
            key = self._parse_identifier()

        self._skip_whitespace()
        self._consume(":")
        self._skip_whitespace()

        value = self._parse_pipe()

        return (key, value)

    def _parse_array(self) -> ASTNode:
        """Parse array construction: [a, b, c]"""
        self._consume("[")
        self._skip_whitespace()

        items: List[ASTNode] = []

        if self._peek() != "]":
            items.append(self._parse_pipe())

            while True:
                self._skip_whitespace()
                if self._peek() == ",":
                    self._consume()
                    self._skip_whitespace()
                    if self._peek() == "]":
                        break
                    items.append(self._parse_pipe())
                else:
                    break

        self._skip_whitespace()
        self._consume("]")

        return ASTNode(NodeType.ARRAY, children=items)

    def _parse_identifier(self) -> str:
        """Parse an identifier."""
        if not self._is_identifier_start(self._peek()):
            raise ParseError(f"Expected identifier at position {self.pos}")

        start = self.pos
        while self._is_identifier_char(self._peek()):
            self._consume()

        return self.expr[start : self.pos]

    def _is_identifier_start(self, char: str) -> bool:
        """Check if character can start an identifier."""
        return bool(char) and (char.isalpha() or char == "_")

    def _is_identifier_char(self, char: str) -> bool:
        """Check if character can be part of an identifier."""
        return bool(char) and (char.isalnum() or char == "_")


def parse_jq_expression(expression: str) -> ASTNode:
    """Parse a jq expression and return the AST."""
    parser = JQParser(expression)
    return parser.parse()
