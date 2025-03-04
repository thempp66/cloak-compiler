from antlr4.Token import CommonToken
from semantic_version import NpmSpec, Version

import cloak.cloak_ast.ast as ast

from cloak.config import cfg
from cloak.solidity_parser.parse import SyntaxException
from cloak.solidity_parser.emit import Emitter
from antlr4.tree.Tree import Token, TerminalNodeImpl
from cloak.solidity_parser.generated.SolidityParser import SolidityParser, ParserRuleContext, CommonTokenStream
from cloak.solidity_parser.generated.SolidityVisitor import SolidityVisitor
from cloak.solidity_parser.parse import MyParser
from cloak.cloak_ast.ast import StateVariableDeclaration, ContractDefinition, NumberLiteralExpr, \
    BooleanLiteralExpr, StringLiteralExpr, FunctionCallExpr, ExpressionStatement, IdentifierExpr, \
    ReclassifyExpr, BuiltinFunction, IndexExpr
from cloak.cloak_ast import ast as ast_module


def build_ast_from_parse_tree(parse_tree: ParserRuleContext, tokens: CommonTokenStream, code: str) -> ast.AST:
    v = BuildASTVisitor(tokens, code)
    return v.visit(parse_tree)


def build_ast(code):
    p = MyParser(code)
    full_ast = build_ast_from_parse_tree(p.tree, p.tokens, code)
    assert isinstance(full_ast, ast.SourceUnit)
    full_ast.original_code = str(code).splitlines()
    return full_ast


def SOL(code: str) -> ast.AST:
    p = MyParser("SOL "+code)
    full_ast = build_ast_from_parse_tree(p.tree, p.tokens, code)
    assert full_ast.sba
    return full_ast.sba


# for deep copy
def rebuild_ast(ast: ast_module.AST) -> ast_module.AST:
    return SOL(ast.code())


