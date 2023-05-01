#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2022
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________
import collections
import logging
import sys
from operator import itemgetter
from itertools import filterfalse

from pyomo.core.expr.current import (
    NegationExpression,
    ProductExpression,
    DivisionExpression,
    PowExpression,
    AbsExpression,
    UnaryFunctionExpression,
    Expr_ifExpression,
    MonomialTermExpression,
    LinearExpression,
    SumExpression,
    ExternalFunctionExpression,
    native_types,
    native_numeric_types,
)
from pyomo.core.expr.visitor import StreamBasedExpressionVisitor
from pyomo.core.expr import is_fixed
from pyomo.core.base.expression import ScalarExpression, _GeneralExpressionData
from pyomo.core.base.objective import ScalarObjective, _GeneralObjectiveData
import pyomo.core.kernel as kernel
from pyomo.repn.util import ExprType, apply_node_operation

logger = logging.getLogger(__name__)

nan = float("nan")

_CONSTANT = ExprType.CONSTANT
_LINEAR = ExprType.LINEAR
_GENERAL = ExprType.GENERAL


def _merge_dict(mult, self_dict, other_dict):
    if mult == 1:
        for vid, coef in other_dict.items():
            if vid in self_dict:
                self_dict[vid] += coef
            else:
                self_dict[vid] = coef
    else:
        for vid, coef in other_dict.items():
            if vid in self_dict:
                self_dict[vid] += mult * coef
            else:
                self_dict[vid] = mult * coef


class LinearRepn(object):
    __slots__ = ("multiplier", "constant", "linear", "nonlinear")

    def __init__(self):
        self.multiplier = 1
        self.constant = 0
        self.linear = {}
        self.nonlinear = None

    def __str__(self):
        return (
            f"LinearRepn(mult={self.multiplier}, const={self.constant}, "
            f"linear={self.linear}, nonlinear={self.nonlinear})"
        )

    def __repr__(self):
        return str(self)

    def walker_exitNode(self):
        if self.nonlinear is not None:
            return _GENERAL, self
        elif self.linear:
            return _LINEAR, self
        else:
            return _CONSTANT, self.multiplier * self.constant

    def duplicate(self):
        ans = self.__class__.__new__(self.__class__)
        ans.multiplier = self.multiplier
        ans.constant = self.constant
        ans.linear = dict(self.linear)
        ans.nonlinear = self.nonlinear
        return ans

    def to_expression(self, visitor):
        if self.linear:
            ans = (
                LinearExpression(
                    [
                        MonomialTermExpression((coef, visitor.var_map[vid]))
                        for vid, coef in self.linear.items()
                        if coef
                    ]
                )
                + self.constant
            )
        else:
            ans = self.constant
        if self.nonlinear is not None:
            ans += self.nonlinear
        if self.multiplier != 1:
            ans *= self.multiplier
        return ans

    def append(self, other):
        """Append a child result from acceptChildResult

        Notes
        -----
        This method assumes that the operator was "+". It is implemented
        so that we can directly use a LinearRepn() as a data object in
        the expression walker (thereby avoiding the function call for a
        custom callback)

        """
        # Note that self.multiplier will always be 1 (we only call append()
        # within a sum, so there is no opportunity for self.multiplier to
        # change). Omitting the assertion for efficiency.
        # assert self.multiplier == 1
        _type, other = other
        if _type is _CONSTANT:
            self.constant += other
            return

        mult = other.multiplier
        self.constant += mult * other.constant
        if other.linear:
            _merge_dict(mult, self.linear, other.linear)
        if other.nonlinear is not None:
            if mult != 1:
                nl = mult * other.nonlinear
            else:
                nl = other.nonlinear
            if self.nonlinear is None:
                self.nonlinear = nl
            else:
                self.nonlinear += nl


def _to_expression_CONST(visitor, arg):
    return arg[1]


def _to_expression_LINEAR(visitor, arg):
    return arg[1].to_expression(visitor)


def _to_expression_GENERAL(visitor, arg):
    return arg[1].to_expression(visitor)


to_expression = {
    _CONSTANT: _to_expression_CONST,
    _LINEAR: _to_expression_LINEAR,
    _GENERAL: _to_expression_GENERAL,
}


_exit_node_handlers = {}

#
# NEGATION handlers
#


def _handle_negation_constant(visitor, node, arg):
    return (_CONSTANT, -1 * arg[1])


def _handle_negation_ANY(visitor, node, arg):
    arg[1].multiplier *= -1
    return arg


