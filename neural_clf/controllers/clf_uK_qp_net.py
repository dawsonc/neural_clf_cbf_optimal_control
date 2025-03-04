import torch
import torch.nn as nn
import torch.nn.functional as F
import cvxpy as cp
from cvxpylayers.torch import CvxpyLayer


def d_tanh_dx(tanh):
    return torch.diag_embed(1 - tanh**2)


class CLF_K_QP_Net(nn.Module):
    """A neural network for simultaneously computing the Lyapunov function and the
    control input. The neural net makes the Lyapunov function, and the control input
    is computed by solving a QP.
    """

    def __init__(self, n_input, n_hidden, n_controls, clf_lambda, clf_relaxation_penalty,
                 control_affine_dynamics, u_nominal, scenarios, nominal_scenario,
                 x_goal, u_eq,
                 G_u=torch.tensor([]), h_u=torch.tensor([])):
        """
        Initialize the network

        args:
            n_input: number of states the system has
            n_hidden: number of hiddent layers to use
            n_controls: number of control outputs to use
            clf_lambda: desired exponential convergence rate for the CLF
            clf_relaxation_penalty: the penalty for relaxing the control lyapunov constraint
            control_affine_dynamics: a function that takes n_batch x n_dims and returns a tuple of:
                f_func: a function n_batch x n_dims -> n_batch x n_dims that returns the
                        state-dependent part of the control-affine dynamics
                g_func: a function n_batch x n_dims -> n_batch x n_dims x n_controls that returns
                        the input coefficient matrix for the control-affine dynamics
            u_nominal: a function n_batch x n_dims -> n_batch x n_controls that returns the nominal
                       control input for the system (even LQR about origin is fine)
            scenarios: a list of dictionaries specifying the parameters to pass to f_func and g_func
            nominal_scenario: a dictionary specifying the parameters to pass to u_nominal
            x_goal: the goal state
            u_eq: the equilibrium control inputs at the goal state
            G_u: a matrix of constraints on fesaible control inputs (n_constraints x n_controls)
            h_u: a vector of constraints on fesaible control inputs (n_constraints x 1)
                Given G_u and h_u, the CLF QP will additionally enforce G_u u <= h_u
        """
        super(CLF_K_QP_Net, self).__init__()

        # Save the dynamics and nominal controller functions
        self.dynamics = control_affine_dynamics
        self.u_nominal = u_nominal
        assert len(scenarios) > 0, "Must pass at least one scenario"
        self.scenarios = scenarios
        self.nominal_scenario = nominal_scenario
        self.x_goal = x_goal
        self.u_eq = u_eq.T

        # The network will have the following architecture
        #
        # n_input -> VFC1 (n_input x n_hidden) -> VFC2 (n_hidden, n_hidden)
        # -> VFC2 (n_hidden, n_hidden) -> V = x^T x --> QP -> u
        self.Vfc_layer_1 = nn.Linear(n_input, n_hidden)
        self.Vfc_layer_2 = nn.Linear(n_hidden, n_hidden)

        # We also train a controller to learn the nominal control input
        # This network outputs a gain matrix, then the control input is -K (x - x_goal)
        self.K1fc_layer_1 = nn.Linear(n_input, n_hidden)
        self.K1fc_layer_2 = nn.Linear(n_hidden, n_input)
        self.K2fc_layer_1 = nn.Linear(n_input, n_hidden)
        self.K2fc_layer_2 = nn.Linear(n_hidden, n_input)

        self.n_controls = n_controls
        self.clf_lambda = clf_lambda
        self.clf_relaxation_penalty = clf_relaxation_penalty
        assert G_u.size(0) == h_u.size(0), "G_u and h_u must have consistent dimensions"
        self.G_u = G_u.double()
        self.h_u = h_u.double()

        # Allow user to toggle QP on and off
        self.use_QP = True

        # To find the control input, we want to solve a QP, so we need to define the QP layer here
        # The decision variables are the control input and relaxation of the CLF condition for each
        # scenario
        u = cp.Variable(self.n_controls)
        clf_relaxations = []
        for scenario in self.scenarios:
            clf_relaxations.append(cp.Variable(1))
        # And it's parameterized by the lie derivatives and value of the Lyapunov function in each
        # scenario
        L_f_Vs = []
        L_g_Vs = []
        for scenario in self.scenarios:
            L_f_Vs.append(cp.Parameter(1))
            L_g_Vs.append(cp.Parameter(self.n_controls))

        V = cp.Parameter(1, nonneg=True)

        # To allow for gradual increasing of the cost of relaxations, set the relaxation penalty
        # as a parameter as well
        clf_relaxation_penalty_param = cp.Parameter(1, nonneg=True)
        # Also allow passing in a nominal controller to filter
        u_nominal = cp.Parameter(self.n_controls)

        # The QP is constrained by
        #
        # L_f V + L_g V u + lambda V <= 0
        #
        # To ensure that this QP is always feasible, we relax the CLF constraint
        #
        # L_f V + L_g V u + lambda V - r <= 0
        #                              r >= 0
        #
        # and later add the cost term relaxation_penalty * r.
        #
        # To encourage robustness to parameter variation, we have four instances of these
        # constraints for different parameters
        constraints = []
        for i in range(len(self.scenarios)):
            constraints.append(
                L_f_Vs[i] + L_g_Vs[i] @ u + self.clf_lambda * V - clf_relaxations[i] <= 0)
            constraints.append(clf_relaxations[i] >= 0)
        # We also add the user-supplied constraints, if provided
        if len(self.G_u) > 0:
            constraints.append(self.G_u @ u <= self.h_u)

        # The cost is quadratic in the controls and linear in the relaxation
        # objective_expression = cp.sum_squares(u - u_nominal)
        objective_expression = cp.sum_squares(u - u_nominal)
        for r in clf_relaxations:
            objective_expression += cp.multiply(clf_relaxation_penalty_param, r)
        objective = cp.Minimize(objective_expression)

        # Finally, create the optimization problem and the layer based on that
        problem = cp.Problem(objective, constraints)
        assert problem.is_dpp()
        variables = [u] + clf_relaxations
        parameters = L_f_Vs + L_g_Vs + [V,
                                        u_nominal,
                                        clf_relaxation_penalty_param]
        self.qp_layer = CvxpyLayer(problem, variables=variables, parameters=parameters)

    def compute_controls(self, x):
        """
        Computes the control input (for use in the QP filter)

        args:
            x: the state at the current timestep [n_batch, n_dims]
        returns:
            u: the value of the barrier at each provided point x [n_batch, n_controls]
        """
        tanh = nn.Tanh()
        K1fc1_act = tanh(self.K1fc_layer_1(x))
        K1 = self.K1fc_layer_2(K1fc1_act)
        K2fc1_act = tanh(self.K2fc_layer_1(x))
        K2 = self.K2fc_layer_2(K2fc1_act)
        K = torch.stack((K1, K2), dim=1)

        return -1.0 * torch.bmm(K, (x - self.x_goal).unsqueeze(-1)) + self.u_eq

    def compute_lyapunov(self, x):
        """
        Computes the value and gradient of the Lyapunov function

        args:
            x: the state at the current timestep [n_batch, n_dims]
        returns:
            V: the value of the Lyapunov at each provided point x [n_batch, 1]
            grad_V: the gradient of V [n_batch, n_dims]
        """
        # Use the first two layers to compute the Lyapunov function
        tanh = nn.Tanh()
        Vfc1_act = tanh(self.Vfc_layer_1(x))
        Vfc2_act = tanh(self.Vfc_layer_2(Vfc1_act))
        # Compute the Lyapunov function as the square norm of the last layer activations
        V = 0.5 * (Vfc2_act * Vfc2_act).sum(1)

        # We also need to calculate the Lie derivative of V along f and g
        #
        # L_f V = \grad V * f
        # L_g V = \grad V * g
        #
        # Since V = tanh(w2 * tanh(w1*x + b1) + b1),
        # grad V = d_tanh_dx(V) * w2 * d_tanh_dx(tanh(w1*x + b1)) * w1

        # Jacobian of first layer wrt input (n_batch x n_hidden x n_input)
        DVfc1_act = torch.matmul(d_tanh_dx(Vfc1_act), self.Vfc_layer_1.weight)
        # Jacobian of second layer wrt input (n_batch x n_hidden x n_input)
        DVfc2_act = torch.bmm(torch.matmul(d_tanh_dx(Vfc2_act), self.Vfc_layer_2.weight), DVfc1_act)
        # Gradient of V wrt input (n_batch x 1 x n_input)
        grad_V = torch.bmm(Vfc2_act.unsqueeze(1), DVfc2_act)

        return V, grad_V

    def forward(self, x):
        """
        Compute the forward pass of the controller

        args:
            x: the state at the current timestep [n_batch, n_dims]
        returns:
            u: the input at the current state [n_batch, n_controls]
            r: the relaxation required to satisfy the CLF inequality
            V: the value of the Lyapunov function at a given point
            Vdot: the time derivative of the Lyapunov function plus self.clf_lambda * V
        """
        # Compute the Lyapunov and barrier functions
        V, grad_V = self.compute_lyapunov(x)
        u_learned = self.compute_controls(x)

        # Compute lie derivatives for each scenario
        L_f_Vs = []
        L_g_Vs = []
        for scenario in self.scenarios:
            f, g = self.dynamics(x, **scenario)
            # Lyapunov Lie derivatives
            L_f_Vs.append(torch.bmm(grad_V, f.unsqueeze(-1)).squeeze(-1))
            L_g_Vs.append(torch.bmm(grad_V, g).squeeze(1))

        # To find the control input, we need to solve a QP
        if self.use_QP:
            result = self.qp_layer(
                *L_f_Vs, *L_g_Vs,
                V.unsqueeze(-1),
                u_learned.squeeze(),
                torch.tensor([self.clf_relaxation_penalty]),
                solver_args={"max_iters": 5000000})
            u = result[0].unsqueeze(-1)
            rs = result[1:]
        else:
            rs = [torch.tensor([0.0])] * len(self.scenarios)
            u = u_learned

        # Accumulate across scenarios
        n_scenarios = len(self.scenarios)
        Vdot = F.relu(L_f_Vs[0].unsqueeze(-1) + torch.bmm(L_g_Vs[0].unsqueeze(1), u)
                      + self.clf_lambda * V)
        relaxation = rs[0]
        for i in range(1, n_scenarios):
            Vdot += F.relu(
                L_f_Vs[i].unsqueeze(-1) + torch.bmm(L_g_Vs[i].unsqueeze(1), u)
                + self.clf_lambda * V)
            relaxation += rs[i]

        Vdot /= n_scenarios
        relaxation /= n_scenarios

        return u, relaxation, V, Vdot


