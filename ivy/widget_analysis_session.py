#
# Copyright (c) Microsoft Corporation. All Rights Reserved.
#
"""
This is an attempt to create a custom IPython widget that contains
an Ivy concept graph.
"""

from copy import deepcopy
from collections import defaultdict, OrderedDict
from textwrap import dedent
from itertools import chain
import os.path
from cPickle import dumps
import time
import datetime

import IPython.html.widgets as widgets
from IPython.html.widgets import HBox, VBox

import z3

from widget_cy_graph import CyGraphWidget
from widget_dialog import DialogWidget
from widget_modal_messages import ModalMessagesWidget
from cy_render import render_concept_graph, render_rg, render_proof_stack
from concept_interactive_session import ConceptInteractiveSession
from concept import (get_initial_concept_domain,
                     get_diagram_concept_domain,
                     get_structure_concept_domain,
                     get_structure_concept_abstract_value,
                     get_structure_renaming,
                     get_standard_combinations)
import cy_styles
from dot_layout import dot_layout
from logic import And, Or, Not, Eq, Apply
from logic_util import used_constants, free_variables
from proof import ProofGoal
from ivy_interp import State
from ui_extensions_api import (arg_node_actions, goal_node_actions,
                               interaction, UserSelectMultiple)
import tactics_api as ta
from ivy_logic_utils import true_clauses, false_clauses
import logic as lg
import logic_util as lu

_last_timestamp = None
def timestamp():
    global _last_timestamp
    current = datetime.datetime.now()
    if _last_timestamp is not None:
        elapsed = current - _last_timestamp
    else:
        elapsed = current - current
    _last_timestamp = current

    return '[{} | {}] '.format(
        current.strftime('%H:%M:%S') + '{:.1f}'.format(current.microsecond / 1e6)[1:],
        elapsed
    )

def _print_args(*args, **kwargs):
    print args, kwargs

_edge_display_classes = ['all_to_all', 'edge_unknown', 'none_to_none']
_edge_display_checkboxes = _edge_display_classes + ['transitive']
_node_label_display_checkboxes = ['node_necessarily', 'node_maybe', 'node_necessarily_not']

# for debugging layouts:
# widgets.Box.border_style.default_value = 'solid'

_graph_background_color = 'rgb(192,192,255)'


def _make_buttons(buttons, **kwargs):
    """
    buttons should be an iterable of (label, onclick function) pairs
    kwargs are passed to widgets.Button

    The result is a list button widgets
    """
    result = []
    for x in buttons:
        if type(x) is tuple:
            k, v = x
            b = widgets.Button(description=k, **kwargs)
            b.on_click(v)
            result.append(b)
        else:
            result.append(x)
    return result


def SmallButton(*args, **kwargs):
    result = widgets.Button(*args, **kwargs)
    result._dom_classes = ['btn-xs']
    return result


class ConceptSessionControls(object):
    """
    This class provides the common widgets and logic for controlling a
    concept session. It is inherited by other widget classes that
    display graphs in the context of a concept session.

    This does not contain any CyGraphWidget's, but only the checkboxes
    and buttons used to control the concept session and their logic.
    """
    def __init__(self):
        self.concept_session = None # to be set externally

        self.concept_style_basis = cy_styles.concept_style
        self.concept_style_colors = []

        self.concept_buttons = _make_buttons([
            ('undo', self.undo),
            ('reset domain', self.reset_domain),
            ('diagram domain', self.diagram_domain),
        ])

        self.transitive_relations = frozenset()

        view_control_header_buttons = []
        for label, edge_class in zip(['+', '?', '-', u'\u2264'], _edge_display_checkboxes):
            btn = SmallButton(
                description=label,
                margin='0px',
                font_weight='bolder',
                background_color=_graph_background_color,
                border_style='none',
            )
            btn.edge_class = edge_class
            btn.on_click(self.edge_class_click)
            view_control_header_buttons.append(btn)

        self.view_controls = VBox(
            [HBox(view_control_header_buttons)],
            background_color=_graph_background_color,
            flex=0,
            overflow_x='scroll',
            overflow_y='scroll',
            _css=[
                (None, 'max-height', '100%'),
                (None, 'max-width', '200px'),
                (None, 'margin-right', '5px'),
            ],
        )

        # dict mapping edge names to widget tuples
        self.edge_display_checkboxes = defaultdict(
            lambda: dict((x, self.new_display_checkbox()) for x in _edge_display_checkboxes)
        )
        # dict mapping label names to widget tuples
        self.node_label_display_checkboxes = defaultdict(
            lambda: dict((x, self.new_display_checkbox()) for x in _node_label_display_checkboxes)
        )
        self.ignore_display_checkbox_change = False

    def new_display_checkbox(self):
        result = widgets.Checkbox(value=False, margin='2px')
        result.on_trait_change(self.change_display_checkbox, 'value')
        return result

    def change_display_checkbox(self):
        if self.ignore_display_checkbox_change:
            return

        self.transitive_relations = frozenset(
            edge_name
            for edge_name, x in self.edge_display_checkboxes.iteritems()
            if x['transitive'].value
        )

        self.concept_session.domain.combinations = get_standard_combinations()

        self.concept_session.domain.concepts['edges'] = [
            edge_name
            for edge_name in self.concept_session.domain.concepts_by_arity(2)
            if any(self.edge_display_checkboxes[edge_name][edge_class].value
                   for edge_class in _edge_display_classes)
        ]

        self.concept_session.domain.concepts['node_labels'] = [
            label_name
            for label_name in self.concept_session.domain.possible_node_labes()
            if any(self.node_label_display_checkboxes[label_name][k].value
                   for k in _node_label_display_checkboxes)
        ]

        self.concept_session.widget = None
        self.concept_session.recompute()
        self.concept_session.widget = self

        self.render_graph()
        self.update_concept_style()

    def edge_name_click(self, button):
        # toggle edge state
        self.ignore_display_checkbox_change = True
        try:
            edge_name = button.edge_name
            new_value = edge_name not in self.concept_session.domain.concepts['edges']
            for k in _edge_display_classes:
                self.edge_display_checkboxes[edge_name][k].value = new_value
        finally:
            self.ignore_display_checkbox_change = False
            self.change_display_checkbox()

    def edge_class_click(self, button):
        """
        toglle all checkboxes on, unless they're all on, in which case
        toggle to off
        """
        self.ignore_display_checkbox_change = True
        try:
            edge_class = button.edge_class
            new_value = not all(
                x[edge_class].value
                for x in self.edge_display_checkboxes.values()
            )
            for x in self.edge_display_checkboxes.values():
                x[edge_class].value = new_value
        finally:
            self.ignore_display_checkbox_change = False
            self.change_display_checkbox()

    def node_label_name_click(self, button):
        # toggle node label
        self.ignore_display_checkbox_change = True
        try:
            label_name = button.label_name
            new_value = label_name not in self.concept_session.domain.concepts['node_labels']
            for k in _node_label_display_checkboxes:
                self.node_label_display_checkboxes[label_name][k].value = new_value
        finally:
            self.ignore_display_checkbox_change = False
            self.change_display_checkbox()

    def get_concept_style(self):
        return deepcopy(
            self.concept_style_basis +
            self.concept_style_colors
        )

    def apply_structure_renaming(self, st):
        return st

    def update_view_controls(self):
        """
        update the view controls to reflect the available concepts, after
        concepts were added or removed from the domain
        """
        view_controls = []

        # add edge view controls
        colors = [
            'blue',
            'green',
            'red',
            'yellow',
            'magenta',
            'pink',
            'purple',
            'black',
            'white',
            '#FF7F50',
            '#008080',
            '#8B4513',
            '#9ACD32',
            '#556b2f',
            '#2f4f4f',
        ]
        self.concept_style_colors = []
        for i, edge_name in enumerate(self.concept_session.domain.concepts_by_arity(2)):
            color = colors[i % len(colors)]
            self.concept_style_colors.append({
                "selector": "edge[obj={!r}]".format(str(edge_name)), # the str() is to convert from unicode
                "style": dict((x, color) for x in [
                    "line-color",
                    "target-arrow-color",
                    "source-arrow-color",
                    "mid-source-arrow-color",
                    "mid-target-arrow-color",
                ]),
            })
            btn = SmallButton(
                description=self.apply_structure_renaming(edge_name),
                color=color,
                margin='5px',
                background_color=_graph_background_color,
                font_weight='bolder',
                border_style='none',
            )
            btn.edge_name = edge_name
            btn.on_click(self.edge_name_click)
            view_controls.append(HBox(
                [
                    self.edge_display_checkboxes[edge_name][k]
                    for k in _edge_display_checkboxes
                ] + [btn],
            ))

        # add node label view controls
        for label_name in sorted(self.concept_session.domain.possible_node_labes()):
            btn = SmallButton(
                description=label_name,
                margin='5px',
                background_color=_graph_background_color,
                font_weight='bolder',
                border_style='none',
            )
            btn.label_name = label_name
            btn.on_click(self.node_label_name_click)
            view_controls.append(HBox(
                [
                    self.node_label_display_checkboxes[label_name][k]
                    for k in _node_label_display_checkboxes
                ] + [btn],
            ))

        # update the view controls and keep the header
        self.view_controls.children = [self.view_controls.children[0]] + view_controls
        self.change_display_checkbox()

    def undo(self, button=None):
        self.concept_session.undo()

    def reset_domain(self, button=None):
        self.concept_session.replace_domain(get_initial_concept_domain(
            self.concept_session.analysis_session.analysis_state.ivy_interp.sig
        ))

    def diagram_domain(self, button=None):
        self.concept_session.replace_domain(get_diagram_concept_domain(
            self.concept_session.analysis_session.analysis_state.ivy_interp.sig,
            And(*self.concept_session.goal_constraints),
        ))

    def remove_concept(self, concept, source=None, target=None):
        concepts = set([concept] + [x[0] for x in self.graph.selected])
        self.concept_session.remove_concepts(*concepts)

    def split(self, concept, by):
        self.concept_session.split(concept, by)

    def suppose_empty(self, concept):
        self.concept_session.suppose_empty(concept)

    def materialize_node(self, concept):
        self.concept_session.materialize_node(concept)

    def materialize_edge(self, edge, source, target, polarity):
        self.concept_session.materialize_edge(
            edge, source, target, polarity
        )

    def add_projection(self, node, name, concept):
        self.edge_display_checkboxes[name]['all_to_all'].value = True
        self.concept_session.add_edge(name, concept)


