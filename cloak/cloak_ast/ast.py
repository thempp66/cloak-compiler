from __future__ import annotations

import abc
from ctypes import Array
import math
import operator
import textwrap
from collections import OrderedDict
from enum import IntEnum
from functools import cmp_to_key
from os import linesep
from typing import List, Dict, Union, Optional, Callable, Set, TypeVar

from cloak.config import cfg, zk_print
from cloak.utils.progress_printer import warn_print
from cloak.cloak_ast.analysis.partition_state import PartitionState
from cloak.cloak_ast.visitor.visitor import AstVisitor
from cloak.errors import exceptions

T = TypeVar('T')


class ChildListBuilder:
    def __init__(self):
        self.children = []

    def add_child(self, ast: AST) -> AST:
        if ast is not None:
            self.children.append(ast)
        return ast


class AST:
    def __init__(self):
        # set later by parent setter
        self.parent: Optional[AST] = None
        self.namespace: Optional[List[Identifier]] = None

        # Names accessible by AST nodes below this node.
        # Does not include names already listed by parents.
        # Maps strings (names) to Identifiers.
        #
        # set later by symbol table
        self.names: Dict[str, Identifier] = {}

        self.line = -1
        self.column = -1

        self.modified_values: OrderedDict[InstanceTarget, None] = OrderedDict()
        self.read_values: Set[InstanceTarget] = set()

    def children(self) -> List[AST]:
        cb = ChildListBuilder()
        self.process_children(cb.add_child)
        return cb.children

    def is_parent_of(self, child: AST) -> bool:
        e = child
        while e != self and e.parent is not None:
            e = e.parent
        return e == self

    def override(self: T, **kwargs) -> T:
        for key, val in kwargs.items():
            if not hasattr(self, key):
                raise ValueError(f'Class "{type(self).__name__}" does not have property "{key}"')
            setattr(self, key, val)
        return self

    def process_children(self, f: Callable[[T], T]):
        pass

    def code(self, for_solidity=False) -> str:
        v = CodeVisitor(for_solidity=for_solidity)
        s = v.visit(self)
        return s

    @property
    def qualified_name(self) -> List[Identifier]:
        if not hasattr(self, 'idf'):
            return []
        if self.namespace[-1] == self.idf:
            return self.namespace
        else:
            return self.namespace + [self.idf]

    def get_related_function(self):
        _ast_iter = self
        while not isinstance(_ast_iter, ContractDefinition):
            if isinstance(_ast_iter, ConstructorOrFunctionDefinition):
                return _ast_iter
            else:
                _ast_iter = _ast_iter.parent
        return None

    def get_related_contract(self):
        _ast_iter = self
        while not isinstance(_ast_iter, SourceUnit):
            if isinstance(_ast_iter, ContractDefinition):
                return _ast_iter
            else:
                _ast_iter = _ast_iter.parent
        return None

    def get_related_sourceuint(self):
        _ast_iter = self
        while not isinstance(_ast_iter, SourceUnit):
            _ast_iter = _ast_iter.parent
        return _ast_iter

    def is_in_assignment_lefthand(self):
        _ast_iter = self
        _child = self
        while not isinstance(_ast_iter, SourceUnit):
            if isinstance(_ast_iter, AssignmentStatement):
                if _child == _ast_iter.lhs:
                    return True
                else:
                    return False
            _child = _ast_iter
            _ast_iter = _ast_iter.parent
        return False

    def __str__(self):
        return self.code()


class Identifier(AST):

    def __init__(self, name: str):
        super().__init__()
        self.name = name

    @property
    def is_immutable(self):
        return isinstance(self.parent, StateVariableDeclaration) and (self.parent.is_final or self.parent.is_constant)


class Comment(AST):

    def __init__(self, text: str = ''):
        super().__init__()
        self.text = text

    @staticmethod
    def comment_list(text: str, block: List[AST]) -> List[AST]:
        return block if not block else [Comment(text)] + block + [BlankLine()]

    @staticmethod
    def comment_wrap_block(text: str, block: List[AST]) -> List[AST]:
        if not block:
            return block
        return [Comment(f'{text}'), Comment('{'), IndentBlock(block), Comment('}'), BlankLine()]


class BlankLine(Comment):
    def __init__(self):
        super().__init__()


class Expression(AST):

    @staticmethod
    def all_expr():
        return AllExpr()

    @staticmethod
    def tee_expr():
        return TeeExpr()

    @staticmethod
    def me_expr(stmt: Optional[Statement] = None):
        me = MeExpr()
        me.statement = stmt
        return me

    # def explicitly_converted(self: T, expected: TypeName) -> Union[T, FunctionCallExpr]:
    #     if expected == TypeName.bool_type() and not self.instanceof_data_type(TypeName.bool_type()):
    #         ret = FunctionCallExpr(BuiltinFunction('!='), [self, NumberLiteralExpr(0)])
    #     elif expected.is_numeric and self.instanceof_data_type(TypeName.bool_type()):
    #         ret = FunctionCallExpr(BuiltinFunction('ite'), [self, NumberLiteralExpr(1), NumberLiteralExpr(0)])
    #     else:
    #         t = self.annotated_type.type_name

    #         if t == expected:
    #             return self

    #         # Explicit casts
    #         cast = False
    #         if isinstance(t, NumberTypeName) and isinstance(expected, (NumberTypeName, AddressTypeName, AddressPayableTypeName, EnumTypeName)):
    #             cast = True
    #         elif isinstance(t, AddressTypeName) and isinstance(expected, NumberTypeName):
    #             cast = True
    #         elif isinstance(t, AddressPayableTypeName) and isinstance(expected, (NumberTypeName, AddressTypeName)):
    #             cast = True
    #         elif isinstance(t, EnumTypeName) and isinstance(expected, NumberTypeName):
    #             cast = True

    #         assert cast
    #         return PrimitiveCastExpr(expected, self).as_type(expected)

    #     ret.annotated_type = AnnotatedTypeName(expected.clone(), self.annotated_type.privacy_annotation.clone())
    #     return ret

    def __init__(self):
        super().__init__()
        # set later by type checker
        self.annotated_type: Optional[AnnotatedTypeName] = None
        # set by expression to statement
        self.statement: Optional[Statement] = None

        self.evaluate_privately = False

    def is_all_expr(self):
        return self == Expression.all_expr()

    def is_me_expr(self):
        return self == Expression.me_expr()

    def is_tee_expr(self):
        return self == Expression.tee_expr()

    def privacy_annotation_label(self):
        if isinstance(self, IdentifierExpr):
            if isinstance(self.target, Mapping):
                return self.target.instantiated_key.privacy_annotation_label()
            else:
                return self.target.idf
        elif self.is_all_expr():
            return self
        elif self.is_me_expr():
            return self
        elif self.is_tee_expr():
            return self
        else:
            return None

    def instanceof_data_type(self, expected: TypeName) -> bool:
        return self.annotated_type.type_name.implicitly_convertible_to(expected)

    def unop(self, op: str) -> FunctionCallExpr:
        return FunctionCallExpr(BuiltinFunction(op), [self])

    def binop(self, op: str, rhs: Expression) -> FunctionCallExpr:
        return FunctionCallExpr(BuiltinFunction(op), [self, rhs])

    def ite(self, e_true: Expression, e_false: Expression) -> FunctionCallExpr:
        return FunctionCallExpr(BuiltinFunction('ite').override(is_private=self.annotated_type.is_private), [self, e_true, e_false])

    def instanceof(self, expected):
        """

        :param expected:
        :return: True, False, or 'make-private'
        """
        assert (isinstance(expected, AnnotatedTypeName))

        actual = self.annotated_type

        if not self.instanceof_data_type(expected.type_name):
            return False

        # check privacy type
        combined_label = actual.combined_privacy(self.analysis, expected)
        if combined_label is None:
            return False
        elif isinstance(combined_label, List):
            assert isinstance(self.annotated_type.type_name, TupleType) and not isinstance(self, TupleExpr)
            return combined_label == [t.privacy_annotation for t in self.annotated_type.type_name.types]
        elif combined_label.privacy_annotation_label() == actual.privacy_annotation.privacy_annotation_label():
            return True
        else:
            return 'make-private'

    def as_type(self: T, t: Union[TypeName, AnnotatedTypeName]) -> T:
        return self.override(annotated_type=t if isinstance(t, AnnotatedTypeName) else AnnotatedTypeName(t))

    @property
    def analysis(self):
        if self.statement is None:
            return None
        else:
            return self.statement.before_analysis


builtin_op_fct = {
    '+': operator.add, '-': operator.sub,
    '**': operator.pow, '*': operator.mul, '/': operator.floordiv, '%': operator.mod,
    'sign+': lambda a: a, 'sign-': operator.neg,
    '<<': operator.lshift, '>>': operator.rshift,
    '|': operator.or_, '&': operator.and_, '^': operator.xor, '~': operator.inv,
    '<': operator.lt, '>': operator.gt, '<=': operator.le, '>=': operator.ge,
    '==': operator.eq, '!=': operator.ne,
    '&&': lambda a, b: a and b, '||': lambda a, b: a or b, '!': operator.not_,
    'ite': lambda a, b, c: b if a else c,
    'parenthesis': lambda a: a
}

builtin_functions = {
    'parenthesis': '({})',
    'ite': '{} ? {} : {}'
}

# arithmetic
arithmetic = {op: f'{{}} {op} {{}}' for op in ['**', '*', '/', '%', '+', '-']}
arithmetic.update({'sign+': '+{}', 'sign-': '-{}'})
# comparison
comp = {op: f'{{}} {op} {{}}' for op in ['<', '>', '<=', '>=']}
# equality
eq = {op: f'{{}} {op} {{}}' for op in ['==', '!=']}
# boolean operations
bop = {op: f'{{}} {op} {{}}' for op in ['&&', '||']}
bop['!'] = '!{}'
# bitwise operations
bitop = {op: f'{{}} {op} {{}}' for op in ['|', '&', '^']}
bitop['~'] = '~{}'
# shift operations
shiftop = {op: f'{{}} {op} {{}}' for op in ['<<', '>>']}

builtin_functions.update(arithmetic)
builtin_functions.update(comp)
builtin_functions.update(eq)
builtin_functions.update(bop)
builtin_functions.update(bitop)
builtin_functions.update(shiftop)

assert builtin_op_fct.keys() == builtin_functions.keys()


