"""
jq expression parser.

Supports a subset of jq syntax commonly used in Claude Code plugin hooks:
- Field access: .field, .a.b.c, ."quoted-key", .["key"]
- Optional access: .a?, .[]?, .a.b? (errors become no output)
- Recursive descent: .. (same as recurse)
- Array indexing: .[0], .[-1]
- Array iteration: .[]
- Pipes: . | .field
- Comma: .a, .b (binds tighter than '|', so [.a | .b, .c] is [.a | (.b, .c)])
- Object construction: {a: .b, c: $var, (.k): .v, "s": 1}, shorthand {a, $var}
- String interpolation: "prefix \\(.field) suffix"
- Default values: .field // "default"
- Function calls: select(...), map(...)
- Variables: $name
- Comparisons: ==, !=, <, >, <=, >=
- Logical operators: and, or (``or`` binds looser than ``and``), not

Anything else (arithmetic, if/then/else,
try/catch, reduce/foreach, ``as`` bindings, def, slices, @formats,
assignment operators) raises ParseError with an "unsupported jq syntax"
message. See the package docstring for the full list of differences.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Union
from enum import Enum


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
    OPTIONAL = "optional"  # expr?
    COMMA = "comma"  # a, b


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


# Nesting limit for (...), [...], {...}, "\\(...)" and prefix operators. Each
# level costs ~8 Python frames in this recursive-descent parser, so this keeps
# pathological programs well inside the interpreter's recursion limit.
MAX_PARSE_DEPTH = 64

# Limit on the depth of the resulting AST (long chains like .a.b.c... nest one
# level per step), which bounds the recursion of validation and evaluation.
MAX_AST_DEPTH = 150

# jq keywords whose constructs the shim does not implement.
UNSUPPORTED_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "end", "reduce", "foreach", "try", "catch",
    "label", "def", "as", "import", "include", "and", "or",
})


class JQParser:
    """
    Recursive descent parser for jq expressions.

    Grammar (simplified):
        expr       -> pipe
        pipe       -> comma ("|" comma)*
        comma      -> default ("," default)*
        default    -> or ("//" or)*
        or         -> and ("or" and)*
        and        -> comparison ("and" comparison)*
        comparison -> term (("==" | "!=" | "<" | ">" | "<=" | ">=") term)?
        term       -> factor | "not" term | "-" term
        factor     -> primary postfix*
        postfix    -> "." IDENT | "." STRING | "[" expr "]" | "[]" | "?"
        primary    -> "." | ".." | "." IDENT | "." STRING | NUMBER | STRING
                    | "true" | "false" | "null"
                    | "$" IDENT | "{" object_pairs "}" | "[" array_items "]"
                    | IDENT "(" args ")" | "(" expr ")"
    """

    def __init__(self, expression: str):
        self.expr = expression
        self.pos = 0
        self.length = len(expression)
        self.depth = 0

    def parse(self) -> ASTNode:
        """Parse the full expression."""
        self._skip_whitespace()
        if self.pos >= self.length:
            return ASTNode(NodeType.IDENTITY)
        try:
            result = self._parse_pipe()
        except RecursionError:
            raise ParseError("jq program is nested too deeply") from None
        self._skip_whitespace()
        if self.pos < self.length:
            raise ParseError(self._describe_unexpected())
        if _ast_depth(result) > MAX_AST_DEPTH:
            raise ParseError(
                f"jq program is nested too deeply (AST depth limit {MAX_AST_DEPTH})"
            )
        return result

    def _enter(self) -> None:
        """Track nesting depth; refuse programs nested beyond MAX_PARSE_DEPTH."""
        self.depth += 1
        if self.depth > MAX_PARSE_DEPTH:
            raise ParseError(
                f"jq program is nested too deeply (limit {MAX_PARSE_DEPTH})"
            )

    def _describe_unexpected(self, expected: Optional[str] = None) -> str:
        """Explain the token at the current position, naming unsupported jq syntax."""
        if self.pos >= self.length:
            if expected:
                return f"Expected {expected!r} but reached end of input"
            return "Unexpected end of input"
        char = self.expr[self.pos]
        where = f"at position {self.pos}"
        if self._is_identifier_start(char):
            end = self.pos
            while end < self.length and self._is_identifier_char(self.expr[end]):
                end += 1
            word = self.expr[self.pos:end]
            if word in UNSUPPORTED_KEYWORDS:
                return f"unsupported jq syntax: '{word}' {where}"
        if char in "+-*/%":
            if self.expr[self.pos:self.pos + 2] in ("+=", "-=", "*=", "/=", "%="):
                return f"unsupported jq syntax: assignment operator {where}"
            return f"unsupported jq syntax: arithmetic operator {char!r} {where}"
        if char == "=" or self.expr[self.pos:self.pos + 2] in ("|=", "//="):
            return f"unsupported jq syntax: assignment operator {where}"
        if char == ":":
            return f"unsupported jq syntax: slice or ':' {where}"
        if char == "@":
            return f"unsupported jq syntax: @format strings {where}"
        if expected:
            return f"Expected {expected!r} but got {char!r} {where}"
        return f"Unexpected character {where}: {char!r}"

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
            raise ParseError(self._describe_unexpected(expected))
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

    def _parse_pipe(self, allow_comma: bool = True) -> ASTNode:
        """Parse pipe expression: a | b | c (object values pass allow_comma=False)"""
        self._enter()
        parse_operand = self._parse_comma if allow_comma else self._parse_default
        left = parse_operand()

        while True:
            self._skip_whitespace()
            if self._peek() == "|" and self._peek(1) not in ("|", "="):
                self._consume()
                self._skip_whitespace()
                right = parse_operand()
                left = ASTNode(NodeType.PIPE, children=[left, right])
            else:
                break

        self.depth -= 1
        return left

    def _parse_comma(self) -> ASTNode:
        """Parse comma expression: a, b, c (a trailing ',' before a closer is tolerated)"""
        items = [self._parse_default()]

        while True:
            self._skip_whitespace()
            if self._peek() != ",":
                break
            self._consume()
            self._skip_whitespace()
            if self._peek() in ("]", "}", ")"):
                break
            items.append(self._parse_default())

        if len(items) == 1:
            return items[0]
        return ASTNode(NodeType.COMMA, children=items)

    def _parse_default(self) -> ASTNode:
        """Parse default expression: a // b"""
        left = self._parse_or()

        while True:
            self._skip_whitespace()
            if self._match("//") and not self._match("//="):
                self._consume_string("//")
                self._skip_whitespace()
                right = self._parse_or()
                left = ASTNode(NodeType.DEFAULT, children=[left, right])
            else:
                break

        return left

    def _parse_or(self) -> ASTNode:
        """Parse 'or' (binds looser than 'and', as in jq)."""
        left = self._parse_and()

        while True:
            self._skip_whitespace()
            if self._match_keyword("or"):
                self._consume_string("or")
                self._skip_whitespace()
                right = self._parse_and()
                left = ASTNode(NodeType.LOGICAL, "or", [left, right])
            else:
                break

        return left

    def _parse_and(self) -> ASTNode:
        """Parse 'and'."""
        left = self._parse_comparison()

        while True:
            self._skip_whitespace()
            if self._match_keyword("and"):
                self._consume_string("and")
                self._skip_whitespace()
                right = self._parse_comparison()
                left = ASTNode(NodeType.LOGICAL, "and", [left, right])
            else:
                break

        return left

    def _match_keyword(self, word: str) -> bool:
        """Check for a whole keyword (not a prefix of a longer identifier)."""
        return self._match(word) and not self._is_identifier_char(
            self._peek(len(word))
        )

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

        # Handle 'not': jq's `f | not` filter, plus the shim's prefix form `not f`
        if self._match_keyword("not"):
            self._consume_string("not")
            self._skip_whitespace()
            if (
                self.pos >= self.length
                or self._peek() in "|);],}"
                or self._match_keyword("and")
                or self._match_keyword("or")
                or self._match("//")
            ):
                return self._parse_postfix(ASTNode(NodeType.FUNCTION, "not"))
            self._enter()
            operand = self._parse_term()
            self.depth -= 1
            return ASTNode(NodeType.LOGICAL, "not", [operand])

        # Handle unary minus (a leading '-' before a digit is a number literal)
        if self._peek() == "-" and not self._peek(1).isdigit():
            self._consume()
            self._skip_whitespace()
            self._enter()
            operand = self._parse_term()
            self.depth -= 1
            return ASTNode(NodeType.NEGATE, children=[operand])

        return self._parse_factor()

    def _parse_factor(self) -> ASTNode:
        """Parse factor with postfix operators."""
        node = self._parse_primary()
        return self._parse_postfix(node)

    def _parse_postfix(self, node: ASTNode) -> ASTNode:
        """Parse postfix operations: .field, ."field", [n], [], ?"""
        while True:
            self._skip_whitespace()
            if self._peek() == ".":
                # Check for .field / ."field" access
                if self._is_identifier_start(self._peek(1)):
                    self._consume()
                    field_name = self._parse_identifier()
                    node = ASTNode(NodeType.PIPE, children=[node, ASTNode(NodeType.FIELD, field_name)])
                elif self._peek(1) == '"':
                    self._consume()
                    node = self._parse_quoted_field(node)
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
                    # term[expr] - index (slices are unsupported). The index
                    # expression is evaluated against `.`, not against the term:
                    # .a[.i] is .a[(.i)], so it is not a pipe.
                    index_expr = self._parse_pipe()
                    self._skip_whitespace()
                    self._consume("]")
                    node = ASTNode(NodeType.INDEX, children=[index_expr, node])
            elif self._peek() == "?":
                # expr? - suppress errors from the term so far
                self._consume()
                node = ASTNode(NodeType.OPTIONAL, children=[node])
            else:
                break

        return node

    def _parse_quoted_field(self, target: Optional[ASTNode] = None) -> ASTNode:
        """Parse the "key" of [target]."key" (the '.' is already consumed)."""
        key_node = self._parse_string()
        if isinstance(key_node.value, str):
            field_node = ASTNode(NodeType.FIELD, key_node.value)
            if target is None:
                return field_node
            return ASTNode(NodeType.PIPE, children=[target, field_node])
        # An interpolated key is evaluated against `.`, like term[expr]
        children = [key_node] if target is None else [key_node, target]
        return ASTNode(NodeType.INDEX, children=children)

    def _parse_primary(self) -> ASTNode:
        """Parse primary expression."""
        self._skip_whitespace()

        char = self._peek()

        # Identity or field access
        if char == ".":
            self._consume()
            if self._peek() == ".":
                # .. - recursive descent
                self._consume()
                return ASTNode(NodeType.FUNCTION, "recurse")
            if self._peek() == '"':
                return self._parse_quoted_field()
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

        if char == "@":
            raise ParseError(self._describe_unexpected())

        # Keywords and functions
        if self._is_identifier_start(char):
            start = self.pos
            name = self._parse_identifier()
            if name in UNSUPPORTED_KEYWORDS:
                self.pos = start
                raise ParseError(self._describe_unexpected())

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
                elif next_char == "/":
                    current_str += "/"
                elif next_char == "b":
                    current_str += "\b"
                elif next_char == "f":
                    current_str += "\f"
                elif next_char == "u":
                    current_str += self._parse_unicode_escape()
                else:
                    raise ParseError(
                        f"Invalid escape '\\{next_char}' at position {self.pos - 1}"
                    )
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

    def _parse_unicode_escape(self) -> str:
        """Parse the XXXX of \\uXXXX (the '\\u' is consumed), joining surrogate pairs."""
        code = self._parse_hex4()
        if 0xD800 <= code < 0xDC00 and self._match("\\u"):
            saved = self.pos
            self.pos += 2
            low = self._parse_hex4()
            if 0xDC00 <= low < 0xE000:
                return chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
            self.pos = saved
        return chr(code)

    def _parse_hex4(self) -> int:
        digits = self.expr[self.pos:self.pos + 4]
        if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
            raise ParseError(f"Invalid \\u escape at position {self.pos}")
        self.pos += 4
        return int(digits, 16)

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
        """Parse a single object key-value pair (or a {a} / {$a} shorthand)."""
        self._skip_whitespace()

        # Key can be identifier or string or parenthesized expression
        shorthand = None
        if self._peek() == '"':
            key_node = self._parse_string()
            key = key_node.value if isinstance(key_node.value, str) else key_node
            if isinstance(key, str):
                shorthand = ASTNode(NodeType.FIELD, key)
        elif self._peek() == "(":
            self._consume()
            self._skip_whitespace()
            key = self._parse_pipe()
            self._skip_whitespace()
            self._consume(")")
        elif self._peek() == "$":
            self._consume()
            key = self._parse_identifier()
            shorthand = ASTNode(NodeType.VARIABLE, key)
        else:
            key = self._parse_identifier()
            shorthand = ASTNode(NodeType.FIELD, key)

        self._skip_whitespace()
        if shorthand is not None and self._peek() in (",", "}"):
            # {a} means {a: .a}; {$x} means {x: $x}
            return (key, shorthand)
        self._consume(":")
        self._skip_whitespace()

        value = self._parse_pipe(allow_comma=False)

        return (key, value)

    def _parse_array(self) -> ASTNode:
        """Parse array construction: [a, b, c] (the body is any expression)"""
        self._consume("[")
        self._skip_whitespace()

        items: List[ASTNode] = []

        if self._peek() != "]":
            body = self._parse_pipe()
            items = body.children if body.type == NodeType.COMMA else [body]

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


def _ast_depth(root: ASTNode) -> int:
    """Depth of an AST, computed iteratively (safe for any depth)."""
    deepest = 0
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        children = list(node.children)
        if node.type == NodeType.OBJECT:
            for key, value in node.value:
                children.append(value)
                if isinstance(key, ASTNode):
                    children.append(key)
        elif node.type == NodeType.STRING and not isinstance(node.value, str):
            children.extend(p for p in node.value if isinstance(p, ASTNode))
        stack.extend((child, depth + 1) for child in children)
    return deepest


def parse_jq_expression(expression: str) -> ASTNode:
    """Parse a jq expression and return the AST."""
    parser = JQParser(expression)
    return parser.parse()
