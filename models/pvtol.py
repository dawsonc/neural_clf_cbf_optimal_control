import torch
import numpy as np

from models.utils import lqr

# Continuous time planar quadcopter control-affine dynamics are given by x_dot = f(x) + g(x) * u

# For the planar vertical takeoff and landing system (PVTOL), the state variables are
#   x, y, theta, vx, vy, thetadot

# Dynamics from http://underactuated.mit.edu/acrobot.html#section3

# Define parameters of the inverted pendulum
g = 9.81  # gravity
# copter mass lower and upper bounds
low_m = 1.0
high_m = low_m * 1.5
# moment of inertia lower and upper bounds
low_I = 0.01
high_I = low_I * 1.5
r = 0.25  # lever arm
n_dims = 6
n_controls = 2

# Define maximum control input
max_u = 100
# Express this as a matrix inequality G * u <= h
G = torch.tensor([
    [1, 0],
    [-1, 0],
    [0, 1],
    [0, -1],
])
h = torch.tensor([max_u, max_u, max_u, max_u]).T


def f_func(x, m=low_m, inertia=low_I):
    """
    Return the state-dependent part of the continuous-time dynamics for the pvtol system

    x = [[x, z, theta, vx, vz, theta_dot]_1, ...]
    """
    # x is batched, so has dimensions [n_batches, n_dims]. Compute x_dot for each bit
    f = torch.zeros_like(x)

    f[:, 0] = x[:, 3]
    f[:, 1] = x[:, 4]
    f[:, 2] = x[:, 5]
    f[:, 3] = 0.0
    f[:, 4] = -g
    f[:, 5] = 0.0

    return f


def g_func(x, m=low_m, inertia=low_I):
    """
    Return the state-dependent coefficient of the control input for the pvtol system.
    """
    n_batch = x.size()[0]
    g = torch.zeros(n_batch, n_dims, n_controls, dtype=x.dtype)

    # Effect on x acceleration
    g[:, 3, 0] = -torch.sin(x[:, 2]) / m
    g[:, 3, 1] = -torch.sin(x[:, 2]) / m

    # Effect on z acceleration
    g[:, 4, 0] = torch.cos(x[:, 2]) / m
    g[:, 4, 1] = torch.cos(x[:, 2]) / m

    # Effect on heading from rotors
    g[:, 5, 0] = r / inertia
    g[:, 5, 1] = -r / inertia

    return g


def control_affine_dynamics(x, m=low_m, inertia=low_I):
    """
    Return the control-affine dynamics evaluated at the given state

    x = [[x, z, theta, vx, vz, theta_dot]_1, ...]
    """
    return f_func(x, m, inertia), g_func(x, m, inertia)


def u_nominal(x, m=low_m, inertia=low_I):
    """
    Return the nominal controller for the system at state x, given by LQR
    """
    # Linearize the system about the x = 0, u1 = u2 = mg / 2
    A = np.zeros((n_dims, n_dims))
    A[0, 3] = 1.0
    A[1, 4] = 1.0
    A[2, 5] = 1.0
    A[3, 2] = -g

    B = np.zeros((n_dims, n_controls))
    B[4, 0] = 1.0 / m
    B[4, 1] = 1.0 / m
    B[5, 0] = r / inertia
    B[5, 1] = -r / inertia

    # Define cost matrices as identity
    Q = np.eye(n_dims)
    R = np.eye(n_controls)

    # Get feedback matrix
    K = torch.tensor(lqr(A, B, Q, R), dtype=x.dtype)

    # Compute nominal control from feedback + equilibrium control
    u_nominal = -(K @ x.T).T
    u_eq = torch.zeros_like(u_nominal) + m*g / 2.0

    return u_nominal + u_eq