class BuiltinFunction(Expression):

    def __init__(self, op: str):
        super().__init__()
        self.op = op
        # set later by type checker
        self.is_private: bool = False

        # input validation
        if op not in builtin_functions:
            raise ValueError(f'{op} is not a known built-in function')

    def format_string(self):
        return builtin_functions[self.op]

    @property
    def op_func(self):
        return builtin_op_fct[self.op]

    def is_arithmetic(self):
        return self.op in arithmetic

    def is_neg_sign(self):
        return self.op == 'sign-'

    def is_comp(self):
        return self.op in comp

    def is_eq(self):
        return self.op in eq

    def is_bop(self):
        return self.op in bop

    def is_bitop(self):
        return self.op in bitop

    def is_shiftop(self):
        return self.op in shiftop

    def is_parenthesis(self):
        return self.op == 'parenthesis'

    def is_ite(self):
        return self.op == 'ite'

    def has_shortcircuiting(self):
        return self.is_ite() or self.op == '&&' or self.op == '||'

    def arity(self):
        return self.format_string().count('{}')

    def input_types(self):
        """

        :return: None if the type is generic
        """
        if self.is_arithmetic():
            t = TypeName.number_type()
        elif self.is_comp():
            t = TypeName.number_type()
        elif self.is_bop():
            t = TypeName.bool_type()
        elif self.is_bitop():
            t = TypeName.number_type()
        elif self.is_shiftop():
            t = TypeName.number_type()
        else:
            # eq, parenthesis, ite
            return None

        return self.arity() * [t]

    def output_type(self):
        """

        :return: None if the type is generic
        """
        if self.is_arithmetic():
            return TypeName.number_type()
        elif self.is_comp():
            return TypeName.bool_type()
        elif self.is_bop():
            return TypeName.bool_type()
        elif self.is_eq():
            return TypeName.bool_type()
        elif self.is_bitop():
            return TypeName.number_type()
        elif self.is_shiftop():
            return TypeName.number_type()
        else:
            # parenthesis, ite
            return None

    def can_be_private(self) -> bool:
        """

        :return: true if operation itself can be run inside a circuit \
                 for equality and ite it must be checked separately whether the arguments are also supported inside circuits
        """
        return self.op not in ['**', '%', '/']


class NamedArgument(AST):
    def __init__(self, key: str, value: Expression):
        super().__init__()
        self.key = key
        self.value = value

    def process_children(self, f: Callable[[T], T]):
        self.value = f(self.value)


class CallArgumentList(AST):
    def __init__(self, args: List[Union[Expression, NamedArgument]], named_arguments=False):
        super().__init__()
        self.args = args or []
        self.named_arguments = named_arguments

    def process_children(self, f: Callable[[T], T]):
        self.args[:] = map(f, self.args)


class FunctionCallOptions(Expression):
    def __init__(self, expr: Expression, args: List[NamedArgument]):
        self.expr = expr
        self.args = args or []


class FunctionCallExpr(Expression):

    def __init__(self, func: Expression, args: CallArgumentList, functionCallOptions: bool = False):
        super().__init__()
        self.func = func
        self.args = args
        if isinstance(args, list):
            self.args = CallArgumentList(args, False)
        self.functionCallOptions = functionCallOptions

    @property
    def is_cast(self):
        return isinstance(self.func, LocationExpr) and isinstance(self.func.target, (ContractDefinition, EnumDefinition, Array))

    def process_children(self, f: Callable[[T], T]):
        self.func = f(self.func)
        self.args = f(self.args)


class MetaTypeExpr(Expression):
    def __init__(self, typeName: TypeName):
        self.typeName = typeName


class NewExpr(Expression):
    def __init__(self, target_type: TypeName):
        self.target_type = target_type

    def process_children(self, f: Callable[[T], T]):
        self.target_type = f(self.target_type)


class PrimitiveCastExpr(Expression):
    def __init__(self, elem_type: TypeName, expr: Expression, is_implicit=False):
        super().__init__()
        self.elem_type = elem_type
        self.expr = expr
        self.is_implicit = is_implicit

    def process_children(self, f: Callable[[T], T]):
        self.elem_type = f(self.elem_type)
        self.expr = f(self.expr)


class LiteralExpr(Expression):
    pass


class BooleanLiteralExpr(LiteralExpr):

    def __init__(self, value: bool):
        super().__init__()
        self.value = value
        self.annotated_type = AnnotatedTypeName(BooleanLiteralType(self.value))


class NumberLiteralExpr(LiteralExpr):

    def __init__(self, value: int, was_hex: bool = False, source_text: Optional[str] = None, unit: Optional[str] = None):
        super().__init__()
        self.value = value
        self.annotated_type = AnnotatedTypeName(NumberLiteralType(self.value))
        self.was_hex = was_hex
        self.source_text = source_text
        self.unit = unit


class StringLiteralExpr(LiteralExpr):

    def __init__(self, value: str):
        super().__init__()
        self.value = value
        self.annotated_type = AnnotatedTypeName(StringTypeName())


class ArrayLiteralExpr(LiteralExpr):

    def __init__(self, values: List[Expression]):
        super().__init__()
        self.values = values

    def process_children(self, f: Callable[[T], T]):
        self.values[:] = map(f, self.values)


class KeyLiteralExpr(ArrayLiteralExpr):
    pass


class TupleOrLocationExpr(Expression):
    def is_lvalue(self) -> bool:
        if isinstance(self.parent, AssignmentStatement):
            return self == self.parent.lhs
        if isinstance(self.parent, IndexExpr) and self == self.parent.arr:
            return self.parent.is_lvalue()
        if isinstance(self.parent, MemberAccessExpr) and self == self.parent.expr:
            return self.parent.is_lvalue()
        if isinstance(self.parent, TupleExpr):
            return self.parent.is_lvalue()
        return False

    def is_rvalue(self) -> bool:
        return not self.is_lvalue()


class TupleExpr(TupleOrLocationExpr):
    def __init__(self, elements: List[Expression]):
        super().__init__()
        self.elements = elements

    def process_children(self, f: Callable[[T], T]):
        self.elements[:] = map(f, self.elements)

    def assign(self, val: Expression) -> AssignmentStatement:
        return AssignmentStatement(self, val)


class InlineArrayExpr(Expression):
    def __init__(self, exprs: List[Expression]):
        super().__init__()
        self.exprs = exprs


class LocationExpr(TupleOrLocationExpr):
    def __init__(self):
        super().__init__()
        # set later by symbol table
        self.target: Optional[TargetDefinition] = None

    def call(self, member: Union[None, str, Identifier], args: List[Expression]) -> FunctionCallExpr:
        if member is None:
            return FunctionCallExpr(self, args)
        else:
            member = Identifier(member) if isinstance(member, str) else member
            return FunctionCallExpr(MemberAccessExpr(self, member), args)

    def dot(self, member: Union[str, Identifier]) -> MemberAccessExpr:
        member = Identifier(member) if isinstance(member, str) else member
        return MemberAccessExpr(self, member)

    def index(self, item: Union[int, Expression]) -> IndexExpr:
        assert isinstance(self.annotated_type.type_name, (Array, Mapping))
        if isinstance(item, int):
            item = NumberLiteralExpr(item)
        return IndexExpr(self, item).as_type(self.annotated_type.type_name.value_type)

    def raw_index(self, item: Union[int, Expression]) -> IndexExpr:
        if isinstance(item, int):
            item = NumberLiteralExpr(item)
        return IndexExpr(self, item)

    def assign(self, val: Expression) -> AssignmentStatement:
        return AssignmentStatement(self, val)


class IdentifierExpr(LocationExpr):

    def __init__(self, idf: Union[str, Identifier], annotated_type: Optional[AnnotatedTypeName] = None):
        super().__init__()
        self.idf: Identifier = idf if isinstance(idf, Identifier) else Identifier(idf)
        self.annotated_type = annotated_type

    def get_annotated_type(self):
        return self.target.annotated_type

    def process_children(self, f: Callable[[T], T]):
        self.idf = f(self.idf)

    def slice(self, offset: int, size: int, base: Optional[Expression] = None) -> SliceExpr:
        return SliceExpr(self, base, offset, size)

class MemberAccessExpr(LocationExpr):
    def __init__(self, expr: LocationExpr, member: Identifier):
        super().__init__()
        assert isinstance(expr, LocationExpr)
        self.expr = expr
        self.member = member

    def process_children(self, f: Callable[[T], T]):
        self.expr = f(self.expr)
        self.member = f(self.member)


class IndexExpr(LocationExpr):
    def __init__(self, arr: LocationExpr, key: Expression):
        super().__init__()
        assert isinstance(arr, LocationExpr)
        self.arr = arr
        # key may be None, e.g. abi.decode(b, (uint[]))
        self.key = key

    def process_children(self, f: Callable[[T], T]):
        self.arr = f(self.arr)
        self.key = f(self.key)

    # for nested mapping
    def get_leftmost_identifier(self) -> IdentifierExpr:
        var = self.arr
        while isinstance(var, IndexExpr):
            var = var.arr
        if not isinstance(var, IdentifierExpr):
            raise exceptions.CloakCompilerError(f"the leftmost expression of {self} is not identifier expression")
        return var


class RangeIndexExpr(LocationExpr):
    def __init__(self, arr: LocationExpr, start: Optional[Expression], end: Optional[Expression]):
        self.arr = arr
        self.start = start
        self.end = end


class SliceExpr(LocationExpr):
    def __init__(self, arr: LocationExpr, base: Optional[Expression], start_offset: int, size: int):
        super().__init__()
        self.arr = arr
        self.base = base
        self.start_offset = start_offset
        self.size = size


class MeExpr(Expression):
    name = 'me'

    @property
    def is_immutable(self) -> bool:
        return True

    def __eq__(self, other):
        return isinstance(other, MeExpr)

    def __hash__(self):
        return hash('me')


class AllExpr(Expression):
    name = 'all'

    @property
    def is_immutable(self) -> bool:
        return True

    def __eq__(self, other):
        return isinstance(other, AllExpr)

    def __hash__(self):
        return hash('all')


class TeeExpr(Expression):
    name = 'tee'

    @property
    def is_immutable(self) -> bool:
        return True

    def __eq__(self, other):
        return isinstance(other, TeeExpr)

    def __hash__(self):
        return hash('tee')

class ReclassifyExpr(Expression):

    def __init__(self, expr: Expression, privacy: Expression):
        super().__init__()
        self.expr = expr
        self.privacy = privacy

    def process_children(self, f: Callable[[T], T]):
        self.expr = f(self.expr)
        self.privacy = f(self.privacy)


