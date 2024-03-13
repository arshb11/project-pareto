#####################################################################################################
# PARETO was produced under the DOE Produced Water Application for Beneficial Reuse Environmental
# Impact and Treatment Optimization (PARETO), and is copyright (c) 2021-2023 by the software owners:
# The Regents of the University of California, through Lawrence Berkeley National Laboratory, et al.
# All rights reserved.
#
# NOTICE. This Software was developed under funding from the U.S. Department of Energy and the U.S.
# Government consequently retains certain rights. As such, the U.S. Government has been granted for
# itself and others acting on its behalf a paid-up, nonexclusive, irrevocable, worldwide license in
# the Software to reproduce, distribute copies to the public, prepare derivative works, and perform
# publicly and display publicly, and to permit others to do so.
#####################################################################################################
"""
Functions used in run_infrastructure_analysis.py
"""

from pareto.other_models.CM_module.models.qcp_br import build_qcp_br
import pyomo.environ as pyo
from pareto.utilities.cm_utils.gen_utils import obj_fix


def max_theoretical_recovery_flow(model, treatment_unit, desired_li_conc):
    """This function computes the largest flow possible to
    the treatment unit while still keeping the Li
    concentration above the desired level.

    This function ignores all infrastructure - this is only
    based on mass balance along.
    """
    # get the efficiency for the treatment unit
    assert model.p_alpha[treatment_unit, "Li"] > 0.999
    alphaW = model.p_alphaW[treatment_unit]
    # get the desired li conc at the inlet - correct this later
    desired_li_conc = desired_li_conc * (1 - alphaW)

    # create a list of the flows and concentrations
    # sort by largest concentration first
    produced_flows_conc = list()
    sumf = 0
    for t in model.p_FGen:
        f = pyo.value(model.p_FGen[t]) * 7  # convert to bbls/wk
        sumf += f
        c = pyo.value(model.p_CGen[t[0], "Li", t[1]])
        produced_flows_conc.append((f, c))
    produced_flows_conc.sort(key=lambda t: t[1], reverse=True)

    # iterate through the flows and accumulate until the
    # overall concentration goes below the desired limit
    cumulative_f = 0
    cumulative_li = 0
    li_conc = 0
    for f, c in produced_flows_conc:
        cf = cumulative_f + f
        cli = cumulative_li + f * c
        li_conc = cli / cf
        # print(f, c, cf, li_conc)
        if li_conc > desired_li_conc:
            cumulative_f = cf
            cumulative_li = cli
        else:
            ff = (cumulative_li - desired_li_conc * cumulative_f) / (
                desired_li_conc - c
            )
            # print('***', ff)
            cumulative_f += ff
            cumulative_li += ff * c
            li_conc = cumulative_li / cumulative_f
            break

    return cumulative_f * (1 - alphaW)


def max_theoretical_recovery_flow_opt(
    model, treatment_unit, desired_li_conc, tee=False
):
    """This function computes the largest flow possible to
    the treatment unit while still keeping the Li
    concentration above the desired level.

    This function ignores all infrastructure - this is only
    based on mass balance along.

    This function differs from max_theoretical_recovery_flow
    since this function uses an LP and Gurobi to find the value
    """
    assert model.p_alpha[treatment_unit, "Li"] > 0.999
    alphaW = model.p_alphaW[treatment_unit]
    # desired concentration at the inlet - this will be corrected at the end
    desired_li_conc = desired_li_conc * (1 - alphaW)

    mm = pyo.ConcreteModel()
    mm.S = list(model.p_FGen.index_set())
    bounds = {k: (0, model.p_FGen[k] * 7) for k in mm.S}
    mm.F = pyo.Var(mm.S, bounds=bounds)
    mm.cumulative_F = pyo.Var(bounds=(10, None))
    mm.cumulative_F_con = pyo.Constraint(
        expr=mm.cumulative_F == sum(mm.F[t] for t in mm.S)
    )
    mm.obj = pyo.Objective(expr=mm.cumulative_F, sense=pyo.maximize)
    mm.total_li = pyo.Expression(
        expr=sum(mm.F[t] * model.p_CGen[t[0], "Li", t[1]] for t in mm.S)
    )
    mm.quality_con = pyo.Constraint(
        expr=mm.total_li >= desired_li_conc * mm.cumulative_F
    )
    # mm.overall_conc = pyo.Expression(expr = mm.total_li/sum(mm.F[t] for t in mm.S))

    status = pyo.SolverFactory("ipopt").solve(mm, tee=tee)
    pyo.assert_optimal_termination(status)
    return pyo.value(mm.cumulative_F) * (1 - alphaW)


