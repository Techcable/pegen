from abc import ABCMeta, abstractmethod
import ast
import re
import token
from typing import IO, Any, Dict, List, Optional, Sequence, Set, Text, Tuple, TypeAlias

from pegen import grammar
from pegen.grammar import (
    Alt,
    Cut,
    Forced,
    Gather,
    GrammarVisitor,
    Group,
    Lookahead,
    NamedItem,
    NameLeaf,
    NegativeLookahead,
    Opt,
    PositiveLookahead,
    Repeat0,
    Repeat1,
    Rhs,
    Rule,
    StringLeaf,
)
from pegen.parser_generator import ParserGenerator

MODULE_PREFIX = """\
#!/usr/bin/env janet
# @generated by pegen from {filename}

(use pegen/parser/helpers)
"""
MODULE_SUFFIX = """

if __name__ == '__main__':
    (import pegen/parser)
    (pegen/parser/simple_parser_main {class_name})
"""

class JanetExpr(metaclass=ABCMeta):
    @abstractmethod
    def __str__(self) -> str:
        pass

JanetLiteralType: TypeAlias = str | float | int | bool | None

class JanetLiteralExpr(JanetExpr):
    value: JanetLiteralType
    def __init__(self, value: JanetLiteralType) -> None:
        self.value = value
        if isinstance(value, str):
            assert value.isascii(), f"TODO: Unicode support {value!r}"
        elif isinstance(value, (int, float, bool)) or value is None:
            pass
        else:
            raise TypeError(f"Unexpected type: {value!r}")


    def __str__(self) -> str:
        val = self.value
        if val is None:
            return "nil"
        elif isinstance(val, str):
            if val.isprintable() and "\\" not in val and '"' not in val:
                return f'"{val}"'
            else:
                backticks = "`" * (val.count("`") + 1)
                return backticks + val + backticks
        elif isinstance(val, (int, float)):
            return str(val)
        elif isinstance(val, bool):
            return "true" if val else "false"
        else:
            raise AssertionError(str(self.value))


class JanetList(JanetExpr):
    values: list[JanetExpr]
    mutable: bool

    def __init__(self, values: list[JanetExpr], *, mutable: bool = False) -> None:
        self.values = list(values)
        assert all(isinstance(e, JanetExpr) for e in values)
        self.mutable = mutable

    def __str__(self) -> str:
        prefix = "@[" if self.mutable else "["
        return prefix + ' '.join(map(str, self.values)) + "]"



def escape(s: JanetLiteralType) -> JanetLiteralExpr:
    return JanetLiteralExpr(s)

class InvalidNodeVisitor(GrammarVisitor):
    def visit_NameLeaf(self, node: NameLeaf) -> bool:
        name = node.value
        return name.startswith("invalid")

    def visit_StringLeaf(self, node: StringLeaf) -> bool:
        return False

    def visit_NamedItem(self, node: NamedItem) -> bool:
        return self.visit(node.item)

    def visit_Rhs(self, node: Rhs) -> bool:
        return any(self.visit(alt) for alt in node.alts)

    def visit_Alt(self, node: Alt) -> bool:
        return any(self.visit(item) for item in node.items)

    def lookahead_call_helper(self, node: Lookahead) -> bool:
        return self.visit(node.node)

    def visit_PositiveLookahead(self, node: PositiveLookahead) -> bool:
        return self.lookahead_call_helper(node)

    def visit_NegativeLookahead(self, node: NegativeLookahead) -> bool:
        return self.lookahead_call_helper(node)

    def visit_Opt(self, node: Opt) -> bool:
        return self.visit(node.node)

    def visit_Repeat(self, node: Repeat0) -> Tuple[str, str]:
        return self.visit(node.node)

    def visit_Gather(self, node: Gather) -> Tuple[str, str]:
        return self.visit(node.node)

    def visit_Group(self, node: Group) -> bool:
        return self.visit(node.rhs)

    def visit_Cut(self, node: Cut) -> bool:
        return False

    def visit_Forced(self, node: Forced) -> bool:
        return self.visit(node.node)


