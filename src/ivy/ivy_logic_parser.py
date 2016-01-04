#
# Copyright (c) Microsoft Corporation. All Rights Reserved.
#
# This file contains parser rules for first-order formulas
from ivy_ast import *
import ivy_logic_utils
import ivy_utils as iu

def get_lineno(p,n):
    return (iu.filename,p.lineno(n))

def symbol(s):
    return Variable(s,universe) if str.isupper(s[0]) else Constant(s)

def p_aterm_symbol(p):
    'aterm : SYMBOL'
    p[0] = App(p[1])
    p[0].lineno = get_lineno(p,1)

def p_aterm_aterm_terms(p):
    'aterm : aterm LPAREN terms RPAREN'
    p[0] = p[1]
    p[0].args.extend(p[3])

if iu.get_numeric_version() <= [1,2]:

    def p_term_term_colon_term(p):
        'aterm : aterm COLON SYMBOL'
        p[0] = compose_atoms(p[1],App(p[3]))
        p[0].lineno = get_lineno(p,2)

else:

    def p_term_term_dot_term(p):
        'aterm : aterm DOT SYMBOL'
        p[0] = compose_atoms(p[1],App(p[3]))
        p[0].lineno = get_lineno(p,2)

def p_var_variable(p):
    'var : VARIABLE'
    p[0] = Variable(p[1],universe)
    p[0].lineno = get_lineno(p,1)

def p_var_variable_colon_symbol(p):
    'var : VARIABLE COLON SYMBOL'
    p[0] = Variable(p[1],p[3])
    p[0].lineno = get_lineno(p,1)

def p_term_aterm(p):
    'term : aterm'
    p[0] = p[1]

def p_aterm_old_symbol(p):
    'term : OLD aterm'
    p[0] = Old(p[2])
    p[0].lineno = get_lineno(p,1)

def p_term_var(p):
    'term : var'
    p[0] = p[1]

def p_term_term_PLUS_term(p):
    'term : term PLUS term'
    p[0] = App(p[2],p[1],p[3])
    p[0].lineno = get_lineno(p,2)
    
def p_term_term_MINUS_term(p):
    'term : term MINUS term'
    p[0] = App(p[2],p[1],p[3])
    p[0].lineno = get_lineno(p,2)

def p_term_term_TIMES_term(p):
    'term : term TIMES term'
    p[0] = App(p[2],p[1],p[3])
    p[0].lineno = get_lineno(p,2)


def p_terms(p):
    'terms : '
    p[0] = []

def p_terms_term(p):
    'terms : term'
    p[0] = [p[1]]

def p_terms_terms_term(p):
    'terms : terms COMMA term'
    p[0] = p[1]
    p[0].append(p[3])


def p_term_lp_term_lp(p):
    'term : LPAREN term RPAREN'
    p[0] = p[2]

def p_vars_var(p):
    'vars : var'
    p[0] = [p[1]]

def p_vars_vars_comma_var(p):
    'vars : vars COMMA var'
    p[0] = p[1]
    p[0].append(p[3])

# apps are terms of the form symbol or symbol(term*)

def p_app_symbol(p):
    'app : SYMBOL'
    p[0] = App(p[1],[])
    p[0].lineno = get_lineno(p,1)

def p_app_symbol_lp_terms_rp(p):
    'app : SYMBOL LPAREN terms RPAREN'
    p[0] = App(p[1],p[3])
    p[0].lineno = get_lineno(p,1)

def p_app_term_infix_term(p):
    'app : term infix term'
    p[0] = App(p[2],p[1],p[3])
    p[0].lineno = get_lineno(p,2)
    

def p_apps_app(p):
    'apps : app'
    p[0] = [p[1]]

def p_apps_apps_app(p):
    'apps : apps COMMA app'
    p[0] = p[1]
    p[0].append(p[3])

# atoms are formulas just of the form symbol or symbol(term*)

def p_atom_symbol(p):
    'atom : SYMBOL'
    p[0] = Atom(p[1],[])
    p[0].lineno = get_lineno(p,1)

def p_atom_symbol_lp_terms_rp(p):
    'atom : SYMBOL LPAREN terms RPAREN'
    p[0] = Atom(p[1],p[3])
    p[0].lineno = get_lineno(p,1)