def lyapunov_loss(x,
                  x_goal,
                  safe_mask,
                  unsafe_mask,
                  net,
                  clf_lambda,
                  safe_level=1.0,
                  timestep=0.001,
                  print_loss=False):
    """
    Compute a loss to train the Lyapunov function

    args:
        x: the points at which to evaluate the loss
        x_goal: the origin
        safe_mask: the points in x marked safe
        unsafe_mask: the points in x marked unsafe
        net: a CLF_CBF_QP_Net instance
        clf_lambda: the rate parameter in the CLF condition
        safe_level: defines the safe region as the sublevel set of the lyapunov function
        timestep: the timestep used to compute a finite-difference approximation of the
                  Lyapunov function
        print_loss: True to enable printing the values of component terms
    returns:
        loss: the loss for the given Lyapunov function
    """
    # Compute loss based on...
    loss = 0.0
    #   1.) squared value of the Lyapunov function at the goal
    V0, _ = net.compute_lyapunov(x_goal)
    goal_term = F.relu(V0)**2
    loss += goal_term.mean()

    #   3.) term to encourage V <= safe_level in the safe region
    V_safe, _ = net.compute_lyapunov(x[safe_mask])
    safe_lyap_term = 100 * F.relu(V_safe - safe_level)
    if safe_lyap_term.nelement() > 0:
        loss += safe_lyap_term.mean()

    #   4.) term to encourage V >= safe_level in the unsafe region
    V_unsafe, _ = net.compute_lyapunov(x[unsafe_mask])
    unsafe_lyap_term = 100 * F.relu(safe_level - V_unsafe)
    if unsafe_lyap_term.nelement() > 0:
        loss += unsafe_lyap_term.mean()

    #   5.) A term to encourage satisfaction of CLF condition
    u, r, V, lyap_descent_term_expected = net(x)
    # We compute the change in V in two ways: simulating x forward in time and check if V decreases
    # in each scenario, and using the expected decrease from Vdot
    lyap_descent_term_sim = 0.0
    for s in net.scenarios:
        f, g = net.dynamics(x, **s)
        xdot = f + torch.bmm(g, u).squeeze()
        x_next = x + timestep * xdot
        V_next, _ = net.compute_lyapunov(x_next)
        lyap_descent_term_sim += F.relu(V_next - (1 - clf_lambda * timestep) * V.squeeze())
    loss += lyap_descent_term_sim.mean() + lyap_descent_term_expected.mean()

    #   6.) A term to discourage relaxations of the CLF condition
    loss += r.mean()

    #   7.) Add a term to encourage a local min of CLF at the goal
    tuning_signal = 0.1 * ((x - x_goal.mean(dim=0))**2).mean(dim=-1)
    lyap_tuning_term = F.relu(tuning_signal - V)
    # loss += lyap_tuning_term.mean()

    if print_loss:
        safe_pct_satisfied = (100.0 * (safe_lyap_term == 0)).mean().item()
        unsafe_pct_satisfied = (100.0 * (unsafe_lyap_term == 0)).mean().item()
        descent_pct_satisfied = (100.0 * (lyap_descent_term_sim == 0)).mean().item()
        print(f"                     CLF origin: {goal_term.mean().item()}")
        print(f"           CLF safe region term: {safe_lyap_term.mean().item()}")
        print(f"                  (% satisfied): {safe_pct_satisfied}")
        print(f"         CLF unsafe region term: {unsafe_lyap_term.mean().item()}")
        print(f"                  (% satisfied): {unsafe_pct_satisfied}")
        print(f"               CLF descent term: {lyap_descent_term_sim.mean().item()}")
        print(f"                  (% satisfied): {descent_pct_satisfied}")
        print(f"            CLF relaxation term: {r.mean().item()}")
        print(f"                CLF tuning term: {lyap_tuning_term.mean().item()}")

    return loss