class Statement(AST):

    def __init__(self):
        super().__init__()
        # set by alias analysis
        self.before_analysis: Optional[PartitionState[PrivacyLabelExpr]] = None
        self.after_analysis: Optional[PartitionState[PrivacyLabelExpr]] = None
        # set by parent setter
        self.function: Optional[ConstructorOrFunctionDefinition] = None

        # set by circuit helper
        self.pre_statements = []


class IfStatement(Statement):

    def __init__(self, condition: Expression, then_branch: Block, else_branch: Optional[Block]):
        super().__init__()
        self.condition = condition
        self.then_branch = then_branch
        self.else_branch = else_branch

    def process_children(self, f: Callable[[T], T]):
        self.condition = f(self.condition)
        self.then_branch = f(self.then_branch)
        self.else_branch = f(self.else_branch)


class WhileStatement(Statement):
    def __init__(self, condition: Expression, body: Block):
        super().__init__()
        self.condition = condition
        self.body = body

    def process_children(self, f: Callable[[T], T]):
        self.condition = f(self.condition)
        self.body = f(self.body)


class DoWhileStatement(Statement):
    def __init__(self, body: Block, condition: Expression):
        super().__init__()
        self.body = body
        self.condition = condition

    def process_children(self, f: Callable[[T], T]):
        self.body = f(self.body)
        self.condition = f(self.condition)


class ForStatement(Statement):
    def __init__(self, init: Optional[SimpleStatement], condition: Expression, update: Optional[SimpleStatement], body: Block):
        super().__init__()
        self.init = init
        self.condition = condition
        self.update = update
        self.body = body

    def process_children(self, f: Callable[[T], T]):
        self.init = f(self.init)
        self.condition = f(self.condition)
        self.update = f(self.update)
        self.body = f(self.body)

    @property
    def statements(self) -> List[Statement]:
        return [self.init, self.condition, self.body, self.update]


class BreakStatement(Statement):
    pass


class CatchClause(AST):
    def __init__(self, idf: Optional[Identifier], args: List[Parameter], body: Block):
        super().__init__()
        self.idf = idf
        self.args = args or []
        self.body = body

    # def process_children(self, f):
    #     self.idf = f(self.idf)
    #     self.args[:] = map(f, self.args)
    #     self.body = f(self.body)


class TryStatement(Statement):
    def __init__(self, expr: Expression, returnParameters: List[Parameter], body: Block, catchClauses: List[CatchClause]):
        super().__init__()
        self.expr = expr
        self.returnParameters = returnParameters or []
        self.body = body
        self.catchClauses = catchClauses


class ContinueStatement(Statement):
    pass


class ReturnStatement(Statement):

    def __init__(self, expr: Expression):
        super().__init__()
        self.expr = expr

    def process_children(self, f: Callable[[T], T]):
        self.expr = f(self.expr)


class EmitStatement(Statement):
    def __init__(self, expr: Expression, args: Optional[CallArgumentList] = None):
        super().__init__()
        self.expr = expr
        self.args = args = args

    def process_children(self, f):
        self.expr = f(self.expr)
        if self.args:
            self.args = f(self.args)


class RevertStatement(Statement):
    def __init__(self, expr: Expression, args: CallArgumentList):
        super().__init__()
        self.expr = expr
        self.args = args


class AssemblyStatement(Statement):
    def __init__(self, text: str):
        super().__init__()
        self.text = text


class SimpleStatement(Statement):
    pass


class ExpressionStatement(SimpleStatement):

    def __init__(self, expr: Expression):
        super().__init__()
        self.expr = expr

    def process_children(self, f: Callable[[T], T]):
        self.expr = f(self.expr)


class RequireStatement(SimpleStatement):

    def __init__(self, condition: Expression, unmodified_code: Optional[str] = None, comment=None):
        super().__init__()
        self.condition = condition
        self.comment = comment
        self.unmodified_code = self.code() if unmodified_code is None else unmodified_code

    def process_children(self, f: Callable[[T], T]):
        self.condition = f(self.condition)


class AssignmentStatement(SimpleStatement):

    def __init__(self, lhs: Union[TupleExpr, LocationExpr], rhs: Expression, op: Optional[str] = None):
        super().__init__()
        self.lhs = lhs
        self.rhs = rhs
        self.op = '' if op is None else op

    def process_children(self, f: Callable[[T], T]):
        self.lhs = f(self.lhs)
        self.rhs = f(self.rhs)


class CircuitInputStatement(AssignmentStatement):
    pass


class StatementList(Statement):
    def __init__(self, statements: List[Statement], excluded_from_simulation: bool = False):
        super().__init__()
        self.statements = statements
        self.excluded_from_simulation = excluded_from_simulation

        # Special case, if processing a statement returns a list of statements,
        # all statements will be integrated into this block

    def process_children(self, f: Callable[[T], T]):
        new_stmts = []
        for idx, stmt in enumerate(self.statements):
            new_stmt = f(stmt)
            if new_stmt is not None:
                if isinstance(new_stmt, List):
                    new_stmts += new_stmt
                else:
                    new_stmts.append(new_stmt)
        self.statements = new_stmts

    def __getitem__(self, key: int) -> Statement:
        return self.statements[key]

    def __contains__(self, stmt: Statement):
        if stmt in self.statements:
            return True
        for s in self.statements:
            if isinstance(s, StatementList) and stmt in s:
                return True
        return False


class Block(StatementList):
    def __init__(self, statements: List[Statement], was_single_statement=False):
        super().__init__(statements)
        self.was_single_statement = was_single_statement


class IndentBlock(StatementList):
    def __init__(self, statements: List[Statement]):
        super().__init__(statements)


class TypeName(AST):
    __metaclass__ = abc.ABCMeta

    @staticmethod
    def bool_type():
        return BoolTypeName()

    @staticmethod
    def uint_type():
        return UintTypeName()

    @staticmethod
    def number_type():
        return NumberTypeName.any()

    @staticmethod
    def address_type():
        return AddressTypeName()

    @staticmethod
    def address_payable_type():
        return AddressPayableTypeName()

    # @staticmethod
    # def cipher_type(plain_type: AnnotatedTypeName):
    #     return CipherText(plain_type)

    # @staticmethod
    # def rnd_type():
    #     return Randomness()

    # @staticmethod
    # def key_type():
    #     return Key()

    # @staticmethod
    # def zk_proof_type():
    #     return ZKProof()

    # @staticmethod
    # def tee_proof_type():
    #     return TEEProof()

    @staticmethod
    def dyn_uint_array():
        return Array(AnnotatedTypeName.uint_all())

    @property
    def size_in_uints(self):
        """How many uints this type occupies when serialized."""
        return 1

    @property
    def elem_bitwidth(self) -> int:
        # Bitwidth, only defined for primitive types
        raise NotImplementedError()

    @property
    def is_literal(self) -> bool:
        return isinstance(self, (NumberLiteralType, BooleanLiteralType, EnumValueTypeName))

    def is_address(self) -> bool:
        return isinstance(self, (AddressTypeName, AddressPayableTypeName))

    def is_primitive_type(self) -> bool:
        return isinstance(self, (ElementaryTypeName, EnumTypeName, EnumValueTypeName, AddressTypeName, AddressPayableTypeName))

    # def is_cipher(self) -> bool:
    #     return isinstance(self, CipherText)

    @property
    def is_numeric(self) -> bool:
        return isinstance(self, NumberTypeName)

    @property
    def is_boolean(self) -> bool:
        return isinstance(self, (BooleanLiteralType, BoolTypeName))

    @property
    def is_signed_numeric(self) -> bool:
        return self.is_numeric and self.signed

    @property
    def is_mapping(self) -> bool:
        return isinstance(self, Mapping)

    def can_be_private(self) -> bool:
        return self.is_primitive_type() and not (self.is_signed_numeric and self.elem_bitwidth == 256)

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        assert isinstance(expected, TypeName)
        return expected == self

    def compatible_with(self, other_type: TypeName) -> bool:
        assert isinstance(other_type, TypeName)
        return self.implicitly_convertible_to(other_type) or other_type.implicitly_convertible_to(self)

    def combined_type(self, other_type: TypeName, convert_literals: bool):
        if other_type.implicitly_convertible_to(self):
            return self
        elif self.implicitly_convertible_to(other_type):
            return other_type
        return None

    # def get_type(self):
    #     n_map = self

    #     while isinstance(n_map, (Mapping, Array)) and not isinstance(n_map, CipherText):
    #         n_map = n_map.value_type.type_name
    #     
    #     return n_map

    def annotate(self, privacy_annotation):
        return AnnotatedTypeName(self, privacy_annotation)

    def __eq__(self, other):
        raise NotImplementedError()


class PrimaryExpression(LocationExpr):
    pass


class ElementaryTypeName(TypeName, PrimaryExpression):

    def __init__(self, name: str):
        super().__init__()
        self.name = name

    def __eq__(self, other):
        return isinstance(other, ElementaryTypeName) and self.name == other.name


class BoolTypeName(ElementaryTypeName):
    def __init__(self, name='bool'):
        super().__init__(name)

    @property
    def elem_bitwidth(self):
        return 1

    def __eq__(self, other):
        return isinstance(other, BoolTypeName)


class BooleanLiteralType(ElementaryTypeName):
    def __init__(self, name: bool):
        super().__init__(str(name).lower())

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        return super().implicitly_convertible_to(expected) or isinstance(expected, BoolTypeName)

    def combined_type(self, other_type: TypeName, convert_literals: bool):
        if isinstance(other_type, BooleanLiteralType):
            return TypeName.bool_type() if convert_literals else 'lit'
        else:
            return super().combined_type(other_type, convert_literals)

    @property
    def value(self):
        return self.name == 'true'

    @property
    def elem_bitwidth(self):
        return 1

    def to_abstract_type(self):
        return TypeName.bool_type()

    def __eq__(self, other):
        return isinstance(other, BooleanLiteralType)