class ConceptStateViewWidget(ConceptSessionControls):
    """
    This does not inherit from widgets.DOMWidget, but instead
    constructs several "real" widgets and displays them when
    displayed.

    To embed this in another widget (a box), embed the .box widget.
    """
    def __init__(self, analysis_session_widget):
        super(ConceptStateViewWidget, self).__init__()
        self.analysis_session_widget = analysis_session_widget

        self.title = ''
        self.current_step = None
        self.arg_node = None
        self.proof_goal = None

        self.box = DialogWidget(
            title='',
            orientation='vertical',
            overflow_x='hidden',
            overflow_y='hidden',
            options={
                'height': 'max',
                'width': 450,
                'position': {
                    'my': 'right',
                    'at': 'right-5',
                },
            },
        )

        self.info_area = widgets.Textarea(margin='5px')
        self.graph = CyGraphWidget(width='100%', height='100%')
        self.graph.info_area = self.info_area

        self.state_text = widgets.Textarea(margin='5px')
        self.constrains_text = widgets.Textarea(margin='5px')
        self.facts_list = widgets.SelectMultiple(margin='5px')

        self.graph.cy_layout = {'name': 'preset'}

        self.buttons = _make_buttons([
            ('gather facts', self.gather_facts),
            ('new goal', self.analysis_session_widget.concept_new_goal),
            ('check', self.analysis_session_widget.concept_check),
            ('min unsat core', self.analysis_session_widget.concept_min_unsat_core),
            ('refine', self.analysis_session_widget.concept_refine),
        ])

        self.extra_buttons_box = HBox(
            width='100%',
            overflow_y='hidden',
        )

        self.box.children = [
            HBox(
                [
                    self.view_controls,
                    HBox(
                        [self.graph],
                        flex=1,
                        height='100%',
                        overflow_x='hidden',
                        overflow_y='hidden',
                    ),
                ],
                flex=1,
                width='100%',
                overflow_y='hidden',
            ),
            # widgets.HBox(
            #     [
            #         self.info_area,
            #         self.state_text,
            #         self.constrains_text,
            #     ],
            # ),
            HBox(
                [
                    self.facts_list,
                ],
                width='100%',
                overflow_y='hidden',
            ),
            HBox(
                self.concept_buttons + self.buttons,
                width='100%',
                overflow_y='hidden',
            ),
            self.extra_buttons_box
        ]

    def _ipython_display_(self):
        """Called when display() or pyout is used to display the session"""
        self.box._ipython_display_()

    def update_concept_style(self):
        self.graph.cy_style = self.get_concept_style()

    def render_graph(self):
        self.graph.cy_elements = dot_layout(render_concept_graph(self), self.transitive_relations)

    def render(self):
        self.info_area.value = ''
        self.state_text.value = str(self.concept_session.state)
        self.constrains_text.value = '\n\n'.join(
            ['From goal:'] +
            [str(x) for x in self.concept_session.goal_constraints] +
            ['', 'From suppositions:'] +
            [str(x) for x in self.concept_session.suppose_constraints]
        )
        self.box.title = self.title
        self.update_view_controls()

    def gather_facts(self, button=None):
        facts = []
        elements = self.graph.selected
        if len(elements) == 0:
            # if nothing is selected, use all elements
            elements = self.graph.elements
        for x in elements:
            if len(x) == 1:
                # a node
                facts += self.concept_session.get_node_facts(*x)
            elif len(x) == 3:
                # an edge
                facts += self.concept_session.get_edge_facts(*x)
            else:
                assert False, x
        self.facts_list.options = [
            (str(f), f)
            for f in facts
        ]

    def get_active_facts(self):
        """
        Return a list of the selected facts, for all facts if none are selected
        """
        result = list(self.facts_list.value)
        if len(result) == 0:
            result = [x[1] for x in self.facts_list.options]
        return result