def controller_loss(x, net, print_loss=False, use_nominal=False, use_eq=None, loss_coeff=1e-8):
    """
    Compute a loss to train the filtered controller

    args:
        x: the points at which to evaluate the loss
        net: a CLF_CBF_QP_Net instance
        print_loss: True to enable printing the values of component terms
        use_nominal: if True, compare u_learned to nominal. If false, just penalize norm of u
    returns:
        loss: the loss for the given controller function
    """
    u_learned, _, _, _ = net(x)
    u_learned = u_learned.squeeze()

    if use_nominal:
        # Compute loss based on difference from nominal controller (e.g. LQR).
        u_nominal = net.u_nominal(x, **net.nominal_scenario)
        controller_squared_error = loss_coeff * ((u_nominal - u_learned)**2).sum(dim=-1)
    elif use_eq is not None:
        # compute loss based on difference from equilibrium control
        u_eq = net.u_nominal(use_eq, **net.nominal_scenario)
        controller_squared_error = loss_coeff * ((u_eq - u_learned)**2).sum(dim=-1)
    else:
        controller_squared_error = loss_coeff * (u_learned**2).sum(dim=-1)
    loss = controller_squared_error.mean()

    if print_loss:
        print(f"                controller term: {controller_squared_error.mean().item()}")

    return loss