class NumberTypeName(ElementaryTypeName):
    def __init__(self, name: str, prefix: str, signed: bool, bitwidth=None):
        assert name.startswith(prefix)
        prefix_len = len(prefix)
        super().__init__(name)
        if bitwidth is None:
            self._size_in_bits = int(name[prefix_len:]) if len(name) > prefix_len else 0
        else:
            self._size_in_bits = bitwidth
        self.signed = signed

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        return super().implicitly_convertible_to(expected) or type(expected) == NumberTypeName

    @staticmethod
    def any():
        return NumberTypeName('', '', True, 256)

    @property
    def elem_bitwidth(self):
        return 256 if self._size_in_bits == 0 else self._size_in_bits

    def can_represent(self, value: int):
        """Return true if value can be represented by this type"""
        lo = - (1 << self.elem_bitwidth - 1) if self.signed else 0
        hi = (1 << self.elem_bitwidth - 1) if self.signed else (1 << self.elem_bitwidth)
        return lo <= value < hi

    def __eq__(self, other):
        return isinstance(other, NumberTypeName) and self.name == other.name


class BytesTypeName(ElementaryTypeName):
    def __init__(self, name="bytes"):
        assert name.startswith("bytes")
        prefix_len = len("bytes")
        super().__init__(name)
        self.len = int(name[prefix_len:]) if len(name) > prefix_len else None


class StringTypeName(ElementaryTypeName):
    def __init__(self):
        super().__init__("string")


class NumberLiteralType(NumberTypeName):
    def __init__(self, name: Union[str, int]):
        name = int(name) if isinstance(name, str) else name
        blen = name.bit_length()
        if name < 0:
            signed = True
            bitwidth = blen + 1 if name != -(1 << (blen-1)) else blen
        else:
            signed = False
            bitwidth = blen
        bitwidth = max(int(math.ceil(bitwidth / 8.0)) * 8, 8)
        assert 8 <= bitwidth <= 256 and bitwidth % 8 == 0

        name = str(name)
        super().__init__(name, name, signed, bitwidth)

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        if expected.is_numeric and not expected.is_literal:
            # Allow implicit conversion only if it fits
            return expected.can_represent(self.value)
        elif expected.is_address() and self.elem_bitwidth == 160 and not self.signed:
            # Address literal case (fake solidity check will catch the cases where this is too permissive)
            return True
        return super().implicitly_convertible_to(expected)

    def combined_type(self, other_type: TypeName, convert_literals: bool):
        if isinstance(other_type, NumberLiteralType):
            return self.to_abstract_type().combined_type(other_type.to_abstract_type(), convert_literals) if convert_literals else 'lit'
        else:
            return super().combined_type(other_type, convert_literals)

    def to_abstract_type(self):
        if self.value < 0:
            return IntTypeName(f'int{self.elem_bitwidth}')
        else:
            return UintTypeName(f'uint{self.elem_bitwidth}')

    @property
    def value(self):
        return int(self.name)

    def __eq__(self, other):
        return isinstance(other, NumberLiteralType)


class IntTypeName(NumberTypeName):
    def __init__(self, name: str = 'int'):
        super().__init__(name, 'int', True)

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        # Implicitly convert smaller int types to larger int types
        return super().implicitly_convertible_to(expected) or (
                isinstance(expected, IntTypeName) and expected.elem_bitwidth >= self.elem_bitwidth)


class UintTypeName(NumberTypeName):
    def __init__(self, name: str = 'uint'):
        super().__init__(name, 'uint', False)

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        # Implicitly convert smaller uint types to larger uint types
        return super().implicitly_convertible_to(expected) or (
                isinstance(expected, UintTypeName) and expected.elem_bitwidth >= self.elem_bitwidth)


class FunctionTypeName(TypeName):
    def __init__(self, parameters: List[Parameter], modifiers: List[str], return_parameters: List[Parameter]):
        self.parameters = parameters or []
        self.modifiers = modifiers or []
        self.return_parameters = return_parameters or []


class UserDefinedTypeName(TypeName):
    def __init__(self, names: List[Identifier], target: Optional[NamespaceDefinition] = None):
        super().__init__()
        self.names = names
        self.target = target

    def __eq__(self, other):
        return isinstance(other, UserDefinedTypeName) and all(e[0].name == e[1].name for e in zip(self.target.qualified_name, other.target.qualified_name))


class EnumTypeName(UserDefinedTypeName):
    @property
    def elem_bitwidth(self):
        return 256


class EnumValueTypeName(UserDefinedTypeName):
    @property
    def elem_bitwidth(self):
        return 256

    def to_abstract_type(self):
        return EnumTypeName(self.names[:-1], self.target.parent)

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        return super().implicitly_convertible_to(expected) or (isinstance(expected, EnumTypeName) and expected.names == self.names[:-1])


class StructTypeName(UserDefinedTypeName):
    pass


class ContractTypeName(UserDefinedTypeName):
    pass


class AddressTypeName(UserDefinedTypeName):
    def __init__(self):
        super().__init__([Identifier('<address>')], None)

    @property
    def elem_bitwidth(self):
        return 160

    def __eq__(self, other):
        return isinstance(other, AddressTypeName)


class AddressPayableTypeName(UserDefinedTypeName):
    def __init__(self):
        super().__init__([Identifier('<address_payable>')], None)

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        # Implicit conversions
        return super().implicitly_convertible_to(expected) or expected == TypeName.address_type()

    @property
    def elem_bitwidth(self):
        return 160

    def __eq__(self, other):
        return isinstance(other, AddressPayableTypeName)


class Mapping(TypeName):

    def __init__(self, key_type: ElementaryTypeName, key_label: Optional[Identifier], value_type: AnnotatedTypeName):
        super().__init__()
        self.key_type = key_type
        self.key_label: Union[str, Optional[Identifier]] = key_label
        self.value_type = value_type
        # set by type checker: instantiation of the key by IndexExpr
        self.instantiated_key: Optional[Expression] = None
        

    def process_children(self, f: Callable[[T], T]):
        self.key_type = f(self.key_type)
        if isinstance(self.key_label, Identifier):
            self.key_label = f(self.key_label)
        self.value_type = f(self.value_type)

    @property
    def has_key_label(self):
        return self.key_label is not None

    def __eq__(self, other):
        if isinstance(other, Mapping):
            return self.key_type == other.key_type and self.value_type == other.value_type
        else:
            return False

    def get_map_depth(self):
        n_map = self
        
        def recursively_update_depth(n_map, depth):
            if isinstance(n_map.value_type.type_name, Mapping):
                depth += 1
                return recursively_update_depth(n_map.value_type.type_name, depth)
            else:
                return depth

        return recursively_update_depth(n_map, 1)

    def split(self) -> (int, [ElementaryTypeName], AnnotatedTypeName):
        def r_split(n_map, depth, keys):
            if isinstance(n_map, Mapping):
                return r_split(n_map.value_type.type_name, depth+1, keys+[n_map.key_type])
            return depth, keys, n_map

        return r_split(self.value_type.type_name, 1, [self.key_type])



class Array(TypeName):

    def __init__(self, value_type: TypeName, expr: Union[int, Expression] = None):
        super().__init__()
        self.value_type = value_type
        self.expr = NumberLiteralExpr(expr) if isinstance(expr, int) else expr

    def process_children(self, f: Callable[[T], T]):
        self.value_type = f(self.value_type)
        self.expr = f(self.expr)

    @property
    def size_in_uints(self):
        if self.expr is None or not isinstance(self.expr, NumberLiteralExpr):
            return -1
        else:
            return self.expr.value

    @property
    def elem_bitwidth(self):
        return self.value_type.type_name.elem_bitwidth

    def __eq__(self, other):
        if not isinstance(other, Array):
            return False
        if self.value_type != other.value_type:
            return False
        if isinstance(self.expr, NumberLiteralExpr) and isinstance(other.expr, NumberLiteralExpr) and self.expr.value == other.expr.value:
            return True
        if self.expr is None and other.expr is None:
            return True
        return False


class DummyAnnotation:
    pass


class TupleType(TypeName):
    """Does not appear in the syntax, but is necessary for type checking"""

    @staticmethod
    def ensure_tuple(t: AnnotatedTypeName):
        if t is None:
            return TupleType.empty()
        elif isinstance(t.type_name, TupleType):
            return t
        else:
            return TupleType([t])

    def __init__(self, types: List[AnnotatedTypeName]):
        super().__init__()
        self.types = types

    def __len__(self):
        return len(self.types)

    def __iter__(self):
        """Make this class iterable, by iterating over its types."""
        return self.types.__iter__()

    def __getitem__(self, i: int):
        return self.types[i]

    def check_component_wise(self, other, f):
        if isinstance(other, TupleType):
            if len(self) != len(other):
                return False
            else:
                for i in range(len(self)):
                    if not f(self[i], other[i]):
                        return False
                return True
        else:
            return False

    def implicitly_convertible_to(self, expected: TypeName) -> bool:
        return self.check_component_wise(expected, lambda x, y: x.type_name.implicitly_convertible_to(y.type_name))

    def compatible_with(self, other_type: TypeName) -> bool:
        return self.check_component_wise(other_type, lambda x, y: x.type_name.compatible_with(y.type_name))

    def combined_type(self, other_type: TupleType, convert_literals: bool):
        if not isinstance(other_type, TupleType) or len(self.types) != len(other_type.types):
            return None
        return TupleType([AnnotatedTypeName(e1.type_name.combined_type(e2.type_name, convert_literals), DummyAnnotation()) for e1, e2 in zip(self.types, other_type.types)])

    def annotate(self, privacy_annotation):
        if isinstance(privacy_annotation, Expression):
            return AnnotatedTypeName(TupleType([t.type_name.annotate(privacy_annotation) for t in self.types]))
        else:
            assert len(self.types) == len(privacy_annotation)
            return AnnotatedTypeName(TupleType([t.type_name.annotate(a) for t, a in zip(self.types, privacy_annotation)]))

    def perfect_privacy_match(self, other):
        def privacy_match(self: AnnotatedTypeName, other: AnnotatedTypeName):
            return self.privacy_annotation == other.privacy_annotation

        self.check_component_wise(other, privacy_match)

    @staticmethod
    def empty() -> TupleType:
        return TupleType([])

    def __eq__(self, other):
        return self.check_component_wise(other, lambda x, y: x == y)


class FunctionTypeName(TypeName):
    def __init__(self, parameters: List[Parameter], modifiers: List[str], return_parameters: List[Parameter]):
        super().__init__()
        self.parameters = parameters
        self.modifiers = modifiers
        self.return_parameters = return_parameters

    def process_children(self, f: Callable[[T], T]):
        self.parameters[:] = map(f, self.parameters)
        self.return_parameters[:] = map(f, self.return_parameters)

    def __eq__(self, other):
        return isinstance(other, FunctionTypeName) and self.parameters == other.parameters and \
               self.modifiers == other.modifiers and self.return_parameters == other.return_parameters