class TransitionViewWidget(ConceptSessionControls):
    """
    This does not inherit from widgets.DOMWidget, but instead
    constructs several "real" widgets and displays them when
    displayed.

    To embed this in another widget (a box), embed the .box widget.
    """
    def __init__(self, analysis_session_widget):
        super(TransitionViewWidget, self).__init__()
        self.analysis_session_widget = analysis_session_widget

        self.box = DialogWidget(
            title='TransitionViewWidget',
            orientation='vertical',
            overflow_x='hidden',
            overflow_y='hidden',
            options={
                'height': 'max',
                'width': 450,
                'position': {
                    'my': 'right',
                    'at': 'right-5',
                },
            },
        )

        self.pre_state = (None, Or(), None)
        self.post_state = (None, Or(), None)
        self.current_conjecture = None

        graph_options = dict(
            width='100%',
            height='100%',
            cy_layout={'name': 'preset'},
        )
        self.pre_graph = CyGraphWidget(**graph_options)
        self.post_graph = CyGraphWidget(**graph_options)
        self.graphs = (self.pre_graph, self.post_graph)

        self.facts_list = widgets.SelectMultiple(margin='5px')
        self.result = widgets.HTML('')
        self.bmc_bound = widgets.Dropdown(
            options=[(str(n), n) for n in [1,3,5,10,15]],
            value=3,
            description='BMC bound:',
        )
        self.relations_to_minimize = widgets.Text(
            value='relations to minimize',
            description='Relations to minimize:',
        )

        self.buttons = _make_buttons([
            ('check inductiveness', self.check_inductiveness),
            ('gather facts', self.gather_facts),
            ('bmc conjecture', self.bmc_conjecture),
            ('minimize conjecture', self.minimize_conjecture),
            ('magic', self.find_relative_inductive_conjecture),
            #('sufficient?', self.is_sufficient),
            ('rel. inductive?', self.is_inductive),
            ('strengthen', self.strengthen),
            ('weaken', self.weaken),
            #('necessary?', self.is_necessary),
            #('new goal', self.analysis_session_widget.concept_new_goal),
            #('min unsat core', self.analysis_session_widget.concept_min_unsat_core),
            #('refine', self.analysis_session_widget.concept_refine),
        ])

        self.box.children = [
            HBox(
                [
                    self.view_controls,
                    HBox(
                        [self.pre_graph],
                        flex=1,
                        height='100%',
                        overflow_x='hidden',
                        overflow_y='hidden',
                        _css=[(None, 'margin-right', '5px')],
                    ),
                    HBox(
                        [self.post_graph],
                        flex=1,
                        height='100%',
                        overflow_x='hidden',
                        overflow_y='hidden',
                    ),
                ],
                flex=1,
                width='100%',
                overflow_y='hidden',
            ),
            HBox(
                [
                    self.bmc_bound,
                    self.relations_to_minimize,
                ],
                width='100%',
                _css=[(None, 'margin-top', '5px')],

            ),
            HBox(
                [
                    self.facts_list,
                    self.result,
                ],
                width='100%',
                overflow_y='hidden',
            ),
            HBox(
                self.buttons,
                width='100%',
                overflow_y='hidden',
            ),
        ]

        # this isn't really a part of the transition view, but a part of the CTI based process
        self.conjectures = []

        self.structure_renaming = {}

    def log(self, message, **extra):
        # TODO: shoud rethink this
        open(self.log_filename, 'a').write(repr([
            time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            message,
            {k: dumps(v, 2) for k, v in [
                ('pre', self.pre_state[0]),
                ('post', self.post_state[0]),
                ('bmc_bound', self.bmc_bound.value),
                ('conjectures', self.conjectures),
                ('current_conjecture', self.current_conjecture),
                ('fact_list_options', self.facts_list.options),
                ('fact_list_value', self.facts_list.value),
                ('active_facts', self.get_active_facts()),
                ('selected_conjecture', self.get_selected_conjecture()),
            ] + extra.items()}
         ]) + '\n')

    def register_session(self, session):
        # TODO: shoud rethink this
        import platform
        self.session = session
        self.log_filename = os.path.splitext(session.filename)[0] + '.log'
        self.log(
            'Started new session',
            hostname=platform.node(),
            ivy_source_filename=os.path.abspath(session.filename),
            ivy_source=open(session.filename).read(),
        )

    def show_result(self, result):
        import cgi
        self.result.value = '''<p style="white-space:pre-wrap">{}</p>'''.format(
            cgi.escape(result).encode('ascii', 'xmlcharrefreplace')
        )
        self.log(result) # TODO: rethink this...

    def set_states(self, pre, post):
        """
        pre, post are expected to be ivy_interp.State objects with a
        self.concept_session.state = Or().universe
        """
        self.facts_list.options = []
        if pre is False:
            self.pre_state = (None, Or(), None)
        else:
            self.pre_state = (pre, pre.clauses.to_formula(), get_structure_concept_abstract_value(pre))
        if post is False:
            self.post_state = (None, Or(), None)
        else:
            self.post_state = (post, post.clauses.to_formula(), get_structure_concept_abstract_value(post))
        if pre is False:
            self.concept_session.replace_domain(self.concept_session.domain)
        else:
            self.structure_renaming = get_structure_renaming(pre, self.transitive_relations)
            self.concept_session.replace_domain(get_structure_concept_domain(pre))


    def _ipython_display_(self):
        """Called when display() or pyout is used to display the session"""
        self.box._ipython_display_()

    def update_concept_style(self):
        for g in self.graphs:
            g.cy_style = self.get_concept_style()

    def render_graph(self, pre_only=False):
        self.concept_session.widget = None
        elements = []
        if pre_only:
            states = [self.pre_state]
            graphs = [self.pre_graph]
        else:
            states = [self.post_state, self.pre_state]
            graphs = [self.post_graph, self.pre_graph]
        for state, formula, cache in states:
            self.concept_session.state = formula
            self.concept_session.cache = cache
            self.concept_session.recompute()
            elements.append(dot_layout(render_concept_graph(self), self.transitive_relations))
            self.concept_session.state = Or()
            self.concept_session.cache = None
        for g, e in zip(graphs, elements):
            g.cy_elements = e
        self.concept_session.widget= self
        # TODO: this is very ugly: the concept_session is left with
        # the abstract value of the pre state for use in gather
        # facts... I should really have two separate concept sessions
        # here...

    def render(self):
        self.update_view_controls()

    def gather_facts(self, button=None):
        """
        Gather only based on selected nodes, taking all visible edges.
        """
        g = self.pre_graph
        facts = [] # list of pairs (formula, graph_elements)
        elements = g.selected
        if len(elements) == 0:
            # if nothing is selected, use all elements
            elements = g.elements
        selected_nodes = [x[0] for x in elements if  len(x) == 1]
        for node in selected_nodes:
            elements = ((node,),)
            facts += [(formula, elements) for formula in self.concept_session.get_node_facts(node)]
        selected_nodes = frozenset(selected_nodes)
        edges = set(
            tag[-3:]
            for tag, value in self.concept_session.abstract_value
            if tag[0] == 'edge_info' and
            tag[-2] in selected_nodes and
            tag[-1] in selected_nodes

        )
        for edge, source, target in sorted(edges):
            elements = ((source,), (target,))
            if (self.edge_display_checkboxes[edge]['all_to_all'].value or
                self.edge_display_checkboxes[edge]['transitive'].value):
                facts += [(formula, elements) for formula in
                          self.concept_session.get_edge_facts(edge, source, target, True)]
            if self.edge_display_checkboxes[edge]['none_to_none'].value or edge == '=':
                # get dis-equalities, don't get other negative
                # transitive facts unless checkboxed
                facts += [(formula, elements) for formula in
                          self.concept_session.get_edge_facts(edge, source, target, False)]
        # filter double equalities and self-edges of reflexive relations
        facts = [(f, elements) for f, elements in facts if not (
            #(type(f) is Eq and f.t1 >= f.t2) or
            (type(f) is Not and type(f.body) is Eq and f.body.t1 >= f.body.t2) #or
            # (
            #     type(f) is Apply and
            #     self.edge_display_checkboxes[f.func.name]['transitive'].value and
            #     f.terms[0] == f.terms[1]
            # )
        )]
        self.facts_list.options = [
            (self.fact_to_label(formula), (formula, elements))
            for formula, elements in facts
        ]
        self.facts_list.value = ()

    def apply_structure_renaming(self, st):
        for k in sorted(self.structure_renaming.keys(), key=len, reverse=True):
            st = st.replace(k, self.structure_renaming[k])
        return st

    def fact_to_label(self, fact):
        return self.apply_structure_renaming(str(fact))

    def get_active_facts(self):
        """
        Return a list of the selected facts, for all facts if none are selected
        """
        if self.facts_list.options is None:
            return []
        value = self.facts_list.value
        if len(value) == 0:
            value = [x[1] for x in self.facts_list.options]
        result = [formula for formula, elements in value]
        return result

    def new_ag(self):
        from ivy_art import AnalysisGraph
        ag = AnalysisGraph(ta._ivy_ag.domain, ta._ivy_ag.pvars)
        ag.actions = ta._ivy_ag.actions
        ag.init_cond = ta._ivy_ag.init_cond
        return ag

    def check_inductiveness(self, button=None):
        import ivy_transrel
        from ivy_solver import get_small_model
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses
        from random import randrange

        ag = self.new_ag()

        pre = State(self.session.analysis_state.ivy_interp)
        pre.clauses = and_clauses(*self.conjectures)

        action = ta.get_action('step')
        post = ag.execute(action, pre, None, 'step')
        post.clauses = true_clauses()

        to_test = list(self.conjectures)
        while len(to_test) > 0:
            # choose randomly, so the user can get another result by
            # clicking again
            conj = to_test.pop(randrange(len(to_test)))
            assert conj.is_universal_first_order()
            used_names = frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values())
            def witness(v):
                c = lg.Const('@' + v.name, v.sort)
                assert c.name not in used_names
                return c
            clauses = dual_clauses(conj, witness)
            history = ag.get_history(post)

            # TODO: this is still a bit hacky, and without nice error reporting
            if self.relations_to_minimize.value == 'relations to minimize':
                self.relations_to_minimize.value = ' '.join(sorted(
                    k for k, v in self.session.analysis_state.ivy_interp.sig.symbols.iteritems()
                    if (type(v.sort) is lg.FunctionSort and
                        v.sort.range == lg.Boolean and
                        v.name not in self.transitive_relations and
                        '.' not in v.name
                    )
                ))

            res = ag.bmc(post, clauses, None, None, lambda clauses: get_small_model(
                clauses,
                sorted(self.session.analysis_state.ivy_interp.sig.sorts.values()),
                [
                    # TODO: this is still a bit hacky, and without nice error reporting
                    history.maps[0].get(relation, relation)
                    for x in self.relations_to_minimize.value.split()
                    for relation in [self.session.analysis_state.ivy_interp.sig.symbols[x]]
                ],
            ))
            if res is not None:
                self.current_conjecture = conj
                assert len(res.states) == 2
                self.ignore_display_checkbox_change = True
                try:
                    for c in lu.used_constants(clauses.to_formula()):
                        if lg.first_order_sort(c.sort) and c.name[0] != '@':
                            name = '={}'.format(c.name)
                            self.node_label_display_checkboxes[name]['node_necessarily'].value = True
                        elif type(c.sort) is lg.FunctionSort and c.sort.arity == 1:
                            self.node_label_display_checkboxes[c.name]['node_necessarily'].value = True
                        elif type(c.sort) is lg.FunctionSort and c.sort.arity == 2:
                            self.edge_display_checkboxes[c.name]['all_to_all'].value = True
                        else:
                            pass
                finally:
                    self.ignore_display_checkbox_change = False
                self.set_states(res.states[0], res.states[1])
                #self.post_graph.selected = self.get_relevant_elements(self.post_state[2], clauses) # TODO - this should be put back or get_relevant_elements should be removed
                self.show_result('The following conjecture is not inductive:\n{}'.format(
                    str(conj.to_formula()),
                ))
                return False

        self.set_states(False, False)
        self.show_result(
            'Inductive invariant found:\n' +
            '\n'.join(
                str(conj) for conj in self.conjectures
            )
        )
        return True

    def get_selected_conjecture(self, facts=None):
        """
        Return a positive universal conjecture based on the selected facts.

        The result is a Clauses object
        """
        from logic_util import used_constants, free_variables, substitute
        from ivy_logic_utils import negate, Clauses, simplify_clauses

        if facts is None:
            facts = self.get_active_facts()
        assert len(free_variables(*facts)) == 0, "conjecture would contain existential quantifiers..."
        sig_symbols = frozenset(self.session.analysis_state.ivy_interp.sig.symbols.values())
        facts_consts = [
            c for c in used_constants(*facts)
            if lg.first_order_sort(c.sort)
        ]
        subs = {}
        count = defaultdict(int)
        for c in sorted(facts_consts, key=lambda c: self.structure_renaming.get(c.name, c.name)):
            if c not in sig_symbols or c.is_skolem():
                if c.name in self.structure_renaming:
                    # keep the correlation between universe constants and variables
                    # TODO: this is just for the demo, and should be made more general
                    name = self.structure_renaming[c.name]
                    name = name[0].upper() + name[-1]
                    subs[c] = lg.Var(name, c.sort)
                else:
                    prefix = str(c.sort)[:2].upper()
                    subs[c] = lg.Var(prefix + str(count[prefix]), c.sort)
                    count[prefix] += 1

        literals = [negate(substitute(f, subs)) for f in facts]
        result = Clauses([lg.Or(*literals)])
        result = simplify_clauses(result)

        # now rename again to get a pretty clause, since some
        # variables have been eliminated by simplify_clauses
        # assert len(result.fmlas) == 1
        # clause = result.fmlas[0]
        # subs = {}
        # count = defaultdict(int)
        # for c in free_variables(clause):
        #     prefix = str(c.sort)[0].upper()
        #     count[prefix] += 1
        #     subs[c] = lg.Var(prefix + str(count[prefix]), c.sort)
        # result = Clauses([substitute(clause, subs)])

        # change to negation of conjunction rather than disjunction
        assert len(result.fmlas) == 1
        if type(result.fmlas[0]) is lg.Or:
            result = Clauses([lg.Not(lg.And(*(negate(lit) for lit in result.fmlas[0])))])

        return result

    def bmc_conjecture(self, button=None, conjecture=None, verbose=False, add_to_crg=True):
        import ivy_transrel
        import ivy_solver
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses


        # TODO: get from somewhere else
        init_action = ta.get_action('initialize')
        step_action = ta.get_action('step')
        n_steps = self.bmc_bound.value

        if conjecture is None:
            conj = self.get_selected_conjecture()
        else:
            conj = conjecture

        assert conj.is_universal_first_order()
        used_names = frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values())
        def witness(v):
            c = lg.Const('@' + v.name, v.sort)
            assert c.name not in used_names
            return c
        clauses = dual_clauses(conj, witness)

        ag = self.new_ag()
        with ag.context as ac:
            ac.new_state(ag.init_cond)
        post = ag.execute(init_action, None, None, 'initialize')
        for n in range(n_steps + 1):
            res = ag.bmc(post, clauses, ta._analysis_state.crg if add_to_crg else None)
            if verbose:
                if res is None:
                    msg = 'BMC with bound {} did not find a counter-example to:\n{}'.format(
                        n,
                        str(conj.to_formula()),
                    )
                else:
                    msg = 'BMC with bound {} found a counter-example to:\n{}'.format(
                        n,
                        str(conj.to_formula()),
                    )
                print '\n' + msg + '\n'
            if res is not None:
                ta.step()
                self.show_result('BMC with bound {} found a counter-example to:\n{}'.format(
                    n,
                    str(conj.to_formula()),
                ))
                return True
            post = ag.execute(step_action, None, None, 'step')

        self.show_result('BMC with bound {} did not find a counter-example to:\n{}'.format(
            n_steps,
            str(conj.to_formula()),
        ))
        return False


    def minimize_conjecture(self, button=None):
        import ivy_transrel
        import ivy_solver
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate
        from ivy_solver import unsat_core
        from logic_util import free_variables, substitute

        if self.bmc_conjecture():
            # found a BMC counter-example
            return

        # TODO: get from somewhere else
        init_action = ta.get_action('initialize')
        step_action = ta.get_action('step')
        n_steps = self.bmc_bound.value

        ag = self.new_ag()
        with ag.context as ac:
            ac.new_state(ag.init_cond)
        post = ag.execute(init_action, None, None, 'initialize')
        for n in range(n_steps):
            post = ag.execute(step_action, None, None, 'step')
        axioms = self.session.analysis_state.ivy_interp.background_theory()
        post_clauses = and_clauses(post.clauses, axioms)

        used_names = (
            frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values()) |
            frozenset(x.name for x in used_symbols_clauses(post_clauses))
        )
        facts = self.get_active_facts()
        assert not any(
            c.is_skolem() and c.name in used_names for c in used_constants(*facts)
        )
        core = unsat_core(Clauses(facts), post_clauses)
        assert core is not None, "bmc_conjecture returned False but unsat core is None"
        core_formulas = frozenset(core.fmlas)
        self.facts_list.value = [(fact, elements) for (label, (fact, elements)) in self.facts_list.options if fact in core_formulas]
        self.highligh_selected_facts()
        self.show_result("BMC found the following possible conjecture:\n{}".format(
            self.get_selected_conjecture()
        ))

    def highligh_selected_facts(self):
        """
        Add custom node labels and edges to reflect the selected
        conjecture in pre_graph
        """
        # first collect all atoms that appear in the facts
        atoms = []
        def collect_atoms(x):
            if type(x) in (lg.Apply, lg.Eq):
                atoms.append(x)
            else:
                for y in x:
                    collect_atoms(y)
        collect_atoms([fact for fact, elements in self.facts_list.value])

        # now collect relevant edges and node labels elements
        self.concept_session.widget = None
        self.concept_session.domain.concepts['edges'] = []
        self.concept_session.domain.concepts['node_labels'] = []
        nodes = frozenset(self.concept_session.domain.concepts['nodes'])
        for atom in atoms:
            if type(atom) is lg.Eq:
                assert type(atom.t2) is lg.Const
                if type(atom.t1) is lg.Const:
                    n1 = atom.t1.name
                    n2 = atom.t2.name
                    if n1 in nodes and n2 in nodes:
                        self.concept_session.add_custom_edge('=', n1, n2)
                    elif n1 in nodes:
                        label_name = '={}'.format(n2)
                        assert label_name in self.concept_session.domain.concepts, atom
                        self.concept_session.add_custom_node_label(n1, label_name)
                    else:
                        # TODO
                        # assert False, atom
                        pass
                else:
                    assert type(atom.t1) is lg.Apply
                    if atom.t1.func.sort.arity == 1:
                        assert type(atom.t1.terms[0]) is lg.Const
                        self.concept_session.add_custom_edge(
                            atom.t1.func.name,
                            atom.t1.terms[0].name,
                            atom.t2.name,
                        )
                    else:
                        # TODO: support higher arity
                        pass
            elif type(atom) is lg.Apply:
                if atom.func.sort.arity == 1:
                    self.concept_session.add_custom_node_label(atom.terms[0].name, atom.func.name)
                elif atom.func.sort.arity == 2:
                    self.concept_session.add_custom_edge(*(c.name for c in atom))
                else:
                    # TODO: support higher arity
                    pass
            else:
                assert False, lit
        self.concept_session.widget = self
        self.render_graph(pre_only=True)
        self.pre_graph.selected = tuple(set(chain(*(elements for fact, elements in self.facts_list.value))))


    def autodetect_transitive(self):
        import tactics_api as ta
        import logic as lg
        from ivy_logic_utils import Clauses
        from ivy_solver import clauses_imply

        self.edge_display_checkboxes['=']['transitive'].value = True
        self.edge_display_checkboxes['=']['all_to_all'].value = True

        axioms = self.session.analysis_state.ivy_interp.background_theory()
        for c in self.session.analysis_state.ivy_interp.sig.symbols.values():
            if (type(c.sort) is lg.FunctionSort and
                c.sort.arity == 2 and
                c.sort.domain[0] == c.sort.domain[1] and
                c.sort.range == lg.Boolean):
                X = lg.Var('X', c.sort.domain[0])
                Y = lg.Var('Y', c.sort.domain[0])
                Z = lg.Var('Z', c.sort.domain[0])
                transitive = lg.ForAll([X, Y, Z], lg.Or(lg.Not(c(X,Y)), lg.Not(c(Y,Z)), c(X,Z)))
                defined_symmetry = lg.ForAll([X, Y], lg.Or(c(X,X), lg.Not(c(Y,Y))))
                t = Clauses([transitive, defined_symmetry])
                if clauses_imply(axioms, t):
                    self.edge_display_checkboxes[c.name]['transitive'].value = True

    def is_sufficient(self, button=None):
        """
        Check if the active conjecture is sufficient to imply the current
        CTI conjecture at the next step

        TODO: this has a lot in common with check_inductiveness,
        should refactor common parts out
        """
        import ivy_transrel
        import ivy_solver
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses
        from random import randrange

        conj = self.get_selected_conjecture()
        target_conj = self.current_conjecture

        ag = self.new_ag()

        pre = State(self.session.analysis_state.ivy_interp)
        pre.clauses = and_clauses(conj, *self.conjectures)

        action = ta.get_action('step')
        post = ag.execute(action, pre, None, 'step')
        post.clauses = true_clauses()

        assert target_conj.is_universal_first_order()
        used_names = frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values())
        def witness(v):
            c = lg.Const('@' + v.name, v.sort)
            assert c.name not in used_names
            return c
        clauses = dual_clauses(target_conj, witness)
        res = ag.bmc(post, clauses)

        if res is not None:
            self.show_result('{}\nis not sufficient to imply the following at the next step:\n{}'.format(
                conj.to_formula(),
                target_conj.to_formula(),
            ))
            return False
        else:
            self.show_result('{}\nis sufficient to imply the following at the next step:\n{}'.format(
                conj.to_formula(),
                target_conj.to_formula(),
            ))
            return True

    def is_inductive(self, button=None):
        """
        Check if the active conjecture implies itself at the next step

        TODO: this has a lot in common with check_inductiveness and is_sufficient,
        should refactor common parts out
        """
        import ivy_transrel
        import ivy_solver
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses
        from random import randrange

        conj = self.get_selected_conjecture()
        target_conj = conj

        ag = self.new_ag()

        pre = State(self.session.analysis_state.ivy_interp)
        pre.clauses = and_clauses(conj, *self.conjectures)

        action = ta.get_action('step')
        post = ag.execute(action, pre, None, 'step')
        post.clauses = true_clauses()

        assert target_conj.is_universal_first_order()
        used_names = frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values())
        def witness(v):
            c = lg.Const('@' + v.name, v.sort)
            assert c.name not in used_names
            return c
        clauses = dual_clauses(target_conj, witness)
        res = ag.bmc(post, clauses)

        if res is not None:
            self.show_result('{}\nis not relative inductive.'.format(
                conj.to_formula(),
             ))
            return False
        else:
            self.show_result('{}\nis relative inductive.'.format(
                conj.to_formula(),
            ))
            return True

    def strengthen(self, button=None):
        conj = self.get_selected_conjecture()
        self.conjectures.append(conj)
        self.show_result('Added the following conjecture:\n{}'.format(
            conj.to_formula(),
        ))

    @interaction
    def weaken(self, button=None):
        user_selection = yield UserSelectMultiple(
            options=[
                (str(conj), conj)
                for conj in self.conjectures
            ],
            title='Conjectures',
            prompt='Select conjectures to remove',
            default=()
        )
        if user_selection is not None:
            for conj in user_selection:
                self.conjectures.remove(conj)
            self.show_result('Removed the following conjectures:\n{}'.format(
                '\n'.join(str(conj) for conj in user_selection)
            ))

    def get_relevant_elements(self, a, clauses):
        """
        a is a concept abstract value dictionary. returns a list of nodes
        and edges that appear in clauses
        """
        if a is None:
            return []

        # first collect all literals that appear in clauses
        literals = []
        def collect_literals(x):
            if type(x) in (lg.Apply, lg.Eq):
                literals.append(x)
            else:
                for y in x:
                    collect_literals(y)
        collect_literals(clauses.to_formula())

        const_to_node = dict()
        for tag, value in a.iteritems():
            if tag[:2] == ('node_label', 'node_necessarily') and tag[3][0] == '=' and value:
                const_to_node[tag[3][1:]] = tag[2]

        elements = []
        # now collect relevant graph elements
        for lit in literals:
            if type(lit) is lg.Eq:
                elements += [(const_to_node[t.name],) for t in lit]
            elif type(lit) is lg.Apply:
                elements += [(const_to_node[t.name],) for t in lit.terms]
                if lit.func.sort.arity == 2:
                    elements.append((
                        lit.func.name,
                        const_to_node[lit.terms[0].name],
                        const_to_node[lit.terms[1].name],
                    ))
            else:
                assert False, lit

        return tuple(set(elements))

    def get_all_pre_facts(self):
        # TODO: this is a bit hacky
        return [
            f for f in self.pre_state[1]
            if type(f) is not lg.ForAll
            and '@' not in str(f)
        ]

    def find_relative_inductive_conjecture(self, button=None):
        """
        Looks for a subset of the selected conjecture that is relative inductive.
        Uses BMC with the current bound as a filter for initiation (use 0 to just test the initial states).

        TODO: this has a lot in common with is_inductive, check_inductiveness and is_sufficient, and minimize_conjecture
        should refactor common parts out
        """
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate
        import ivy_transrel
        from ivy_solver import atom_to_z3, formula_to_z3, clauses_to_z3
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses

        if False:
            facts = self.get_active_facts()
        else:
            facts = self.get_all_pre_facts()

        n = len(facts)
        print "len(self.get_active_facts()) = {}".format(n)

        # TODO: get from somewhere else
        init_action = ta.get_action('initialize')
        step_action = ta.get_action('step')
        n_steps = self.bmc_bound.value

        # helper functions
        def select(lst, indices):
            return [lst[i] for i in indices]

        axioms = self.session.analysis_state.ivy_interp.background_theory()

        # create a solver that is used to test if a conjecture is
        # true in all reachable states upto BMC bound
        ag = self.new_ag()
        with ag.context as ac:
            ac.new_state(ag.init_cond)
        post = ag.execute(init_action, None, None, 'initialize')
        for i in range(n_steps):
            post = ag.execute(step_action, None, None, 'step')
            # TODO: check if the axioms get added to every step
        post_clauses = and_clauses(post.clauses, axioms)
        used_names = (
            frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values()) |
            frozenset(x.name for x in used_symbols_clauses(post_clauses))
        )
        assert not any(
            c.is_skolem() and c.name in used_names
            for c in used_constants(*facts)
        )
        s_reach = z3.Solver()
        alits = [z3.Const("__c{}".format(i), z3.BoolSort())
                 for i in range(n)]
        cc = [z3.Or(z3.Not(a),formula_to_z3(f))
              for a,f in zip(alits,facts)]
        for c in cc:
            s_reach.add(c)
        s_reach.add(clauses_to_z3(post_clauses))

        # create a solver that is used to check if a conjecture in the
        # pre state implies another conjecture in the post state
        def get_bool(name):
            # use a nullary relation
            return lg.Apply(lg.Const(
                name, lg.FunctionSort(lg.BooleanSort(),)),)
        guards = [get_bool('__guard_{}'.format(i))
                  for i in range(n)]
        z3_guards = [atom_to_z3(g) for g in guards]
        guarded_facts = [
            lg.Or(g, f)
            for f, g in zip(facts, guards)
        ]
        guarded_conj = self.get_selected_conjecture(guarded_facts)
        ag = self.new_ag()
        pre = State(self.session.analysis_state.ivy_interp)
        pre.clauses = and_clauses(axioms, guarded_conj, *self.conjectures)
        post = ag.execute(step_action, pre, None, 'step')
        post_clauses = and_clauses(post.clauses, axioms)
        used_names = (
            frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values()) |
            frozenset(x.name for x in used_symbols_clauses(post_clauses))
        )
        assert not any(
            c.is_skolem() and c.name in used_names
            for c in used_constants(*facts)
        )
        s_step = z3.Solver()
        for c in cc:
            s_step.add(c)
        s_step.add(clauses_to_z3(post_clauses))

        lm = LatticeMap(n)
        if n > 100:
            print timestamp(), "Too many facts", n
            return False

        print timestamp(), "Starting while True loop", datetime.datetime.now()
        for i in range(n):
            print "    ", i, facts[i]
        relative_inductive = []
        pre_at_most = 1
        while pre_at_most <= 5: # TODO change this hardcoded 5
            print timestamp(), '=== while True ===', "pre_at_most = ", pre_at_most
            sample = lm.sample(pre_at_most)
            if sample is None:
                pre_at_most += 1
                continue

            pre_facts = [i for i in range(n)
                         if ('pre', i) in sample]
            post_facts = [i for i in range(n)
                          if ('post', i) in sample]

            # check if the negation of the pre facts is reachable
            if s_reach.check(select(alits, pre_facts)) == z3.sat:
                print "pre is too strong, it's violated in reachable states"
                # grow by adding to pre until it is maximal satisfiable
                current = pre_facts
                for i in range(n):
                    if (i not in current and
                        s_reach.check(select(alits, current + [i]))
                        == z3.sat):
                        current.append(i)
                        # print "adding {} to pre".format(i)
                    else:
                        # print "not adding {}".format(i)
                        pass
                # block any pre conjecture that is stronger than current, since current is already violated in the known reachable states
                lm.block_pre_down(current)
                continue

            # check if the pre implies the post
            pre_guards = select(z3_guards, [i for i in range(n)
                                            if i not in pre_facts])
            if (s_step.check(pre_guards + select(alits,post_facts))
                == z3.sat):
                # grow by adding to post
                current_post = post_facts
                for i in range(n):
                    if (i not in current_post and
                        s_step.check(pre_guards +
                                     select(alits, current_post + [i]))
                        == z3.sat):
                        current_post.append(i)
                # now grow more by removing from pre
                current_pre = pre_facts
                current_pre_guards = pre_guards
                for i in range(n):
                    if (i in current_pre and
                        s_step.check(current_pre_guards +
                                     [z3_guards[i]] +
                                     select(alits, current_post))
                        == z3.sat):
                        # TODO: maybe we should also check s_reach
                        # here as we are removing from pre. For now
                        # I'm not checking it, as it could also be
                        # useful - it's just something to try
                        current_pre.remove(i)
                        current_pre_guards.append(z3_guards[i])
                # now we know that pre does not imply post at the next step. block this
                lm.block_non_implication(current_pre, current_post)
                continue

            # now we know that pre implies post
            # shrink by adding to pre or removing from post
            # TODO: use unsat cores here...
            current_post = post_facts
            current_pre = pre_facts
            # first, try to remove from post
            for i in range(n):
                if (i in current_post and
                    s_step.check(select(z3_guards, [
                        j for j in range(n)
                        if j not in current_pre
                    ]) + select(alits, set(current_post) - set([i])))
                    == z3.unsat):
                    current_post.remove(i)
            # now, try to add to pre
            for i in range(n):
                if (i not in current_pre and
                    s_step.check(select(z3_guards, [
                        j for j in range(n)
                        if j not in current_pre + [i]
                    ]) + select(alits, current_post))
                == z3.unsat):
                    current_pre.append(i)
            if set(current_pre) >= set(current_post):
                # found relative inductive conjecture
                relative_inductive.append(select(facts, current_post))
                # maybe should stop here alreadby, but at least say we don't want posts that are subsumed by this
                lm.block_implication(range(n), current_post)
                break


                # rel_ind_facts = select(facts, current_post)

                # self.facts_list.value = [
                #     (fact, elements)
                #     for (label, (fact, elements)) in self.facts_list.options
                #     if fact in rel_ind_facts
                # ]
                # self.highligh_selected_facts()
                # self.show_result("Found the following relative inductive conjecture:\n{}".format(
                #     self.get_selected_conjecture()
                # ))
                # return True
            else:
                lm.block_implication(current_pre, current_post)

        global debug
        debug = (relative_inductive, lm)

        # got out of the loop with no conjecture
        if len(relative_inductive) == 0:
            msg = 'No relative inductive generalization found.'
            print timestamp(), '\n' + msg + '\n'
            self.show_result(msg)
            return False
        else:
            relative_inductive.sort(key=len)
            msg = "Found the following relative inductive conjectures:\n{}".format(
                '\n'.join(str(self.get_selected_conjecture(fs)) for fs in relative_inductive)
            )
            self.show_result(msg)
            print timestamp(), '\n' + msg + '\n'
            # override the fact list - replace gather facts to get high arity facts
            self.facts_list.options = [
                (self.fact_to_label(formula), (formula, ()))
                for formula in relative_inductive[0]
            ]
            self.facts_list.value = [
                (fact, elements)
                for (label, (fact, elements)) in self.facts_list.options
                if fact in relative_inductive[0]
            ]
            self.highligh_selected_facts()
            return True



    # the new algorithm to find inductive invariants
    # this should later move to a new class

    def le(self, a1, a2):
        """
        Abstract domain order, by subsumption

        Elements in the domain are sets of sets of literals (CNF)
        """
        return all(any(frozenset(c1) <= frozenset(c2) for c1 in a1) for c2 in a2)

    def get_mus(self, s, args, fixed=()):
        """
        s should be a Z3 solver that just returned unsat to:
        s.check(*set(fixed + args)).

        Returns a minimal subset a of args such that
        s.check(*(fixed + a)) is also unsat.
        """
        global oded
        oded = s, args, fixed
        args = list(args)
        fixed = list(fixed)
        args_ids = set(x.get_id() for x in args)
        a = [x for x in s.unsat_core() if x.get_id() in args_ids]
        mus = []
        while len(a) > 0:
            res = s.check(*(fixed + mus + a[1:]))
            if res == z3.sat:
                mus.append(a[0])
                args_ids.remove(a[0].get_id())
                a = a[1:]
            elif res == z3.unsat:
                a = [x for x in s.unsat_core() if x.get_id() in args_ids]
            else:
                assert False, res
        return mus

    def alpha_init(self):
        """
        Return the abstraction of the initial state

        The initial state is taken as all steps reachable from the
        true initial state by n steps, where n is given by the BMC
        bound (so 0 gives the real initial state)
        """
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate, clause_to_formula
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate, clause_to_formula
        import ivy_transrel
        from ivy_solver import atom_to_z3, formula_to_z3, clauses_to_z3
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses


        # convert self.A to a list of lists of literals
        A = [[lit for lit in c] for c in self.A]

        init_action = ta.get_action('initialize')
        step_action = ta.get_action('step')
        n_steps = self.bmc_bound.value # n_steps = 0 gives the real initial state
        axioms = self.session.analysis_state.ivy_interp.background_theory()

        # create a solver that is used to test if a conjecture is true
        # at "Init", which is actually definded as all reachable
        # states upto BMC bound
        ag = self.new_ag()
        with ag.context as ac:
            ac.new_state(ag.init_cond)
        post = ag.execute(init_action, None, None, 'initialize')
        for i in range(n_steps):
            post = ag.execute(step_action, None, None, 'step')
            # TODO: check if the axioms get added to every step
        post_clauses = and_clauses(post.clauses, axioms)
        used_names = (
            frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values()) |
            frozenset(x.name for x in used_symbols_clauses(post_clauses))
        )
        assert not any(
            const.is_skolem() and const.name in used_names
            for const in used_constants(*(lit for c in A for lit in c))
        ), used_names
        s_init = z3.Solver()
        s_init.add(clauses_to_z3(post_clauses))
        lit_guards = [
            [
                z3.Const("__lit_guard_{}_{}".format(i, j), z3.BoolSort())
                for j, lit in enumerate(c)
            ]
            for i, c in enumerate(A)
        ]
        clause_guards = [
            z3.Const("__clause_guard_{}".format(i), z3.BoolSort())
            for i, c in enumerate(A)
        ]
        s_init.add(z3.Or(*(
            z3.And(z3.Not(clause_guards[i]), *(
                z3.Implies(lit_guards[i][j], formula_to_z3(negate(lit)))
                for j, lit in enumerate(c)
            ))
            for i, c in enumerate(A)
        )))
        # print '\n\n', s_init, '\n\n'

        def SAT(args):
            res = s_init.check(*args)
            assert res in (z3.sat, z3.unsat)
            return res == z3.sat

        result = dict()

        for ci, c in enumerate(A):
            result[frozenset(c)] = top
            n = len(c)
            lm = LatticeMap(n)
            print '\n', timestamp(), "Checking alpha_init of clause:", datetime.datetime.now()
            for i in range(n):
                print '    ', i, c[i]
            cg = select(clause_guards, range(0, ci) + range(ci + 1, len(A)))

            while True:
                print timestamp(), '==='
                sample = lm.sample(0,6)
                if sample is None:
                    break


                post_facts = [i for i in range(n)
                              if ('post', i) in sample]

                # check if the negation of the pre facts is reachable
                if SAT(cg + select(lit_guards[ci], post_facts)):
                    print "SAT, growing..."
                    # grow by adding to pre until it is maximal satisfiable
                    current = post_facts
                    for i in range(n):
                        if (i not in current and
                            SAT(cg + select(lit_guards[ci], current + [i]))):
                            current.append(i)
                        else:
                            pass
                    assert SAT(cg + select(lit_guards[ci], current)) # TODO remove this
                    # block any conjecture that is stronger than
                    # current, since current is already not implied by Init
                    lm.block_non_implication([], current)
                    continue
                else:
                    print "UNSAT, shrinking..."
                    mus = self.get_mus(s_init, select(lit_guards[ci], post_facts), cg)
                    mus_ids = frozenset(x.get_id() for x in mus)
                    current = [i for i in range(n)
                               if lit_guards[ci][i].get_id() in mus_ids]
                    assert not SAT(cg + select(lit_guards[ci], current)) # TODO remove this
                    # block any conjecture weaker than current, as
                    # current is already implied by Init
                    lm.block_implication([], current)
                    result[frozenset(c)] |= frozenset([frozenset(c[i] for i in current)])

        return result

    def alpha_step(self, abs_pre):
        """
        Return the abstraction of the post state of abs_pre, joined with abs_pre.
        """
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate, clause_to_formula
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate, clause_to_formula
        import ivy_transrel
        from ivy_solver import atom_to_z3, formula_to_z3, clauses_to_z3
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses


        # convert self.A to a list of lists of literals
        A = [[lit for lit in c] for c in self.A]

        init_action = ta.get_action('initialize')
        step_action = ta.get_action('step')
        axioms = self.session.analysis_state.ivy_interp.background_theory()

        ################################
        # TODO: use this later, for now I'm doing something simpler
        # # create a solver that is used to check if a CNF in the pre
        # # state implies a clause in the post state
        # def get_bool(name):
        #     # use a nullary relation
        #     return lg.Apply(lg.Const(
        #         name, lg.FunctionSort(lg.BooleanSort(),)),)
        # lg_pre_lit_guards = [
        #     [
        #         get_bool("__pre_lit_guard_{}_{}".format(i, j))
        #         for j, lit in enumerate(c)
        #     ]
        #     for i, c in enumerate(A)
        # ]
        # lg_pre_clause_guards = [
        #     get_bool("__pre_clause_guard_{}".format(i))
        #     for i, c in enumerate(A)
        # ]
        # pre_lit_guards = [[atom_to_z3(g) for x in y] for y in lg_pre_lit_guards]
        # pre_clause_guards = [atom_to_z3(g) for g in lg_pre_clause_guards]
        # ag = self.new_ag()
        # pre = State(self.session.analysis_state.ivy_interp)
        # pre.clauses = and_clauses(axioms, *(
        #     self.get_selected_conjecture([lg_pre_clause_guards[i]] + [
        #         lg.Or(lg_pre_lit_guards[i][j], negate(lit))
        #         for j, lit in enumerate(c)
        #     ])
        #     for i, c in enumerate(A)
        # ))
        ################################

        ag = self.new_ag()
        pre = State(self.session.analysis_state.ivy_interp)
        pre.clauses = and_clauses(axioms, *(
            self.get_selected_conjecture([negate(lit) for lit in b])
            for bs in abs_pre.values()
            for b in bs
        ))
        post = ag.execute(step_action, pre, None, 'step')
        post_clauses = and_clauses(post.clauses, axioms)
        used_names = (
            frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values()) |
            frozenset(x.name for x in used_symbols_clauses(post_clauses))
        )
        assert not any(
            const.is_skolem() and const.name in used_names
            for const in used_constants(*(lit for c in A for lit in c))
        )
        s_step = z3.Solver()
        s_step.add(clauses_to_z3(post_clauses))
        post_lit_guards = [
            [
                z3.Const("__post_lit_guard_{}_{}".format(i, j), z3.BoolSort())
                for j, lit in enumerate(c)
            ]
            for i, c in enumerate(A)
        ]
        post_clause_guards = [
            z3.Const("__post_clause_guard_{}".format(i), z3.BoolSort())
            for i, c in enumerate(A)
        ]
        s_step.add(z3.Or(*(
            z3.And(z3.Not(post_clause_guards[i]), *(
                z3.Implies(post_lit_guards[i][j], formula_to_z3(negate(lit)))
                for j, lit in enumerate(c)
            ))
            for i, c in enumerate(A)
        )))
        def SAT(args):
            global oded
            oded = s_step, args
            res = s_step.check(*args)
            assert res in (z3.sat, z3.unsat), res
            return res == z3.sat

        result = dict()

        for ci, c in enumerate(A):
            # TODO: change this to also change the pre, and not just the post...

            result[frozenset(c)] = top
            n = len(c)
            lm = LatticeMap(n)
            print '\n', timestamp(), "Checking alpha_step of clause:", datetime.datetime.now()
            for i in range(n):
                print '    ', i, c[i]
            cg = select(post_clause_guards, range(0, ci) + range(ci + 1, len(A)))

            while True:
                print timestamp(), '=== new sample ==='
                sample = lm.sample(0,6)
                if sample is None:
                    break

                post_facts = [i for i in range(n)
                              if ('post', i) in sample]

                # check if the current conjecture is implied
                if SAT(cg + select(post_lit_guards[ci], post_facts)):
                    print "SAT, growing..."
                    # grow by adding to pre until it is maximal satisfiable
                    current = post_facts
                    for i in range(n):
                        if (i not in current and
                            SAT(cg + select(post_lit_guards[ci], current + [i]))):
                            current.append(i)
                        else:
                            pass
                    assert SAT(cg + select(post_lit_guards[ci], current)) # TODO remove this
                    # block any conjecture that is stronger than
                    # current, since current is already not implied by Step
                    lm.block_non_implication([], current)
                    continue
                else:
                    print "UNSAT, shrinking..."
                    mus = self.get_mus(s_step, select(post_lit_guards[ci], post_facts), cg)
                    mus_ids = frozenset(x.get_id() for x in mus)
                    current = [i for i in range(n)
                               if post_lit_guards[ci][i].get_id() in mus_ids]
                    assert not SAT(cg + select(post_lit_guards[ci], current)) # TODO remove this
                    # block any conjecture weaker than current, as
                    # current is already implied by Step
                    lm.block_implication([], current)
                    result[frozenset(c)] |= frozenset([frozenset(c[i] for i in current)])

        return result

    def step_model(self, abs_pre, v):
        """
        Return s1, s2 such that s1 satisfies abs_pre, s1 makes a step to
        s2, and s2 violates the clause v.

        TODO: This has so much in common with check_inductiveness that it's a crime not to refactor...
        """
        import ivy_transrel
        from ivy_solver import get_small_model
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate, clause_to_formula
        from random import randrange
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate, clause_to_formula
        import ivy_transrel
        from ivy_solver import atom_to_z3, formula_to_z3, clauses_to_z3
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses


        # convert self.A to a list of lists of literals
        A = [[lit for lit in c] for c in self.A]

        init_action = ta.get_action('initialize')
        step_action = ta.get_action('step')
        axioms = self.session.analysis_state.ivy_interp.background_theory()

        ag = self.new_ag()
        pre = State(self.session.analysis_state.ivy_interp)
        pre.clauses = and_clauses(axioms, *(
            self.get_selected_conjecture([negate(lit) for lit in b])
            for bs in abs_pre.values()
            for b in bs
        ))
        post = ag.execute(step_action, pre, None, 'step')
        post.clauses = true_clauses()
        conj = self.get_selected_conjecture([negate(lit) for lit in v])
        assert conj.is_universal_first_order()
        used_names = frozenset(x.name for x in self.session.analysis_state.ivy_interp.sig.symbols.values())
        def witness(v):
            c = lg.Const('@' + v.name, v.sort)
            assert c.name not in used_names
            return c
        clauses = dual_clauses(conj, witness)
        history = ag.get_history(post)

        # TODO: this is still a bit hacky, and without nice error reporting
        if self.relations_to_minimize.value == 'relations to minimize':
            self.relations_to_minimize.value = ' '.join(sorted(
                k for k, v in self.session.analysis_state.ivy_interp.sig.symbols.iteritems()
                if (type(v.sort) is lg.FunctionSort and
                    v.sort.range == lg.Boolean and
                    v.name not in self.transitive_relations and
                    '.' not in v.name
                )
            ))

        res = ag.bmc(post, clauses, None, None, lambda clauses: get_small_model(
            clauses,
            sorted(self.session.analysis_state.ivy_interp.sig.sorts.values()),
            [
                # TODO: this is still a bit hacky, and without nice error reporting
                history.maps[0].get(relation, relation)
                for x in self.relations_to_minimize.value.split()
                for relation in [self.session.analysis_state.ivy_interp.sig.symbols[x]]
            ],
        ))
        if res is None:
            return None
        else:
            # display the transition and return the pre facts
            assert len(res.states) == 2
            self.ignore_display_checkbox_change = True
            try:
                for c in lu.used_constants(clauses.to_formula()):
                    if lg.first_order_sort(c.sort) and c.name[0] != '@':
                        name = '={}'.format(c.name)
                        self.node_label_display_checkboxes[name]['node_necessarily'].value = True
                    elif type(c.sort) is lg.FunctionSort and c.sort.arity == 1:
                        self.node_label_display_checkboxes[c.name]['node_necessarily'].value = True
                    elif type(c.sort) is lg.FunctionSort and c.sort.arity == 2:
                        self.edge_display_checkboxes[c.name]['all_to_all'].value = True
                    else:
                        pass
            finally:
                self.ignore_display_checkbox_change = False
            self.set_states(res.states[0], res.states[1])
            #self.post_graph.selected = self.get_relevant_elements(self.post_state[2], clauses) # TODO - this should be put back or get_relevant_elements should be removed
            self.show_result('The following conjecture is violated:\n{}'.format(
                str(conj.to_formula()),
            ))
            return self.get_all_pre_facts()

    def find_inductive_invariant(self, button=None):
        """
        Use the new algorithm to find an inductive invariant, sometimes
        with the help of the user.
        """
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses, used_symbols_clauses, negate, clause_to_formula, lit_to_formula, var_to_skolem
        import ivy_transrel
        from ivy_solver import atom_to_z3, formula_to_z3, clauses_to_z3
        import tactics_api as ta
        from proof import ProofGoal
        from ivy_logic_utils import Clauses, and_clauses, dual_clauses


        if not hasattr(self, 'A'):
            # first time running the method, initialize

            self.A = [] # A is a list of conjectures of the form frozenset of literals (used to be Not(And(*literals)))
            # Add self.conjectures to A
            for conj in self.conjectures:
                assert conj.is_universal_first_order()
                assert len(conj.defs) == 0
                assert len(conj.clauses) == 1
                d = dual_clauses(conj, lambda v: var_to_skolem('__sk_',lg.Var(v.rep,v.sort)))
                assert len(d.defs) == 0
                assert all(len(x) == 1 for x in d.clauses)
                lits = [negate(lit_to_formula(x[0])) for x in d.clauses]
                self.A.append(frozenset(lits))
                # c = clause_to_formula(conj.clauses[0])
                # if type(c) is not lg.Or:
                #     c = lg.Or(c)
                # #self.A.append(lg.Not(lg.And(*(negate(lit) for lit in c))))
                # self.A.append(frozenset(c))

            self.Fs = dict() # dict mapping from len(A) to copy of self.F
            # the keys of f are integers
            # F[i] is a dict mapping each c in A to a set of subsets of c

        while True:
            self.F = dict()
            self.Fs[len(self.A)] = self.F
            self.F[-1] = dict((c, bottom) for c in self.A)
            self.F[0] = self.alpha_init()
            if any(self.F[0][c] == top for c in self.A):
                # return relaxed trace
                return False, self.A
            i = 0
            while not all(self.le(self.F[i][c], self.F[i - 1][c]) for c in self.A):
                print timestamp(), "====== i = {}, len(A) = {}".format(i, len(self.A))
                i += 1
                self.F[i] = self.alpha_step(self.F[i - 1])
                print "----  F[{}]:".format(i)
                x = and_clauses(*(
                    self.get_selected_conjecture([negate(lit) for lit in b])
                    for bs in self.F[i].values()
                    for b in bs
                ))
                if x == lg.And():
                    print "    TOP"
                else:
                    for f in x.fmlas:
                        print "    ", f
                print
                v = None
                for c in self.A:
                    if self.F[i][c] == top:
                        v = c
                        break
                if v is not None:
                    s = self.step_model(self.F[i - 1], v)
                    assert s is not None
                    self.A.append(frozenset(
                        negate(lit) for lit in s
                        #if (type(lit) is not lg.Not or type(lit.body) is lg.Eq) and
                        #'head' not in str(lit) and
                        #'tail' not in str(lit)
                    ))
                    break
            if v is None:
                # return inductive invariant
                return True, and_clauses(*(
                    self.get_selected_conjecture([negate(lit) for lit in b])
                    for bs in self.F[i - 1].values()
                    for b in bs
                ))