def p_atoms_atom(p):
    'atoms : atom'
    p[0] = [p[1]]

def p_atoms_atoms_atom(p):
    'atoms : atoms COMMA atom'
    p[0] = p[1]
    p[0].append(p[3])

# literal is an atom or its negation

def p_lit_atom(p):
    'lit : atom'
    p[0] = Literal(1,p[1])
    p[0].lineno = get_lineno(p,1)

def p_lit_term_eq_term(p):
    'lit : SYMBOL EQ SYMBOL'
    p[0] = Literal(1,Atom(p[2],[symbol(p[1]),symbol(p[3])]))
    p[0].lineno = get_lineno(p,2)

def p_lit_term_tildaeq_term(p):
    'lit : SYMBOL TILDAEQ SYMBOL'
    p[0] = Literal(0,Atom(p[2],[symbol(p[1]),symbol(p[3])]))
    p[0].lineno = get_lineno(p,2)

def p_lit_tilda_atom(p):
    'lit : TILDA lit'
    p[0] = ~p[2]
    p[0].lineno = get_lineno(p,1)

def p_relop_eq(p):
    'relop : EQ'
    p[0] = p[1]

def p_relop_le(p):
    'relop : LE'
    p[0] = p[1]

def p_relop_lt(p):
    'relop : LT'
    p[0] = p[1]

def p_relop_ge(p):
    'relop : GE'
    p[0] = p[1]

def p_relop_gt(p):
    'relop : GT'
    p[0] = p[1]
    
def p_infix_plus(p):
    'infix : PLUS'
    p[0] = p[1]

def p_infix_minus(p):
    'infix : MINUS'
    p[0] = p[1]

def p_infix_times(p):
    'infix : TIMES'
    p[0] = p[1]

# formulas are boolean combinations of terms and equalities between terms

def p_fmla_term(p):
    'fmla : term'
    p[0] = app_to_atom(p[1])

def p_fmla_term_relop_term(p):
    'fmla : term relop term'
    p[0] = Atom(p[2],[p[1],p[3]])
    p[0].lineno = get_lineno(p,2)

def p_fmla_term_tildaeq_term(p):
    'fmla : term TILDAEQ term'
    p[0] = Not(Atom('=',[p[1],p[3]]))
    p[0].lineno = get_lineno(p,2)

def p_fmla_lparen_fmla_rparen(p):
    'fmla : LPAREN fmla RPAREN'
    p[0] = p[2]

def p_fmla_true(p):
    'fmla : TRUE'
    p[0] = And()
    p[0].lineno = get_lineno(p,1)
    
def p_fmla_false(p):
    'fmla : FALSE'
    p[0] = Or()
    p[0].lineno = get_lineno(p,1)

def p_fmla_not_fmla(p):
    'fmla : TILDA fmla'
    p[0] = Not(p[2])
    p[0].lineno = get_lineno(p,1)

def p_fmla_fmla_and_fmla(p):
    'fmla : fmla AND fmla'
    if isinstance(p[1],And):
        p[0] = p[1]
        p[0].args.append(p[3])
    else:
        p[0] = And(p[1],p[3])
        p[0].lineno = get_lineno(p,2)

def p_fmla_fmla_or_fmla(p):
    'fmla : fmla OR fmla'
    if isinstance(p[1],Or):
        p[0] = p[1]
        p[0].args.append(p[3])
    else:
        p[0] = Or(p[1],p[3])
        p[0].lineno = get_lineno(p,2)

if not (iu.get_numeric_version() <= [1]):

    def p_fmla_fmla_arrow_fmla(p):
        'fmla : fmla ARROW fmla'
        p[0] = Implies(p[1],p[3])
        p[0].lineno = get_lineno(p,2)

def p_fmla_fmla_iff_fmla(p):
    'fmla : fmla IFF fmla'
    p[0] = Iff(p[1],p[3])
    p[0].lineno = get_lineno(p,2)

def p_fmla_forall_vars_dot_fmla(p):
    'fmla : FORALL vars DOT fmla'
    p[0] = Forall(p[2],p[4])
    p[0].lineno = get_lineno(p,1)

def p_fmla_exists_vars_dot_fmla(p):
    'fmla : EXISTS vars DOT fmla'
    p[0] = Exists(p[2],p[4])
    p[0].lineno = get_lineno(p,1)