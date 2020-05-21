# Copyright (c) 2020, Xilinx
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of FINN nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import numpy as np
from onnx import helper as oh

from finn.transformation import Transformation
from finn.transformation.infer_shapes import InferShapes
from finn.core.onnx_exec import execute_node
from finn.util.basic import get_by_name


class MoveAddPastMul(Transformation):
    """Move add operations past multiply operations. The aim is to have them
    next to each other such that they can be collapsed into a single add."""

    def apply(self, model):
        graph = model.graph
        node_ind = 0
        graph_modified = False
        for n in graph.node:
            node_ind += 1
            if n.op_type == "Add":
                consumer = model.find_consumer(n.output[0])
                if consumer is not None and consumer.op_type == "Mul":
                    # have: (x) -> add(,B) -> (x+B) -> mul(,A) -> (xA+BA)
                    # want: (x) -> mul(,A) -> (xA) -> add(,BA) -> (xA+BA)
                    # assume input 0 is from the previous layer, input 1 is the
                    # trained (constant) parameter
                    mul_weight_name = consumer.input[1]
                    add_weight_name = n.input[1]
                    A = model.get_initializer(mul_weight_name)
                    B = model.get_initializer(add_weight_name)
                    assert A is not None, "Initializer for mul weights is not set."
                    assert B is not None, "Initializer for add weights is not set."
                    start_name = n.input[0]
                    middle_name = n.output[0]
                    end_name = consumer.output[0]
                    # compute new param value for add
                    BA = B * A
                    # make and insert new nodes
                    new_mul = oh.make_node(
                        "Mul", [start_name, mul_weight_name], [middle_name]
                    )
                    new_add = oh.make_node(
                        "Add", [middle_name, add_weight_name], [end_name]
                    )
                    graph.node.insert(node_ind, new_mul)
                    graph.node.insert(node_ind + 1, new_add)
                    # replace add value
                    model.set_initializer(add_weight_name, BA)
                    # remove old nodes
                    graph.node.remove(n)
                    graph.node.remove(consumer)
                    graph_modified = True
        model = model.transform(InferShapes())
        return (model, graph_modified)


class MoveScalarMulPastMatMul(Transformation):
    """Move scalar mul operations past matmul operations. We want to have muls
    next to each other such that they can be collapsed into a single mul."""

    def apply(self, model):
        graph = model.graph
        node_ind = 0
        graph_modified = False
        for n in graph.node:
            node_ind += 1
            if n.op_type == "Mul":
                consumer = model.find_consumer(n.output[0])
                if consumer is not None and consumer.op_type == "MatMul":
                    mul_weight_name = n.input[1]
                    matmul_weight_name = consumer.input[1]
                    A = model.get_initializer(mul_weight_name)
                    W = model.get_initializer(matmul_weight_name)
                    assert A is not None, "Initializer for mul weights is not set."
                    assert W is not None, "Initializer for matmul weights is not set."
                    start_name = n.input[0]
                    middle_name = n.output[0]
                    end_name = consumer.output[0]
                    mm_out_shape = model.get_tensor_shape(end_name)
                    if all(x == 1 for x in A.shape):
                        # if the mul is scalar, we can simply swap the order of ops
                        # make and insert new nodes
                        new_matmul = oh.make_node(
                            "MatMul", [start_name, matmul_weight_name], [middle_name]
                        )
                        new_mul = oh.make_node(
                            "Mul", [middle_name, mul_weight_name], [end_name]
                        )
                        graph.node.insert(node_ind, new_matmul)
                        graph.node.insert(node_ind + 1, new_mul)
                        model.set_tensor_shape(middle_name, mm_out_shape)
                        # remove old nodes
                        graph.node.remove(n)
                        graph.node.remove(consumer)
                        graph_modified = True
        model = model.transform(InferShapes())
        return (model, graph_modified)