top = frozenset()
bottom = frozenset([frozenset()])
def select(lst, indices):
    return [lst[i] for i in indices]


class AnalysisSessionWidget(object):
    """
    This does not inherit from widgets.DOMWidget, but instead
    constructs several "real" widgets and displays them when
    displayed.
    """
    def __init__(self):
        self.session = None # to be set by AnalysisSession's constructor
        self.current_step = 0
        self.silent = False

        self.box = DialogWidget(
            title='Ivy Main',
            orientation='vertical',
            overflow_x='hidden',
            overflow_y='hidden',
            options={
                'height': 'max',
                'width': 450,
                'position': {
                    'my': 'left',
                    'at': 'left+5',
                },
            },
        )

        self.modal_messages = ModalMessagesWidget()

        self.concept = ConceptStateViewWidget(self)
        self.transition_view = TransitionViewWidget(self)

        self.info_area = widgets.Textarea()
        self.step_box = widgets.Textarea()

        self.proof_graph = CyGraphWidget(width='100%', height='100%')
        self.arg = CyGraphWidget(width='100%', height='100%')
        self.crg = CyGraphWidget(width='100%', height='100%')
        for w in [self.proof_graph, self.arg, self.crg]:
            w.info_area = self.info_area
        for x in [self.arg, self.crg]:
            x.cy_layout = {'name': 'preset'}
            x.cy_style = cy_styles.arg_style
        self.proof_graph.cy_layout = {'name': 'preset'}
        self.proof_graph.cy_style = cy_styles.proof_style

        self.buttons = _make_buttons([
            ('first', self.first),
            ('prev', self.prev),
            ('next', self.next),
            ('last', self.last),
        ])

        self.select_abstractor = widgets.Dropdown(
            options=[
                ('top/bottom', 'ta.Abstractors.top_bottom'),
                ('concrete', 'ta.Abstractors.concrete'),
                ('propagate', 'ta.Abstractors.propagate'),
                ('propagate & conjectures', 'ta.Abstractors.propagate_and_conjectures'),
                ('concept space', 'ta.Abstractors.concept_space'),
            ],
            value='ta.Abstractors.top_bottom',
        )

        self.box.children = [
            self.modal_messages, # invisible
            # self.concept.box, # will display in a separate dialog
            self.transition_view.box, # will display in a separate dialog

            HBox(
                [
                    # HBox(
                    #     [self.proof_graph],
                    #     flex=1,
                    #     height='100%',
                    #     overflow_x='hidden',
                    #     overflow_y='hidden',
                    #     _css=[
                    #         (None, 'margin-right', '5px'),
                    #         (None, 'min-width', '150px'),
                    #     ],
                    # ),
                    # HBox(
                    #     [self.arg],
                    #     flex=2,
                    #     height='100%',
                    #     overflow_x='hidden',
                    #     overflow_y='hidden',
                    #     _css=[
                    #         (None, 'margin-right', '5px'),
                    #         (None, 'min-width', '150px'),
                    #     ],
                    # ),
                    widgets.HBox(
                        [self.crg],
                        flex=1,
                        height='100%',
                        overflow_x='hidden',
                        overflow_y='hidden',
                        _css=[
                            (None, 'min-width', '150px'),
                        ],
                    ),
                ],
                flex=1,
                width='100%',
                overflow_y='hidden',
                _css=[
                    (None, 'margin-bottom', '5px'),
                ],
            ),
            #self.select_abstractor,
            self.info_area,
            #self.step_box,
            # widgets.HBox(
            #     self.buttons,
            #     width='100%',
            #     overflow_y='hidden',
            # ),
        ]

        self.arg_node_events = [
            ('tap', self.arg_node_click),
        ]

        self.crg_node_events = [
            ('tap', self.crg_node_click)
        ]

        self.proof_node_events = [
            ('tap', self.proof_node_click),
        ]

    analysis_state = property(lambda self: self.session.history[self.current_step][0])
    step_info = property(lambda self: self.session.history[self.current_step][1])

    def _ipython_display_(self):
        """Called when display() or pyout is used to display the session"""
        self.box._ipython_display_()

    def register_session(self, session):
        self.session = session
        self.concept_session = ConceptInteractiveSession(
            get_initial_concept_domain(self.session.analysis_state.ivy_interp.sig),
            And(),
            session.analysis_state.ivy_interp.background_theory().to_formula(),
            [],
            [],
            self.concept,
            self.session,
        )
        self.concept.title = 'true'
        self.concept.concept_session.recompute()

        # TODO: change this
        ConceptInteractiveSession(
            get_initial_concept_domain(self.session.analysis_state.ivy_interp.sig),
            And(),
            session.analysis_state.ivy_interp.background_theory().to_formula(),
            [],
            [],
            self.transition_view,
            self.session,
        )
        self.transition_view.register_session(session)

    def render(self):
        analysis_state, step_info = self.session.history[self.current_step]

        self.proof_graph.cy_elements = dot_layout(render_proof_stack(
            analysis_state.goal_stack,
            node_events=self.proof_node_events,
            node_actions=lambda goal: list(chain(*goal_node_actions(self, goal))),
        ), ())

        self.arg.cy_elements = dot_layout(render_rg(
            analysis_state.ivy_ag,
            self.arg_node_events,
            node_actions=lambda s: list(chain(*arg_node_actions(self, s))),
        ), ())

        self.crg.cy_elements = dot_layout(render_rg(
            analysis_state.crg,
            self.crg_node_events,
            node_actions=lambda node: [],
        ), ())

        msg = step_info.get('msg', '')
        tactic = step_info.get('tactic', '?')
        step_lines = [
            'Step {}:'.format(self.current_step),
            'Tactic: {}'.format(tactic),
            'Message:',
            msg,
            ''
        ]
        step_lines += [
            '{}: {}'.format(k, step_info[k])
            for k in sorted(step_info.keys())
            if k not in ('msg', 'tactic')
        ]
        self.step_box.value = '\n'.join(step_lines)

    def prev(self, button=None):
        if self.current_step > 0:
            self.current_step -= 1
            self.render()

    def next(self, button=None):
        if self.current_step < len(self.session.history) - 1:
            self.current_step += 1
            self.render()

    def first(self, button=None):
        self.current_step = 0
        self.render()

    def last(self, button=None):
        self.current_step = len(self.session.history) - 1
        self.render()

    def step(self):
        self.current_step = len(self.session.history) - 1

        msg = self.step_info.get('msg')
        if msg is not None and not self.silent:
            self.modal_messages.new_message(
                title="Tactic {} says:".format(self.step_info.get('tactic', '?')),
                body=msg,
            )

        self.render()

        if not self.silent:
            active = self.step_info.get('active')
            if type(active) is ProofGoal:
                self.proof_node_click(active)
            elif type(active) is State:
                # find if it's an arg or crg
                if (active.id < len(self.analysis_state.ivy_ag.states) and
                    active is self.analysis_state.ivy_ag.states[active.id]):
                    self.arg_node_click(active)
                elif (active.id < len(self.analysis_state.crg.states) and
                      active is self.analysis_state.crg.states[active.id]):
                    self.crg_node_click(active)
                else:
                    assert False, active
            else:
                assert active is None, active

    def arg_node_click(self, arg_node):
        self.concept.concept_session.state = arg_node.clauses.to_formula()
        self.concept.concept_session.goal_constraints = []
        self.concept.current_step = self.current_step
        self.concept.arg_node = arg_node
        self.concept.proof_goal = None
        self.concept.title = "Step {}, ARG {}".format(
            self.current_step,
            arg_node.id,
        )
        self.concept.concept_session.recompute()

    def crg_node_click(self, crg_node):
        self.concept.concept_session.state = crg_node.clauses.to_formula()
        self.concept.concept_session.goal_constraints = []
        self.concept.current_step = self.current_step
        self.concept.arg_node = getattr(crg_node, 'arg_node', None)
        self.concept.proof_goal = None
        self.concept.title = "Step {}, CRG {}".format(
            self.current_step,
            crg_node.id,
        )
        self.concept.concept_session.replace_domain(get_structure_concept_domain(crg_node))

        # TODO: get rid of this, this is just to experiment
        analysis_state, step_info = self.session.history[self.current_step]
        t = analysis_state.crg.transition_to(crg_node)
        if t is not None:
            self.transition_view.set_states(t[0], t[-1])

    def proof_node_click(self, goal):
        self.concept.concept_session.state = (goal.node.clauses.to_formula())
        self.concept.concept_session.goal_constraints = [goal.formula.to_formula()]
        self.concept.current_step = self.current_step
        self.concept.arg_node = goal.node
        self.concept.proof_goal = goal
        self.concept.title = "Step {}, Goal {} at ARG {}".format(
            self.current_step,
            goal,
            goal.node.id,
        )
        self.concept.concept_session.recompute()

    def concept_new_goal(self, button=None):
        assert self.current_step == len(self.session.history) - 1 # TODO: maybe this is too restrictive?
        assert self.current_step == self.concept.current_step
        code = dedent('''
        facts = [
            {}
        ]
        push_new_goal(Clauses(facts), arg_node({!r}))
        ''').strip().format(
            ',\n    '.join(repr(x) for x in self.concept.get_active_facts()),
            self.concept.arg_node.id,
        )
        self.proof_graph.execute_new_cell(code)

    def concept_check(self, button=None):
        from ivy_logic_utils import Clauses
        assert self.current_step == len(self.session.history) - 1 # TODO: maybe this is too restrictive?
        assert self.current_step == self.concept.current_step

        g = ta.goal_at_arg_node(
            Clauses(self.concept.get_active_facts()),
            self.concept.arg_node
        )
        x, y = ta.refine_or_reverse(g)
        if x:
            self.concept.result.value = 'UNSAT'
        else:
            self.concept.result.value = 'SAT'

    def concept_min_unsat_core(self, button=None):
        from ivy_logic_utils import Clauses
        import tactic_api as ta
        assert self.current_step == len(self.session.history) - 1 # TODO: maybe this is too restrictive?
        assert self.current_step == self.concept.current_step

        g = ta.goal_at_arg_node(
            Clauses(self.concept.get_active_facts()),
            self.concept.arg_node
        )
        x, y = ta.refine_or_reverse(g)
        if x:
            self.concept.result.value = 'UNSAT'
            # TODO: make the minimal unsat core selected
            assert False
        else:
            self.concept.result.value = 'SAT'

    def concept_refine(self, button=None):
        import z3
        import tactics_api as ta
        import tactics as t
        import ivy_transrel
        from ivy_logic_utils import negate_clauses, Clauses, and_clauses, simplify_clauses
        from ui_extensions_api import interaction, ShowModal, InteractionError, UserSelectMultiple
        import ivy_solver
        from ivy_solver import clauses_to_z3

        assert self.current_step == len(self.session.history) - 1 # TODO: maybe this is too restrictive?
        assert self.current_step == self.concept.current_step

        goal = ta.goal_at_arg_node(
            Clauses(self.concept.get_active_facts()),
            ta._ivy_ag.states[self.concept.arg_node.id],
        )
        preds, action = ta.arg_get_preds_action(goal.node)
        assert action != 'join'
        assert len(preds) == 1
        pred = preds[0]
        axioms = self.session.analysis_state.ivy_interp.background_theory()
        theory = and_clauses(
            ivy_transrel.forward_image(
                pred.clauses,
                axioms,
                action.update(self.session.analysis_state.ivy_interp, None)
            ),
            axioms
        )
        goal_clauses = goal.formula
        assert len(goal_clauses.defs) == 0

        s = z3.Solver()
        s.add(clauses_to_z3(theory))
        s.add(clauses_to_z3(goal_clauses))
        is_sat = s.check()
        if is_sat == z3.sat:
            self.concept.result = 'SAT'
        elif is_sat == z3.unsat:
            x, y = True, ivy_transrel.interp_from_unsat_core(goal_clauses, theory, goal_clauses, None)
        else:
            assert False, is_sat

        t.custom_refine_or_reverse(goal, x, y, False)

