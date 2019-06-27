# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import dataclasses
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Type, TypeVar
from unittest.mock import patch

import libcst.nodes as cst
from libcst._base_visitor import CSTVisitor
from libcst.nodes._internal import CodegenState, visit_required
from libcst.testing.utils import UnitTest


_CSTNodeT = TypeVar("_CSTNodeT", bound="cst.CSTNode")


@dataclass(frozen=True)
class _CSTCodegenPatchTarget:
    type: Type[cst.CSTNode]
    name: str
    old_codegen: Callable[..., None]


class _NOOPVisitor(CSTVisitor):
    pass


def _cst_node_equality_func(a: cst.CSTNode, b: cst.CSTNode, msg=None) -> None:
    """
    For use with addTypeEqualityFunc.
    """
    if not a.deep_equals(b):
        suffix = "" if msg is None else f"\n{msg}"
        raise AssertionError(f"\n{a!r}\nis not deeply equal to \n{b!r}{suffix}")


# We can't use an ABCMeta here, because of metaclass conflicts
class CSTNodeTest(UnitTest):
    def setUp(self) -> None:
        # Fix `self.assertEqual` for CSTNode subclasses. We should compare equality by
        # value instead of identity (what `CSTNode.__eq__` does) for tests.
        #
        # The time complexity of CSTNode.deep_equals doesn't matter much inside tests.
        for v in cst.__dict__.values():
            if isinstance(v, type) and issubclass(v, cst.CSTNode):
                self.addTypeEqualityFunc(v, _cst_node_equality_func)
        self.addTypeEqualityFunc(DummyIndentedBlock, _cst_node_equality_func)

    def validate_node(
        self,
        node: _CSTNodeT,
        code: str,
        parser: Optional[Callable[[str], _CSTNodeT]] = None,
    ) -> None:
        self.__assert_codegen(node, code)

        if parser is not None:
            parsed_node = parser(code)
            self.assertEqual(parsed_node, node)

        # Tests of children should unwrap DummyIndentedBlock first, because we don't
        # want to test DummyIndentedBlock's behavior.
        unwrapped_node = node
        while isinstance(unwrapped_node, DummyIndentedBlock):
            unwrapped_node = unwrapped_node.child
        self.__assert_children_match_codegen(unwrapped_node)
        self.__assert_children_match_fields(unwrapped_node)
        self.__assert_visit_returns_identity(unwrapped_node)

    def assert_invalid(
        self, get_node: Callable[[], cst.CSTNode], expected_re: str
    ) -> None:
        with self.assertRaisesRegex(cst.CSTValidationError, expected_re):
            get_node()

    def __assert_codegen(self, node: cst.CSTNode, expected: str) -> None:
        """
        Verifies that the given node's `_codegen` method is correct.
        """
        self.assertEqual(cst.Module([]).code_for_node(node), expected)

    def __assert_children_match_codegen(self, node: cst.CSTNode) -> None:
        children = node.children
        codegen_children = self.__derive_children_from_codegen(node)
        self.assertSequenceEqual(
            children,
            codegen_children,
            msg=(
                "The list of children we got from `node.children` differs from the "
                + "children that were visited by `node._codegen`."
            ),
        )

    def __derive_children_from_codegen(
        self, node: cst.CSTNode
    ) -> Sequence[cst.CSTNode]:
        """
        Patches all subclasses of `CSTNode` exported by the `cst` module to track which
        `_codegen` methods get called, generating a list of children.

        Because all children must be rendered out into lexical order, this should be
        equivalent to `node.children`.

        `node.children` uses `_visit_and_replace_children` under the hood, not
        `_codegen`, so this helps us verify that both of those two method's behaviors
        are in sync.
        """

        patch_targets: Iterable[_CSTCodegenPatchTarget] = [
            _CSTCodegenPatchTarget(type=v, name=k, old_codegen=v._codegen)
            for (k, v) in cst.__dict__.items()
            if isinstance(v, type)
            and issubclass(v, cst.CSTNode)
            and hasattr(v, "_codegen")
        ]

        children: List[cst.CSTNode] = []
        codegen_stack: List[cst.CSTNode] = []

        def _get_codegen_override(target: _CSTCodegenPatchTarget):
            def _codegen(self, *args, **kwargs) -> None:
                should_pop = False
                # Don't stick duplicates in the stack. This is needed so that we don't
                # track calls to `super()._codegen()`.
                if len(codegen_stack) == 0 or codegen_stack[-1] is not self:
                    # Check the stack to see that we're a direct child, not the root or
                    # a transitive child.
                    if len(codegen_stack) == 1:
                        children.append(self)
                    codegen_stack.append(self)
                    should_pop = True
                target.old_codegen(self, *args, **kwargs)
                # only pop if we pushed something to the stack earlier
                if should_pop:
                    codegen_stack.pop()

            return _codegen

        with ExitStack() as patch_stack:
            for t in patch_targets:
                patch_stack.enter_context(
                    # pyre-ignore Incompatible parameter type [6]: Expected
                    # pyre-ignore `typing.ContextManager[Variable[contextlib._T]]`
                    # pyre-ignore for 1st anonymous parameter to call
                    # pyre-ignore `contextlib.ExitStack.enter_context` but got
                    # pyre-ignore `unittest.mock._patch`.
                    patch(f"libcst.nodes.{t.name}._codegen", _get_codegen_override(t))
                )
            # Execute `node._codegen()`
            cst.Module([]).code_for_node(node)

        return children

    def __assert_children_match_fields(self, node: cst.CSTNode) -> None:
        """
        We expect `node.children` to match everything we can extract from the node's
        fields, but maybe in a different order. This asserts that those things match.

        If you want to verify order as well, use `assert_children_ordered`.
        """
        node_children_ids = {id(child) for child in node.children}
        fields = dataclasses.fields(node)
        field_child_ids = set()
        for f in fields:
            value = getattr(node, f.name)
            if isinstance(value, cst.CSTNode):
                field_child_ids.add(id(value))
            elif isinstance(value, Iterable):
                field_child_ids.update(
                    id(el) for el in value if isinstance(el, cst.CSTNode)
                )

        # order doesn't matter
        self.assertSetEqual(
            node_children_ids,
            field_child_ids,
            msg="`node.children` doesn't match what we found through introspection",
        )

    def __assert_visit_returns_identity(self, node: cst.CSTNode) -> None:
        """
        When visit is called with a visitor that acts as a no-op, the visit method
        should return the same node it started with.
        """
        # TODO: We're only checking equality right now, because visit currently clones
        # the node, since that was easier to implement. We should fix that behavior in a
        # later version and tighten this check.
        self.assertEqual(node, node.visit(_NOOPVisitor()))


@dataclass(frozen=True)
class DummyIndentedBlock(cst.CSTNode):
    """
    A stripped-down version of cst.IndentedBlock that only sets/clears the indentation
    state for the purpose of testing cst.IndentWhitespace in isolation.
    """

    value: str
    child: cst.CSTNode

    def _codegen(self, state: CodegenState) -> None:
        state.increase_indent(self.value)
        self.child._codegen(state)
        state.decrease_indent()

    def _visit_and_replace_children(self, visitor: CSTVisitor) -> "DummyIndentedBlock":
        return DummyIndentedBlock(
            value=self.value, child=visit_required("child", self.child, visitor)
        )