class MoveScalarAddPastMatMul(Transformation):
    """Move scalar add operations past matmul operations. We want to have adds
    next to each other such that they can be collapsed into a single add."""

    def apply(self, model):
        graph = model.graph
        node_ind = 0
        graph_modified = False
        for n in graph.node:
            node_ind += 1
            if n.op_type == "Add":
                consumer = model.find_consumer(n.output[0])
                if consumer is not None and consumer.op_type == "MatMul":
                    add_weight_name = n.input[1]
                    matmul_weight_name = consumer.input[1]
                    A = model.get_initializer(add_weight_name)
                    W = model.get_initializer(matmul_weight_name)
                    assert A is not None, "Initializer for add weights is not set."
                    assert W is not None, "Initializer for matmul weights is not set."
                    start_name = n.input[0]
                    middle_name = n.output[0]
                    end_name = consumer.output[0]
                    mm_out_shape = model.get_tensor_shape(end_name)
                    if all(x == 1 for x in A.shape):
                        # if the add is scalar, we can move it past the matmul
                        # by taking it past the matmul with a dot product
                        Anew = np.dot(A * np.ones(W.shape[0], dtype=np.float32), W)
                        # update the add weight
                        model.set_initializer(add_weight_name, Anew)
                        new_matmul = oh.make_node(
                            "MatMul", [start_name, matmul_weight_name], [middle_name]
                        )
                        new_add = oh.make_node(
                            "Add", [middle_name, add_weight_name], [end_name]
                        )
                        graph.node.insert(node_ind, new_matmul)
                        graph.node.insert(node_ind + 1, new_add)
                        model.set_tensor_shape(middle_name, mm_out_shape)
                        # remove old nodes
                        graph.node.remove(n)
                        graph.node.remove(consumer)
                        graph_modified = True
        model = model.transform(InferShapes())
        return (model, graph_modified)


class MoveScalarAddPastConv(Transformation):
    """Move scalar add operations past conv operations. We want to have adds
    next to each other such that they can be collapsed into a single add."""

    def apply(self, model):
        graph = model.graph
        node_ind = 0
        graph_modified = False
        for n in graph.node:
            node_ind += 1
            if n.op_type == "Add":
                consumer = model.find_consumer(n.output[0])
                if consumer is not None and consumer.op_type == "Conv":
                    conv_node = consumer
                    add_node = n
                    add_weight_name = n.input[1]
                    conv_in_name = consumer.input[0]
                    conv_in_shape = model.get_tensor_shape(conv_in_name)
                    A = model.get_initializer(add_weight_name)
                    assert A is not None, "Initializer for add weights is not set."
                    start_name = n.input[0]
                    end_name = consumer.output[0]
                    conv_out_shape = model.get_tensor_shape(end_name)
                    if all(x == 1 for x in A.shape):
                        # create a tensor filled with the add constant, in
                        # the shape expected by the convolution
                        conv_in_const = np.zeros(conv_in_shape, dtype=np.float32)
                        conv_in_const.fill(A.item())
                        # create an execution context and put in const input
                        exec_ctx = model.make_empty_exec_context()
                        exec_ctx[conv_in_name] = conv_in_const
                        # execute the conv node only
                        execute_node(conv_node, exec_ctx, model.graph)
                        # retrieve the conv output
                        Anew = exec_ctx[end_name]
                        # strip out repetition
                        Anew = Anew[0, :, 0, 0].reshape(1, -1, 1, 1)
                        # update the add weight
                        model.set_initializer(add_weight_name, Anew)
                        # rewire add input to be conv input
                        conv_node.input[0] = start_name
                        model.set_tensor_shape(start_name, conv_in_shape)
                        # use old conv input tensor as conv output
                        conv_node.output[0] = conv_in_name
                        model.set_tensor_shape(conv_in_name, conv_out_shape)
                        # use new conv output as new add node input
                        add_node.input[0] = conv_in_name
                        # use old conv output as new add node output
                        add_node.output[0] = end_name
                        # move add node past conv node
                        graph.node.remove(add_node)
                        graph.node.insert(node_ind, add_node)
                        graph_modified = True
        model = model.transform(InferShapes())
        return (model, graph_modified)


class MoveScalarMulPastConv(Transformation):
    """Move scalar mul operations past conv operations. We want to have muls
    next to each other such that they can be collapsed into a single mul."""

    def apply(self, model):
        graph = model.graph
        node_ind = 0
        graph_modified = False
        for n in graph.node:
            node_ind += 1
            if n.op_type == "Mul":
                consumer = model.find_consumer(n.output[0])
                if consumer is not None and consumer.op_type == "Conv":
                    mul_weight_name = n.input[1]
                    A = model.get_initializer(mul_weight_name)
                    assert A is not None, "Initializer for mul weights is not set."
                    conv_node = consumer
                    mul_node = n
                    start_name = mul_node.input[0]
                    conv_in_name = conv_node.input[0]
                    conv_in_shape = model.get_tensor_shape(conv_in_name)
                    conv_out_name = conv_node.output[0]
                    conv_out_shape = model.get_tensor_shape(conv_out_name)
                    if all(x == 1 for x in A.shape):
                        # if the mul is scalar, we can simply swap the order of ops
                        # rewire mul input to be conv input
                        conv_node.input[0] = start_name
                        model.set_tensor_shape(start_name, conv_in_shape)
                        # use old conv input tensor as conv output
                        conv_node.output[0] = conv_in_name
                        model.set_tensor_shape(conv_in_name, conv_out_shape)
                        # use new conv output as new mul node input
                        mul_node.input[0] = conv_in_name
                        # use old conv output as new mul node output
                        mul_node.output[0] = conv_out_name
                        # move add node past conv node
                        graph.node.remove(mul_node)
                        graph.node.insert(node_ind, mul_node)
                        graph_modified = True
        model = model.transform(InferShapes())
        return (model, graph_modified)