class BuildASTVisitor(SolidityVisitor):

    def __init__(self, tokens: CommonTokenStream, code: str):
        self.emitter = Emitter(tokens)
        self.code = code

    def visit(self, tree):
        sub_ast = super().visit(tree)
        if isinstance(sub_ast, ast.AST):
            sub_ast.line = tree.start.line
            sub_ast.column = tree.start.column + 1
        return sub_ast

    def visitChildren(self, ctx: ParserRuleContext):
        # determine corresponding class name
        t = type(ctx).__name__
        t = t.replace('Context', '')

        # may be able to return the result for a SINGLE, UNNAMED CHILD without wrapping it in an object
        direct_unnamed = ['ContractBodyElement', 'StateMutability', 'Visibility', 'Statement', 'SimpleStatement', 'PrimaryExpression']
        if t in direct_unnamed:
            if ctx.getChildCount() != 1:
                raise TypeError(t + ' does not have a single, unnamed child')
            ret = self.handle_field(ctx.getChild(0))
            return ret

        # HANDLE ALL FIELDS of ctx
        d = ctx.__dict__

        # extract fields
        fields = d.keys()
        fields = [f for f in fields if not f.startswith('_')]
        ignore = ['parentCtx', 'invokingState', 'children', 'start', 'stop', 'exception', 'parser']
        fields = [f for f in fields if f not in ignore]

        # visit fields
        visited_fields = {}
        for f in fields:
            visited_fields[f] = self.handle_field(d[f])

        # may be able to return the result for a SINGLE, NAMED CHILD without wrapping it in an object
        direct = ['ModifierList', 'ParameterList', 'ReturnParameters', 'FunctionCallArguments']
        if t in direct:
            if len(visited_fields) != 1:
                raise TypeError(t + ' does not have a single, named child')
            key = list(visited_fields.keys())[0]
            return visited_fields[key]

        # CONSTRUCT AST FROM FIELDS
        if hasattr(ast, t):
            c = getattr(ast, t)
            # call initializer
            try:
                return c(**visited_fields)
            except TypeError as e:
                raise TypeError("Could not call initializer for " + t) from e
        else:
            # abort if not constructor found for this node type
            raise ValueError(t)

    def handle_field(self, field):
        if field is None:
            return None
        elif isinstance(field, list):
            return [self.handle_field(element) for element in field]
        elif isinstance(field, Token):
            # text
            return field.text
        elif isinstance(field, TerminalNodeImpl):
            return field.symbol.text
        else:
            # other
            return self.visit(field)

    def visitIdentifier(self, ctx: SolidityParser.IdentifierContext):
        name: str = ctx.name.text
        # renqian TODO: add startswith check of tee_reserved_name_prefix
        if name.startswith(cfg.zk_reserved_name_prefix) or name.startswith(f'_{cfg.zk_reserved_name_prefix}'):
            raise SyntaxException(f'Identifiers must not start with reserved prefix _?{cfg.zk_reserved_name_prefix}', ctx, self.code)
        elif name.endswith(cfg.reserved_conflict_resolution_suffix):
            raise SyntaxException(f'Identifiers must not end with reserved suffix {cfg.zk_reserved_name_prefix}', ctx, self.code)
        return ast.Identifier(name)

    def visitPragmaDirective(self, ctx: SolidityParser.PragmaDirectiveContext):
        return ast_module.PragmaDirective(self.handle_field(ctx.name), ctx.ver.getText())

    # def visitVersionPragma(self, ctx: SolidityParser.VersionPragmaContext):
    #     version = ctx.ver.getText().strip()
    #     spec = NpmSpec(version)
    #     name = self.handle_field(ctx.name)
    #     if name == 'cloak' and Version(cfg.cloak_version) not in spec:
    #         raise SyntaxException(f'Contract requires a different cloak version.\n'
    #                               f'Current version is {cfg.cloak_version} but pragma zkay mandates {version}.',
    #                               ctx.ver, self.code)
    #     elif name != 'cloak' and spec != cfg.cloak_solc_version_compatibility:
    #         # For backwards compatibility with older zkay versions
    #         assert name == 'solidity'
    #         raise SyntaxException(f'Contract requires solidity version {spec}, which is not compatible '
    #                               f'with the current zkay version (requires {cfg.cloak_solc_version_compatibility}).',
    #                               ctx.ver, self.code)

    #     return f'{name} {version}'

    # Visit a parse tree produced by SolidityParser#contractDefinition.
    def visitContractDefinition(self, ctx: SolidityParser.ContractDefinitionContext):
        identifier = self.visit(ctx.idf)
        units = [self.visit(c) for c in ctx.parts]
        return ContractDefinition(identifier, units)

    def visitFunctionDefinition(self, ctx:SolidityParser.FunctionDefinitionContext):
        name = self.handle_field(ctx.getChild(1))
        if isinstance(name, str):
            name = ast_module.Identifier(name)
        ps = self.handle_field(ctx.parameters)
        modifiers = self.get_modifiers(ctx, modifiers=True, visibility=True, stateMutability=True, \
                modifierInvocation=True, overrideSpecifier=True)
        rts = self.handle_field(ctx.return_parameters)
        body = self.handle_field(ctx.body)
        return ast_module.ConstructorOrFunctionDefinition(name, ps, modifiers, rts, body, "function")

    def visitConstructorDefinition(self, ctx:SolidityParser.ConstructorDefinitionContext):
        idf = ast_module.Identifier("constructor")
        ps = self.handle_field(ctx.parameters)
        modifiers = self.get_modifiers(ctx, modifiers=True, modifierInvocation=True)
        body = self.visit(ctx.body)
        return ast_module.ConstructorOrFunctionDefinition(idf, ps, modifiers, [], body, "constructor")

    def visitEnumDefinition(self, ctx:SolidityParser.EnumDefinitionContext):
        idf = self.visit(ctx.idf)
        if '$' in idf.name:
            raise SyntaxException('$ is not allowed in zkay enum identifiers', ctx.idf, self.code)
        values = [self.visit(v) for v in ctx.values]
        return ast.EnumDefinition(idf, values)

    def visitEnumValue(self, ctx:SolidityParser.EnumValueContext):
        idf = self.visit(ctx.idf)
        if '$' in idf.name:
            raise SyntaxException('$ is not allowed in zkay enum value identifiers', ctx.idf, self.code)
        return ast.EnumValue(idf)

    # Visit a parse tree produced by SolidityParser#NumberLiteralExpr.
    def visitNumberLiteralExpr(self, ctx: SolidityParser.NumberLiteralExprContext):
        unit = ctx.NumberUnit.getText() if ctx.NumberUnit() else None
        if ctx.HexNumber():
            was_hex = True
            value = int(ctx.HexNumber().getText().replace('_', ''), 16)
        else:
            was_hex = False
            value = int(ctx.DecimalNumber().getText().replace('_', ''))
        return NumberLiteralExpr(value, was_hex, ctx.getText())

    # Visit a parse tree produced by SolidityParser#BooleanLiteralExpr.
    def visitBooleanLiteralExpr(self, ctx: SolidityParser.BooleanLiteralExprContext):
        b = ctx.getText() == 'true'
        return BooleanLiteralExpr(b)

    def visitStringLiteralExpr(self, ctx: SolidityParser.StringLiteralExprContext):
        s = ctx.getText()

        # Remove quotes
        if s.startswith('"'):
            s = s[1:-1].replace('\\"', '"')
        else:
            s = s[1:-1]

        # raise SyntaxException('Use of unsupported string literal expression', ctx, self.code)
        return StringLiteralExpr(s)

    def visitTupleExpr(self, ctx:SolidityParser.TupleExprContext):
        contents = ctx.expr.children[1:-1]
        elements = []
        for idx in range(0, len(contents), 2):
            elements.append(self.visit(contents[idx]))
        return ast.TupleExpr(elements)

    def visitAnnotatedTypeName(self, ctx: SolidityParser.AnnotatedTypeNameContext):
        pa = None
        if ctx.privacy_annotation is not None:
            pa = self.visit(ctx.privacy_annotation)

            if not (isinstance(pa, ast.AllExpr) or isinstance(pa, ast.MeExpr) or isinstance(pa, ast.TeeExpr) or isinstance(pa, IdentifierExpr)):
                raise SyntaxException('Privacy annotation can only be me | all | tee| Identifier', ctx.privacy_annotation, self.code)

        return ast.AnnotatedTypeName(self.visit(ctx.type_name), pa)

    def visitElementaryTypeName(self, ctx: SolidityParser.ElementaryTypeNameContext):
        t = ctx.getText()
        if t == 'address':
            return ast.AddressTypeName()
        elif t == 'address payable':
            return ast.AddressPayableTypeName()
        elif t == 'bool':
            return ast.BoolTypeName()
        elif t.startswith('int'):
            return ast.IntTypeName(t)
        elif t.startswith('uint'):
            return ast.UintTypeName(t)
        elif t.startswith('bytes'):
            return ast.BytesTypeName(t)
        elif t == 'string':
            return ast.StringTypeName()
        elif t == 'var':
            raise SyntaxException(f'Use of unsupported var keyword', ctx, self.code)
        else:
            raise SyntaxException(f"Use of unsupported type '{t}'.", ctx, self.code)

    def visitIndexExpr(self, ctx: SolidityParser.IndexExprContext):
        arr = self.visit(ctx.arr)
        if not isinstance(arr, ast.LocationExpr):
            raise SyntaxException(f'Expression cannot be indexed', ctx.arr, self.code)
        index = None
        if ctx.index is not None:
            index = self.visit(ctx.index)
        return IndexExpr(arr, index)

    # def visitParenthesisExpr(self, ctx: SolidityParser.ParenthesisExprContext):
    #     f = BuiltinFunction('parenthesis').override(line=ctx.start.line, column=ctx.start.column)
    #     expr = self.visit(ctx.expr)
    #     return FunctionCallExpr(f, [expr])

    def visitSignExpr(self, ctx: SolidityParser.SignExprContext):
        f = BuiltinFunction('sign' + ctx.op.text).override(line=ctx.op.line, column=ctx.op.column)
        expr = self.visit(ctx.expr)
        return FunctionCallExpr(f, [expr])

    def visitNotExpr(self, ctx: SolidityParser.NotExprContext):
        f = BuiltinFunction('!').override(line=ctx.start.line, column=ctx.start.column)
        expr = self.visit(ctx.expr)
        return FunctionCallExpr(f, [expr])

    def visitBitwiseNotExpr(self, ctx: SolidityParser.BitwiseNotExprContext):
        f = BuiltinFunction('~').override(line=ctx.start.line, column=ctx.start.column)
        expr = self.visit(ctx.expr)
        return FunctionCallExpr(f, [expr])

    def _visitBinaryExpr(self, ctx):
        lhs = self.visit(ctx.lhs)
        rhs = self.visit(ctx.rhs)
        f = BuiltinFunction(ctx.op.text).override(line=ctx.op.line, column=ctx.op.column)
        return FunctionCallExpr(f, [lhs, rhs])

    def _visitBoolExpr(self, ctx):
        return self._visitBinaryExpr(ctx)

    def visitPowExpr(self, ctx: SolidityParser.PowExprContext):
        return self._visitBinaryExpr(ctx)

    def visitMultDivModExpr(self, ctx: SolidityParser.MultDivModExprContext):
        return self._visitBinaryExpr(ctx)

    def visitPlusMinusExpr(self, ctx: SolidityParser.PlusMinusExprContext):
        return self._visitBinaryExpr(ctx)

    def visitCompExpr(self, ctx: SolidityParser.CompExprContext):
        return self._visitBinaryExpr(ctx)

    def visitEqExpr(self, ctx: SolidityParser.EqExprContext):
        return self._visitBinaryExpr(ctx)

    def visitAndExpr(self, ctx: SolidityParser.AndExprContext):
        return self._visitBoolExpr(ctx)

    def visitOrExpr(self, ctx: SolidityParser.OrExprContext):
        return self._visitBoolExpr(ctx)

    def visitBitwiseOrExpr(self, ctx: SolidityParser.BitwiseOrExprContext):
        return self._visitBinaryExpr(ctx)

    def visitBitShiftExpr(self, ctx: SolidityParser.BitShiftExprContext):
        return self._visitBinaryExpr(ctx)

    def visitBitwiseAndExpr(self, ctx: SolidityParser.BitwiseAndExprContext):
        return self._visitBinaryExpr(ctx)

    def visitBitwiseXorExpr(self, ctx: SolidityParser.BitwiseXorExprContext):
        return self._visitBinaryExpr(ctx)

    def visitIteExpr(self, ctx: SolidityParser.IteExprContext):
        f = BuiltinFunction('ite')
        cond = self.visit(ctx.cond)
        then_expr = self.visit(ctx.then_expr)
        else_expr = self.visit(ctx.else_expr)
        return FunctionCallExpr(f, [cond, then_expr, else_expr])

    def visitFunctionCallExpr(self, ctx: SolidityParser.FunctionCallExprContext):
        func = self.visit(ctx.expression())
        args = self.visit(ctx.callArgumentList())

        if isinstance(func, IdentifierExpr):
            if func.idf.name == 'reveal':
                if len(args.args) != 2:
                    raise SyntaxException(f'Invalid number of arguments for reveal: {args}', ctx.args, self.code)
                return ReclassifyExpr(args.args[0], args.args[1])

        return FunctionCallExpr(func, args)

    def visitIfStatement(self, ctx: SolidityParser.IfStatementContext):
        cond = self.visit(ctx.condition)
        then_branch = self.visit(ctx.then_branch)
        if not isinstance(then_branch, ast.Block):
            then_branch = ast.Block([then_branch], was_single_statement=True)

        if ctx.else_branch is not None:
            else_branch = self.visit(ctx.else_branch)
            if not isinstance(else_branch, ast.Block):
                else_branch = ast.Block([else_branch], was_single_statement=True)
        else:
            else_branch = None

        return ast.IfStatement(cond, then_branch, else_branch)

    def visitWhileStatement(self, ctx: SolidityParser.WhileStatementContext):
        cond = self.visit(ctx.condition)
        body = self.visit(ctx.body)
        if not isinstance(body, ast.Block):
            body = ast.Block([body], was_single_statement=True)
        return ast.WhileStatement(cond, body)

    def visitDoWhileStatement(self, ctx: SolidityParser.DoWhileStatementContext):
        body = self.visit(ctx.body)
        cond = self.visit(ctx.condition)
        if not isinstance(body, ast.Block):
            body = ast.Block([body], was_single_statement=True)
        return ast.DoWhileStatement(body, cond)

    def visitForStatement(self, ctx: SolidityParser.ForStatementContext):
        init = None if ctx.init is None else self.visit(ctx.init)
        cond = self.visit(ctx.condition)
        update = None if ctx.update is None else self.visit(ctx.update)
        if isinstance(update, ast.Expression):
            update = ast.ExpressionStatement(update)
        body = self.visit(ctx.body)
        if not isinstance(body, ast.Block):
            body = ast.Block([body], was_single_statement=True)
        return ast.ForStatement(init, cond, update, body)

    def is_expr_stmt(self, ctx: SolidityParser.ExpressionContext) -> bool:
        if isinstance(ctx.parentCtx, SolidityParser.ExpressionStatementContext):
            return True
        elif isinstance(ctx.parentCtx, SolidityParser.ForStatementContext) and ctx == ctx.parentCtx.update:
            return True
        else:
            return False

    def visitAssignmentExpr(self, ctx: SolidityParser.AssignmentExprContext):
        if not self.is_expr_stmt(ctx):
            raise SyntaxException('Assignments are only allowed as statements', ctx, self.code)
        lhs = self.visit(ctx.lhs)
        rhs = self.visit(ctx.rhs)
        assert ctx.op.text[-1] == '='
        op = ctx.op.text[:-1] if ctx.op.text != '=' else ''
        if op:
            # If the assignment contains an additional operator -> replace lhs = rhs with lhs = lhs 'op' rhs
            rhs = FunctionCallExpr(BuiltinFunction(op).override(line=ctx.op.line, column=ctx.op.column), [self.visit(ctx.lhs), rhs])
            rhs.line = ctx.rhs.start.line
            rhs.column = ctx.rhs.start.column + 1
        return ast.AssignmentStatement(lhs, rhs, op)

    def _handle_crement_expr(self, ctx, kind: str):
        if not self.is_expr_stmt(ctx):
            raise SyntaxException(f'{kind}-crement expressions are only allowed as statements', ctx, self.code)
        op = '+' if ctx.op.text == '++' else '-'

        one = NumberLiteralExpr(1)
        one.line = ctx.op.line
        one.column = ctx.op.column + 1

        fct = FunctionCallExpr(BuiltinFunction(op).override(line=ctx.op.line, column=ctx.op.column), [self.visit(ctx.expr), one])
        fct.line = ctx.op.line
        fct.column = ctx.op.column + 1

        return ast.AssignmentStatement(self.visit(ctx.expr), fct, f'{kind}{ctx.op.text}')

    def visitPreCrementExpr(self, ctx: SolidityParser.PreCrementExprContext):
        return self._handle_crement_expr(ctx, 'pre')

    def visitPostCrementExpr(self, ctx: SolidityParser.PostCrementExprContext):
        return self._handle_crement_expr(ctx, 'post')

    def visitExpressionStatement(self, ctx: SolidityParser.ExpressionStatementContext):
        e = self.visit(ctx.expr)
        if isinstance(e, ast.Statement):
            return e
        else:
            # handle require
            if isinstance(e, FunctionCallExpr):
                f = e.func
                if isinstance(f, IdentifierExpr):
                    if f.idf.name == 'require':
                        args = e.args.args
                        if len(args) == 1:
                            return ast.RequireStatement(args[0],)
                        if len(args) == 2:
                            return ast.RequireStatement(args[0], comment=args[1])
                        raise SyntaxException(f'Invalid number of arguments for require: {e.args}', ctx.expr, self.code)

            assert isinstance(e, ast.Expression)
            return ExpressionStatement(e)

    def visitTypeName(self, ctx: SolidityParser.TypeNameContext) -> ast.TypeName:
        if ctx.value_type is not None:
            val_type = self.handle_field(ctx.value_type)
            expr = self.handle_field(ctx.expr)
            return ast.Array(val_type, expr)
        return self.handle_field(ctx.getChild(0))

    def visitTupleVariableDeclarationStatement(self, ctx: SolidityParser.TupleVariableDeclarationStatementContext):
        vs = []
        marked = False
        for child in ctx.children:
            c = self.handle_field(child)
            if isinstance(c, ast.VariableDeclaration):
                vs.append(c)
                marked = True
            elif c == ",":
                if not marked:
                    vs.append(None)
                marked = False
            elif c == ")":
                if not marked:
                    vs.append(None)
                break
        return ast.TupleVariableDeclarationStatement(vs, self.handle_field(ctx.expression()))

    def visitDataLocation(self, ctx: SolidityParser.DataLocationContext):
        return ctx.getText()

    def visitSourceUnit(self, ctx: SolidityParser.SourceUnitContext):
        if ctx.sba():
            sba = self.visit(ctx.sba())
            return ast_module.SourceUnit(sba=sba)
        units = self.handle_field(ctx.children)[:-1]
        return ast_module.SourceUnit(units=units)

    def visitPath(self, ctx: SolidityParser.PathContext):
        return ctx.getText()

    def visitSba(self, ctx: SolidityParser.SbaContext):
        return self.handle_field(ctx.getChild(1))

    def visitNamedArgument(self, ctx: SolidityParser.NamedArgumentContext):
        return ast_module.NamedArgument(ctx.name.name.text, self.visit(ctx.value))

    def visitCallArgumentList(self, ctx: SolidityParser.CallArgumentListContext):
        if ctx.namedArgument():
            return ast_module.CallArgumentList(self.handle_field(ctx.namedArgument()), True)
        return ast_module.CallArgumentList(self.handle_field(ctx.expression()), False)

    def visitIdentifierPath(self, ctx: SolidityParser.IdentifierPathContext):
        return [i.name.text for i in ctx.identifier()]

    def visitInheritanceSpecifier(self, ctx: SolidityParser.InheritanceSpecifierContext):
        path = self.visit(ctx.identifierPath())
        args = self.visit(ctx.callArgumentList())
        return ast_module.InheritanceSpecifier(path, args)

    def visitInterfaceDefinition(self, ctx: SolidityParser.InterfaceDefinitionContext):
        name = self.visit(ctx.name)
        inheritanceSpecifiers = []
        if ctx.inheritanceSpecifierList():
            inheritanceSpecifiers = self.handle_field(ctx.inheritanceSpecifierList().inheritanceSpecifier())
        body_elems = self.handle_field(ctx.contractBodyElement())
        return ast_module.InterfaceDefinition(name, inheritanceSpecifiers, body_elems)

    def visitLibraryDefinition(self, ctx: SolidityParser.LibraryDefinitionContext):
        name = self.visit(ctx.name)
        body_elems = self.handle_field(ctx.contractBodyElement())
        return ast_module.LibraryDefinition(name, body_elems)

    def visitModifierInvocation(self, ctx: SolidityParser.ModifierInvocationContext):
        path = self.visit(ctx.identifierPath())
        args = self.visit(ctx.callArgumentList())
        return ast_module.ModifierInvocation(path, args)

    def visitOverrideSpecifier(self, ctx: SolidityParser.OverrideSpecifierContext):
        paths = self.handle_field(ctx.identifierPath())
        return ast_module.OverrideSpecifier(paths)

    def visitModifierDefinition(self, ctx: SolidityParser.ModifierDefinitionContext):
        idf = self.handle_field(ctx.name)
        ps = self.handle_field(ctx.parameters)
        virtual = ctx.virtual is not None
        overrideSpecifiers = self.handle_field(ctx.overrideSpecifier())
        body = self.handle_field(ctx.body)
        return ast_module.ModifierDefinition(idf, ps, virtual, overrideSpecifiers, body)

    def get_modifiers(self, ctx, modifiers=False, visibility=False,
            stateMutability=False, modifierInvocation=False, overrideSpecifier=False):
        res = []
        if modifiers and ctx.modifiers:
            res += self.handle_field(ctx.modifiers)
        if visibility and ctx.visibility():
            res += self.handle_field(ctx.visibility())
        if stateMutability and ctx.stateMutability():
            res += self.handle_field(ctx.stateMutability())
        if modifierInvocation and ctx.modifierInvocation():
            res += self.handle_field(ctx.modifierInvocation())
        if overrideSpecifier and ctx.overrideSpecifier():
            res += self.handle_field(ctx.overrideSpecifier())
        return res

    def visitFallbackFunctionDefinition(self, ctx: SolidityParser.FallbackFunctionDefinitionContext):
        idf = ast_module.Identifier("fallback")
        kind = "fallback"
        parameters = self.handle_field(ctx.parameters)
        return_parameters = self.handle_field(ctx.return_parameters)
        body = self.handle_field(ctx.body)
        modifiers = self.get_modifiers(ctx, modifiers=True, stateMutability=True, modifierInvocation=True, overrideSpecifier=True)
        return ast_module.ConstructorOrFunctionDefinition(idf, parameters, modifiers, return_parameters, body, kind)

    def visitReceiveFunctionDefinition(self, ctx: SolidityParser.ReceiveFunctionDefinitionContext):
        idf = ast_module.Identifier("receive")
        kind = "receive"
        body = self.handle_field(ctx.body)
        modifiers = self.get_modifiers(ctx, modifiers=True, modifierInvocation=True, overrideSpecifier=True)
        return ast_module.ConstructorOrFunctionDefinition(idf, [], modifiers, [], body, kind)

    def visitStructMember(self, ctx: SolidityParser.StructMemberContext):
        idf = self.visit(ctx.name)
        type_name = self.visit(ctx.typeName())
        return ast_module.VariableDeclaration([], ast_module.AnnotatedTypeName(type_name), idf)

    def visitStructDefinition(self, ctx: SolidityParser.StructDefinitionContext):
        idf = self.visit(ctx.name)
        members = self.handle_field(ctx.structMember())
        return ast_module.StructDefinition(idf, members)

    def visitUserDefinedValueTypeDefinition(self, ctx: SolidityParser.UserDefinedValueTypeDefinitionContext):
        idf = self.visit(ctx.name)
        underlying_type = self.visit(ctx.elementaryTypeName())
        return ast_module.UserDefinedValueTypeDefinition(idf, underlying_type)

    def visitConstantVariableDeclaration(self, ctx: SolidityParser.ConstantVariableDeclarationContext):
        t = self.visit(ctx.annotated_type)
        idf = self.visit(ctx.idf)
        return ast_module.StateVariableDeclaration(t, ['constant'], idf, self.visit(ctx.expr))

    def visitEventParameter(self, ctx: SolidityParser.EventParameterContext):
        t = self.visit(ctx.annotatedTypeName())
        indexed = self.handle_field(ctx.IndexedKeyword())
        name = self.handle_field(ctx.name)
        return ast_module.EventParameter(t, indexed, name)

    def visitEventDefinition(self, ctx: SolidityParser.EventDefinitionContext):
        idf = self.visit(ctx.name)
        parameters = self.handle_field(ctx.parameters)
        anonymous = self.handle_field(ctx.AnonymousKeyword())
        return ast_module.EventDefinition(idf, parameters, anonymous)

    def visitEmitStatement(self, ctx: SolidityParser.EmitStatementContext):
        return ast_module.EmitStatement(self.visit(ctx.expression()), self.handle_field(ctx.callArgumentList()))

    def visitErrorParameter(self, ctx: SolidityParser.ErrorParameterContext):
        t = self.visit(ctx.typ)
        name = self.handle_field(ctx.name)
        return ast_module.ErrorParameter(t, name)

    def visitErrorDefinition(self, ctx: SolidityParser.ErrorDefinitionContext):
        idf = self.visit(ctx.name)
        ps = self.handle_field(ctx.parameters)
        return ast_module.ErrorDefinition(idf, ps)

    def visitUsingDirective(self, ctx: SolidityParser.UsingDirectiveContext):
        path = self.visit(ctx.identifierPath())
        t = self.visit(ctx.typeName())
        return ast_module.UsingDirective(path, t)

    def visitCatchClause(self, ctx: SolidityParser.CatchClauseContext):
        idf = self.handle_field(ctx.identifier())
        args = self.handle_field(ctx.arguments)
        body = self.visit(ctx.block())
        return ast_module.CatchClause(idf, args, body)

    def visitTryStatement(self, ctx: SolidityParser.TryStatementContext):
        expr = self.visit(ctx.expression())
        rts = self.handle_field(ctx.return_parameters)
        body = self.visit(ctx.block())
        ccs = self.handle_field(ctx.catchClause())
        return ast_module.TryStatement(expr, rts, body, ccs)

    def visitRevertStatement(self, ctx: SolidityParser.RevertStatementContext):
        expr = self.visit(ctx.expression())
        args = self.visit(ctx.callArgumentList())
        return ast_module.RevertStatement(expr, args)

    def visitRangeIndexExpr(self, ctx: SolidityParser.RangeIndexExprContext):
        arr = self.visit(ctx.arr)
        start = self.handle_field(ctx.start)
        end = self.handle_field(ctx.end)
        return ast_module.RangeIndexExpr(arr, start, end)

    def visitMemberAccessExpr(self, ctx: SolidityParser.MemberAccessExprContext):
        expr = self.visit(ctx.expr)
        member = self.visit(ctx.identifier()) if ctx.identifier() else ast_module.Identifier("address")
        return ast_module.MemberAccessExpr(expr, member)

    def visitFunctionCallOptions(self, ctx: SolidityParser.FunctionCallOptionsContext):
        expr = self.visit(ctx.expression())
        args = self.handle_field(ctx.namedArgument())
        return ast_module.FunctionCallExpr(expr, args, True)

    def visitPayableConversion(self, ctx: SolidityParser.PayableConversionContext):
        idf = ast_module.Identifier("payable")
        return FunctionCallExpr(IdentifierExpr(idf), self.visit(ctx.callArgumentList()))

    def visitMetaType(self, ctx: SolidityParser.MetaTypeContext):
        return ast_module.MetaTypeExpr(self.visit(ctx.typeName()))

    def visitInlineArrayExpr(self, ctx: SolidityParser.InlineArrayExprContext):
        return ast_module.InlineArrayExpr(self.handle_field(ctx.expression()))

    def visitAssemblyStatement(self, ctx: SolidityParser.AssemblyStatementContext):
        return ast_module.AssemblyStatement(self.extract_original_text(ctx))

    def extract_original_text(self, ctx):
        token_source = ctx.start.getTokenSource()
        input_stream = token_source.inputStream
        start, stop  = ctx.start.start, ctx.stop.stop
        return input_stream.getText(start, stop)

    def visitStateVariableDeclaration(self, ctx: SolidityParser.StateVariableDeclarationContext):
        t = self.visit(ctx.annotated_type)
        ks = self.handle_field(ctx.keywords)
        overrideSpecifier = None
        if ctx.overrideSpecifier():
            overrideSpecifier = self.visit(ctx.overrideSpecifier(0))
        idf = self.visit(ctx.idf)
        expr = self.handle_field(ctx.expr)
        return ast_module.StateVariableDeclaration(t, ks, idf, expr)

    def visitFunctionTypeName(self, ctx: SolidityParser.FunctionTypeNameContext):
        ps = self.handle_field(ctx.parameters)
        rts = self.handle_field(ctx.return_parameters)
        modifiers = self.get_modifiers(ctx, visibility=True, stateMutability=True)
        return ast_module.FunctionTypeName(ps, modifiers, rts)