## LatticeMap

def make_or(*args):
    if len(args) == 0:
       return False
    else:
       return z3.Or(*args)

class LatticeMap(object):
    """
    A map for the implication lattice
    """

    def __init__(self, n):
        self.n = n
        self.s = z3.Solver()
        self.pres = [z3.Bool('pre_{}'.format(i)) for i in range(n)]
        self.posts = [z3.Bool('post_{}'.format(i)) for i in range(n)]
        self.litmap = {
            '{}_{}'.format(i,j): (i,j)
            for i in ['pre', 'post']
            for j in range(self.n)
        }
        # we always want pre stronger (smaller) than post
        for i in range(n):
            self.s.add(z3.Implies(
                z3.Bool('pre_{}'.format(i)),
                z3.Bool('post_{}'.format(i))
            ))

        # cardinality constrains on number of pre facts
        self.pre_at_mosts = [z3.Bool('pre_at_most_{}'.format(i)) for i in range(n)]
        for i in range(n):
            self.s.add(z3.Implies(
                self.pre_at_mosts[i],
                z3.Sum([z3.If(x, 1, 0) for x in self.pres]) <= i
            ))

        # cardinality constrains on number of post facts
        self.post_at_mosts = [z3.Bool('post_at_most_{}'.format(i)) for i in range(n)]
        for i in range(n):
            self.s.add(z3.Implies(
                self.post_at_mosts[i],
                z3.Sum([z3.If(x, 1, 0) for x in self.posts]) <= i
            ))


    def sample(self, pre_at_most=None, post_at_most=None):
        constrains = []
        if pre_at_most is not None and pre_at_most < self.n:
            assert 0 <= pre_at_most < self.n
            constrains.append(self.pre_at_mosts[pre_at_most])
        if post_at_most is not None and post_at_most < self.n:
            assert 0 <= post_at_most < self.n
            constrains.append(self.post_at_mosts[post_at_most])


        if self.s.check(constrains) == z3.unsat:
            print "LatticeMap: no more samples"
            return None
        else:
            m = self.s.model()
            # sample = set(self.litmap.values())
            sample = set(
                (i,j)
                for i in ['post']
                for j in range(self.n)
            )

            for x in m:
                if x.name() not in self.litmap:
                    continue
                i, j = self.litmap[x.name()]
                if i == 'post' and z3.is_false(m[x]):
                    sample.remove((i, j))
                elif i == 'pre' and z3.is_true(m[x]):
                    sample.add((i, j))
            print "LatticeMap: sample = {}, {}".format(
                [i for i in range(self.n) if ('pre', i) in sample],
                [i for i in range(self.n) if ('post', i) in sample],
            )
            return sample

    def block_pre_down(self, pre):
        # next time, a weaker pre
        pre = sorted(pre)
        print "LatticeMap: block_pre_down({})".format(pre)
        self.block_implication(pre, [])

    def block_non_implication(self, pre, post):
        """
        Similar to block down - a maximal satiafiable set.
        """
        # next time, either a weaker post or a stronger pre
        pre = sorted(pre)
        post = sorted(post)
        print "LatticeMap: block_non_implication({}, {})".format(pre, post)
        self.s.add(make_or([
            z3.Bool('post_{}'.format(i))
            for i in range(self.n) if i not in post
        ] + [
            z3.Not(z3.Bool('pre_{}'.format(i)))
            for i in pre
        ]))

    def block_implication(self, pre, post):
        # next time, either a stronger post or a weaker pre
        pre = sorted(pre)
        post = sorted(post)
        print "LatticeMap: block_implication({}, {})".format(pre, post)
        self.s.add(make_or([
            z3.Not(z3.Bool('post_{}'.format(i)))
            for i in post
        ] + [
            z3.Bool('pre_{}'.format(i))
            for i in range(self.n) if i not in pre
        ]))
