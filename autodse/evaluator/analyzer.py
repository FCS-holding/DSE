"""
The main module of analyzer.
"""
import json
import os
from logging import Logger
from typing import Any, Dict, List, Optional, Tuple

from ..logger import get_eval_logger
from ..result import HLSResult, HierPathNode, Job, MerlinResult, Result


class Analyzer():
    """Main analyzer class"""

    @staticmethod
    def get_analyzer_logger() -> Logger:
        """Attach the analyzer logger"""

        return get_eval_logger('Analyzer')

    @staticmethod
    def analyze(job: Job, mode: str, config: Dict[str, Any]) -> Optional[Result]:
        """Analyze the job result and return a result object

        Parameters
        ----------
        job:
            The job to be analyzed.

        mode:
            The customized mode for analysis.

        config:
            The DSE configure.

        Returns
        -------
        Optional[Result]:
            The analysis result.
        """
        raise NotImplementedError()

    @staticmethod
    def desire(mode: str) -> List[str]:
        """Return a list of desired file name wildcards for analysis

        Parameters
        ----------
        mode:
            The customized mode for analysis.

        Returns
        -------
        List[str]:
            A list of desired file name wildcards for analysis.
        """
        raise NotImplementedError()


class MerlinAnalyzer(Analyzer):
    """"The analyzer especially for Merlin projects"""

    critical_msgs = [
        'Memory burst NOT inferred', 'Coarse-grained pipelining NOT applied on loop',
        'Coarse-grained parallelization NOT applied'
    ]

    resource_types = ['BRAM', 'FF', 'LUT', 'DSP']

    @staticmethod
    def analyze_merlin_log(job: Job, success_msg) -> Tuple[bool, float]:
        """Analyze merlin.log to check if Merlin flow encounters any issues

        Parameters
        ----------
        job:
            The job to be analyzed.

        success_msg:
            A string in the log file to indicate the flow was success.

        Returns
        -------
        Tuple[bool, float]:
            Indicate if the flow was success and the total runtime if success.
        """
        log = Analyzer.get_analyzer_logger()

        merlin_log_path = os.path.join(job.path, 'merlin.log')
        if not os.path.exists(merlin_log_path):
            log.debug('Cannot find merlin.log for analysis')
            return (False, -1)

        success = False
        eval_time = 0.0
        with open(merlin_log_path, 'r') as log_file:
            for line in log_file:
                if line.find(success_msg) != -1:
                    success = True
                elif line.find('Total time: ') != -1:
                    try:
                        eval_time += float(line[12:line.find('seconds')])
                    except ValueError:
                        log.error('Failed to convert runtime %s to float',
                                  line[12:line.find('seconds')])
                        return (False, -1)
        return (success, eval_time)

    @staticmethod
    def analyze_merlin_transform(job: Job) -> Optional[MerlinResult]:
        """Analyze the Merlin transformation result and fetch critical messages

        Parameters
        ----------
        job:
            The job to be analyzed.

        Returns
        -------
        Optional[MerlinResult]:
            The analysis result.
        """

        log = Analyzer.get_analyzer_logger()
        merlin_log_path = os.path.join(job.path, 'merlin.log')
        if not os.path.exists(merlin_log_path):
            log.debug('Cannot find merlin.log for analysis')
            return None

        success, eval_time = MerlinAnalyzer.analyze_merlin_log(
            job, 'Compilation finished successfully')
        if not success:
            return None

        result = MerlinResult()
        result.valid = True
        result.eval_time = eval_time
        with open(merlin_log_path, 'r') as log_file:
            for line in log_file:
                if any(line.find(msg) != -1 for msg in MerlinAnalyzer.critical_msgs):
                    result.criticals.append(line.replace('\n', ''))

        # Result validation: valid if no critical messages
        result.valid = not bool(result.criticals)
        return result

    @staticmethod
    def analyze_merlin_hls(job: Job, config: Dict[str, Any]) -> Optional[HLSResult]:
        """Analyze the Merlin HLS result for QoR and performance bottleneck

        Parameters
        ----------
        job:
            The job to be analyzed.

        Returns
        -------
        Optional[HLSResult]:
            The analysis result.
        """

        log = Analyzer.get_analyzer_logger()
        success, eval_time = MerlinAnalyzer.analyze_merlin_log(job, 'Estimation successfully.')
        if not success:
            log.debug('Merlin estimation failure, no analysis result')
            return None

        result = HLSResult()
        result.eval_time = eval_time

        # Merlin HLS report analysis
        report_path = os.path.join(job.path, '.merlin_prj/run/implement/exec/hls/report_merlin')
        #hier_path = os.path.join(report_path, 'hierarchy.json')
        topo_path = os.path.join(report_path, 'gen_token/topo_info.json')
        info_path = os.path.join(report_path, 'final_info.json')
        if not os.path.exists(info_path):
            log.debug('Cannot find Merlin report files for analysis')
            return None

        with open(info_path, 'r') as filep:
            try:
                hls_info = json.load(filep)
            except ValueError as err:
                log.debug('Failed to read Merlin report %s: %s', info_path, str(err))
                return None

        # Fetch total cycle and resource util as performance QoR
        top_res_info = {}
        for elt in hls_info:
            # Extract cycles
            if 'CYCLE_TOT' in hls_info[elt]:
                try:
                    result.perf = max(float(hls_info[elt]['CYCLE_TOT']), result.perf)
                except ValueError as err:
                    # Some compoenents may be flatten and do not have cycle number (valid),
                    # or HLS cannot estimate the cycle due to insufficient information.
                    if str(err).find('?') != -1:
                        log.error('Found "?" in HLS report. Please use assert to indicate '
                                  'the loop trip count to get rid of all "?" in HLS report.')
                        return None

            # Extract resource utilizations
            for res in MerlinAnalyzer.resource_types:
                util_key = 'util-{0}'.format(res)
                total_key = 'total-{0}'.format(res)

                if not util_key in hls_info[elt] or not total_key in hls_info[elt]:
                    continue

                try:
                    if elt == 'TOP_res_info':
                        # Not kernel resource, process separately
                        top_res_info[util_key] = float(hls_info[elt][util_key]) / 100.0
                        top_res_info[total_key] = float(hls_info[elt][total_key])
                    else:
                        result.res_util[util_key] = max(
                            float(hls_info[elt][util_key]) / 100.0, result.res_util[util_key])
                        result.res_util[total_key] = max(float(hls_info[elt][total_key]),
                                                         result.res_util[total_key])
                except ValueError as err:
                    # Some compoenents may not have resource number (valid),
                    # or HLS cannot estimate the cycle due to insufficient information.
                    if str(err).find('?') != -1:
                        log.error('Found "?" in HLS report. Please use assert to indicate '
                                  'the loop trip count to get rid of all "?" in HLS report.')
                        return None

        # Sum the kernel resource and BSP resource
        for key in result.res_util:
            if key in top_res_info:
                result.res_util[key] += top_res_info[key]

        # Result validation: resource utilization is under the threshold
        max_utils = config['max-util']
        utils = {k[5:]: u for k, u in result.res_util.items() if k.startswith('util-')}
        result.valid = all([utils[res] < max_utils[res] for res in max_utils])

        # Hotspot analysis
        result.ordered_paths = MerlinAnalyzer.analyze_hotspot(topo_path, info_path)

        return result

    @staticmethod
    def find_all_hotspot_paths(cycles: Dict[str, Any], sub_funcs: Dict[str, Any],
                               node: Dict[str, Any]) -> List[List[HierPathNode]]:
        """TBA"""

        def int_or_zero(string: str) -> int:
            """Cast the input string to an integer or 0 otherwise.

            Parameters
            ----------
            string:
                The string to be casted.

            Returns
            -------
            int:
                The integer that the string represents, or 0 if the string cannot be represented
                as an integer.
            """
            try:
                return int(string)
            except ValueError:
                return 0

        def float_or_zero(string: str) -> float:
            """Cast the input string to a float point or 0 otherwise.

            Parameters
            ----------
            string:
                The string to be casted.

            Returns
            -------
            float:
                The float that the string represents, or 0 if the string cannot be represented
                as an float.
            """
            try:
                return float(string)
            except ValueError:
                return 0

        if not node['childs']:
            if node['type'] == 'callfunction' and node['name'] in sub_funcs:
                # Traverse functions by its function call
                paths = MerlinAnalyzer.find_all_hotspot_paths(cycles, sub_funcs,
                                                              sub_funcs[node['name']])
            else:
                # Innermost component, terminate
                paths = []
        else:
            # Sort child based on cycle count
            sorted_child = sorted(
                [child for child in node['childs']],
                key=lambda c: int_or_zero(cycles[c['topo_id']]['CYCLE_TOT'])
                if c['topo_id'] in cycles and 'CYCLE_TOT' in cycles[c['topo_id']] else 0,
                reverse=True)
            paths = []
            for child in sorted_child:
                paths += MerlinAnalyzer.find_all_hotspot_paths(cycles, sub_funcs, child)

        if node['topo_id'] in cycles:
            org_id = cycles[node['topo_id']]['org_identifier']
            total = (float_or_zero(cycles[node['topo_id']]['CYCLE_TOT'])
                     if 'CYCLE_TOT' in cycles[node['topo_id']] else 0)
            comm = (float_or_zero(cycles[node['topo_id']]['CYCLE_BURST'])
                    if 'CYCLE_BURST' in cycles[node['topo_id']] else 0)

            is_compute_bound = None
            if total == 0:
                pass  # No data, set to an invalid value
            elif comm == 0:
                is_compute_bound = True
            else:
                # This is a heuristic since BURST cycle is from
                # model but total cycle is from vendor report.
                # FIXME we should have a better way to judge it.
                is_compute_bound = (comm / total) < 0.8

            # Fitler out the components that we should not spend time on
            if (is_compute_bound is not None and not org_id.startswith('X')):
                if not org_id.startswith('BuiltIn') or is_compute_bound is False:
                    # Cover Merlin generated memcpy functions
                    # since we will try memory coalescing when
                    # bounded by bandwidth no matter what this
                    # scope can be mapped to pragma or not.
                    if paths:
                        for path in paths:
                            path.append(HierPathNode(org_id, total, is_compute_bound))
                    else:
                        paths = [[HierPathNode(org_id, total, is_compute_bound)]]
        else:
            log = Analyzer.get_analyzer_logger()
            log.warning('Hierarchy node %s has no cycle info', node['topo_id'])

        return paths

    @staticmethod
    def analyze_hotspot(hier_path: str, rpt_path: str) -> List[List[HierPathNode]]:
        """TBA"""

        log = Analyzer.get_analyzer_logger()

        # Check and load necessary reports
        if not os.path.exists(hier_path) or not os.path.exists(rpt_path):
            log.debug('Cannot find Merlin report files for hotspot analysis: %s and %s', hier_path,
                      rpt_path)
            return []

        with open(rpt_path, 'r') as filep:
            try:
                cycles = json.load(filep)
            except ValueError as err:
                log.error('Failed to read Merlin report %s: %s', rpt_path, str(err))
                return []

        with open(hier_path, 'r') as filep:
            try:
                hier = json.load(filep)
            except ValueError as err:
                log.error('Failed to read hierarchy info %s: %s', hier_path, str(err))
                return []

        # Find all critical paths
        crit_paths: List[List[HierPathNode]] = []
        for kernel in hier:
            sub_funcs = {func['name']: func for func in kernel['sub_functions']}
            for path in MerlinAnalyzer.find_all_hotspot_paths(cycles, sub_funcs, kernel):
                crit_paths.append(path)

        return crit_paths

    @staticmethod
    def analyze(job: Job, mode: str, config: Dict[str, Any]) -> Optional[Result]:
        #pylint:disable=missing-docstring

        log = Analyzer.get_analyzer_logger()

        if mode == 'transform':
            result: Optional[Result] = MerlinAnalyzer.analyze_merlin_transform(job)
        elif mode == 'hls':
            result = MerlinAnalyzer.analyze_merlin_hls(job, config)
        else:
            log.error('Unrecognized analysis target %s', mode)
            return None

        # QoR computation
        if result and result.perf != 0.0:
            result.quality = float(1.0 / result.perf)

        return result

    @staticmethod
    def desire(mode: str) -> List[str]:
        #pylint:disable=missing-docstirng

        log = Analyzer.get_analyzer_logger()
        if mode == 'transform':
            return ['merlin.log']
        if mode == 'hls':
            return [
                'merlin.log', '.merlin_prj/run/implement/exec/hls/report_merlin/final_info.json',
                '.merlin_prj/run/implement/exec/hls/report_merlin/gen_token/topo_info.json',
                '.merlin_prj/run/implement/exec/hls/report_merlin/hierarchy.json'
            ]

        log.error('Unrecognized analysis target %s', mode)
        return []