class AnnotatedTypeName(AST):

    def __init__(self, type_name: TypeName, privacy_annotation: Optional[Expression] = None):
        super().__init__()
        self.type_name = type_name
        self.had_privacy_annotation = privacy_annotation is not None
        if self.had_privacy_annotation:
            self.privacy_annotation = privacy_annotation
        else:
            self.privacy_annotation = AllExpr()

    def process_children(self, f: Callable[[T], T]):
        self.type_name = f(self.type_name)
        self.privacy_annotation = f(self.privacy_annotation)

    # @property
    # def zkay_type(self) -> AnnotatedTypeName:
    #     if isinstance(self.type_name, CipherText):
    #         return self.type_name.plain_type
    #     else:
    #         return self

    def __eq__(self, other):
        if isinstance(other, AnnotatedTypeName):
            return self.type_name == other.type_name and self.privacy_annotation == other.privacy_annotation
        else:
            return False

    def combined_privacy(self, analysis: PartitionState[PrivacyLabelExpr], other: AnnotatedTypeName):
        if isinstance(self.type_name, TupleType):
            assert isinstance(other.type_name, TupleType) and len(self.type_name.types) == len(other.type_name.types)
            return [e1.combined_privacy(analysis, e2) for e1, e2 in zip(self.type_name.types, other.type_name.types)]

        p_expected = other.privacy_annotation.privacy_annotation_label()
        p_actual = self.privacy_annotation.privacy_annotation_label()
        if p_expected and p_actual:
            if p_expected == p_actual or (analysis is not None and analysis.same_partition(p_expected, p_actual)):
                return self.privacy_annotation
            elif self.privacy_annotation.is_all_expr():
                return other.privacy_annotation
        else:
            return None

    def is_public(self):
        return self.privacy_annotation.is_all_expr()

    def is_private(self):
        return not self.is_public()

    def is_address(self) -> bool:
        return isinstance(self.type_name, (AddressTypeName, AddressPayableTypeName))

    # def is_cipher(self) -> bool:
    #     return isinstance(self.type_name, CipherText)

    @staticmethod
    def uint_all():
        return AnnotatedTypeName(TypeName.uint_type())

    @staticmethod
    def bool_all():
        return AnnotatedTypeName(TypeName.bool_type())

    @staticmethod
    def address_all():
        return AnnotatedTypeName(TypeName.address_type())

    # @staticmethod
    # def cipher_type(plain_type: AnnotatedTypeName):
    #     return AnnotatedTypeName(TypeName.cipher_type(plain_type))

    @staticmethod
    def key_type():
        return AnnotatedTypeName(TypeName.key_type())

    @staticmethod
    def zk_proof_type():
        return AnnotatedTypeName(TypeName.zk_proof_type())

    @staticmethod
    def tee_proof_type():
        return AnnotatedTypeName(TypeName.tee_proof_type())

    @staticmethod
    def all(type: TypeName):
        return AnnotatedTypeName(type, Expression.all_expr())

    @staticmethod
    def me(type: TypeName):
        return AnnotatedTypeName(type, Expression.me_expr())

    @staticmethod
    def array_all(value_type: AnnotatedTypeName, *length: int):
        t = value_type
        for l in length:
            t = AnnotatedTypeName(Array(t, NumberLiteralExpr(l)))
        return t


class IdentifierDeclaration(AST):

    def __init__(self, keywords: List[str], annotated_type: AnnotatedTypeName, idf: Identifier, storage_location: Optional[str] = None):
        super().__init__()
        self.keywords = keywords
        self.annotated_type = annotated_type
        self.idf = idf
        self.storage_location = storage_location

    @property
    def is_final(self) -> bool:
        return 'final' in self.keywords

    @property
    def is_constant(self) -> bool:
        return 'constant' in self.keywords

    def process_children(self, f: Callable[[T], T]):
        self.annotated_type = f(self.annotated_type)
        self.idf = f(self.idf)


class VariableDeclaration(IdentifierDeclaration):

    def __init__(self, keywords: List[str], annotated_type: AnnotatedTypeName, idf: Identifier, storage_location: Optional[str] = None):
        super().__init__(keywords, annotated_type, idf, storage_location)


class VariableDeclarationStatement(SimpleStatement):

    def __init__(self, variable_declaration: VariableDeclaration, expr: Optional[Expression] = None):
        """

        :param variable_declaration:
        :param expr: can be None
        """
        super().__init__()
        self.variable_declaration = variable_declaration
        self.expr = expr

    def process_children(self, f: Callable[[T], T]):
        self.variable_declaration = f(self.variable_declaration)
        self.expr = f(self.expr)


class TupleVariableDeclarationStatement(SimpleStatement):
    def __init__(self, vs: [VariableDeclaration], expr: Expression):
        self.vs = vs
        self.expr = expr


class Parameter(IdentifierDeclaration):

    def __init__(
            self,
            keywords: List[str],
            annotated_type: AnnotatedTypeName,
            idf: Identifier,
            storage_location: Optional[str] = None):
        super().__init__(keywords, annotated_type, idf, storage_location)

    def copy(self) -> Parameter:
        return Parameter(self.keywords, self.annotated_type, self.idf if self.idf else None, self.storage_location)

    def with_changed_storage(self, match_storage: str, new_storage: str) -> Parameter:
        if self.storage_location == match_storage:
            self.storage_location = new_storage
        return self


class NamespaceDefinition(AST):
    def __init__(self, idf: Identifier):
        super().__init__()
        self.idf = idf

    def process_children(self, f: Callable[[T], T]):
        oldidf = self.idf
        self.idf = f(self.idf)
        assert oldidf == self.idf # must be readonly


class FunctionPrivacyType(IntEnum):
    PUB = 0
    ZKP = 1
    MPC = 2
    TEE = 3


class ModifierInvocation(AST):
    def __init__(self, path: List[str], args: CallArgumentList):
        super().__init__()
        self.path = path or []
        self.args = args

    def process_children(self, f):
        self.args = f(self.args)


class OverrideSpecifier(AST):
    def __init__(self, paths: List[List[str]]):
        super().__init__()
        self.paths = paths


class ConstructorOrFunctionDefinition(NamespaceDefinition):

    def __init__(self, idf: Identifier, parameters: List[Parameter], modifiers: List[Union[str, AST]],
            return_parameters: Optional[List[Parameter]], body: Optional[Block] = None, kind: str = "function"):
        assert idf
        super().__init__(idf)
        self.kind = kind
        self.parameters = parameters
        self.modifiers = modifiers
        self.body = body
        self.return_parameters = [] if return_parameters is None else return_parameters

        # specify parent type
        self.parent: Optional[ContractDefinition] = None
        self.original_body: Optional[Block] = None

        # Set function type
        self.annotated_type = None
        self._update_fct_type()

        # Analysis properties
        self.called_functions: OrderedDict[ConstructorOrFunctionDefinition, None] = OrderedDict()
        self.is_recursive = False
        self.has_static_body = True
        self.can_be_private = True

        # # True if this function contains private expressions
        # self.requires_verification = False

        # True if this function is public and either requires verification or has private arguments
        self.requires_verification_when_external = False
        
        self.privacy_type: FunctionPrivacyType = FunctionPrivacyType.PUB

        # True if this funtion inside contains privacy variable
        self.is_privacy_related_function = False

        # assigned by type_checker
        self.privacy_related_params = None
        self.mutate_states = []

        self.return_var_decls: List[VariableDeclaration] = [
            VariableDeclaration([], rp.annotated_type, Identifier(f'{cfg.return_zk_var_name}_{idx}'), rp.storage_location) \
            if self.is_zkp() else VariableDeclaration([], rp.annotated_type, Identifier(f'{cfg.return_tee_var_name}_{idx}'), rp.storage_location) \
            for idx, rp in enumerate(self.return_parameters)
        ]

        for vd in self.return_var_decls:
            vd.idf.parent = vd

    @property
    def requires_verification(self) -> bool:
        if self.is_zkp():
            return True
        return False

    @requires_verification.setter
    def requires_verification(self, req_verify):
        if req_verify:
            self.privacy_type = FunctionPrivacyType.ZKP

    @property
    def has_side_effects(self) -> bool:
        return not ('pure' in self.modifiers or 'view' in self.modifiers)

    @property
    def can_be_external(self) -> bool:
        return not ('private' in self.modifiers or 'internal' in self.modifiers)

    @property
    def is_external(self) -> bool:
        return 'external' in self.modifiers

    @property
    def is_payable(self) -> bool:
        return 'payable' in self.modifiers

    @property
    def name(self) -> str:
        return self.idf.name

    @property
    def return_type(self) -> TupleType:
        return TupleType([p.annotated_type for p in self.return_parameters])

    @property
    def parameter_types(self) -> TupleType:
        return TupleType([p.annotated_type for p in self.parameters])

    @property
    def is_constructor(self) -> bool:
        return self.kind == "constructor"

    @property
    def is_function(self) -> bool:
        return self.kind == "function"

    def _update_fct_type(self):
        self.annotated_type = AnnotatedTypeName(FunctionTypeName(self.parameters, self.modifiers, self.return_parameters))

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.parameters[:] = map(f, self.parameters)
        self.modifiers[:] = map(lambda x: x if isinstance(x, str) else f(x), self.modifiers)
        self.return_parameters[:] = map(f, self.return_parameters)
        if self.body:
            self.body = f(self.body)

    def add_param(self, t: Union[TypeName, AnnotatedTypeName], idf: Union[str, Identifier], ref_storage_loc: str = 'memory'):
        t = t if isinstance(t, AnnotatedTypeName) else AnnotatedTypeName(t)
        idf = Identifier(idf) if isinstance(idf, str) else idf
        storage_loc = '' if t.type_name.is_primitive_type() else ref_storage_loc
        self.parameters.append(Parameter([], t, idf, storage_loc))
        self._update_fct_type()

    def get_privacy_participants_by_id(self, id):
        for idf in self.privacy_participants:
            if isinstance(idf, Identifier) and id == idf.name:
                return idf
        return None

    def is_pub(self):
        return FunctionPrivacyType.PUB == self.privacy_type

    def is_zkp(self):
        return FunctionPrivacyType.ZKP == self.privacy_type

    def is_tee(self):
        return FunctionPrivacyType.TEE == self.privacy_type


