"""
minimze.py
author: Samuel B. Kachuck
date: January 4, 2018

Minimization routines. Implemented are a Levenberg-Marquardt as in [1] and the
geodesic Levenberg-Marquardt from [2].

References:
[1] Press, Flannery, Teukolsky, and Vetterling. Numerical Recipes.
    Cambridge University Press, Cambridge UK.

[2] Transtrum and Sethna (2012). Improvements to the Levenberg-Marquardt
    algorithm for nonlinear least-squares minimization. Downloaded from
    http://arxiv.org/abs/1201.5885.
"""

import numpy as np

def lm_minimize(f, x0, jac=None, lup=5, ldo=10, fargs=(), fkwargs={}, jargs=(),
                jkwargs={}, keep_steps=False, j=None, r=None):
    SAFE = 0.5

    x = np.atleast_1d(x0)
    if keep_steps:
        xs = [x]
    if r is None:
        r = f(x, *fargs, **fkwargs)
    l = 100
    I = np.eye(len(x))
    if jac is None:
        jac = lambda xp: jacfridr(f, xp, np.ones_like(x), ndim=len(r),
                                    fargs=fargs, fkwargs=fkwargs)
    if j is None:
        j = jac(x, *jargs, **jkwargs)

    C = 0.5*r.dot(r)

    MAXSTEP = 10
    i = 0

    while i<=MAXSTEP: 
        i += 1

        g = j.T.dot(j) + l*I
        gradC = j.T.dot(r)

        xnew = x - SAFE*np.linalg.inv(g).dot(gradC)
        rnew = f(xnew, *fargs, **fkwargs)
        Cnew = 0.5*rnew.dot(rnew)

        if Cnew < C:
            x = xnew
            r = rnew
            Cnew = C
            l = l/ldo

            if keep_steps:
                xs.append(x)
            
            if np.mean(r.dot(r)) < 1e-5:
                if keep_steps:
                    return x, xs, j, r
                else:
                    return x
            else: 
                j = jac(x, *jargs, **jkwargs)

        else:
            l = l*lup
    if keep_steps:
        return x, xs, j, r
    else:
        return x

def geolm_minimize(f, x0, jac=None, lup=5, ldo=10, fargs=(), fkwargs={}, jargs=(),
                jkwargs={}, keep_steps=False, j0=None, r0=None, geo=False):
    """
    Geodesic-accelerated Levenberg-Marquardt for nonlinear least-squares.

    Parameters
    ----------
    f - the function whose squared sum is to be minimized (reqidual function)
    x0 - the starting point for minimizaiont
    jac - jacobian function of the residuals (default is numerical dfridr)
    lup, ldo - Levenberg-Marquardt parameter stepping factor for up (fail) and
        down (success). (Default are 5 and 10, respectively.)
    fargs, fkwargs - arguments for the residual function (default None)
    jargs, jkwargs - arguments for the jacobian function (default None)
    keep_steps - Boolean for keeping intermediary steps (defautl False)
    j0, r0 - initial residual and jacobian values (default is to recompute).
    geo - Boolean for whether to use geodesic acceleration.

    Returns
    -------
    If keep_steps is False, the location of the minimum. Otherwise, (x, xs, j,r)
        a tuple of the location of the minimum (x), the steps (xs), the final
        jacobian (j) and the final residuals (r).
    """
    SAFE = 0.5
    ALPHA = 0.75
    h=0.1

    x = np.atleast_1d(x0)
    r = r0 or f(x, *fargs, **fkwargs)

    if keep_steps:
        xs = [x]
        rs = [r]

    l = 100
    I = np.eye(len(x))
    if jac is None:
        jac = lambda xp: jacfridr(f, xp, np.ones_like(x), ndim=len(r),
                                    fargs=fargs, fkwargs=fkwargs)
    j = j0 or jac(x, *jargs, **jkwargs)

    C = 0.5*r.dot(r)

    MAXSTEP = 100
    MAXJEVAL = 15
    MAXFEVAL = 200
    jevals = 0
    fevals = 0

    i = 0

    while i<=MAXSTEP and jevals <= MAXJEVAL and fevals <= MAXFEVAL: 
        i += 1

        g = j.T.dot(j) + l*I
        gradC = j.T.dot(r)

        gi = np.linalg.inv(g)

        dx1 = - gi.dot(gradC)

        if not geo:
            dx2 = 0
        else:
            k = 2/h*((f(x + h*dx1, *fargs, **fkwargs) - r)/h - j.dot(dx1))
            fevals += 1
            dx2 = - 0.5*gi.dot(j.T.dot(k))

            truncerr = 2*np.sqrt(dx2.dot(dx2))/np.sqrt(dx1.dot(dx1))
            print(truncerr)

        xnew = x + SAFE*(dx1 + 0.5*dx2)
        rnew = f(xnew, *fargs, **fkwargs)
        fevals += 1
        Cnew = 0.5*rnew.dot(rnew)

        if not geo:
            accept = (Cnew < C)
        else:
            accept = (Cnew < C and truncerr < ALPHA)

        if accept:
            x = xnew
            r = rnew
            Cnew = C
            l = l/ldo

            if keep_steps:
                xs.append(x)
                rs.append(r)
            
            if np.mean(r.dot(r)) < 1e-5:
                if keep_steps:
                    return x, i, xs, rs, j, r, fevals, jevals
                else:
                    return x
            else: 
                j = jac(x, *jargs, **jkwargs)
                jevals += 1

        else:
            l = l*lup
    if keep_steps:
        return x, i, xs, rs, j, r, fevals, jevals
    else:
        return x