class JanetCallMakerVisitor(GrammarVisitor):
    def __init__(self, parser_generator: ParserGenerator):
        self.gen = parser_generator
        self.cache: Dict[Any, Any] = {}
        self.keywords: Set[str] = set()
        self.soft_keywords: Set[str] = set()

    def visit_NameLeaf(self, node: NameLeaf) -> Tuple[Optional[str], str]:
        name = node.value
        if name == "SOFT_KEYWORD":
            return "soft_keyword", "(self soft_keyword)"
        if name in ("NAME", "NUMBER", "STRING", "OP", "TYPE_COMMENT"):
            name = name.lower()
            return name, f"(self {name})"
        if name in ("NEWLINE", "DEDENT", "INDENT", "ENDMARKER", "ASYNC", "AWAIT"):
            # Avoid using names that can be Python keywords
            # TODO: Replace with janet keywords?
            return "_" + name.lower(), f"(self expect {escape(name)})"
        return name, f"(self {name})"

    def visit_StringLeaf(self, node: StringLeaf) -> Tuple[str, str]:
        val = ast.literal_eval(node.value)
        if re.match(r"[a-zA-Z_]\w*\Z", val):  # This is a keyword
            if node.value.endswith("'"):
                self.keywords.add(val)
            else:
                self.soft_keywords.add(val)
        return "literal", f"(expect {escape(node.value)})"

    def visit_Rhs(self, node: Rhs) -> Tuple[Optional[str], str]:
        if node in self.cache:
            return self.cache[node]
        if len(node.alts) == 1 and len(node.alts[0].items) == 1:
            self.cache[node] = self.visit(node.alts[0].items[0])
        else:
            name = self.gen.name_node(node)
            self.cache[node] = name, f"(self {name})"
        return self.cache[node]

    def visit_NamedItem(self, node: NamedItem) -> Tuple[Optional[str], str]:
        name, call = self.visit(node.item)
        if node.name:
            name = node.name
        return name, call

    def lookahead_call_helper(self, node: Lookahead) -> Tuple[str, str]:
        name, call = self.visit(node.node)
        head, tail = call.split("(", 1)
        assert tail[-1] == ")"
        tail = tail[:-1]
        return head, tail

    def visit_PositiveLookahead(self, node: PositiveLookahead) -> Tuple[None, str]:
        head, tail = self.lookahead_call_helper(node)
        return None, f"(self positive_lookahead {head} {tail})"

    def visit_NegativeLookahead(self, node: NegativeLookahead) -> Tuple[None, str]:
        head, tail = self.lookahead_call_helper(node)
        return None, f"(self negative_lookahead {head} {tail})"

    def visit_Opt(self, node: Opt) -> Tuple[str, str]:
        name, call = self.visit(node.node)
        # Note trailing comma (the call may already have one comma
        # at the end, for example when rules have both repeat0 and optional
        # markers, e.g: [rule*])
        if call.endswith(","):
            return "opt", call
        else:
            return "opt", f"{call},"

    def visit_Repeat0(self, node: Repeat0) -> Tuple[str, str]:
        if node in self.cache:
            return self.cache[node]
        name = self.gen.name_loop(node.node, False)
        self.cache[node] = name, f"(self {name}),"  # Also a trailing comma!
        return self.cache[node]

    def visit_Repeat1(self, node: Repeat1) -> Tuple[str, str]:
        if node in self.cache:
            return self.cache[node]
        name = self.gen.name_loop(node.node, True)
        self.cache[node] = name, f"(self {name})"  # But no trailing comma here!
        return self.cache[node]

    def visit_Gather(self, node: Gather) -> Tuple[str, str]:
        if node in self.cache:
            return self.cache[node]
        name = self.gen.name_gather(node)
        self.cache[node] = name, f"(self {name})"  # No trailing comma here either!
        return self.cache[node]

    def visit_Group(self, node: Group) -> Tuple[Optional[str], str]:
        return self.visit(node.rhs)

    def visit_Cut(self, node: Cut) -> Tuple[str, str]:
        return "cut", "true"

    def visit_Forced(self, node: Forced) -> Tuple[str, str]:
        if isinstance(node.node, Group):
            _, val = self.visit(node.node.rhs)
            return "forced", f"(self expect_forced {escape(val)} ````({node.node.rhs!s})````)"
        else:
            return (
                "forced",
                f"(self expect_forced (self expect {escape(node.node.value)}) {escape(node.node.value)})",
            )