_exit_node_handlers[NegationExpression] = {
    (_CONSTANT,): _handle_negation_constant,
    (_LINEAR,): _handle_negation_ANY,
    (_GENERAL,): _handle_negation_ANY,
}

#
# PRODUCT handlers
#


def _handle_product_constant_constant(visitor, node, arg1, arg2):
    _, arg1 = arg1
    _, arg2 = arg2
    return _, arg1 * arg2


def _handle_product_constant_ANY(visitor, node, arg1, arg2):
    _, arg1 = arg1
    if not arg1 or arg1 != arg1:
        # This catches both 0*arg2 and nan*arg2
        return _CONSTANT, arg1
    arg2[1].multiplier *= arg1
    return arg2


def _handle_product_ANY_constant(visitor, node, arg1, arg2):
    _, arg2 = arg2
    if not arg2 or arg2 != arg2:
        # This catches both arg1*0 and arg1*nan
        return _CONSTANT, arg2
    arg1[1].multiplier *= arg2
    return arg1


def _handle_product_nonlinear(visitor, node, arg1, arg2):
    ans = visitor.Result()
    ans.nonlinear = to_expression[arg1[0]](visitor, arg1) * to_expression[arg2[0]](
        visitor, arg2
    )
    return _GENERAL, ans


_exit_node_handlers[ProductExpression] = {
    (_CONSTANT, _CONSTANT): _handle_product_constant_constant,
    (_CONSTANT, _LINEAR): _handle_product_constant_ANY,
    (_CONSTANT, _GENERAL): _handle_product_constant_ANY,
    (_LINEAR, _CONSTANT): _handle_product_ANY_constant,
    (_LINEAR, _LINEAR): _handle_product_nonlinear,
    (_LINEAR, _GENERAL): _handle_product_nonlinear,
    (_GENERAL, _CONSTANT): _handle_product_ANY_constant,
    (_GENERAL, _LINEAR): _handle_product_nonlinear,
    (_GENERAL, _GENERAL): _handle_product_nonlinear,
}

#
# DIVISION handlers
#


def _handle_division_constant_constant(visitor, node, arg1, arg2):
    _, arg1 = arg1
    _, arg2 = arg2
    return _, arg1 / arg2


def _handle_division_ANY_constant(visitor, node, arg1, arg2):
    _, arg2 = arg2
    if arg2 != arg2:
        # This catches arg1/nan
        return _CONSTANT, arg2
    arg1[1].multiplier /= arg2
    return arg1


def _handle_division_nonlinear(visitor, node, arg1, arg2):
    ans = visitor.Result()
    ans.nonlinear = to_expression[arg1[0]](visitor, arg1) / to_expression[arg2[0]](
        visitor, arg2
    )
    return _GENERAL, ans


_exit_node_handlers[DivisionExpression] = {
    (_CONSTANT, _CONSTANT): _handle_division_constant_constant,
    (_CONSTANT, _LINEAR): _handle_division_nonlinear,
    (_CONSTANT, _GENERAL): _handle_division_nonlinear,
    (_LINEAR, _CONSTANT): _handle_division_ANY_constant,
    (_LINEAR, _LINEAR): _handle_division_nonlinear,
    (_LINEAR, _GENERAL): _handle_division_nonlinear,
    (_GENERAL, _CONSTANT): _handle_division_ANY_constant,
    (_GENERAL, _LINEAR): _handle_division_nonlinear,
    (_GENERAL, _GENERAL): _handle_division_nonlinear,
}

#
# EXPONENTIATION handlers
#


def _handle_pow_constant_constant(visitor, node, *args):
    return _CONSTANT, apply_node_operation(node, args)


def _handle_pow_ANY_constant(visitor, node, arg1, arg2):
    if arg2[1] == 1:
        return arg1
    else:
        return _handle_pow_nonlinear(visitor, node, arg1, arg2)


def _handle_pow_nonlinear(visitor, node, arg1, arg2):
    ans = visitor.Result()
    ans.nonlinear = to_expression[arg1[0]](visitor, arg1) ** to_expression[arg2[0]](
        visitor, arg2
    )
    return _GENERAL, ans


_exit_node_handlers[PowExpression] = {
    (_CONSTANT, _CONSTANT): _handle_pow_constant_constant,
    (_CONSTANT, _LINEAR): _handle_pow_nonlinear,
    (_CONSTANT, _GENERAL): _handle_pow_nonlinear,
    (_LINEAR, _CONSTANT): _handle_pow_ANY_constant,
    (_LINEAR, _LINEAR): _handle_pow_nonlinear,
    (_LINEAR, _GENERAL): _handle_pow_nonlinear,
    (_GENERAL, _CONSTANT): _handle_pow_ANY_constant,
    (_GENERAL, _LINEAR): _handle_pow_nonlinear,
    (_GENERAL, _GENERAL): _handle_pow_nonlinear,
}