class ModifierDefinition(NamespaceDefinition):
    def __init__(self, idf: Identifier, parameters: List[Parameter], virtual: bool = False,
            overrideSpecifiers: List[OverrideSpecifier] = None, body: Optional[Block] = None):
        super().__init__(idf)
        self.parameters = parameters
        self.virtual = virtual
        self.overrideSpecifiers = overrideSpecifiers
        self.body = body

    def process_children(self, f):
        super().process_children(f)
        self.parameters[:] = map(f, self.parameters)
        self.overrideSpecifiers[:] = map(f, self.overrideSpecifiers)
        if self.body:
            self.body = f(self.body)


class ConstantVariableDeclaration(IdentifierDeclaration):
    def __init__(annotated_type, idf, expr):
        super().__init__([], annotated_type, idf)
        self.expr = expr

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.expr = f(self.expr)


class StateVariableDeclaration(IdentifierDeclaration):

    def __init__(self, annotated_type: AnnotatedTypeName, keywords: List[str], idf: Identifier,
            expr: Optional[Expression], overrideSpecifier: Optional[OverrideSpecifier] = None):
        super().__init__(keywords, annotated_type, idf)
        self.expr = expr
        self.overrideSpecifier = overrideSpecifier

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.expr = f(self.expr)


class EnumValue(AST):
    def __init__(self, idf: Identifier):
        super().__init__()
        self.idf = idf
        self.annotated_type: Optional[AnnotatedTypeName] = None

    def process_children(self, f: Callable[[T], T]):
        self.idf = f(self.idf)


class EnumDefinition(NamespaceDefinition):
    def __init__(self, idf: Identifier, values: List[EnumValue]):
        super().__init__(idf)
        self.values = values

        self.annotated_type: Optional[AnnotatedTypeName] = None

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.values[:] = map(f, self.values)


class UserDefinedValueTypeDefinition(NamespaceDefinition):
    def __init__(self, idf: Identifier, underlying_type: ElementaryTypeName):
        super().__init__(idf)
        self.underlying_type = underlying_type

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.underlying_type = f(self.underlying_type)


class EventParameter(AST):
    def __init__(self, annotated_type: AnnotatedTypeName, indexed: Optional[str] = None, name: Optional[Identifier] = None):
        super().__init__()
        self.annotated_type = annotated_type
        self.indexed = indexed
        self.name = name

    def process_children(self, f: Callable[[T], T]):
        self.annotated_type = f(self.annotated_type)
        if self.name:
            self.name = f(self.name)


class EventDefinition(NamespaceDefinition):
    def __init__(self, idf: Identifier, parameters: List[EventParameter], anonymous: Optional[str] = None):
        super().__init__(idf)
        self.parameters = parameters or []
        self.anonymous = anonymous

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.parameters[:] = map(f, self.parameters)


class ErrorParameter(AST):
    def __init__(self, annotated_type: AnnotatedTypeName, name: Optional[Identifier] = None):
        super().__init__()
        self.annotated_type = annotated_type
        self.name = name

    def process_children(self, f: Callable[[T], T]):
        self.annotated_type = f(self.annotated_type)
        if self.name:
            self.name = f(self.name)


class ErrorDefinition(NamespaceDefinition):
    def __init__(self, idf: Identifier, parameters: List[Parameter]):
        super().__init__(idf)
        self.parameters = parameters or []

    def process_children(self, f):
        super().process_children(f)
        self.parameters[:] = map(f, self.parameters)


class UsingDirective(AST):
    def __init__(self, path: List[str], type_name: Optional[TypeName] = None):
        self.path = path
        self.type_name = type_name

    def process_children(self, f):
        self.type_name = f(self.type_name)


class StructDefinition(NamespaceDefinition):
    def __init__(self, idf: Identifier, members: List[VariableDeclaration]):
        super().__init__(idf)
        self.members = members

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.members[:] = map(f, self.members)


class ContractDefinition(NamespaceDefinition):

    def __init__(self, idf: Identifier, units: List[AST]):
        super().__init__(idf)
        self.units = units

        # extra body parts
        self.extra_head_parts: Liast[AST] = []
        self.extra_tail_parts: Liast[AST] = []

    @property
    def function_definitions(self):
        return [u for u in self.units if isinstance(u, ConstructorOrFunctionDefinition) and u.kind == "function"]

    @property
    def constructor_definitions(self):
        return [u for u in self.units if isinstance(u, ConstructorOrFunctionDefinition) and u.kind == "constructor"]

    @property
    def state_variable_declarations(self):
        return [u for u in self.units if isinstance(u, StateVariableDeclaration)]

    def process_children(self, f: Callable[[T], T]):
        super().process_children(f)
        self.units[:] = map(f, self.units)

    def __getitem__(self, key: str):
        if key == 'constructor':
            if len(self.constructor_definitions) == 0:
                # return empty constructor
                c = ConstructorOrFunctionDefinition(None, [], [], None, Block([]))
                c.parent = self
                return c
            elif len(self.constructor_definitions) == 1:
                return self.constructor_definitions[0]
            else:
                raise ValueError('Multiple constructors exist')
        else:
            d_identifier = self.names[key]
            return d_identifier.parent

    def states_types(self) -> Dict[str, TypeName]:
        res = {}
        for v in self.units:
            if isinstance(v, StateVariableDeclaration):
                res[v.idf.name] = v.annotated_type.type_name
        return res


class PragmaDirective(AST):
    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version


class ImportDirective(AST):
    def __init__(self, import_path: str, unitAlias: Optional[str] = None, aliases: Optional[Dict[str, str]] = None):
        self.path = import_path
        self.unitAlias = unitAlias
        self.aliases = aliases or {}
        # bond definition of symbols
        self.processed_aliases = {}


class InheritanceSpecifier(AST):
    def __init__(self, path: List[str], args: CallArgumentList):
        self.path = path
        self.args = args

    def process_children(self, f):
        self.args = f(self.args)


class InterfaceDefinition(AST):
    def __init__(self, name: str, inheritanceSpecifiers: List[InheritanceSpecifier], body_elems: List[AST]):
        self.name = name
        self.inheritanceSpecifiers = inheritanceSpecifiers or []
        self.body_elems = body_elems or []

    def process_children(self, f):
        self.inheritanceSpecifiers[:] = map(f, self.inheritanceSpecifiers)
        self.body_elems[:] = map(f, self.body_elems)


class LibraryDefinition(AST):
    def __init__(self, name: str, body_elems: List[AST]):
        self.name = name
        self.body_elems = body_elems or []

    def process_children(self, f):
        self.body_elems[:] = map(f, self.body_elems)


class SourceUnit(AST):

    def __init__(self, units: List[AST] = None, sba: AST = None):
        super().__init__()
        self.units = units or []

        self.extra_head_parts: List[AST] = []

        # sba: sol badguy ast, for generating expression/statement/function_definition from string
        self.sba = sba

        self.privacy_policy = None
        self.generated_policy: Optional[str] = None
        self.original_code: List[str] = []

    @property
    def contracts(self):
        return [u for u in self.units if isinstance(u, ContractDefinition)]

    def process_children(self, f: Callable[[T], T]):
        self.units[:] = map(f, self.units)

    def __getitem__(self, key: str):
        c_identifier = self.names[key]
        c = c_identifier.parent
        assert (isinstance(c, ContractDefinition))
        return c


PrivacyLabelExpr = Union[MeExpr, AllExpr, TeeExpr, Identifier]
TargetDefinition = Union[IdentifierDeclaration, NamespaceDefinition]


def get_privacy_expr_from_label(plabel: PrivacyLabelExpr):
    """Turn privacy label into expression (i.e. Identifier -> IdentifierExpr, Me and All stay the same)."""
    if isinstance(plabel, Identifier):
        return IdentifierExpr(plabel, AnnotatedTypeName.address_all()).override(target=plabel.parent)
    else:
        return plabel


class InstanceTarget(tuple):
    def __new__(cls, expr: Union[tuple, VariableDeclaration, LocationExpr]):
        if isinstance(expr, tuple):
            # copy constructor
            target_key = expr[:]
        else:
            target_key = [None, None, None]
            if isinstance(expr, VariableDeclaration):
                target_key[0] = expr
            elif isinstance(expr, IdentifierExpr):
                target_key[0] = expr.target
            elif isinstance(expr, MemberAccessExpr):
                target_key[0] = expr.expr.target
                target_key[1] = expr.member
            else:
                assert isinstance(expr, IndexExpr)
                target_key[0] = expr.get_leftmost_identifier().target
                target_key[1] = expr.key
                target_key[2] = expr

        assert isinstance(target_key[0], (VariableDeclaration, Parameter, StateVariableDeclaration))
        return super(InstanceTarget, cls).__new__(cls, target_key)

    def __eq__(self, other):
        return isinstance(other, type(self)) and super().__eq__(other)

    def __hash__(self):
        return hash(self[:])

    @property
    def target(self) -> IdentifierDeclaration:
        return self[0]

    @property
    def key(self) -> Optional[Union[Identifier, Expression]]:
        return self[1]

    # @property
    # def privacy(self) -> PrivacyLabelExpr:
    #     if self.key is None or not isinstance(self.target.annotated_type.type_name, Mapping):
    #         return self.target.annotated_type.zkay_type.privacy_annotation.privacy_annotation_label()
    #     else:
    #         t = self.target.annotated_type.zkay_type.type_name
    #         assert isinstance(t, Mapping)
    #         if t.has_key_label:
    #             return self.key.privacy_annotation_label()
    #         else:
    #             t.value_type.privacy_annotation.privacy_annotation_label()

    def in_scope_at(self, ast: AST) -> bool:
        from cloak.cloak_ast.pointers.symbol_table import SymbolTableLinker
        return SymbolTableLinker.in_scope_at(self.target.idf, ast)


# UTIL FUNCTIONS


def indent(s: str):
    return textwrap.indent(s, cfg.indentation)


# EXCEPTIONS


def get_code_error_msg(line: int, column: int, code: List[str], ctr: Optional[ContractDefinition] = None,
                       fct: Optional[ConstructorOrFunctionDefinition] = None, stmt: Optional[Statement] = None):
    # Print Location
    error_msg = f'At line: {line};{column}'

    # If error location is outside code bounds, only show line;col
    if line <= 0 or column <= 0 or line > len(code):
        return error_msg

    if fct is not None:
        assert ctr is not None
        error_msg += f', in function \'{fct.name}\' of contract \'{ctr.idf.name}\''
    elif ctr is not None:
        error_msg += f', in contract \'{ctr.idf.name}\''
    error_msg += '\n'

    start_line = line if stmt is None else stmt.line
    if start_line != -1:
        for line in range(start_line, line + 1):
            # replace tabs with 4 spaces for consistent output
            orig_line: str = code[line - 1]
            orig_line = orig_line.replace('\t', '    ')
            error_msg += f'{orig_line}\n'

        affected_line: str = code[line - 1]
        loc_string = ''.join('----' if c == '\t' else '-' for c in affected_line[:column - 1])
        return f'{error_msg}{loc_string}/'
    else:
        return error_msg