class UsedNamesVisitor(ast.NodeVisitor):
    def generic_visit(self, node: ast.AST) -> Set[str]:
        result = set()
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        result.update(self.visit(item))
            elif isinstance(value, ast.AST):
                result.update(self.visit(value))
        return result

    def visit_Name(self, node: ast.Name) -> Set[str]:
        return {node.id}


class JanetParserGenerator(ParserGenerator, GrammarVisitor):
    callmakervisitor: JanetCallMakerVisitor
    def __init__(
        self,
        grammar: grammar.Grammar,
        file: Optional[IO[Text]],
        tokens: Set[str] = set(token.tok_name.values()),
        location_formatting: Optional[str] = None,
        unreachable_formatting: Optional[str] = None,
    ):
        tokens.add("SOFT_KEYWORD")
        super().__init__(grammar, tokens, file)
        self.callmakervisitor = JanetCallMakerVisitor(self)
        self.invalidvisitor: InvalidNodeVisitor = InvalidNodeVisitor()
        self.usednamesvisitor: UsedNamesVisitor = UsedNamesVisitor()
        self.unreachable_formatting = unreachable_formatting or "None  # pragma: no cover"
        if location_formatting is None:
            location_formatting = (
                "{:lineno start_lineno :cool_offset start_col_offset"
                + " :end_lineno end_lineno :end_col_offset end_col_offset}"
            )
        self.location_formatting = location_formatting
        self.cleanup_statements: List[str] = []

    def generate(self, filename: str) -> None:
        header = self.grammar.metas.get("header", MODULE_PREFIX)
        if header is not None:
            self.print(header.rstrip("\n").format(filename=filename))
        subheader = self.grammar.metas.get("subheader", "")
        if subheader:
            self.print(subheader)
        cls_name = self.grammar.metas.get("class", "GeneratedParser")
        self.print("# Keywords and soft keywords are listed at the end of the parser definition.")
        self.print(f"(defparser {cls_name}")
        while self.todo:
            for rulename, rule in list(self.todo.items()):
                del self.todo[rulename]
                self.print()
                with self.indent():
                    self.visit(rule)

        self.print()
        with self.indent():
            self.print(f"(def KEYWORDS {tuple(sorted(self.callmakervisitor.keywords))})")
            self.print(f"(def SOFT_KEYWORDS {tuple(sorted(self.callmakervisitor.soft_keywords))})")

        trailer = self.grammar.metas.get("trailer", MODULE_SUFFIX.format(class_name=cls_name))
        if trailer is not None:
            self.print(trailer.rstrip("\n"))

    def alts_uses_locations(self, alts: Sequence[Alt]) -> bool:
        for alt in alts:
            if alt.action and "LOCATIONS" in alt.action:
                return True
            for n in alt.items:
                if isinstance(n.item, Group) and self.alts_uses_locations(n.item.rhs.alts):
                    return True
        return False

    def add_return(self, ret_val: str) -> None:
        for stmt in self.cleanup_statements:
            self.print(stmt)
        self.print(f"(return {ret_val})")

    def visit_Rule(self, node: Rule) -> None:
        is_loop = node.is_loop()
        is_gather = node.is_gather()
        rhs = node.flatten()
        decorators = []
        if node.left_recursive:
            if node.leader:
                decorators.append("memoize_left_rec")
            else:
                # Non-leader rules in a cycle are not memoized,
                # but they must still be logged.
                decorators.append("@logger")
        else:
            decorators.append("@memoize")
        decorator_array_repr = f"[{','.join(decorators)}]"
        node_type = node.type or "Any"
        definition_type = "decorated-defn" if decorators else "defn"
        self.print(f"({definition_type} {decorator_array_repr} {node.name} [self]")
        with self.indent():
            self.print(f"# {node.name}: {rhs}")
            if node.nullable:
                self.print(f"# nullable={node.nullable}")

            if node.name.endswith("without_invalid"):
                self.print("(def _prev_call_invalid (self :call_invalid_rules))")
                self.print("(set self :call_invalid_rules False)")
                self.cleanup_statements.append("(set self :call_invalid_rules _prev_call_invalid)")

            self.print("(var mark (:_mark self))")
            if self.alts_uses_locations(node.rhs.alts):
                self.print("(var tok (:peek (self :_tokenizer)))")
                self.print("(var [start_lineno start_col_offset] tok.start)")
            if is_loop:
                self.print("(def children @[])")
            self.visit(rhs, is_loop=is_loop, is_gather=is_gather)
            if is_loop:
                self.add_return("children")
            else:
                self.add_return("None")
        self.print(")")

        if node.name.endswith("without_invalid"):
            self.cleanup_statements.pop()

    def visit_NamedItem(
        self, node: NamedItem, used: Optional[Set[str]], unreachable: bool
    ) -> None:
        name, call = self.callmakervisitor.visit(node.item)
        if unreachable:
            name = None
        elif node.name:
            name = node.name

        if used is not None and name not in used:
            name = None

        if not name:
            # Parentheses are needed because the trailing comma may appear :>
            self.print(f"({call})")
        else:
            if name != "cut":
                name = self.dedupe(name)
            self.print(f"(var {name} {call})")

    def visit_Rhs(self, node: Rhs, is_loop: bool = False, is_gather: bool = False) -> None:
        if is_loop:
            assert len(node.alts) == 1
        for alt in node.alts:
            self.visit(alt, is_loop=is_loop, is_gather=is_gather)

    def print_action(
        self,
        action: Optional[str],
        locations: bool,
        unreachable: bool,
        is_gather: bool,
        is_loop: bool,
        has_invalid: bool,
    ) -> None:
        if not action:
            if is_gather:
                assert len(self.local_variable_names) == 2
                action = f"[{self.local_variable_names[0]}] + {self.local_variable_names[1]}"
            else:
                if has_invalid:
                    assert unreachable
                    assert isinstance(action, str)  # for type checker
                elif len(self.local_variable_names) == 1:
                    action = f"{self.local_variable_names[0]}"
                else:
                    action = f"[{', '.join(self.local_variable_names)}]"

        if locations:
            self.print("(var tok (:get_last_non_whitespace_token (self :_tokenizer)))")
            self.print("(var [end_lineno end_col_offset] (tok :end))")

        if is_loop:
            self.print(f"(array/push children {action})")
            self.print("(set mark self._mark())")
        else:
            self.add_return(f"{action}")

    def visit_Alt(self, node: Alt, is_loop: bool, is_gather: bool) -> None:
        has_cut = any(isinstance(item.item, Cut) for item in node.items)
        has_invalid = self.invalidvisitor.visit(node)

        action = node.action
        if not action and not is_gather and has_invalid:
            action = "UNREACHABLE"

        locations = False
        unreachable = False
        used = None
        if action:
            # Replace magic name in the action rule
            if "LOCATIONS" in action:
                locations = True
                action = action.replace("LOCATIONS", self.location_formatting)
            if "UNREACHABLE" in action:
                unreachable = True
                action = action.replace("UNREACHABLE", self.unreachable_formatting)

            # Extract the names actually used in the action.
            if False:
                used = self.usednamesvisitor.visit(ast.parse(action))
                if has_cut:
                    used.add("cut")

        with self.local_variable_context():
            if has_cut:
                self.print("(set cut False)")
            if is_loop:
                self.print("(while (and ")
            else:
                self.print("(when (and ")
            with self.indent():
                if has_invalid:
                    self.print("(self :call_invalid_rules)")
                for item in node.items:
                    if is_gather:
                        self.print("(not (nil? ")
                    self.visit(item, used=used, unreachable=unreachable)
                    if is_gather:
                        self.print("))")
            self.print(")")
            with self.indent():
                # flake8 complains that visit_Alt is too complicated, so here we are :P
                self.print_action(action, locations, unreachable, is_gather, is_loop, has_invalid)

            self.print("(:_reset self mark)")
            # Skip remaining alternatives if a cut was reached.
            if has_cut:
                self.print("(when cut (return nil))")