#
# ABS and UNARY handlers
#


def _handle_unary_constant(visitor, node, arg):
    return _CONSTANT, apply_node_operation(node, (arg[1],))


def _handle_unary_nonlinear(visitor, node, arg):
    ans = visitor.Result()
    ans.nonlinear = node.create_node_with_local_data(
        (to_expression[arg[0]](visitor, arg),)
    )
    return _GENERAL, ans


_exit_node_handlers[UnaryFunctionExpression] = {
    (_CONSTANT,): _handle_unary_constant,
    (_LINEAR,): _handle_unary_nonlinear,
    (_GENERAL,): _handle_unary_nonlinear,
}
_exit_node_handlers[AbsExpression] = _exit_node_handlers[UnaryFunctionExpression]

#
# NAMED EXPRESSION handlers
#


def _handle_named_constant(visitor, node, arg1):
    # Record this common expression
    visitor.subexpression_cache[id(node)] = arg1
    return arg1


def _handle_named_ANY(visitor, node, arg1):
    # Record this common expression
    visitor.subexpression_cache[id(node)] = arg1
    _type, arg1 = arg1
    return _type, arg1.duplicate()


_exit_node_handlers[ScalarExpression] = {
    (_CONSTANT,): _handle_named_constant,
    (_LINEAR,): _handle_named_ANY,
    (_GENERAL,): _handle_named_ANY,
}

_named_subexpression_types = [
    ScalarExpression,
    _GeneralExpressionData,
    kernel.expression.expression,
    kernel.expression.noclone,
    # Note: objectives are special named expressions
    _GeneralObjectiveData,
    ScalarObjective,
    kernel.objective.objective,
]

#
# EXPR_IF handlers
#


def _handle_expr_if_const(visitor, node, arg1, arg2, arg3):
    _type, _test = arg1
    if _type is not _CONSTANT:
        return _handle_expr_if_nonlinear(visitor, node, arg1, arg2, arg3)
    if _test:
        if _test != _test:
            # nan
            return _handle_expr_if_nonlinear(visitor, node, arg1, arg2, arg3)
        return arg2
    else:
        return arg3


def _handle_expr_if_nonlinear(visitor, node, arg1, arg2, arg3):
    ans = visitor.Result()
    ans.nonlinear = Expr_ifExpression(
        (
            to_expression[arg1[0]](visitor, arg1),
            to_expression[arg2[0]](visitor, arg2),
            to_expression[arg3[0]](visitor, arg3),
        )
    )
    return _GENERAL, ans


_exit_node_handlers[Expr_ifExpression] = {
    (i, j, k): _handle_expr_if_nonlinear
    for i in (_LINEAR, _GENERAL)
    for j in (_CONSTANT, _LINEAR, _GENERAL)
    for k in (_CONSTANT, _LINEAR, _GENERAL)
}
for j in (_CONSTANT, _LINEAR, _GENERAL):
    for k in (_CONSTANT, _LINEAR, _GENERAL):
        _exit_node_handlers[Expr_ifExpression][_CONSTANT, j, k] = _handle_expr_if_const


def _before_native(visitor, child):
    return False, (_CONSTANT, child)


def _before_var(visitor, child):
    _id = id(child)
    if _id not in visitor.var_map:
        if child.fixed:
            return False, (_CONSTANT, child())
        visitor.var_map[_id] = child
        visitor.var_order[_id] = len(visitor.var_order)
    ans = visitor.Result()
    ans.linear[_id] = 1
    return False, (_LINEAR, ans)


def _before_npv(visitor, child):
    # TBD: It might be more efficient to cache the value of NPV
    # expressions to avoid duplicate evaluations.  However, current
    # examples do not benefit from this cache.
    #
    # _id = id(child)
    # if _id in visitor.value_cache:
    #     child = visitor.value_cache[_id]
    # else:
    #     child = visitor.value_cache[_id] = child()
    # return False, (_CONSTANT, child)
    try:
        tmp = child()
        if tmp.__class__ is complex:
            return True, None
        return False, (_CONSTANT, tmp)
    except:
        # If there was an exception evaluating the subexpression, then
        # we need to descend into it (in case there is something like 0 *
        # nan that we need to map to 0)
        return True, None