def get_ast_exception_msg(ast: AST, msg: str):
    # Get surrounding statement
    if isinstance(ast, Expression):
        stmt = ast.statement
    elif isinstance(ast, Statement):
        stmt = ast
    else:
        stmt = None

    # Get surrounding function
    if stmt is not None:
        fct = stmt.function
    elif isinstance(ast, ConstructorOrFunctionDefinition):
        fct = ast
    else:
        fct = None

    # Get surrounding contract
    ctr = ast if fct is None else fct
    while ctr is not None and not isinstance(ctr, ContractDefinition):
        ctr = ctr.parent

    # Get source root
    root = ast if ctr is None else ctr
    while root is not None and not isinstance(root, SourceUnit):
        root = root.parent

    if root is None:
        error_msg = 'error'
    else:
        error_msg = get_code_error_msg(ast.line, ast.column, root.original_code, ctr, fct, stmt)

    return f'\n{error_msg}\n\n{msg}'


def issue_compiler_warning(ast: AST, warning_type: str, msg: str):
    if cfg.is_unit_test:
        return
    with warn_print():
        zk_print(f'\n\nWARNING: {warning_type}{get_ast_exception_msg(ast, msg)}')


class AstException(Exception):
    """Generic exception for errors in an AST"""

    def __init__(self, msg, ast):
        super().__init__(get_ast_exception_msg(ast, msg))


# CODE GENERATION