class MoveLinearPastEltwiseAdd(Transformation):
    """Move linear operations (mul, add) past elementwise add operations where possible.
       Specifically,matches and transforms the following patterns:
       (x*C) + (y*C) -> (x + y) * C
       (x+A) + (y+B) -> (x + y) + (A + B)
       where x and y are dynamic inputs, A, B, C are constant tensors (in general).
    """

    def move_node(self, graph, n, prod0, prod1, node_ind):
        # found! move one of the muls to output, remove the other one
        lin0_in0 = prod0.input[0]
        lin1_in0 = prod1.input[0]
        in0 = n.input[0]
        out = n.output[0]
        # TODO: check shapes don't change through scalar mul or add
        # connect the eltwise add inputs to mul inputs
        n.input[0] = lin0_in0
        n.input[1] = lin1_in0
        # connect mul0 output to eltwise add output
        prod0.output[0] = out
        # connect the input of mul0 and output of eltwise add together
        n.output[0] = in0
        prod0.input[0] = in0
        # move prod0 node past eltwise add node, and remove prod1
        graph.node.remove(prod1)
        graph.node.remove(prod0)
        graph.node.insert(node_ind - 2, prod0)

    def apply(self, model):
        graph = model.graph
        node_ind = 0
        graph_modified = False
        nodes = [n for n in graph.node]
        for n in nodes:
            node_ind += 1
            if n.op_type == "Add":
                # check for tensors on both inputs (eltwise add)
                # scalar add has an initializer on one input
                in0 = n.input[0]
                in1 = n.input[1]
                if in0 is None or in1 is None:
                    continue
                A = model.get_initializer(in0)
                B = model.get_initializer(in1)
                if A is not None or B is not None:
                    continue
                # check for mul with same initializer on both inputs
                prod0 = model.find_producer(in0)
                prod1 = model.find_producer(in1)
                # Also check case when both branches are empty and come
                # from the same node: (prod0 == prod1)
                # Other transform should handle that
                if prod0 is None or prod1 is None or (prod0 == prod1):
                    continue
                init0 = model.get_initializer(prod0.input[1])
                init1 = model.get_initializer(prod1.input[1])
                # if either initializer is None, skip
                if init0 is None or init1 is None:
                    continue
                if prod0.op_type == "Mul" and prod1.op_type == "Mul":
                    if np.array_equal(init0, init1):
                        self.move_node(graph, n, prod0, prod1, node_ind)
                        node_ind -= 1
                        graph_modified = True
                elif prod0.op_type == "Add" and prod1.op_type == "Add":
                    init = init0 + init1
                    # update initializer of prod0, which we'll move
                    model.set_initializer(prod0.input[1], init)
                    self.move_node(graph, n, prod0, prod1, node_ind)
                    node_ind -= 1
                    graph_modified = True
                else:
                    continue
        model = model.transform(InferShapes())
        return (model, graph_modified)


class MakeMaxPoolNHWC(Transformation):
    """Convert (MaxPool, NHWCTranpose) into (MaxPoolNHWC)."""

    def apply(self, model):
        graph = model.graph
        node_ind = 0
        graph_modified = False
        for n in graph.node:
            node_ind += 1
            if n.op_type == "MaxPool":
                consumer = model.find_consumer(n.output[0])
                if consumer is not None and consumer.op_type == "Transpose":
                    perms = list(get_by_name(consumer.attribute, "perm").ints)
                    if perms == [0, 2, 3, 1]:
                        n.op_type = "MaxPoolNHWC"
                        n.domain = "finn"
                        start_name = n.input[0]
                        mid_name = consumer.input[0]
                        end_name = consumer.output[0]
                        (b, c, hi, wi) = model.get_tensor_shape(start_name)
                        (b, c, ho, wo) = model.get_tensor_shape(mid_name)
                        consumer.input[0] = start_name
                        consumer.output[0] = mid_name
                        n.input[0] = mid_name
                        n.output[0] = end_name
                        model.set_tensor_shape(mid_name, (b, hi, wi, c))
                        model.set_tensor_shape(end_name, (b, ho, wo, c))
                        graph.node.remove(consumer)
                        graph.node.insert(node_ind - 1, consumer)
                        graph_modified = True
        return (model, graph_modified)