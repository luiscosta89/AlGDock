# Implementation of the Tricubic Interpolation Algorithm for AlGDock

# Author: Luis Antonio Leite Francisco da Costa

#
# Get all the required declarations
#
include "MMTK/python.pxi"
include "MMTK/numeric.pxi"
include "MMTK/core.pxi"
include "MMTK/universe.pxi"
include 'MMTK/forcefield.pxi'
from Scientific.Geometry import Vector, ex, ey, ez

import gzip

import numpy as np
cimport numpy as np

ctypedef np.float_t float_t
ctypedef np.int_t int_t

Eigen::Matrix<fptype,64,64> _C;
Eigen::Matrix<fptype,64,1> _coefs;

#
# The force field term implementation.
# The rules:
#
# - The class must inherit from EnergyTerm.
#
# - EnergyTerm.__init__() must be called with the arguments
#   shown here. The third argument is the name of the EnergyTerm
#   object, the fourth a tuple of the names of all the terms it
#   implements (one object can implement several terms).
#   The assignment to self.eval_func is essential, without it
#   any energy evaluation will crash.
#
# - The function "evaluate" must have exactly the parameter
#   list given in this example.
#
cdef class TricubicGridTerm(EnergyTerm):
  cdef char* grid_name
  cdef np.ndarray scaling_factor, vals, counts, spacing, hCorner
  cdef int npts, nyz, natoms
  cdef float_t max_val, strength, k
  # The __init__ method remembers parameters and loads the potential
  # file. Note that EnergyTerm.__init__ takes care of storing the
  # name and the universe object.

  cdef float_t tricubicInterpolate(self, float_t p[4], float_t x, float_t y, float_t z):
    return (0.5*p[0]-0.25*p[1]+0.125*p[2]-p[3] + \
      x*(-0.5*p[0]+0.25*p[1]-0.125*p[2]+0.625*p[3] + \
      y*(0.5*p[0]-0.25*p[1]+0.125*p[2]-0.625*p[3] + \
      z*(-0.5*p[0]+0.25*p[1]-0.125*p[2]+0.625*p[3]))))/6
 
  # Calculate the first derivatives estimatives interpolation
  cdef float_t derivativeIntp(self, float_t p[4], float_t x, float_t y, float_t z):
    return (-0.1*p[0]+0.5*p[1]-0.25*p[2]+0.125*p[3] + \
      x*(0.1*p[0]-0.5*p[1]+0.25*p[2]-0.125*p[3] + \
      x*(-0.1*p[0]+0.5*p[1]-0.25*p[2]+0.125*p[3])))/6
	  
  cdef float_t tricubicInterpolate(list xyz):
	  fptype x = extract<fptype>(xyz[0]);
	  fptype y = extract<fptype>(xyz[1]);
	  fptype z = extract<fptype>(xyz[2]);
	  
	  fptype dx = fmod(x/_spacing, _n1), dy = fmod(y/_spacing, _n2), dz = fmod(z/_spacing, _n3); //determine the relative position in the box enclosed by nearest data points
	  
	  if(dx < 0) dx += _n1; #periodicity is built in
	  if(dy < 0) dy += _n2;
	  if(dz < 0) dz += _n3;
	  
	  int xi = (int)floor(dx); #calculate lower-bound grid indices
	  int yi = (int)floor(dy);
	  int zi = (int)floor(dz);

    #temporary array is necessary, otherwise compiler has problems with Eigen and takes very long to compile
    int temp[64][64] = {
    { 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {-3, 3, 0, 0, 0, 0, 0, 0,-2,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 2,-2, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {-3, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0,-3, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 9,-9,-9, 9, 0, 0, 0, 0, 6, 3,-6,-3, 0, 0, 0, 0, 6,-6, 3,-3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 2, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {-6, 6, 6,-6, 0, 0, 0, 0,-3,-3, 3, 3, 0, 0, 0, 0,-4, 4,-2, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2,-2,-1,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 2, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 2, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {-6, 6, 6,-6, 0, 0, 0, 0,-4,-2, 4, 2, 0, 0, 0, 0,-3, 3,-3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2,-1,-2,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 4,-4,-4, 4, 0, 0, 0, 0, 2, 2,-2,-2, 0, 0, 0, 0, 2,-2, 2,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 3, 0, 0, 0, 0, 0, 0,-2,-1, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2,-2, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-1, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 9,-9,-9, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 3,-6,-3, 0, 0, 0, 0, 6,-6, 3,-3, 0, 0, 0, 0, 4, 2, 2, 1, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-6, 6, 6,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3,-3, 3, 3, 0, 0, 0, 0,-4, 4,-2, 2, 0, 0, 0, 0,-2,-2,-1,-1, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-6, 6, 6,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-4,-2, 4, 2, 0, 0, 0, 0,-3, 3,-3, 3, 0, 0, 0, 0,-2,-1,-2,-1, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4,-4,-4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 2,-2,-2, 0, 0, 0, 0, 2,-2, 2,-2, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0},
    {-3, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0, 0, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0,-3, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0, 0, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 9,-9, 0, 0,-9, 9, 0, 0, 6, 3, 0, 0,-6,-3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6,-6, 0, 0, 3,-3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 2, 0, 0, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {-6, 6, 0, 0, 6,-6, 0, 0,-3,-3, 0, 0, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-4, 4, 0, 0,-2, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2,-2, 0, 0,-1,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0, 0, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0, 0, 0,-1, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 9,-9, 0, 0,-9, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 3, 0, 0,-6,-3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6,-6, 0, 0, 3,-3, 0, 0, 4, 2, 0, 0, 2, 1, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-6, 6, 0, 0, 6,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3,-3, 0, 0, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-4, 4, 0, 0,-2, 2, 0, 0,-2,-2, 0, 0,-1,-1, 0, 0},
    { 9, 0,-9, 0,-9, 0, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 0, 3, 0,-6, 0,-3, 0, 6, 0,-6, 0, 3, 0,-3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 0, 2, 0, 2, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 9, 0,-9, 0,-9, 0, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 0, 3, 0,-6, 0,-3, 0, 6, 0,-6, 0, 3, 0,-3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 0, 2, 0, 2, 0, 1, 0},
    {-27,27,27,-27,27,-27,-27,27,-18,-9,18, 9,18, 9,-18,-9,-18,18,-9, 9,18,-18, 9,-9,-18,18,18,-18,-9, 9, 9,-9,-12,-6,-6,-3,12, 6, 6, 3,-12,-6,12, 6,-6,-3, 6, 3,-12,12,-6, 6,-6, 6,-3, 3,-8,-4,-4,-2,-4,-2,-2,-1},
    {18,-18,-18,18,-18,18,18,-18, 9, 9,-9,-9,-9,-9, 9, 9,12,-12, 6,-6,-12,12,-6, 6,12,-12,-12,12, 6,-6,-6, 6, 6, 6, 3, 3,-6,-6,-3,-3, 6, 6,-6,-6, 3, 3,-3,-3, 8,-8, 4,-4, 4,-4, 2,-2, 4, 4, 2, 2, 2, 2, 1, 1},
    {-6, 0, 6, 0, 6, 0,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 0,-3, 0, 3, 0, 3, 0,-4, 0, 4, 0,-2, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-2, 0,-1, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0,-6, 0, 6, 0, 6, 0,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 0,-3, 0, 3, 0, 3, 0,-4, 0, 4, 0,-2, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-2, 0,-1, 0,-1, 0},
    {18,-18,-18,18,-18,18,18,-18,12, 6,-12,-6,-12,-6,12, 6, 9,-9, 9,-9,-9, 9,-9, 9,12,-12,-12,12, 6,-6,-6, 6, 6, 3, 6, 3,-6,-3,-6,-3, 8, 4,-8,-4, 4, 2,-4,-2, 6,-6, 6,-6, 3,-3, 3,-3, 4, 2, 4, 2, 2, 1, 2, 1},
    {-12,12,12,-12,12,-12,-12,12,-6,-6, 6, 6, 6, 6,-6,-6,-6, 6,-6, 6, 6,-6, 6,-6,-8, 8, 8,-8,-4, 4, 4,-4,-3,-3,-3,-3, 3, 3, 3, 3,-4,-4, 4, 4,-2,-2, 2, 2,-4, 4,-4, 4,-2, 2,-2, 2,-2,-2,-2,-2,-1,-1,-1,-1},
    { 2, 0, 0, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {-6, 6, 0, 0, 6,-6, 0, 0,-4,-2, 0, 0, 4, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 3, 0, 0,-3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2,-1, 0, 0,-2,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 4,-4, 0, 0,-4, 4, 0, 0, 2, 2, 0, 0,-2,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2,-2, 0, 0, 2,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-6, 6, 0, 0, 6,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-4,-2, 0, 0, 4, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-3, 3, 0, 0,-3, 3, 0, 0,-2,-1, 0, 0,-2,-1, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4,-4, 0, 0,-4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 0, 0,-2,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2,-2, 0, 0, 2,-2, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0},
    {-6, 0, 6, 0, 6, 0,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0,-4, 0,-2, 0, 4, 0, 2, 0,-3, 0, 3, 0,-3, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-1, 0,-2, 0,-1, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0,-6, 0, 6, 0, 6, 0,-6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,-4, 0,-2, 0, 4, 0, 2, 0,-3, 0, 3, 0,-3, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0,-2, 0,-1, 0,-2, 0,-1, 0},
    {18,-18,-18,18,-18,18,18,-18,12, 6,-12,-6,-12,-6,12, 6,12,-12, 6,-6,-12,12,-6, 6, 9,-9,-9, 9, 9,-9,-9, 9, 8, 4, 4, 2,-8,-4,-4,-2, 6, 3,-6,-3, 6, 3,-6,-3, 6,-6, 3,-3, 6,-6, 3,-3, 4, 2, 2, 1, 4, 2, 2, 1},
    {-12,12,12,-12,12,-12,-12,12,-6,-6, 6, 6, 6, 6,-6,-6,-8, 8,-4, 4, 8,-8, 4,-4,-6, 6, 6,-6,-6, 6, 6,-6,-4,-4,-2,-2, 4, 4, 2, 2,-3,-3, 3, 3,-3,-3, 3, 3,-4, 4,-2, 2,-4, 4,-2, 2,-2,-2,-1,-1,-2,-2,-1,-1},
    { 4, 0,-4, 0,-4, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 2, 0,-2, 0,-2, 0, 2, 0,-2, 0, 2, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    { 0, 0, 0, 0, 0, 0, 0, 0, 4, 0,-4, 0,-4, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 2, 0,-2, 0,-2, 0, 2, 0,-2, 0, 2, 0,-2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0},
    {-12,12,12,-12,12,-12,-12,12,-8,-4, 8, 4, 8, 4,-8,-4,-6, 6,-6, 6, 6,-6, 6,-6,-6, 6, 6,-6,-6, 6, 6,-6,-4,-2,-4,-2, 4, 2, 4, 2,-4,-2, 4, 2,-4,-2, 4, 2,-3, 3,-3, 3,-3, 3,-3, 3,-2,-1,-2,-1,-2,-1,-2,-1},
    { 8,-8,-8, 8,-8, 8, 8,-8, 4, 4,-4,-4,-4,-4, 4, 4, 4,-4, 4,-4,-4, 4,-4, 4, 4,-4,-4, 4, 4,-4,-4, 4, 2, 2, 2, 2,-2,-2,-2,-2, 2, 2,-2,-2, 2, 2,-2,-2, 2,-2, 2,-2, 2,-2, 2,-2, 1, 1, 1, 1, 1, 1, 1, 1}
  };

  for(int i=0;i<64;i++){
    for(int j=0;j<64;j++){
      _C(i,j) = temp[i][j];
    }
  }

  # Convert voxel values and partial derivatives to interpolation coefficients.
    _coefs = _C * x;
	  
	  #Numeric derivatives
	  cdef float_t dfdx[8] = {0.5*(_data[_index(xi+1,yi,zi)]-_data[_index(xi-1,yi,zi)]),
							  0.5*(_data[_index(xi+2,yi,zi)]-_data[_index(xi,yi,zi)]),
							  0.5*(_data[_index(xi+1,yi+1,zi)]-_data[_index(xi-1,yi+1,zi)]),
							  0.5*(_data[_index(xi+2,yi+1,zi)]-_data[_index(xi,yi+1,zi)]),
							  0.5*(_data[_index(xi+1,yi,zi+1)]-_data[_index(xi-1,yi,zi+1)]),
							  0.5*(_data[_index(xi+2,yi,zi+1)]-_data[_index(xi,yi,zi+1)]),
							  0.5*(_data[_index(xi+1,yi+1,zi+1)]-_data[_index(xi-1,yi+1,zi+1)]),
							  0.5*(_data[_index(xi+2,yi+1,zi+1)]-_data[_index(xi,yi+1,zi+1)])};
							  
	  cdef float_t dfdy[8] = {0.5*(_data[_index(xi,yi+1,zi)]-_data[_index(xi,yi-1,zi)]),
							  0.5*(_data[_index(xi+1,yi+1,zi)]-_data[_index(xi+1,yi-1,zi)]),
							  0.5*(_data[_index(xi,yi+2,zi)]-_data[_index(xi,yi,zi)]),
							  0.5*(_data[_index(xi+1,yi+2,zi)]-_data[_index(xi+1,yi,zi)]),
							  0.5*(_data[_index(xi,yi+1,zi+1)]-_data[_index(xi,yi-1,zi+1)]),
							  0.5*(_data[_index(xi+1,yi+1,zi+1)]-_data[_index(xi+1,yi-1,zi+1)]),
							  0.5*(_data[_index(xi,yi+2,zi+1)]-_data[_index(xi,yi,zi+1)]),
							  0.5*(_data[_index(xi+1,yi+2,zi+1)]-_data[_index(xi+1,yi,zi+1)])};
							  
	  cdef float_t dfdz[8] = {0.5*(_data[_index(xi,yi,zi+1)]-_data[_index(xi,yi,zi-1)]),
							  0.5*(_data[_index(xi+1,yi,zi+1)]-_data[_index(xi+1,yi,zi-1)]),
							  0.5*(_data[_index(xi,yi+1,zi+1)]-_data[_index(xi,yi+1,zi-1)]),
							  0.5*(_data[_index(xi+1,yi+1,zi+1)]-_data[_index(xi+1,yi+1,zi-1)]),
							  0.5*(_data[_index(xi,yi,zi+2)]-_data[_index(xi,yi,zi)]),
							  0.5*(_data[_index(xi+1,yi,zi+2)]-_data[_index(xi+1,yi,zi)]),
							  0.5*(_data[_index(xi,yi+1,zi+2)]-_data[_index(xi,yi+1,zi)]),
							  0.5*(_data[_index(xi+1,yi+1,zi+2)]-_data[_index(xi+1,yi+1,zi)])};
	  
	  cdef float_t d2fdxdy[8] = {0.25*(_data[_index(xi+1,yi+1,zi)]-_data[_index(xi-1,yi+1,zi)]-_data[_index(xi+1,yi-1,zi)]+_data[_index(xi-1,yi-1,zi)]),
							  0.25*(_data[_index(xi+2,yi+1,zi)]-_data[_index(xi,yi+1,zi)]-_data[_index(xi+2,yi-1,zi)]+_data[_index(xi,yi-1,zi)]),
							  0.25*(_data[_index(xi+1,yi+2,zi)]-_data[_index(xi-1,yi+2,zi)]-_data[_index(xi+1,yi,zi)]+_data[_index(xi-1,yi,zi)]),
							  0.25*(_data[_index(xi+2,yi+2,zi)]-_data[_index(xi,yi+2,zi)]-_data[_index(xi+2,yi,zi)]+_data[_index(xi,yi,zi)]),
							  0.25*(_data[_index(xi+1,yi+1,zi+1)]-_data[_index(xi-1,yi+1,zi+1)]-_data[_index(xi+1,yi-1,zi+1)]+_data[_index(xi-1,yi-1,zi+1)]),
							  0.25*(_data[_index(xi+2,yi+1,zi+1)]-_data[_index(xi,yi+1,zi+1)]-_data[_index(xi+2,yi-1,zi+1)]+_data[_index(xi,yi-1,zi+1)]),
							  0.25*(_data[_index(xi+1,yi+2,zi+1)]-_data[_index(xi-1,yi+2,zi+1)]-_data[_index(xi+1,yi,zi+1)]+_data[_index(xi-1,yi,zi+1)]),
							  0.25*(_data[_index(xi+2,yi+2,zi+1)]-_data[_index(xi,yi+2,zi+1)]-_data[_index(xi+2,yi,zi+1)]+_data[_index(xi,yi,zi+1)])};
	  
	  cdef float_t d2fdxdz[8] = {0.25*(_data[_index(xi+1,yi,zi+1)]-_data[_index(xi-1,yi,zi+1)]-_data[_index(xi+1,yi,zi-1)]+_data[_index(xi-1,yi,zi-1)]),
							  0.25*(_data[_index(xi+2,yi,zi+1)]-_data[_index(xi,yi,zi+1)]-_data[_index(xi+2,yi,zi-1)]+_data[_index(xi,yi,zi-1)]),
							  0.25*(_data[_index(xi+1,yi+1,zi+1)]-_data[_index(xi-1,yi+1,zi+1)]-_data[_index(xi+1,yi+1,zi-1)]+_data[_index(xi-1,yi+1,zi-1)]),
							  0.25*(_data[_index(xi+2,yi+1,zi+1)]-_data[_index(xi,yi+1,zi+1)]-_data[_index(xi+2,yi+1,zi-1)]+_data[_index(xi,yi+1,zi-1)]),
							  0.25*(_data[_index(xi+1,yi,zi+2)]-_data[_index(xi-1,yi,zi+2)]-_data[_index(xi+1,yi,zi)]+_data[_index(xi-1,yi,zi)]),
							  0.25*(_data[_index(xi+2,yi,zi+2)]-_data[_index(xi,yi,zi+2)]-_data[_index(xi+2,yi,zi)]+_data[_index(xi,yi,zi)]),
							  0.25*(_data[_index(xi+1,yi+1,zi+2)]-_data[_index(xi-1,yi+1,zi+2)]-_data[_index(xi+1,yi+1,zi)]+_data[_index(xi-1,yi+1,zi)]),
							  0.25*(_data[_index(xi+2,yi+1,zi+2)]-_data[_index(xi,yi+1,zi+2)]-_data[_index(xi+2,yi+1,zi)]+_data[_index(xi,yi+1,zi)])};
	  
	  cdef float_t d2fdydz[8] = {0.25*(_data[_index(xi,yi+1,zi+1)]-_data[_index(xi,yi-1,zi+1)]-_data[_index(xi,yi+1,zi-1)]+_data[_index(xi,yi-1,zi-1)]),
							  0.25*(_data[_index(xi+1,yi+1,zi+1)]-_data[_index(xi+1,yi-1,zi+1)]-_data[_index(xi+1,yi+1,zi-1)]+_data[_index(xi+1,yi-1,zi-1)]),
							  0.25*(_data[_index(xi,yi+2,zi+1)]-_data[_index(xi,yi,zi+1)]-_data[_index(xi,yi+2,zi-1)]+_data[_index(xi,yi,zi-1)]),
							  0.25*(_data[_index(xi+1,yi+2,zi+1)]-_data[_index(xi+1,yi,zi+1)]-_data[_index(xi+1,yi+2,zi-1)]+_data[_index(xi+1,yi,zi-1)]),
							  0.25*(_data[_index(xi,yi+1,zi+2)]-_data[_index(xi,yi-1,zi+2)]-_data[_index(xi,yi+1,zi)]+_data[_index(xi,yi-1,zi)]),
							  0.25*(_data[_index(xi+1,yi+1,zi+2)]-_data[_index(xi+1,yi-1,zi+2)]-_data[_index(xi+1,yi+1,zi)]+_data[_index(xi+1,yi-1,zi)]),
							  0.25*(_data[_index(xi,yi+2,zi+2)]-_data[_index(xi,yi,zi+2)]-_data[_index(xi,yi+2,zi)]+_data[_index(xi,yi,zi)]),
							  0.25*(_data[_index(xi+1,yi+2,zi+2)]-_data[_index(xi+1,yi,zi+2)]-_data[_index(xi+1,yi+2,zi)]+_data[_index(xi+1,yi,zi)])};
  
	  cdef float_t d3fdxdydz[8] = {0.125*(_data[_index(xi+1,yi+1,zi+1)]-_data[_index(xi-1,yi+1,zi+1)]-_data[_index(xi+1,yi-1,zi+1)]+_data[_index(xi-1,yi-1,zi+1)]-_data[_index(xi+1,yi+1,zi-1)]+_data[_index(xi-1,yi+1,zi-1)]+_data[_index(xi+1,yi-1,zi-1)]-_data[_index(xi-1,yi-1,zi-1)]),
								  0.125*(_data[_index(xi+2,yi+1,zi+1)]-_data[_index(xi,yi+1,zi+1)]-_data[_index(xi+2,yi-1,zi+1)]+_data[_index(xi,yi-1,zi+1)]-_data[_index(xi+2,yi+1,zi-1)]+_data[_index(xi,yi+1,zi-1)]+_data[_index(xi+2,yi-1,zi-1)]-_data[_index(xi,yi-1,zi-1)]),
								  0.125*(_data[_index(xi+1,yi+2,zi+1)]-_data[_index(xi-1,yi+2,zi+1)]-_data[_index(xi+1,yi,zi+1)]+_data[_index(xi-1,yi,zi+1)]-_data[_index(xi+1,yi+2,zi-1)]+_data[_index(xi-1,yi+2,zi-1)]+_data[_index(xi+1,yi,zi-1)]-_data[_index(xi-1,yi,zi-1)]),
								  0.125*(_data[_index(xi+2,yi+2,zi+1)]-_data[_index(xi,yi+2,zi+1)]-_data[_index(xi+2,yi,zi+1)]+_data[_index(xi,yi,zi+1)]-_data[_index(xi+2,yi+2,zi-1)]+_data[_index(xi,yi+2,zi-1)]+_data[_index(xi+2,yi,zi-1)]-_data[_index(xi,yi,zi-1)]),
								  0.125*(_data[_index(xi+1,yi+1,zi+2)]-_data[_index(xi-1,yi+1,zi+2)]-_data[_index(xi+1,yi-1,zi+2)]+_data[_index(xi-1,yi-1,zi+2)]-_data[_index(xi+1,yi+1,zi)]+_data[_index(xi-1,yi+1,zi)]+_data[_index(xi+1,yi-1,zi)]-_data[_index(xi-1,yi-1,zi)]),
								  0.125*(_data[_index(xi+2,yi+1,zi+2)]-_data[_index(xi,yi+1,zi+2)]-_data[_index(xi+2,yi-1,zi+2)]+_data[_index(xi,yi-1,zi+2)]-_data[_index(xi+2,yi+1,zi)]+_data[_index(xi,yi+1,zi)]+_data[_index(xi+2,yi-1,zi)]-_data[_index(xi,yi-1,zi)]),
								  0.125*(_data[_index(xi+1,yi+2,zi+2)]-_data[_index(xi-1,yi+2,zi+2)]-_data[_index(xi+1,yi,zi+2)]+_data[_index(xi-1,yi,zi+2)]-_data[_index(xi+1,yi+2,zi)]+_data[_index(xi-1,yi+2,zi)]+_data[_index(xi+1,yi,zi)]-_data[_index(xi-1,yi,zi)]),
								  0.125*(_data[_index(xi+2,yi+2,zi+2)]-_data[_index(xi,yi+2,zi+2)]-_data[_index(xi+2,yi,zi+2)]+_data[_index(xi,yi,zi+2)]-_data[_index(xi+2,yi+2,zi)]+_data[_index(xi,yi+2,zi)]+_data[_index(xi+2,yi,zi)]-_data[_index(xi,yi,zi)])};
								  
	# Convert voxel values and partial derivatives to interpolation coefficients.
    _coefs = _C * x;
    # Remember this voxel for next time.
    _i1 = xi;
    _i2 = yi;
    _i3 = zi;
    _initialized = true;
  
	  # Evaluate the interpolation within this grid voxel.
	  dx -= xi;
	  dy -= yi;
	  dz -= zi;
	  int ijkn(0);
	  fptype dzpow(1);
	  fptype result(0);
	  for(int k = 0; k < 4; ++k) {
		fptype dypow(1);
		for(int j = 0; j < 4; ++j) {
		  result += dypow*dzpow*(_coefs[ijkn] + dx*(_coefs[ijkn+1] + dx*(_coefs[ijkn+2] + dx*_coefs[ijkn+3])));
		  ijkn += 4;
		  dypow *= dy;
		}
		dzpow *= dz;
	  }
	  return result;

  
  # Suggestion to make the derivatives: 
  # Use the four points in the BSPline, but make an addition, for example, add the 0 with the 1. 
  # For the interpolator, it can be used to get the module, in this case

  # Values of df/dx
  cdef float_t dfdx(self, float_t* p[4], float_t x):
    cdef float_t arr[4]
    arr[0] = self.tricubicInterpolate(p[0], x, 0, 0) 
    arr[1] = self.tricubicInterpolate(p[1], x, 0, 0) 
    arr[2] = self.tricubicInterpolate(p[2], x, 0, 0) 
    arr[3] = self.tricubicInterpolate(p[3], x, 0, 0)
    return self.derivativeIntp(arr, x, 0, 0)
  
  # Values of df/dy
  cdef float_t dfdy(self, float_t* p[4], float_t y):
    cdef float_t arr[4]
    arr[0] = self.tricubicInterpolate(p[0], 0, y, 0) 
    arr[1] = self.tricubicInterpolate(p[1], 0, y, 0) 
    arr[2] = self.tricubicInterpolate(p[2], 0, y, 0) 
    arr[3] = self.tricubicInterpolate(p[3], 0, y, 0)
    return self.derivativeIntp(arr, 0, y, 0)    

  # Values of df/dz
  cdef float_t dfdz(self, float_t* p[4], float_t z):
    cdef float_t arr[4]
    arr[0] = self.tricubicInterpolate(p[0], 0, 0, z) 
    arr[1] = self.tricubicInterpolate(p[1], 0, 0, z)  
    arr[2] = self.tricubicInterpolate(p[2], 0, 0, z) 
    arr[3] = self.tricubicInterpolate(p[3], 0, 0, z)   
    return self.derivativeIntp(arr, 0, 0, z)

  # Values of d2f/dxdy
  cdef float_t d2fdxdy(self, float_t* p[4][4], float_t x, float_t y):
    cdef float_t arr[4]
    arr[0] = self.tricubicInterpolate(p[0], x, y, 0) 
    arr[1] = self.tricubicInterpolate(p[1], x, y, 0)  
    arr[2] = self.tricubicInterpolate(p[2], x, y, 0) 
    arr[3] = self.tricubicInterpolate(p[3], x, y, 0)      
    return self.derivativeIntp(arr, x, y, 0)

  # Values of d2f/dxdz
  cdef float_t d2fdxdz(self, float_t* p[4][4], float_t x, float_t z):
    cdef float_t arr[4]
    arr[0] = self.tricubicInterpolate(p[0], x, 0, z) 
    arr[1] = self.tricubicInterpolate(p[1], x, 0, z)  
    arr[2] = self.tricubicInterpolate(p[2], x, 0, z) 
    arr[3] = self.tricubicInterpolate(p[3], x, 0, z) 
    return self.derivativeIntp(arr, x, 0, z)

  # Values of d2f/dydz
  cdef float_t d2fdydz(self, float_t* p[4][4], float_t y, float_t z):
    cdef float_t arr[4]
    arr[0] = self.tricubicInterpolate(p[0], 0, y, z) 
    arr[1] = self.tricubicInterpolate(p[1], 0, y, z)  
    arr[2] = self.tricubicInterpolate(p[2], 0, y, z) 
    arr[3] = self.tricubicInterpolate(p[3], 0, y, z)
    return self.derivativeIntp(arr, 0, y, z)

  # Values of d3f/dxdydz
  cdef float_t d3fdxdydz(self, float_t* p[4][4][4], float_t x, float_t y, float_t z):
    cdef float_t arr[4]
    arr[0] = self.tricubicInterpolate(p[0], x, y, z) 
    arr[1] = self.tricubicInterpolate(p[1], x, y, z)  
    arr[2] = self.tricubicInterpolate(p[2], x, y, z) 
    arr[3] = self.tricubicInterpolate(p[3], x, y, z)
    return self.derivativeIntp(arr, x, y, z)

  def __init__(self, universe, spacing, counts, vals, strength, \
    scaling_factor, grid_name, max_val):
      EnergyTerm.__init__(self, universe,
                          grid_name, (grid_name,))
      self.eval_func = <void *>TricubicGridTerm.evaluate

      self.strength = strength
      self.scaling_factor = np.array(scaling_factor, dtype=float)
      self.natoms = len(self.scaling_factor)
      self.grid_name = grid_name
      self.max_val = max_val

      self.spacing = spacing
      self.counts = counts
      self.vals = vals
      self.nyz = self.counts[1]*self.counts[2]
      self.hCorner = np.array((self.spacing[0]*(self.counts[0]-1),
                      self.spacing[1]*(self.counts[1]-1),
                      self.spacing[2]*(self.counts[2]-1)), dtype=float)
      # To keep atoms within the grid
      self.k = 10000. # kJ/mol nm**2

      # "Cap" the grid values
      if max_val>0.0:
        self.vals = max_val*np.tanh(self.vals/max_val)
      else:
        self.vals = np.copy(vals)

  # This method is called for every single energy evaluation, so make
  # it as efficient as possible. The parameters do_gradients and
  # do_force_constants are flags that indicate if gradients and/or
  # force constants are requested.
  cdef void evaluate(self, PyFFEvaluatorObject *eval, \
    energy_spec *input, energy_data *energy):

      # Input
      cdef vector3 *coordinates
      cdef float_t *scaling_factor
      cdef float_t *vals
      cdef int_t *counts
      cdef float_t *spacing
      cdef float_t *hCorner
      # Output
      cdef float_t gridEnergy
      cdef vector3 *gradients
      cdef np.ndarray[float_t, ndim=4] force_constants

      # Processing
      cdef int i, ix, iy, iz, atom_index

      #Initialize the 4*4*4 matrix to store the energy value
      cdef float_t vertex[4][4][4]

      #Initialize a hessian matrix
      #cdef float_t hessian[3][3]

      cdef float_t dvdx, dvdy, dvdz
      cdef float_t dvdxdx, dvdxdy, dvdxdz,dvdydy,dvdydz,dvdzdz

      gridEnergy = 0
      coordinates = <vector3 *>input.coordinates.data

      # Pointers to numpy arrays for faster indexing
      scaling_factor = <float_t *>self.scaling_factor.data
      vals = <float_t *>self.vals.data
      counts = <int_t *>self.counts.data
      spacing = <float_t *>self.spacing.data
      hCorner = <float_t *>self.hCorner.data

      # Initialize variables
      if energy.gradients != NULL:
        gradients = <vector3 *>(<PyArrayObject *> energy.gradients).data

      # Initialize variables
      if energy.force_constants != NULL:
          force_constants = <np.ndarray[float_t, ndim=4] >(<PyArrayObject *> energy.force_constants)

      indicies = [ai for ai in range(self.natoms) if self.scaling_factor[ai]!=0]
      for atom_index in indicies:
        # Check to make sure coordinate is in grid
        if (coordinates[atom_index][0]>0 and 
            coordinates[atom_index][1]>0 and 
            coordinates[atom_index][2]>0 and
            coordinates[atom_index][0]<hCorner[0] and
            coordinates[atom_index][1]<hCorner[1] and
            coordinates[atom_index][2]<hCorner[2]):

          # Index within the grid
          ix = int(coordinates[atom_index][0]/spacing[0]-1)
          iy = int(coordinates[atom_index][1]/spacing[1]-1)
          iz = int(coordinates[atom_index][2]/spacing[2]-1)
          
          i = ix*self.nyz + iy*counts[2] + iz

          for x in range(0,4):
              for y in range(0,4):
                  for z in range(0,4):
                      vertex[x][y][z]=vals[i+x*self.nyz+y*counts[2]+z]

         # Fraction within the box
          fx = (coordinates[atom_index][0] - (ix*spacing[0]))/spacing[0]
          fy = (coordinates[atom_index][1] - (iy*spacing[1]))/spacing[1]
          fz = (coordinates[atom_index][2] - (iz*spacing[2]))/spacing[2]

          gridEnergy += scaling_factor[atom_index]*self.tricubicInterpolate(vertex,fx,fy,fz)

          # Hessian
          if energy.force_constants !=NULL:
             # x direction
            dvdxdx = self.dfdx(vertex, fx)
            dvdxdy = self.df2dxdy(vertex, fx, fy)
            dvdxdz = self.df2dxdz(vertex, fx, fz)
            # y direction
            dvdydy = self.dfdy(vertex, fy)
            dvdydz = self.df2dydz(vertex, fy, fz)
            # z direction
            dvdzdz = self.dfdz(vertex, fz) + self.d3fdxdydz(vertex, fx, fy, fz)
            
            force_constants[atom_index][0][atom_index][0] += self.strength*scaling_factor[atom_index]*dvdxdx/spacing[0]/spacing[0]
            force_constants[atom_index][1][atom_index][1] += self.strength*scaling_factor[atom_index]*dvdydy/spacing[1]/spacing[1]
            force_constants[atom_index][2][atom_index][2] += self.strength*scaling_factor[atom_index]*dvdzdz/spacing[2]/spacing[2]
            force_constants[atom_index][1][atom_index][0] += self.strength*scaling_factor[atom_index]*dvdxdy/spacing[0]/spacing[1]
            force_constants[atom_index][2][atom_index][0] += self.strength*scaling_factor[atom_index]*dvdxdz/spacing[0]/spacing[2]
            force_constants[atom_index][2][atom_index][1] += self.strength*scaling_factor[atom_index]*dvdydz/spacing[1]/spacing[2]
            force_constants[atom_index][0][atom_index][1] = force_constants[atom_index][1][atom_index][0]
            force_constants[atom_index][0][atom_index][2] = force_constants[atom_index][2][atom_index][0]
            force_constants[atom_index][1][atom_index][2] = force_constants[atom_index][2][atom_index][1]

          if energy.gradients != NULL:
            # x coordinate
            dvdx = self.dfdx(vertex, fx) + self.df2dxdy(vertex, fx, fy) + self.df2dxdz(vertex, fx, fz)
            # y coordinate
            dvdy = self.dfdy(vertex, fy) + self.df2dxdy(vertex, fx, fy) + self.df2dydz(vertex, fy, fz)
            # z coordinate
            dvdz = self.dfdz(vertex, fz) + self.df2dxdz(vertex, fx, fz) + self.df2dydz(vertex, fy, fz) + self.d3fdxdydz(vertex, fx, fy, fz)

            gradients[atom_index][0] += self.strength*scaling_factor[atom_index]*dvdx/spacing[0]
            gradients[atom_index][1] += self.strength*scaling_factor[atom_index]*dvdy/spacing[1]
            gradients[atom_index][2] += self.strength*scaling_factor[atom_index]*dvdz/spacing[2]
        else: # The coordinate is not in the grid
          for i in range(3):
            if (coordinates[atom_index][i]<0):
              gridEnergy += self.k*coordinates[atom_index][i]**2/2.
              if energy.gradients != NULL:
                gradients[atom_index][i] += self.k*coordinates[atom_index][i]
            elif (coordinates[atom_index][i]>hCorner[i]):
              gridEnergy += self.k*(coordinates[atom_index][i]-hCorner[i])**2/2.
              if energy.gradients != NULL:
                gradients[atom_index][i] += self.k*(coordinates[atom_index][i]-hCorner[i])

      energy.energy_terms[self.index] = gridEnergy*self.strength