class CodeVisitor(AstVisitor):

    def __init__(self, display_final=True, for_solidity=False):
        super().__init__('node-or-children')
        self.display_final = not for_solidity and display_final
        self.for_solidity = for_solidity

    def visit_list(self, l: List[Union[AST, str]], sep='\n'):
        if l is None:
            return 'None'

        def handle(e: Union[AST, str]):
            if isinstance(e, str):
                return e
            else:
                return self.visit(e)

        s = filter(None.__ne__, [handle(e) for e in l])
        s = sep.join(s)
        return s

    def visit_single_or_list(self, v: Union[List[AST], AST, str], sep='\n'):
        if isinstance(v, List):
            return self.visit_list(v, sep)
        elif isinstance(v, str):
            return v
        else:
            return self.visit(v)

    def visitAST(self, ast: AST):
        # should never be called
        raise NotImplementedError("Did not implement code generation for " + repr(ast))

    def visitComment(self, ast: Comment):
        if ast.text == '':
            return ''
        elif ast.text.find('\n') != -1:
            return f'/* {ast.text} */'
        else:
            return f'// {ast.text}'

    def visitIdentifier(self, ast: Identifier):
        return ast.name

    def visitFunctionCallExpr(self, ast: FunctionCallExpr):
        if isinstance(ast.func, BuiltinFunction):
            args = [self.visit(a) for a in ast.args.args]
            return ast.func.format_string().format(*args)
        f = self.visit(ast.func)
        a = self.visit(ast.args) if ast.functionCallOptions else f"({self.visit(ast.args)})"
        return f'{f}{a}'

    def visitNamedArgument(self, ast: NamedArgument) -> str:
        return f"{ast.key}: {self.visit(ast.value)}"

    def visitCallArgumentList(self, ast: CallArgumentList) -> str:
        args = self.visit_list(ast.args, ", ")
        return f"{{{args}}}" if ast.named_arguments else args

    def visitPrimitiveCastExpr(self, ast: PrimitiveCastExpr):
        if ast.is_implicit:
            return self.visit(ast.expr)
        else:
            return f'{self.visit(ast.elem_type)}({self.visit(ast.expr)})'

    def visitBooleanLiteralExpr(self, ast: BooleanLiteralExpr):
        return str(ast.value).lower()

    def visitNumberLiteralExpr(self, ast: NumberLiteralExpr):
        if ast.source_text is not None:
            return ast.source_text
        else:
            unit = f" {ast.unit}" if ast.unit else ""
            value = hex(ast.value) if ast.was_hex else str(ast.value)
            return f"{value}{unit}"

    def visitStringLiteralExpr(self, ast: StringLiteralExpr):
        return f'\'{ast.value}\''

    def visitArrayLiteralExpr(self, ast: ArrayLiteralExpr):
        return f'[{self.visit_list(ast.values, sep=", ")}]'

    def visitTupleExpr(self, ast: TupleExpr):
        return f'({self.visit_list(ast.elements, sep=", ")})'

    def visitIdentifierExpr(self, ast: IdentifierExpr):
        return self.visit(ast.idf)

    def visitMemberAccessExpr(self, ast: MemberAccessExpr):
        return f'{self.visit(ast.expr)}.{self.visit(ast.member)}'

    def visitIndexExpr(self, ast: IndexExpr):
        key = ''
        if ast.key is not None:
            key = self.visit(ast.key)
        return f'{self.visit(ast.arr)}[{key}]'

    def visitMeExpr(self, _: MeExpr):
        if self.for_solidity:
            return 'msg.sender'
        return 'me'

    def visitAllExpr(self, _: AllExpr):
        return 'all'

    def visitTeeExpr(self, _: TeeExpr):
        return 'tee'

    def visitReclassifyExpr(self, ast: ReclassifyExpr):
        e = self.visit(ast.expr)
        if self.for_solidity:
            return e
        p = self.visit(ast.privacy)
        return f'reveal({e}, {p})'

    def visitIfStatement(self, ast: IfStatement):
        c = self.visit(ast.condition)
        t = self.visit_single_or_list(ast.then_branch)
        # if ast.get_related_function().privacy_type == FunctionPrivacyType.TEE:
        #     # TODO: delete redundant table before statements
        #     ret = f'{t[1:-1]}' if t else ''
        #     if ast.else_branch:
        #         e = self.visit_single_or_list(ast.else_branch)
        #         ret += f'{e[1:-1]}' if e else ''
        # else:
        ret = f'if ({c}) {t}'
        if ast.else_branch:
            e = self.visit_single_or_list(ast.else_branch)
            ret += f'\n else {e}'

        return ret

    def visitWhileStatement(self, ast: WhileStatement):
        c = self.visit(ast.condition)
        b = self.visit_single_or_list(ast.body)
        ret = f'while ({c}) {b}'
        return ret

    def visitDoWhileStatement(self, ast: DoWhileStatement):
        b = self.visit_single_or_list(ast.body)
        c = self.visit(ast.condition)
        ret = f'do {b} while ({c});'
        return ret

    def visitForStatement(self, ast: ForStatement):
        i = ';' if ast.init is None else f'{self.visit_single_or_list(ast.init)}'
        c = self.visit(ast.condition)
        u = '' if ast.update is None else f' {self.visit_single_or_list(ast.update).replace(";", "")}'
        b = self.visit_single_or_list(ast.body)
        ret = f'for ({i} {c};{u}) {b}'
        return ret

    def visitBreakStatement(self, _: BreakStatement):
        return 'break;'

    def visitContinueStatement(self, _: ContinueStatement):
        return 'continue;'

    def visitReturnStatement(self, ast: ReturnStatement):
        if ast.expr:
            e = self.visit(ast.expr)
            return f'return {e};'
        else:
            return 'return;'

    def visitExpressionStatement(self, ast: ExpressionStatement):
        return self.visit(ast.expr) + ';'

    def visitRequireStatement(self, ast: RequireStatement):
        c = self.visit(ast.condition)
        if ast.comment:
            return f'require({c}, {ast.comment});'
        return f'require({c});'

    def visitAssignmentStatement(self, ast: AssignmentStatement):
        lhs = ast.lhs
        op = ast.op
        if ast.lhs.annotated_type is not None and ast.lhs.annotated_type.is_private():
            op = ''
        rhs = ast.rhs.args.args[1] if op else ast.rhs

        if op.startswith('pre'):
            op = op[3:]
            fstr = '{1}{0};'
        elif op.startswith('post'):
            op = op[4:]
            fstr = '{0}{1};'
        else:
            fstr = '{} {}= {};'

        if isinstance(lhs, SliceExpr) and isinstance(rhs, SliceExpr):
            assert lhs.size == rhs.size, "Slice ranges don't have same size"
            s = ''
            lexpr, rexpr = self.visit(lhs.arr), self.visit(rhs.arr)
            lbase = '' if lhs.base is None else f'{self.visit(lhs.base)} + '
            rbase = '' if rhs.base is None else f'{self.visit(rhs.base)} + '
            for i in range(lhs.size):
                s += fstr.format(f'{lexpr}[{lbase}{lhs.start_offset + i}]', op, f'{rexpr}[{rbase}{rhs.start_offset + i}]') + '\n'
            return s[:-1]
        else:
            lhs = self.visit(lhs)
            rhs = self.visit(rhs)
            return fstr.format(lhs, op, rhs)

    def handle_block(self, ast: StatementList):
        s = self.visit_list(ast.statements)
        s = indent(s)
        return s

    def visitStatementList(self, ast: StatementList):
        return self.visit_list(ast.statements)

    def visitBlock(self, ast: Block):
        b = self.handle_block(ast).rstrip()
        if ast.was_single_statement and len(ast.statements) == 1:
            return b
        else:
            return f'{{\n{b}\n}}'

    def visitIndentBlock(self, ast: IndentBlock):
        return self.handle_block(ast)

    def visitElementaryTypeName(self, ast: ElementaryTypeName):
        return ast.name

    def visitUserDefinedTypeName(self, ast: UserDefinedTypeName):
        return self.visit_list(ast.names, '.')

    def visitAddressTypeName(self, ast: AddressTypeName):
        return 'address'

    def visitAddressPayableTypeName(self, ast: AddressPayableTypeName):
        return 'address payable'

    def visitAnnotatedTypeName(self, ast: AnnotatedTypeName):
        t = self.visit(ast.type_name)
        if not self.for_solidity and ast.had_privacy_annotation:
            p = self.visit(ast.privacy_annotation)
            return f'{t}@{p}'
        return t

    def visitMapping(self, ast: Mapping):
        k = self.visit(ast.key_type)
        v = self.visit(ast.value_type)
        label = ''
        if not self.for_solidity:
            if isinstance(ast.key_label, Identifier):
                label = '!' + self.visit(ast.key_label)
            else:
                label = f'/*!{ast.key_label}*/' if ast.key_label is not None else ''
        return f"mapping({k}{label} => {v})"

    def visitArray(self, ast: Array):
        t = self.visit(ast.value_type)
        if ast.expr is not None:
            e = self.visit(ast.expr)
        else:
            e = ''
        return f'{t}[{e}]'

    # def visitCipherText(self, ast: CipherText):
    #     e = self.visitArray(ast)
    #     return f'{e}/*{ast.plain_type.code()}*/'

    def visitTupleType(self, ast: TupleType):
        s = self.visit_list(ast.types, ', ')
        return f'({s})'

    def visitVariableDeclaration(self, ast: VariableDeclaration):
        keywords = [k for k in ast.keywords if self.display_final or k != 'final']
        k = ' '.join(keywords)
        t = self.visit(ast.annotated_type)
        s = '' if not ast.storage_location else f' {ast.storage_location}'
        i = self.visit(ast.idf)
        return f'{k} {t}{s} {i}'.strip()

    def visitVariableDeclarationStatement(self, ast: VariableDeclarationStatement):
        s = self.visit(ast.variable_declaration)
        if ast.expr:
            s += ' = ' + self.visit(ast.expr)
        s += ';'
        return s

    def visitParameter(self, ast: Parameter):
        if not self.display_final:
            f = None
        else:
            f = 'final' if 'final' in ast.keywords else None
        t = self.visit(ast.annotated_type)
        if ast.idf is None:
            i = None
        else:
            i = self.visit(ast.idf)

        description = [f, t, ast.storage_location, i]
        description = [d for d in description if d is not None]
        s = ' '.join(description)
        return s

    def visitConstructorOrFunctionDefinition(self, ast: ConstructorOrFunctionDefinition):
        definition = ast.kind
        if ast.kind == 'function':
            definition += f' {ast.idf.name}'
        ps = self.visit_list(ast.parameters, ', ')
        modifiers = ' '.join(map(lambda x: x if isinstance(x, str) else self.visit(x), ast.modifiers))
        if modifiers != '':
            modifiers = f' {modifiers}'
        rs = self.visit_list(ast.return_parameters, ', ')
        if rs != '':
            rs = f' returns ({rs})'
        body = ";"
        if ast.body:
            body = self.visit_single_or_list(ast.body)

        return f"{definition}({ps}){modifiers}{rs} {body}"

    def visitEnumValue(self, ast: EnumValue):
        return self.visit(ast.idf)

    def visitEnumDefinition(self, ast: EnumDefinition):
        values = self.visit_list(ast.values, sep=', ')
        return f'enum {self.visit(ast.idf)} {{\n{indent(values)}\n}}'

    @staticmethod
    def __cmp_type_size(v1: VariableDeclaration, v2: VariableDeclaration):
        t1, t2 = v1.annotated_type.type_name, v2.annotated_type.type_name
        cmp = (t1.size_in_uints > t2.size_in_uints) - (t1.size_in_uints < t2.size_in_uints)
        if cmp == 0:
            cmp = (t1.elem_bitwidth > t2.elem_bitwidth) - (t1.elem_bitwidth < t2.elem_bitwidth)
        return cmp

    def visitStructDefinition(self, ast: StructDefinition):
        members = self.visit_list(ast.members, ";\n") + ";"
        return f'struct {self.visit(ast.idf)} {{\n{indent(members)}\n}}'

    def visitStateVariableDeclaration(self, ast: StateVariableDeclaration):
        keywords = [k for k in ast.keywords if self.display_final or k != 'final']
        f = 'final ' if 'final' in keywords else ''
        t = self.visit(ast.annotated_type)
        k = ' '.join([k for k in keywords if k != 'final'])
        if k != '':
            k = f'{k} '
        i = self.visit(ast.idf)
        overrideSpecifier = f' {self.visit(ast.overrideSpecifier)}' if ast.overrideSpecifier else ''
        ret = f"{f}{t} {k}{overrideSpecifier}{i}".strip()
        if ast.expr:
            ret += ' = ' + self.visit(ast.expr)
        return ret + ';'

    def visitContractDefinition(self, ast: ContractDefinition):
        extra_head_parts = indent(self.visit_list(ast.extra_head_parts))
        units = indent(self.visit_list(ast.units))
        extra_tail_parts = indent(self.visit_list(ast.extra_tail_parts))
        return f"contract {ast.idf} {{\n{extra_head_parts}\n\n{units}\n\n{extra_tail_parts}\n}}"

    def visitPragmaDirective(self, ast: PragmaDirective) -> str:
        if self.for_solidity:
            return f"pragma solidity {cfg.cloak_solc_version_compatibility.expression};"
        return f"pragma {ast.name} {ast.version};"

    def visitSourceUnit(self, ast: SourceUnit):
        extra_head_parts = self.visit_list(ast.extra_head_parts)
        lst = self.visit_list(ast.units, "\n\n")
        return f"{extra_head_parts}\n\n{lst}"

    def visitNewExpr(self, ast: NewExpr) -> str:
        return f"new {self.visit(ast.target_type)}"

    def visitTupleVariableDeclarationStatement(self, ast: TupleVariableDeclarationStatement) -> str:
        ss = map(lambda x: "" if x is None else self.visit(x), ast.vs)
        res = f"({', '.join(ss)}) = {self.visit(ast.expr)};"
        return res

    def visitStringTypeName(self, ast: StringTypeName) -> str:
        return ast.name

    def visitImportDirective(self, ast: ImportDirective) -> str:
        if ast.aliases:
            return f'import {ast.aliases} from "{ast.path}";'
        if ast.unitAlias:
            return f'import "{ast.path}" as {ast.unitAlias};'
        return f'import "{ast.path}";'

    def visitInheritanceSpecifier(self, ast: InheritanceSpecifier) -> str:
        return f"{self.visit_list(ast.path, '.')}({self.visit(ast.args)})"

    def visitInterfaceDefinition(self, ast: InterfaceDefinition) -> str:
        inheritanceSpecifiers = ""
        if ast.inheritanceSpecifiers:
            inheritanceSpecifiers = f" is {self.visit_list(ast.inheritanceSpecifiers, ', ')}"
        lst = self.visit_list(ast.body_elems)
        return f"interface {ast.name}{inheritanceSpecifiers} {{\n{indent(lst)}\n}}"

    def visitLibraryDefinition(self, ast: LibraryDefinition) -> str:
        lst = self.visit_list(ast.body_elems)
        return f"library {ast.name} {{\n{indent(lst)}\n}}"

    def visitModifierInvocation(self, ast: ModifierInvocation) -> str:
        return f"{'.'.join(ast.path)}({self.visit(ast.args)})"

    def visitOverrideSpecifier(self, ast: OverrideSpecifier) -> str:
        return self.visit_list(map('.'.join, ast.paths), ", ")

    def visitModifierDefinition(self, ast: ModifierDefinition):
        ps = self.visit_list(ast.parameters, ", ")
        virtual = " virtual" if ast.virtual else ""
        body = ";"
        if ast.body:
            body = self.visitBlock(ast.body)
        return f"modifier {self.visit(ast.idf)}({ps}){virtual} {self.visit_list(ast.overrideSpecifiers, ', ')} {body}"

    def visitUserDefinedValueTypeDefinition(self, ast: UserDefinedValueTypeDefinition):
        return f"type {self.visit(ast.idf)} is {self.visit(ast.underlying_type)};"

    def visitEventParameter(self, ast: EventParameter):
        indexed = " indexed" if ast.indexed else ""
        name = f" {self.visit(ast.name)}" if ast.name else ""
        return f"{self.visit(ast.annotated_type)}{indexed}{name}"

    def visitEventDefinition(self, ast: EventDefinition):
        anonymous = f" anonymous" if ast.anonymous else ""
        return f"event {self.visit(ast.idf)}({self.visit_list(ast.parameters, ', ')}){anonymous};"

    def visitEmitStatement(self, ast: EmitStatement):
        return f"emit {self.visit(ast.expr)}({self.visit(ast.args)});"

    def visitErrorParameter(self, ast: ErrorParameter):
        name = f" {self.visit(ast.name)}" if ast.name else ""
        return f"{self.visit(ast.annotated_type)}{name}"

    def visitErrorDefinition(self, ast: ErrorDefinition):
        ps = self.visit_list(ast.parameters, ', ')
        return f"error {self.visit(ast.idf)}({ps});"

    def visitUsingDirective(self, ast: UsingDirective):
        t = self.visit(ast.type_name) if ast.type_name else "*"
        path = self.visit_list(ast.path, '.')
        return f"using {path} for {t};"

    def visitCatchClause(self, ast: CatchClause):
        idf = f" self.visit(ast.idf)" if ast.idf else ""
        args = f"({self.visit_list(ast.args, ', ')})" if ast.args else ""
        body = self.visitBlock(ast.body)
        return f"catch{idf}{args}{body}"

    def visitTryStatement(self, ast: TryStatement):
        expr = self.visit(ast.expr)
        rts = f"returns ({self.visit_list(ast.returnParameters)})" if ast.returnParameters else ""
        body = self.visitBlock(ast.body)
        ccs = self.visit_list(ast.catchClauses, ' ')
        return f"try {expr} {rts}{body} {ccs}"

    def visitRevertStatement(self, ast: RevertStatement):
        expr = self.visit(ast.expr)
        args = self.visit(ast.args)
        return f"revert {expr}({args});"

    def visitRangeIndexExpr(self, ast: RangeIndexExpr):
        arr = self.visit(ast.arr)
        start = self.visit(ast.start) if ast.start else ""
        end = self.visit(ast.end) if ast.end else ""
        return f"arr[{start}:{end}]"

    def visitMetaTypeExpr(self, ast: MetaTypeExpr):
        return f"type({self.visti(ast.typeName)})"

    def visitInlineArrayExpr(self, ast: InlineArrayExpr):
        return f"[{self.visit_list(ast.exprs, ', ')}]"

    def visitAssemblyStatement(self, ast: AssemblyStatement):
        return ast.text

    def visitFunctionTypeName(self, ast: FunctionTypeName):
        ps = self.visit_list(ast.parameters, ', ')
        modifiers = self.visit_list(ast.modifiers, ' ')
        rts = f"returns ({self.visit_list(ast.return_parameters, ', ')})" if ast.return_parameters else ""
        return f"function({ps}) {modifiers} {rts}"
