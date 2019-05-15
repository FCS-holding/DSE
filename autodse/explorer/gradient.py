"""
The gradient-based search algorithm
"""
import heapq as pq
from collections import OrderedDict
from typing import Dict, Generator, List, NamedTuple, Optional, Set, Tuple

from texttable import Texttable

from ..parameter import (DesignParameter, DesignPoint, DesignSpace, MerlinParameter,
                         gen_key_from_design_point)
from ..result import HierPathNode, HLSResult, MerlinResult, Result
from .algorithm import SearchAlgorithm


class ParamWPointBatch(NamedTuple):
    """The data class for a parameter and batches of design points that
    manipulated the parameter.
    """

    # The manipulated parameter. It can also be none if we only want to store a batch of points.
    param: Optional[DesignParameter]

    # A list of batches for design points by manipulating the parameter.
    # Element in each batch is a tuple of design point key and content.
    batches: List[List[Tuple[str, DesignPoint]]]


class ExploreTreeNode(NamedTuple):
    """A node in the exploration tree"""

    # The data to the point of this node.
    point_key: str
    quality: float
    perf: float
    result: Result

    # A stack of point batches to be tuned. The top child goes first.
    child: List[ParamWPointBatch]

    # A set of tuned parameter IDs
    tuned: Set[str]