def _before_monomial(visitor, child):
    #
    # The following are performance optimizations for common
    # situations (Monomial terms and Linear expressions)
    #
    arg1, arg2 = child._args_
    if arg1.__class__ not in native_types:
        # TBD: It might be more efficient to cache the value of NPV
        # expressions to avoid duplicate evaluations.  However, current
        # examples do not benefit from this cache.
        #
        # _id = id(arg1)
        # if _id in visitor.value_cache:
        #     arg1 = visitor.value_cache[_id]
        # else:
        #     arg1 = visitor.value_cache[_id] = arg1()
        try:
            arg1 = arg1()
        except:
            # If there was an exception evaluating the subexpression,
            # then we need to descend into it (in case there is something
            # like 0 * nan that we need to map to 0)
            return True, None

    # Trap multiplication by 0 and nan.
    if not arg1 or arg1 != arg1:
        return False, (_CONSTANT, arg1)

    _id = id(arg2)
    if _id not in visitor.var_map:
        if arg2.fixed:
            return False, (_CONSTANT, arg1 * arg2())
        visitor.var_map[_id] = arg2
        visitor.var_order[_id] = len(visitor.var_order)
    ans = visitor.Result()
    ans.linear[_id] = arg1
    return False, (_LINEAR, ans)


def _before_linear(visitor, child):
    var_map = visitor.var_map
    var_order = visitor.var_order
    next_i = len(var_order)
    ans = visitor.Result()
    const = 0
    linear = ans.linear
    for arg in child.args:
        if arg.__class__ is MonomialTermExpression:
            arg1, arg2 = arg._args_
            if arg1.__class__ not in native_types:
                try:
                    arg1 = arg1()
                except:
                    # If there was an exception evaluating the
                    # subexpression, then we need to descend into it (in
                    # case there is something like 0 * nan that we need
                    # to map to 0)
                    return True, None
            if not arg1:
                continue
            elif arg1 != arg1:
                # arg1 == NaN
                const += arg1
                continue
            _id = id(arg2)
            if _id not in var_map:
                if arg2.fixed:
                    const += arg1 * arg2()
                    continue
                var_map[_id] = arg2
                var_order[_id] = next_i
                next_i += 1
                linear[_id] = arg1
            elif _id in linear:
                linear[_id] += arg1
            else:
                linear[_id] = arg1
        elif arg.__class__ not in native_numeric_types:
            try:
                const += arg()
            except:
                # If there was an exception evaluating the
                # subexpression, then we need to descend into it (in
                # case there is something like 0 * nan that we need to
                # map to 0)
                return True, None
        else:
            const += arg
    if linear:
        ans.constant = const
        return False, (_LINEAR, ans)
    else:
        return False, (_CONSTANT, const)


def _before_named_expression(visitor, child):
    _id = id(child)
    if _id in visitor.subexpression_cache:
        _type, expr = visitor.subexpression_cache[_id]
        if _type is _CONSTANT:
            return False, (_type, expr)
        else:
            return False, (_type, expr.duplicate())
    else:
        return True, None


def _before_expr_if(visitor, child):
    test, t, f = child.args
    if is_fixed(test):
        try:
            test = test()
        except:
            return True, None
        subexpr = LinearRepnVisitor(
            self.subexpression_cache, self.var_map, self.var_order
        ).walk_expression(t if test else f)
        if subexpr.nonlinear:
            return False, (_GENERAL, subexpr)
        elif subexpr.linear:
            return False, (_LINEAR, subexpr)
        else:
            return False, (_CONSTANT, subexpr.constant)
    return True, None


def _before_external(visitor, child):
    ans = visitor.Result()
    if all(is_fixed(arg) for arg in child.args):
        try:
            ans.constant = test()
            return False, (_CONSTANT, ans)
        except:
            pass
    ans.nonlinear = child
    return False, (_GENERAL, ans)


def _before_general_expression(visitor, child):
    return True, None