def max_recovery_with_infrastructure(data, tee=False):
    # build the model from the loaded data
    model = build_qcp_br(data)

    ###
    # First, we solve a flow-based LP without any concentration variables
    ###
    model.obj.deactivate()
    model.br_obj.deactivate()
    model.treatment_only_obj.activate()

    # create lists of constraints involving concentrations and all flow / inventory variables
    conclist = [
        model.MSconc,
        model.Pconc,
        model.Cconc,
        model.Sconc,
        model.Wconc,
        model.TINconc,
        model.NTTWconc,
        model.NTCWconc,
        model.NTSrcconc,
        model.minconccon,
    ]
    flowlist = [
        model.Cflow,
        model.noinvflow,
        model.Sinv,
        model.Dflow,
        model.Wflow,
        model.TINflow,
        model.NTTWflow,
        model.NTCWflow,
    ]

    # I think this fixes the concentration variables and removes the concentration constraints
    # TODO: Why is this called obj_fix? what is "obj"?
    model = obj_fix(model, ["v_C"], activate=flowlist, deactivate=conclist)

    # Solve the linear flow model
    print("   ... running linear flow model")
    opt = pyo.SolverFactory("ipopt")
    # opt.options['NonConvex'] = 2
    # opt.options['TimeLimit'] = 150
    status = opt.solve(model, tee=tee)

    # terminating script early if optimal solution not found
    pyo.assert_optimal_termination(status)

    ###
    # Bilinear NLP
    ###

    # unfixing all the initialized variables
    model = obj_fix(model, [], deactivate=[], activate=conclist + flowlist)

    # solve for the maximum possible recovery revenue given this infrastructure
    model.TINflow.deactivate()
    for k in model.Dflow:
        if k[0] == "K1_TW" or k[0] == "K1_CW":
            model.Dflow[k].deactivate()

    # running bilinear model
    print("   ... running bilinear model")
    opt = pyo.SolverFactory("ipopt")
    opt.options["ma27_pivtol"] = 1e-2
    opt.options["tol"] = 1e-6
    status = opt.solve(model, tee=tee)
    pyo.assert_optimal_termination(status)
    print(
        "Max lithium revenue with existing infrastructure:", pyo.value(model.treat_rev)
    )
    return model


def cost_optimal(data, tee=False):
    # build the model from the loaded data
    model = build_qcp_br(data)

    ###
    # First, we solve a flow-based LP without any concentration variables
    ###

    # create lists of constraints involving concentrations and all flow / inventory variables
    conclist = [
        model.MSconc,
        model.Pconc,
        model.Cconc,
        model.Sconc,
        model.Wconc,
        model.TINconc,
        model.NTTWconc,
        model.NTCWconc,
        model.NTSrcconc,
        model.minconccon,
    ]
    flowlist = [
        model.Cflow,
        model.noinvflow,
        model.Sinv,
        model.Dflow,
        model.Wflow,
        model.TINflow,
        model.NTTWflow,
        model.NTCWflow,
    ]

    # I think this fixes the concentration variables and removes the concentration constraints
    # TODO: Why is this called obj_fix? what is "obj"?
    model = obj_fix(model, ["v_C"], activate=flowlist, deactivate=conclist)

    # Solve the linear flow model
    print("   ... running linear flow model")
    opt = pyo.SolverFactory("ipopt")
    # opt.options['NonConvex'] = 2
    # opt.options['TimeLimit'] = 150
    status = opt.solve(model, tee=tee)

    # terminating script early if optimal solution not found
    pyo.assert_optimal_termination(status)

    ###
    # Bilinear NLP
    ###

    # unfixing all the initialized variables
    model = obj_fix(model, [], deactivate=[], activate=conclist + flowlist)

    # running bilinear model
    print("   ... running bilinear model")
    opt = pyo.SolverFactory("ipopt")
    opt.options["max_iter"] = 10000
    status = opt.solve(model, tee=tee)
    pyo.assert_optimal_termination(status)
    return model