class GradientAlgorithm(SearchAlgorithm):
    """Explore the design space according to gradient. We leverage hotspot analysis to locate
    a small set of high impact parameters and prior to them in order to deliver high QoR results
    at the early stage of exploration.
    """

    def __init__(self,
                 ds: DesignSpace,
                 latency_thd: float = 64,
                 fg_first=True,
                 log_file_name: str = 'algo.log'):
        super(GradientAlgorithm, self).__init__(ds, log_file_name)
        self.latency_thd = latency_thd
        self.fg_first = fg_first

        # TODO: Customizable
        self.comp_order = ['PARALLEL', 'PIPELINE']
        self.comm_order = ['INTERFACE', 'CACHE', 'PIPELINE', 'TILE', 'TILING']

        # Build scope map
        self.scope2param: Dict[str, List[DesignParameter]] = {}
        for param in ds.values():
            if param.scope not in self.scope2param:
                self.scope2param[param.scope] = []
            self.scope2param[param.scope].append(param)

    def get_hotspot_params(self, result: Result, tuned: Set[str]) -> List[DesignParameter]:
        """Identify the hotspot and tunable parameters using ordered_hotspot in result.
        With the ordered performance bottleneck list that was analyzed by Merlin analyzer,
        we create the ordered hotspot parameter list to determine the search order.

        Parameters
        ----------
        result:
            The result generated by the analyzer.

        tuned:
            A set of parameter IDs that have been tuned.

        Returns
        -------
        List[DesignParameter]:
            A list of parameters that have impacts to the hotspot.
        """

        # First set candidates as all tunable parameters
        cand_params = [param for pid, param in self.ds.items() if pid not in tuned]

        if isinstance(result, MerlinResult) and result.ret_code == Result.RetCode.EARLY_REJECT:
            # We may improve the QoR from the early-rejected point if it was reject by memory burst
            if all([msg.find('Memory burst NOT inferred') != -1 for msg in result.criticals]):
                # Only explore tiling factors in this case
                cand_params = [
                    param for pid, param in self.ds.items() if pid not in tuned
                    and isinstance(param, MerlinParameter) and param.ds_type.startswith('TIL')
                ]
        elif isinstance(result, HLSResult) and result.ordered_paths:
            # Generate hotspot scopes
            hotspot_scopes: Dict[str, HierPathNode] = OrderedDict()
            for path in result.ordered_paths:
                for node in path if not self.fg_first else reversed(path):
                    if node.latency >= self.latency_thd:
                        hotspot_scopes[node.nid] = node

            scope_params: Set[DesignParameter] = set()
            cand_params_set: Dict[str, DesignParameter] = OrderedDict()
            lowest_order_params: Dict[str, DesignParameter] = OrderedDict()
            for node in hotspot_scopes.values():
                if node.nid not in self.scope2param:  # No parameters can benefit this scope
                    continue

                # The parameters to this scope
                for param in self.scope2param[node.nid]:
                    scope_params.add(param)

                # The global parameters for memory bound scope
                if not node.is_compute_bound and 'GLOBAL' in self.scope2param:
                    for param in self.scope2param['GLOBAL']:
                        scope_params.add(param)

                # Remove tuned parameters
                tunable_params = [p for p in scope_params if p.name not in tuned]
                if not tunable_params:
                    continue

                # Sort the tunable parameters according to the bottleneck type
                params_w_order = []
                for param in tunable_params:
                    try:
                        if not isinstance(param, MerlinParameter):
                            raise ValueError()
                        if node.is_compute_bound:  # Compute bound
                            order = self.comp_order.index(param.ds_type)
                        else:  # Memory bound
                            order = self.comm_order.index(param.ds_type)
                        params_w_order.append((order, param))
                    except ValueError:
                        # DS type is not listed in the both lists. This may be due to UNKNOWN
                        # or unspecified ds_type or the new pragma type.
                        # Set it to the lowest priority but do not ignore.
                        lowest_order_params[param.name] = param

                # Add parameters to the candidate set in order
                for _, param in sorted(params_w_order, key=lambda x: x[0]):
                    if param.name in cand_params_set:
                        continue
                    cand_params_set[param.name] = param

            # Add the lowest priority parameters
            for param in lowest_order_params.values():
                cand_params_set[param.name] = param

            cand_params = [param for param in cand_params_set.values()]

            # Reverse the list so that the high priority goes to the tail
            # as we will use it as a stack.
            cand_params.reverse()

        return cand_params

    def gen_child_points(self, root_point: DesignPoint,
                         focus: List[DesignParameter]) -> List[ParamWPointBatch]:
        """Generate points by manipulating the given focus parameters and sort them
        based on the order. We evaluate the option expression in parameters to get
        a complete option list and cluster them to batches. The order of batches is
        determined by the "order" attribute in design space. In other words,
        the concept of batches is same as partitions.

        Parameters
        ----------
        root_point:
            The current design point we manipulated from.

        focus:
            A list of focus parameters to be manipulated.

        Returns
        -------
        List[ParamWPointBatch]:
            A list of parameter-based design point batches.
        """

        rets: List[ParamWPointBatch] = []
        for param in focus:
            batch_w_order: Dict[int, List[Tuple[str, DesignPoint]]] = {}
            point = self.clone_point(root_point)
            while self.move_by(point, param.name) == 1:
                # Name the point by its value (only for investigation)
                tag = gen_point_tag(point, param)

                # Store the new point based on its order
                order = self.get_order(point, param.name)
                if order not in batch_w_order:
                    batch_w_order[order] = []
                batch_w_order[order].append((tag, point))

                # Prepare another new point
                point = self.clone_point(point)

            # Store batches by otders (high order goes to the tail)
            batches = []
            for _, batch in sorted(batch_w_order.items(), key=lambda x: x[0], reverse=True):
                batches.append(batch)
            rets.append(ParamWPointBatch(param, batches))

        return rets

    def gen_flatten_points(self, root_point: DesignPoint) -> Optional[ParamWPointBatch]:
        """Generate a batch of points that manipulate pipeline flatten positions
        and attach it to the input parameter. This is an ad-hoc for the default
        config since the the benefit of exploring pipeline flatten position is
        pretty obvious.

        Parameters
        ----------
        root_point:
            The current design point we manipulated from.

        Returns
        -------
        Optional[ParamWPointBatch]:
            A point batch with each flatten point, or None if no such points.
        """

        batch: List[Tuple[str, DesignPoint]] = []
        for param in self.ds.values():
            if not isinstance(param, MerlinParameter):
                self.log.warning('Parameter %s is not a Merlin parameter and cannot be analyzed')
                continue
            elif param.ds_type == 'UNKNOWN':
                self.log.warning('Parameter %s has an unknown design space type and '
                                 'cannot be analyzed')
                continue
            elif param.ds_type != 'PIPELINE' or root_point[param.name] == 'flatten':
                continue

            # Looking for flatten option
            point = self.clone_point(root_point)
            while self.move_by(point, param.name) == 1 and point[param.name] != 'flatten':
                pass
            if point[param.name] != 'flatten':
                continue

            # Store the point
            batch.append((gen_point_tag(point, param), point))

        if batch:
            return ParamWPointBatch(None, [batch])
        return None

    def latency_as_quality(self, new_result: Result) -> float:
        """Compute the quality of the point by (1 / latency).

        Parameters
        ----------
        new_result:
            The new result to be qualified.

        Returns
        -------
        float:
            The quality value. Larger the better.
        """
        return 1.0 / new_result.perf

    def compute_quality(self, curr_result: Result, new_result: Result) -> float:
        """Compute the quality of the point by referring the base one. Larger the better.

        Parameters
        ----------
        curr_result:
            The reference result.

        new_result:
            The new result to be qualified.

        Returns
        -------
        float:
            The quality value. Larger the better.
        """

        if not new_result.valid:  # New result is invalid, abandon.
            return -float('inf')
        if not curr_result.valid:  # Current result is invalid, do not use it as a reference.
            return 1.0 / new_result.perf

        # FIXME: This can be configured by users
        return self.latency_as_quality(new_result)

    def gen(self) -> Generator[List[DesignPoint], Optional[Dict[str, Result]], None]:
        #pylint:disable=missing-docstring

        self.log.info('Launch gradient search algorithm')

        # Start from the default point
        curr_point = self.get_default_point()
        curr_key = gen_key_from_design_point(curr_point)
        key_n_results = yield [curr_point]
        assert key_n_results is not None
        curr_result = list(key_n_results.values())[0]

        if not curr_result.valid:
            self.log.warning('Default point is invalid: %s', str(curr_point))
            self.log.warning('This exploration is low confidence to achieve high QoR.')

        focus_params = self.get_hotspot_params(curr_result, set())
        self.log.info('Focus parameters (high order goes to tail): %s',
                      [p.name for p in focus_params])
        child = self.gen_child_points(curr_point, focus_params)
        flatten_child = self.gen_flatten_points(curr_point)
        if flatten_child:
            child.append(flatten_child)

        # Push the default point as the tree node
        explore_tree: List[List[ExploreTreeNode]] = [[]]
        tuned: Set[str] = set()
        pq.heappush(explore_tree[0],
                    ExploreTreeNode(curr_key, 0, curr_result.perf, curr_result, child, tuned))

        iter_cnt = 0
        while True:
            iter_cnt += 1
            self.log.info('=== ITERATION %d ===', iter_cnt)

            # Identify the next available level with tunable parameters
            curr_lv = -1
            while -curr_lv <= len(explore_tree) and not explore_tree[curr_lv]:
                curr_lv -= 1
            if -curr_lv > len(explore_tree):
                self.log.info('Explore tree is totally empty, stop.')
                return
            self.log.info('Tree Level %d', len(explore_tree) + curr_lv)

            # Take the next point with the best cost to explore
            # Note: we only peek the first node in the heap without popping it
            curr_node = explore_tree[curr_lv][0]
            tuned = curr_node.tuned

            # Get the focus parameters to be tuned
            # Note: the child is a stack so we always pick the top (last) value.
            param_w_points = curr_node.child[-1]

            self.log.info('Working Node:')
            self.log_node(curr_node)

            if param_w_points.batches:
                self.log.info('Working batch: %s', get_batch_name(param_w_points))
                key_2_tags: Dict[str, str] = {}
                pending: List[DesignPoint] = []

                # Consume one batch and evaluate it
                curr_batch = param_w_points.batches.pop()
                for tag, point in curr_batch:
                    key_2_tags[gen_key_from_design_point(point)] = tag
                    pending.append(point)
                self.log.info('Evaluating %d poitns', len(pending))
                key_n_results = yield pending
                assert key_n_results is not None
                self.log.info('Received results from evaluator')

                # Create the next level if needed
                if curr_lv == -1:
                    explore_tree.append([])
                    curr_lv -= 1

                # Process the results and generate next points
                self.log.info('Results')
                for key, result in key_n_results.items():
                    quality = self.compute_quality(curr_result, result)
                    self.log_result(key_2_tags[key], quality, result)

                    if quality == -float('inf'):  # Evaluation failure or point-of-not-interest
                        self.log.info('-> No quality, abandon')
                        continue

                    assert result.valid
                    assert result.point is not None

                    # Add current parameter itself to tuned parameter
                    new_tuned = set(tuned)
                    if param_w_points.param:
                        new_tuned.add(param_w_points.param.name)

                    # Identify the hotspot
                    focus_params = self.get_hotspot_params(result, new_tuned)
                    if not focus_params:
                        self.log.info('-> No tunable parameters, abandon')
                        continue

                    # Gnerate next points and add to the tree
                    child = self.gen_child_points(result.point, focus_params)
                    pq.heappush(
                        explore_tree[curr_lv + 1],
                        ExploreTreeNode(key, quality, result.perf, result, child, new_tuned))
                    self.log.info('-> Add to level %d with %d tunable parameters: %s',
                                  len(explore_tree) + curr_lv + 1, len(child),
                                  [p.name for p in focus_params])
            else:
                self.log.info('No batch to be worked')

            # Finished batches related this parameter
            if not param_w_points.batches:  # No more batches
                self.log.info('Run out of batches of parameter %s',
                              param_w_points.param.name if param_w_points.param else 'builtin')
                curr_node.child.pop()

            # Finished all child of this node
            if not curr_node.child:
                pq.heappop(explore_tree[curr_lv])
                self.log.info('Finished exploring current node, remove from the tree')

        self.log.info('No more points to be explored, stop.')

    def log_node(self, node: ExploreTreeNode) -> None:
        """Log information of an explore tree node

        Parameters
        ----------
        node:
            The target explore tree node.
        """

        child_str_pairs = []
        for param_w_point in node.child:
            name = get_batch_name(param_w_point)
            batch_sizes = ','.join([str(len(b)) for b in param_w_point.batches])
            child_str_pairs.append((name, batch_sizes))

        tbl = Texttable(max_width=120)
        tbl.set_cols_dtype(['t'] * 2)
        tbl.add_row(['Point', node.point_key])
        tbl.add_row(['Quality', '{:.2e}'.format(node.quality)])
        tbl.add_row(['Perf.', '{:.2e}'.format(node.perf)])
        tbl.add_row(['Child', '<-'.join([str(c) for c in child_str_pairs])])
        tbl.add_row(['Tuned', ','.join(node.tuned)])
        for line in tbl.draw().split('\n'):
            self.log.info(line)

    def log_result(self, tag: str, quality: float, result: Result) -> None:
        """Log information of the given result

        Parameters
        ----------
        tag:
            The tag of the corresponding design point.

        quality:
            The quantified result quality.

        result:
            The result to be logged.
        """

        tbl = Texttable(max_width=120)
        tbl.header([
            'Tag', 'Quality', 'Perf.',
            ', '.join([k[5:] for k in result.res_util.keys() if k.startswith('util')]), 'Status',
            'Valid'
        ])
        tbl.set_cols_dtype(['t'] * 6)
        tbl.add_row([
            tag, '{:.2e}'.format(quality), '{:.2e}'.format(result.perf), ', '.join([
                '{:.1f}'.format(result.res_util[k]) for k in result.res_util.keys()
                if k.startswith('util')
            ]),
            str(result.ret_code)[8:],
            str(result.valid)
        ])
        for line in tbl.draw().split('\n'):
            self.log.info(line)


def gen_point_tag(point: DesignPoint, param: DesignParameter) -> str:
    """Generate a tag for the given design point with 'pid-value' as its format.

    Parameters
    ----------
    point:
        The design point to be tagged.

    param:
        The parameter we interested in.

    Returns
    -------
    str:
        The tag of the point.
    """
    return '{0}-{1}'.format(param.name, point[param.name])


def get_batch_name(batches: ParamWPointBatch) -> str:
    """Get the name of given batch set. It is either the name of the represented parameter or
    'builtin' is no parameter is specified.

    Parameters
    ----------
    batches:
        The parameter with point batches.

    Returns
    -------
    str:
        The name of batches
    """
    return batches.param.name if batches.param else 'builtin'
