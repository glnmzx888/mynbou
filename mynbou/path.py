#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
This module contains the main pathing classes of mynbou.

OntdekBaan which discovers every path on a DAG from one node until it reaches a break condition or all nodes are visited.
Volg which traverses backwards from the selected release to collect change metrics
"""

import logging
import copy

from collections import deque

import networkx as nx
from Levenshtein import distance
from dateutil.relativedelta import relativedelta

from pycoshark.mongomodels import Commit, CodeEntityState, FileAction, File, Issue, Hunk, Refactoring, CommitChanges
from pycoshark.utils import java_filename_filter, jira_is_resolved_and_fixed, get_commit_graph, heuristic_renames

from bson.objectid import ObjectId
from mynbou.constants import *


class OntdekBaan(object):
    """Simple variant of OntdekBaan which yields the paths via bfs until a break condition is hit or no unvisited nodes remain."""

    def __init__(self, g):
        self._graph = g.copy()
        self._nodes = set()
        self._log = logging.getLogger(self.__class__.__name__)

    def _bfs_paths(self, source, predecessors, break_condition):
        paths = {0: [source]}
        visited = set()

        if source not in self._graph:
            raise Exception('Commit {} is not contained in the commit graph'.format(source))

        queue = deque([(source, predecessors(source))])
        while queue:
            parent, children = queue[0]

            try:
                # iterate over children list
                child = next(children)

                # we keep track of visited pairs so that we do not have common suffixes
                if (parent, child) not in visited:

                    break_child = False
                    if break_condition is not None and break_condition(child):
                        break_child = True

                    # find path which last node is parent, append first child
                    if not break_child:
                        for path_num, nodes in paths.items():
                            if parent == nodes[-1]:
                                paths[path_num].append(child)
                                break
                        else:
                            paths[len(paths)] = [parent, child]

                    visited.add((parent, child))

                    if not break_child:
                        queue.append((child, predecessors(child)))

            # every child iterated
            except StopIteration:
                queue.popleft()
        return paths

    def set_path(self, start, direction='backward', break_condition=None):
        """Set start node and travel direction for the BFS."""
        self._start = start
        self._direction = direction
        self._break_condition = break_condition

    def all_paths(self):
        """Generator that yields all possible paths fomr the given start node and the direction."""
        if self._direction == 'backward':
            paths = self._bfs_paths(self._start, self._graph.predecessors, self._break_condition)

            for path_num, path in paths.items():
                yield path

        elif self._direction == 'forward':
            paths = self._bfs_paths(self._start, self._graph.successors, self._break_condition)

            for path_num, path in paths.items():
                yield path

        else:
            raise Exception('no such direction: {}, please use backward or forward'.format(self._direction))


class Volg(object):
    """Volg follows file renaming within git.

    It takes a target release and tracks the files contained in the target release backwards.
    If we encounter a rename we add the old name of the file to the aliases of the filename we know, this allows us to keep track of these files.
    If we encounter a copy operation we do not add the old name of the file to the aliases because that file contiues to exist and we would then mix them up.
    """

    def __init__(self, graph, vcs, target_release_hash):
        self._log = logging.getLogger(self.__class__.__name__)

        # the metrics that are collected for each file
        self._init_metrics = {'change_types': [], 'bug_fixes': [], 'authors': [], 'revisions': [], 'lines_added': [], 'lines_deleted': [], 'changesets': [], 'ages': [], 'aliases': [], 'linked_issues': [], 'commit_messages': [], 'days_from_release': [], 'refactorings': []}

        # global cache of expensive first occurence calculation for files
        self._first_occurences = {}

        # all files in target release
        self._release_files = []

        # we need the graph to traverse it
        self._graph = graph

        # change metrics we collect
        self._change_metrics = {}

        self._target_release_hash = target_release_hash

        # all paths back to origin
        self._origin_paths = self._origin_paths(graph, target_release_hash)

        # all paths back to origin for 6 months
        self._change_paths = self._change_paths(vcs, graph, target_release_hash)

        self._vcs = vcs

        # get release files
        c = Commit.objects.get(vcs_system_id=vcs.id, revision_hash=target_release_hash)
        for ces in CodeEntityState.objects.filter(id__in=c.code_entity_states, ce_type='file', long_name__endswith='.java'):
            if java_filename_filter(ces.long_name, production_only=True):
                self._release_files.append(ces.long_name)
                self._change_metrics[ces.long_name] = copy.deepcopy(self._init_metrics)

        self._release_commit = c

        # and release date
        self._release_date = c.committer_date

        # used to track static metric deltas to construct dambros delta matrix
        self._dambros_values = []
        self._dambros_metrics_used = ['wmc', 'dit', 'rfc', 'noc', 'cbo', 'lcom5', 'nii', 'noi', 'tna', 'tnpa', 'tna-tnpa', 'tna-tnla', 'tloc', 'tnm', 'tnlpm', 'tnm-tnpm', 'tnm-tnlm']
        self._dambros_window_size_days = 14
        self._dambros_last_date = self._release_date + relativedelta(days=self._dambros_window_size_days + 1)

        # get first occurences of release files
        self._first_occurences, self._aliases, self._file_name_changes = self.first_occured(vcs, self._origin_paths, self._release_files)

    def _origin_paths(self, graph, target_release_hash):
        o = OntdekBaan(graph)
        o.set_path(target_release_hash, 'backward')
        return list(o.all_paths())

    def _change_paths(self, vcs, graph, target_release_hash):
        target_release = Commit.objects.get(vcs_system_id=vcs.id, revision_hash=target_release_hash)
        previous1 = target_release.committer_date - relativedelta(months=6)

        def break_condition(commit):
            tr = Commit.objects.get(revision_hash=commit, vcs_system_id=vcs.id)
            return tr.committer_date < previous1

        o = OntdekBaan(graph)
        o.set_path(target_release_hash, 'backward', break_condition)
        return list(o.all_paths())

    def calc_current_files(self, commit, release_commit, commit_graph, undirected_graph, rename_cache, current_files):
        """determines the java files changed by a commit and returns them as a set"""
        path_valid = False
        current_files_start = current_files.copy()
        try:
            shortest_paths = list(nx.all_shortest_paths(undirected_graph, release_commit.revision_hash, commit.revision_hash))
        except nx.NetworkXNoPath:
            shortest_paths = []
        for path in shortest_paths:
            # path = nx.shortest_path(undirected_graph, release_commit.revision_hash, commit.revision_hash)
            current_files = current_files_start.copy()
            path_valid = True
            had_backward_edge = False
            for i in range(len(path)-1, 0, -1): # going backwards
                if path[i-1] in commit_graph.pred[path[i]]:
                    if had_backward_edge:
                        # invalid change of direction
                        path_valid = False
                        break
                    if path[i] in rename_cache:
                        renames = rename_cache[path[i]]
                    else:
                        renames = heuristic_renames(self._vcs.id, path[i])
                        rename_cache[path[i]] = renames
                    if renames is not None:
                        for rename in renames[0]:
                            if rename[1] in current_files:
                                current_files.remove(rename[1])
                                current_files.add(rename[0])
                        for deletion in renames[1]:
                            current_files.discard(deletion)
                elif path[i-1] in commit_graph.succ[path[i]]:
                    had_backward_edge = True
                    if path[i-1] in rename_cache:
                        renames = rename_cache[path[i-1]]
                    else:
                        renames = heuristic_renames(self._vcs.id, path[i-1])
                        rename_cache[path[i-1]] = renames
                    if renames is not None:
                        for rename in renames[0]:
                            if rename[0] in current_files:
                                current_files.remove(rename[0])
                                current_files.add(rename[1])
            if path_valid:
                break
        return current_files, path_valid

    def issues_six_months_szz(self):
        """basically looks six months into the future from the release and counts the defects that we can match

        returns a dict, {filename: [list of issues]}
        """

        
        files_release = self._release_files

        commit_graph = get_commit_graph(self._vcs.id)
        undirected_graph = commit_graph.to_undirected(as_view=True)
        rename_cache = {}
        delete_cache = {}

        all_fixed_issues = set()
        six_months = self._release_date + relativedelta(months=6)
        for commit in Commit.objects.filter(vcs_system_id=self._vcs.id, committer_date__gt=self._release_date, committer_date__lt=six_months, labels__adjustedszz_bugfix=True, szz_issue_ids__0__exists=True).only('id', 'committer_date', 'szz_issue_ids', 'revision_hash').timeout(False):
            for issue in Issue.objects.filter(id__in=commit.szz_issue_ids):
                if str(issue.issue_type).lower() == "bug" and jira_is_resolved_and_fixed(issue):
                    all_fixed_issues.add(issue)

        ret = {rfile: [] for rfile in files_release}

        for issue in all_fixed_issues:
            for bugfix_commit in Commit.objects.filter(vcs_system_id=self._vcs.id, labels__adjustedszz_bugfix=True, szz_issue_ids=issue.id, committer_date__gt=self._release_date, committer_date__lt=six_months).only('revision_hash', 'id', 'szz_issue_ids', 'committer_date').timeout(False):
                
                # in comparison to issues_six_months_szzr() we skip the inducing step and just use every bugfix commit within 6 months window
                changed_files = set()
                for fa in FileAction.objects.filter(commit_id=bugfix_commit.id, mode='M'):

                    f = File.objects.get(id=fa.file_id)
                    if f.path not in changed_files and java_filename_filter(f.path):
                        changed_files.add(f.path)

                current_files = None
                if current_files is None:
                    current_files, path_valid = self.calc_current_files(bugfix_commit, self._release_commit, commit_graph, undirected_graph, rename_cache, changed_files)

                    if path_valid and len(current_files.intersection(files_release))>0:
                        for f in current_files:
                            if f in ret.keys():
                                inf = (issue.external_id, str(bugfix_commit.committer_date), bugfix_commit.revision_hash, str(issue.priority).lower(), str(issue.issue_type).lower(), str(issue.created_at))

                                if inf not in ret[f]:
                                    ret[f].append(inf)

        # todo
        # for all files changed in a bugfixing commit find the corresponding names of the file in the release and count those
        # bugs toward the file name
        return ret

    def issues_six_months_szzr(self):
        """basically looks six months into the future from the release and counts the defects that we can match

        returns a dict, {filename: [list of issues]}
        """

        
        files_release = self._release_files

        commit_graph = get_commit_graph(self._vcs.id)
        undirected_graph = commit_graph.to_undirected(as_view=True)
        rename_cache = {}
        delete_cache = {}

        all_fixed_issues = set()
        six_months = self._release_date + relativedelta(months=6)
        for commit in Commit.objects.filter(vcs_system_id=self._vcs.id, committer_date__gt=self._release_date, committer_date__lt=six_months, labels__issueonly_bugfix=True, linked_issue_ids__0__exists=True).only('id', 'committer_date', 'linked_issue_ids', 'revision_hash').timeout(False):
            for issue in Issue.objects.filter(id__in=commit.linked_issue_ids):
                if str(issue.issue_type).lower() == "bug" and jira_is_resolved_and_fixed(issue):
                    all_fixed_issues.add(issue)

        ret = {rfile: [] for rfile in files_release}

        for issue in all_fixed_issues:
            for bugfix_commit in Commit.objects.filter(vcs_system_id=self._vcs.id, linked_issue_ids=issue.id, committer_date__gt=self._release_date, labels__issueonly_bugfix=True, committer_date__lt=six_months).only('revision_hash', 'id', 'linked_issue_ids', 'committer_date').timeout(False):
                
                # calculate if there are any inducings for this fa with the specific label
                # if yes then we consider it?

                changed_files = set()
                for fa in FileAction.objects.filter(commit_id=bugfix_commit.id, mode='M'):

                    # check if we find at least one inducing to this fa
                    if not FileAction.objects.filter(induces__match={'change_file_action_id': fa.id, 'label': 'JL+R'}).count() > 0:
                        continue

                    f = File.objects.get(id=fa.file_id)
                    if f.path not in changed_files and java_filename_filter(f.path):
                        changed_files.add(f.path)

                if len(changed_files)>0:
                    current_files, path_valid = self.calc_current_files(bugfix_commit, self._release_commit, commit_graph, undirected_graph, rename_cache, changed_files)

                    if path_valid and len(current_files.intersection(files_release))>0:
                        for f in current_files:
                            if f in ret.keys():
                                inf = (issue.external_id, str(bugfix_commit.committer_date), bugfix_commit.revision_hash, str(issue.priority).lower(), str(issue.issue_type).lower(), str(issue.created_at))

                                if inf not in ret[f]:
                                    ret[f].append(inf)

        # todo
        # for all files changed in a bugfixing commit find the corresponding names of the file in the release and count those
        # bugs toward the file name
        return ret

    def issues(self):
        """Load inducing file actions for labeling files accordingly.

        For all bug-fixing commits that happened after our chosen release:
        find bug-inducing commits via induced of FileAction

        - uses commit label: validated_bugfix
        - uses issues from: fixed_issue_ids (manually validated links from commit to issue)
        - uses inducing label: JLMIV+ (Jira Links Manual(JLM), Issue Validation(IV), only java files(+), skip comments and empty spaces in blame(+))

        ALL inducing commits of ALL bug fixing commits MUST have a path to the release.
        The only exceptions are partial fixes when the inducing commit without a path to the release is a bug fixing commit for the same issue.
        """
        buginducing_commits = {}
        skipped_issues = set()
        all_fixed_issues = set()

        for commit in Commit.objects.filter(vcs_system_id=self._vcs.id, committer_date__gt=self._release_date, labels__validated_bugfix=True, fixed_issue_ids__0__exists=True).only('id', 'committer_date', 'fixed_issue_ids', 'revision_hash').timeout(False):
            for issue in Issue.objects.filter(id__in=commit.fixed_issue_ids):
                if issue.issue_type_verified and issue.issue_type_verified.lower() == "bug" and jira_is_resolved_and_fixed(issue):
                    all_fixed_issues.add(issue)

        for issue in all_fixed_issues:
            inducings_have_path = True
            blame_commits = []

            for bugfix_commit in Commit.objects.filter(vcs_system_id=self._vcs.id, fixed_issue_ids=issue.id, committer_date__gt=self._release_date).only('revision_hash', 'id', 'fixed_issue_ids', 'committer_date').timeout(False):

                for fa in FileAction.objects.filter(commit_id=bugfix_commit.id, mode='M'):

                    # load bug_inducing FileActions
                    for ifa in FileAction.objects.filter(induces__match={'change_file_action_id': fa.id, 'label': 'JLMIV+R'}):

                        # still need to fetch the correct one
                        for ind in ifa.induces:
                            if ind['change_file_action_id'] == fa.id and ind['label'] == 'JLMIV+R' and ind['szz_type'] != 'hard_suspect':

                                bc = Commit.objects.get(id=ifa.commit_id)
                                blame_commit = bc.revision_hash
                                blame_file = File.objects.get(id=ifa.file_id).path

                                blame_id = '{}_{}'.format(blame_commit, issue.external_id)

                                blame_commits.append(blame_id)

                                # if this inducing commit has no path to our release we skip it altogether
                                if not nx.has_path(self._graph, blame_commit, self._target_release_hash):
                                    if bc.fixed_issue_ids is None or issue.id not in bc.fixed_issue_ids:
                                        inducings_have_path = False
                                        self._log.debug('[{}] has no path to release, skipping issue: {}'.format(blame_commit, issue.external_id))
                                else:
                                    # skip if we are not interested in the blame_file (because it does not point to a release file)
                                    if blame_file not in self._aliases.keys():
                                        if java_filename_filter(blame_file, production_only=True):
                                            self._log.debug('[{}] {} not in release files or aliases {}, skipping issues: {}'.format(blame_commit, blame_file, self._aliases.keys(), issue.external_id))
                                        skipped_issues.add(issue.external_id)
                                        continue

                                    if blame_id not in buginducing_commits.keys():
                                        buginducing_commits[blame_id] = {}

                                    if blame_file not in buginducing_commits[blame_id].keys():
                                        buginducing_commits[blame_id][blame_file] = []

                                    buginducing_commits[blame_id][blame_file].append((issue.external_id, str(bugfix_commit.committer_date), bugfix_commit.revision_hash, str(issue.priority).lower(), str(issue.issue_type_verified).lower(), str(issue.created_at)))

            # not every blame commit has a path to the release
            # we need to remove all of them in this case
            if not inducings_have_path:
                skipped_issues.add(issue.external_id)
                for blame_id in blame_commits:
                    if blame_id in buginducing_commits.keys():  # may not be present because its a hard_suspect
                        self._log.debug('[{}] found inducing commits wihtout path, deleting issue {}'.format(blame_id.split('_')[0], blame_id.split('_')[1]))
                        del buginducing_commits[blame_id]
        ret_issues = {}
        for blame_commit, values in buginducing_commits.items():
            for file, issues in values.items():
                release_file = self._aliases[file]
                if release_file not in ret_issues.keys():
                    ret_issues[release_file] = []
                for i in issues:
                    if i not in ret_issues[release_file]:
                        ret_issues[release_file].append(i)
                        self._log.debug('adding issue {} to file {}'.format(i[0], release_file))
                        if i[0] in skipped_issues:
                            skipped_issues.remove(i[0])
        return ret_issues

    def _add_linked_issues(self, file, commit):
        for issue_id in commit.linked_issue_ids:
            i = Issue.objects.get(id=issue_id)
            self._change_metrics[file]['linked_issues'].append({'external_id': i.external_id, 'priority': i.priority, 'issue_type': i.issue_type})

    def _add_change_metrics(self, file, fa, commit):
        """Add change metrics to our current batch.

        It prepends to a list because we are traversing backwards from the release date.
        """
        author_identity = commit.author_id  # author_identity = Identity.objects.get(people=commit.author_id)  for now we ignore Identities

        self._change_metrics[file]['authors'] = ['{}'.format(author_identity)] + self._change_metrics[file]['authors']
        self._change_metrics[file]['revisions'] = [commit.revision_hash] + self._change_metrics[file]['revisions']
        self._change_metrics[file]['lines_added'] = [fa.lines_added] + self._change_metrics[file]['lines_added']
        self._change_metrics[file]['lines_deleted'] = [fa.lines_deleted] + self._change_metrics[file]['lines_deleted']
        self._change_metrics[file]['changesets'] = [len(Hunk.objects.filter(file_action_id__in=[ffa.id for ffa in FileAction.objects.filter(commit_id=commit.id)]))] + self._change_metrics[file]['changesets']
        self._change_metrics[file]['commit_messages'] = [commit.message] + self._change_metrics[file]['commit_messages']

        # we also calculate a list of ages to calulate weighted age later
        # weighted age according to Moser et al.
        td = commit.committer_date - self._first_occurences[file]
        td2 = self._release_date - commit.committer_date
        self._change_metrics[file]['ages'] = [td.days] + self._change_metrics[file]['ages']
        self._change_metrics[file]['days_from_release'] = [td2.days] + self._change_metrics[file]['days_from_release']

    def _add_refactorings(self, commit):
        cache = set()
        for ref in Refactoring.objects.filter(commit_id=commit.id):
            if 'ce_after' in ref.ce_state.keys():
                ces = CodeEntityState.objects.get(id=ref.ce_state['ce_after'])
                if ces:
                    file = File.objects.get(id=ces.file_id)

                    if file.path not in self._aliases.keys():
                        continue

                    cache.add((file.path, ref.type, ces.long_name))
                    # self._log.debug('[{}] refactoring File: {}, CES: {}, Type: {}'.format(revision_hash, file.path, ces.long_name, ref.type))
        for (ref_file, ref, long_name) in cache:
            self._change_metrics[self._aliases[ref_file]]['refactorings'].append(ref)

    def _add_change_types(self, old_commit, new_commit):
        try:
            cc = CommitChanges.objects.get(old_commit_id=old_commit.id, new_commit_id=new_commit.id)
        except CommitChanges.DoesNotExist:
            return

        if not cc.classification:
            return

        for file_id, changes in cc.classification.items():
            file = File.objects.get(id=file_id)

            if file.path not in self._aliases.keys():
                continue

            # initialize the file with 0 if it does not exist
            change_types = {d: 0 for d in CHANGE_TYPES}

            # update with new values
            for ctype, cvalue in changes.items():
                change_types[ctype.lower()] += cvalue

            self._change_metrics[self._aliases[file.path]]['change_types'] += [change_types]

    def _add_dambros_metrics(self, commit):
        """Use for dambros."""
        # 1. check if current commit is within the window_size in days, if yes skip this commit
        if self._dambros_last_date - relativedelta(days=self._dambros_window_size_days) < commit.committer_date:
            return

        self._dambros_last_date = commit.committer_date

        # we need to collect the classes per file
        files = File.objects.filter(path__in=self._aliases.keys())

        # 2. if not collect metrics from the commit and filter for files in our aliases
        classes = CodeEntityState.objects().aggregate(*[
            {'$match': {'_id': {'$in': [ObjectId(cesid) for cesid in commit.code_entity_states]}, 'ce_type': 'class', 'file_id': {'$in': [ObjectId(f.id) for f in files]}}},
            {'$group': {'_id': '$file_id',
                        'wmc': {'$avg': '$metrics.WMC'},
                        'dit': {'$avg': '$metrics.DIT'},
                        'rfc': {'$avg': '$metrics.RFC'},
                        'noc': {'$avg': '$metrics.NOC'},
                        'cbo': {'$avg': '$metrics.CBO'},
                        'lcom5': {'$avg': '$metrics.LCOM5'},
                        'nii': {'$avg': '$metrics.NII'},
                        'noi': {'$avg': '$metrics.NOI'},
                        'tna': {'$avg': '$metrics.TNA'},
                        'tnpa': {'$avg': '$metrics.TNPA'},
                        'tloc': {'$avg': '$metrics.TLOC'},
                        'tnm': {'$avg': '$metrics.TNM'},
                        'tnlpm': {'$avg': '$metrics.TNLPM'},
                        'tnla': {'$avg': '$metrics.TNLA'},
                        'tnpm': {'$avg': '$metrics.TNPM'},
                        'tnlm': {'$avg': '$metrics.TNLM'}
                        }},
            {'$addFields': {'tna-tnpa': {'$subtract': ['$tna', '$tnpa']},
                            'tna-tnla': {'$subtract': ['$tna', '$tnla']},
                            'tnm-tnpm': {'$subtract': ['$tnm', '$tnpm']},
                            'tnm-tnlm': {'$subtract': ['$tnm', '$tnlm']}}}
        ])

        # grouped by file id
        tmp = {}
        for cl in classes:
            f = File.objects.get(id=cl['_id'])
            target = self._aliases[f.path]

            tmp[target] = {}
            for m in self._dambros_metrics_used:
                if m in cl.keys() and cl[m]:
                    tmp[target][m] = cl[m]

        self._dambros_values.append(tmp)

    def dambros_deltas(self):
        """Create the dambros delta matrix of our collected metrics."""
        deltas = {}
        for m in self._dambros_metrics_used:
            deltas[m] = {}
            for file in self._aliases.values():
                deltas[m][file] = []

        # reverse the entries as we are going from release to end of change path
        self._dambros_values = list(reversed(self._dambros_values))

        # create the deltas pairwise
        for entry1, entry2 in zip(self._dambros_values[::2], self._dambros_values[1::2]):
            for file in self._aliases.values():
                # if the file does not exist in our data in one or the other (or both) set the value to -1
                if file not in entry1.keys() or file not in entry2.keys():
                    for m in deltas.keys():
                        deltas[m][file].append(-1)

                # otherwise we set the value to the absolute delta
                else:
                    for m in deltas.keys():
                        if m in entry1[file].keys() and m in entry2[file].keys():
                            deltas[m][file].append(abs(entry1[file][m] - entry2[file][m]))
        return deltas

    def change_metrics(self):
        """Change path metric calculation.

        Uses the change paths which uses a cutoff time.
        """
        for path in self._change_paths:
            for revision_hash in path:
                c = Commit.objects.get(vcs_system_id=self._vcs.id, revision_hash=revision_hash)

                # skip merge commits as we traverse all possible paths
                if len(c.parents) > 1:
                    continue

                for fa in FileAction.objects.filter(commit_id=c.id):
                    f = File.objects.get(id=fa.file_id)

                    # skip file we are not interested in
                    if f.path not in self._aliases.keys():
                        continue

                    self._add_linked_issues(self._aliases[f.path], c)
                    self._add_change_metrics(self._aliases[f.path], fa, c)
                    self._add_refactorings(c)

                if c.parents:
                    prev = Commit.objects.get(vcs_system_id=self._vcs.id, revision_hash=c.parents[0])
                    self._add_change_types(prev, c)

                self._add_dambros_metrics(c)

        for file in self._change_metrics.keys():
            fo = self._first_occurences[file]
            td = self._release_date - fo
            self._change_metrics[file]['age'] = td.days
            self._change_metrics[file]['first_occurence'] = fo

        return self._change_metrics

    def _heuristic_renames(self, commit):
        """Return most probable rename from all FileActions, rest count as DEL/NEW.

        There may be multiple renames of the same file in the same commit, e.g., A->B, A->C.
        This is due to pygit2 and the Git heuristic for rename detection.
        This function uses another heuristic to detect renames by employing a string distance metric on the file name.
        This captures things like commons-math renames org.apache.math -> org.apache.math3.
        """
        renames = {}
        for fa in FileAction.objects.filter(commit_id=commit.id, mode='R'):
            new_file = File.objects.get(id=fa.file_id)
            old_file = File.objects.get(id=fa.old_file_id)

            if old_file.path not in renames.keys():
                renames[old_file.path] = []
            renames[old_file.path].append(new_file.path)

        true_renames = []
        added_files = []
        for old_file, new_files in renames.items():

            # only one file, easy
            if len(new_files) == 1:
                true_renames.append((old_file, new_files[0]))
                continue

            # multiple files, find the best matching
            min_dist = float('inf')
            probable_file = None
            for new_file in new_files:
                d = distance(old_file, new_file)
                if d < min_dist:
                    min_dist = d
                    probable_file = new_file
            true_renames.append((old_file, probable_file))

            for new_file in new_files:
                if new_file == probable_file:
                    continue
                added_files.append(new_file)
        return true_renames, added_files

    def _first_occured_fallback(self, vcs, file_name):

        needle = file_name

        for c in Commit.objects.filter(vcs_system_id=vcs.id).order_by('-committer_date', '-author_date').only('id', 'revision_hash', 'parents', 'committer_date'):

            if not nx.has_path(self._graph, c.revision_hash, self._target_release_hash):
                continue

            # merge commits are allowd in fallback mode
            # if len(c.parents) > 1:
            #    continue

            for fa in FileAction.objects.filter(commit_id=c.id, mode__in=['A', 'C']):
                f = File.objects.get(id=fa.file_id)
                if f.path == needle:
                    return c.committer_date

            true_renames, false_renames = self._heuristic_renames(c)
            for old_file, new_file in true_renames:
                if needle == new_file:
                    needle = old_file

            for new_file in false_renames:
                if new_file == needle:
                    return c.committer_date

    def first_occured(self, vcs, paths, release_files):
        """Traverse all FileActions of all paths to find when which file was added.

        Follows subsequent renames. We collect aliases for files because we need to know
        which names point to a file contained in the release.
        We do this by having key, value pairs of alias -> release file.
        """
        additions = {}
        aliases = {}
        file_name_changes = {}

        # prefill aliases with release files
        for release_file in release_files:
            aliases[release_file] = release_file

        for c in Commit.objects.filter(vcs_system_id=vcs.id).order_by('-committer_date', '-author_date').only('id', 'revision_hash', 'parents', 'committer_date'):

            revision_hash = c.revision_hash
            if not nx.has_path(self._graph, c.revision_hash, self._target_release_hash):
                continue

            if len(c.parents) > 1:
                continue

            true_renames, false_renames = self._heuristic_renames(c)

            for old_file, new_file in true_renames:
                if old_file in aliases.keys() and new_file in aliases.keys() and aliases[old_file] != aliases[new_file]:
                    self._log.warning('[{}] would overwrite target {} of alias {} with target {}, creating fake addition of the target, skipping'.format(revision_hash, aliases[old_file], old_file, aliases[new_file]))
                    # test with fallback
                    # false_renames.append(aliases[new_file])
                    continue

                # if the file is in our release files (directly)
                if new_file in release_files:
                    aliases[old_file] = aliases[new_file]  # this works because we prefilled the aliases with target -> target

                # else we have a subsequent rename
                elif new_file in aliases.keys() and aliases[new_file] in release_files:
                    aliases[old_file] = aliases[new_file]

                # also record file name changes, currently only used by external dambros
                if len(c.parents) == 1 and new_file in aliases.keys():
                    file_name_changes[aliases[new_file]] = {c.parents[0]: old_file}

            # we collect additions from three sources:
            # 1. additions via doublicate renames (false_renames, see _heuristic_renames)
            # 2. real file addtions from git
            # 3. targets of copy operations
            added_files = []
            for new_file in false_renames:
                added_files.append(new_file)

            for fa in FileAction.objects.filter(commit_id=c.id, mode__in=['A', 'C']):
                f = File.objects.get(id=fa.file_id)
                added_files.append(f.path)

            for new_file in added_files:
                if new_file not in additions.keys():
                    additions[new_file] = []
                additions[new_file].append(c.committer_date)

        ret = {}
        for file_name, add_dates in additions.items():
            if file_name not in aliases.keys():
                continue
            if aliases[file_name] not in ret.keys():
                ret[aliases[file_name]] = []
            ret[aliases[file_name]] += add_dates

        # added files contains all files but we only need release files so we only trigger the fallback for release files
        for file_name in release_files:
            if file_name not in ret:
                ret[file_name] = [self._first_occured_fallback(vcs, file_name)]

        first_occurences = {}
        for file_name, add_dates in ret.items():
            first_occurences[file_name] = max(add_dates)  # if we have multiple possible addition dates we use the max

        return first_occurences, aliases, file_name_changes
