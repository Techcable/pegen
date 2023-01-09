from __future__ import annotations

from abc import ABCMeta, abstractmethod
from os import PathLike
import tokenize
from typing import Dict, Set, Tuple, Optional, IO, ClassVar
from pathlib import Path
from dataclasses import dataclass

from pegen.grammar import Grammar
from pegen.grammar_parser import GeneratedParser as GrammarParser
from pegen.parser import Parser
from pegen.parser_generator import ParserGenerator
from pegen.python_generator import PythonParserGenerator
from pegen.janet_generator import JanetParserGenerator
from pegen.tokenizer import Tokenizer

MOD_DIR = Path(__file__).resolve().parent

TokenDefinitions = Tuple[Dict[int, str], Dict[str, int], Set[str]]


@dataclass
class BuilderConfig:
    verbose_tokenizer: bool = False
    verbose_parser: bool = False
    # generator
    skip_actions: bool = False


class Builder(metaclass=ABCMeta):
    grammar_file: Path
    output_file: Path
    config: BuilderConfig

    BUILDERS_BY_GENERATOR_NAME: ClassVar[dict[str, type[Builder]]]

    def __init__(
        self,
        grammar_file: PathLike,
        output_file: PathLike,
        config: Optional[BuilderConfig] = None
    ):
        self.grammar_file = Path(grammar_file)
        self.output_file = Path(output_file)
        self.config = config if config is not None else BuilderConfig()

    def build_parser(self) -> Tuple[Grammar, Parser, Tokenizer]:
        with open(self.grammar_file) as file:
            tokenizer = Tokenizer(
                tokenize.generate_tokens(file.readline),
                verbose=self.config.verbose_tokenizer
            )
            parser = GrammarParser(tokenizer, verbose=self.config.verbose_parser)
            grammar = parser.start()

            if not grammar:
                raise parser.make_syntax_error(str(self.grammar_file))

        return grammar, parser, tokenizer

    def build_generator(self, grammar: Grammar) -> ParserGenerator:
        assert isinstance(grammar, Grammar)
        with open(self.output_file, "w") as file:
            gen = self._create_generator(grammar, file)
            gen.generate(self.grammar_file)
        return gen

    def build(self) -> Tuple[Grammar, Parser, Tokenizer, ParserGenerator]:
        """Generate rules, parser object, tokenizer, parser generator for a given grammar"""
        grammar, parser, tokenizer = self.build_parser()
        gen = self.build_generator(
            grammar,
        )
        return grammar, parser, tokenizer, gen

    @abstractmethod
    def _create_generator(self, grammar: Grammar, file: IO[str]) -> ParserGenerator:
        pass


class PythonBuilder(Builder):
    def _create_generator(self, grammar: Grammar, file: IO[str]):
        if self.config.skip_actions:
            raise NotImplementedError("TODO: Support `skip_actions` option")
        return PythonParserGenerator(grammar, file)


class JanetBuilder(Builder):
    def _create_generator(self, grammar: Grammar, file: IO[str]):
        if self.config.skip_actions:
            raise NotImplementedError("TODO: Support `skip_actions` option")
        return JanetParserGenerator(grammar, file)


Builder.BUILDERS_BY_GENERATOR_NAME = {
    'python': PythonBuilder,
    'janet': JanetBuilder,
}