def _register_new_before_child_dispatcher(visitor, child):
    dispatcher = _before_child_dispatcher
    child_type = child.__class__
    if child_type in native_numeric_types:
        dispatcher[child_type] = _before_native
    elif not child.is_expression_type():
        if child.is_potentially_variable():
            dispatcher[child_type] = _before_var
        else:
            dispatcher[child_type] = _before_npv
    elif not child.is_potentially_variable():
        dispatcher[child_type] = _before_npv
        # If we descend into the named expression (because of an
        # evaluation error), then on the way back out, we will use
        # the potentially variable handler to process the result.
        pv_base_type = child.potentially_variable_base_class()
        if pv_base_type not in dispatcher:
            try:
                child.__class__ = pv_base_type
                _register_new_before_child_dispatcher(self, child)
            finally:
                child.__class__ = child_type
        if pv_base_type in visitor.exit_node_handlers:
            visitor.exit_node_handlers[child_type] = visitor.exit_node_handlers[
                pv_base_type
            ]
            for args, fcn in visitor.exit_node_handlers[child_type].items():
                visitor.exit_node_dispatcher[(child_type, *args)] = fcn
    elif id(child) in visitor.subexpression_cache or issubclass(
        child_type, _GeneralExpressionData
    ):
        dispatcher[child_type] = _before_named_expression
        visitor.exit_node_handlers[child_type] = visitor.exit_node_handlers[
            ScalarExpression
        ]
        for args, fcn in visitor.exit_node_handlers[child_type].items():
            visitor.exit_node_dispatcher[(child_type, *args)] = fcn
    else:
        dispatcher[child_type] = _before_general_expression
    return dispatcher[child_type](visitor, child)


_before_child_dispatcher = collections.defaultdict(
    lambda: _register_new_before_child_dispatcher
)

# Register an initial set of known expression types with the "before
# child" expression handler lookup table.
for _type in native_numeric_types:
    _before_child_dispatcher[_type] = _before_native
# general operators
for _type in _exit_node_handlers:
    _before_child_dispatcher[_type] = _before_general_expression
# override for named subexpressions
for _type in _named_subexpression_types:
    _before_child_dispatcher[_type] = _before_named_expression
# Special handling for expr_if and external functions: will be handled
# as terminal nodes from the point of view of the visitor
_before_child_dispatcher[Expr_ifExpression] = _before_expr_if
_before_child_dispatcher[ExternalFunctionExpression] = _before_external
# Special linear / summation expressions
_before_child_dispatcher[MonomialTermExpression] = _before_monomial
_before_child_dispatcher[LinearExpression] = _before_linear
_before_child_dispatcher[SumExpression] = _before_general_expression


#
# Initialize the _exit_node_dispatcher
#
def _initialize_exit_node_dispatcher(exit_handlers):
    # expand the knowns set of named expressiosn
    for expr in _named_subexpression_types:
        exit_handlers[expr] = exit_handlers[ScalarExpression]

    exit_dispatcher = {}
    for cls, handlers in exit_handlers.items():
        for args, fcn in handlers.items():
            exit_dispatcher[(cls, *args)] = fcn
    return exit_dispatcher


class LinearRepnVisitor(StreamBasedExpressionVisitor):
    Result = LinearRepn
    exit_node_handlers = _exit_node_handlers
    exit_node_dispatcher = _initialize_exit_node_dispatcher(_exit_node_handlers)

    def __init__(self, subexpression_cache, var_map, var_order):
        super().__init__()
        self.subexpression_cache = subexpression_cache
        self.var_map = var_map
        self.var_order = var_order

    def initializeWalker(self, expr):
        walk, result = self.beforeChild(None, expr, 0)
        if not walk:
            return False, self.finalizeResult(result)
        return True, expr

    def beforeChild(self, node, child, child_idx):
        return _before_child_dispatcher[child.__class__](self, child)

    def enterNode(self, node):
        # SumExpression are potentially large nary operators.  Directly
        # populate the result
        if node.__class__ is SumExpression:
            return node.args, self.Result()
        else:
            return node.args, []

    def exitNode(self, node, data):
        if data.__class__ is self.Result:
            return data.walker_exitNode()
        #
        # General expressions...
        #
        return self.exit_node_dispatcher[(node.__class__, *map(itemgetter(0), data))](
            self, node, *data
        )

    def finalizeResult(self, result):
        ans = result[1]
        if ans.__class__ is self.Result:
            mult = ans.multiplier
            if mult == 1:
                zeros = list(filterfalse(itemgetter(1), ans.linear.items()))
                for vid, coef in zeros:
                    del ans.linear[vid]
            else:
                linear = ans.linear
                zeros = []
                for vid, coef in linear.items():
                    if coef:
                        linear[vid] = coef * mult
                    else:
                        zeros.append(vid)
                for vid in zeros:
                    del linear[vid]
                if ans.nonlinear is not None:
                    ans.nonlinear *= mult
            return ans
        ans = self.Result()
        assert result[0] is _CONSTANT
        ans.constant = result[1]
        return ans
