#!/usr/bin/env python

# TODO: Heat capacity calculation

import os # Miscellaneous operating system interfaces
from os.path import join
import cPickle as pickle
import gzip
import copy

import time
import numpy as np

import MMTK
import MMTK.Units
from MMTK.ParticleProperties import Configuration
from MMTK.ForceFields import ForceField

import Scientific
try:
  from Scientific._vector import Vector
except:
  from Scientific.Geometry.VectorModule import Vector
  
import AlGDock as a
import pymbar.timeseries

import multiprocessing
from multiprocessing import Process

import psutil

# For profiling. Unnecessary for normal execution.
# from memory_profiler import profile

#############
# Constants #
#############

R = 8.3144621 * MMTK.Units.J / MMTK.Units.mol / MMTK.Units.K
T_HIGH = 600.0 * MMTK.Units.K
T_TARGET = 300.0 * MMTK.Units.K
RT_HIGH = R * T_HIGH
RT_TARGET = R * T_TARGET

term_map = {
  'cosine dihedral angle':'MM',
  'electrostatic/pair sum':'MM',
  'harmonic bond':'MM',
  'harmonic bond angle':'MM',
  'Lennard-Jones':'MM',
  'site':'site',
  'sLJr':'sLJr',
  'sELE':'sELE',
  'sLJa':'sLJa',
  'LJr':'LJr',
  'LJa':'LJa',
  'ELE':'ELE',
  'electrostatic':'misc'}

allowed_phases = ['Gas','GBSA','PBSA','NAMD_Gas','NAMD_OBC','APBS']

#############
# Arguments #
#############

arguments = {
  'dir_dock':{'help':'Directory where docking results are stored'},
  'dir_cool':{'help':'Directory where cooling results are stored'},
  'namd':{'help':'Location of Not Another Molecular Dynamics (NAMD)'},
  'vmd':{'help':'Location of Visual Molecular Dynamics (VMD)'},
  'sander':{'help':'Location of sander (from AMBER)'},
  #   Stored in both dir_cool and dir_dock
  'ligand_database':{'help':'MMTK molecule definition for ligand'},
  'forcefield':{'help':'AMBER force field file'},
  'frcmodList':{'help':'AMBER force field modifications file(s)', 'nargs':'+'},
  'ligand_prmtop':{'help':'AMBER prmtop for the ligand'},
  'ligand_inpcrd':{'help':'AMBER coordinates for the ligand'},
  #   Stored in dir_dock
  'receptor_prmtop':{'help':'AMBER prmtop for the receptor'},
  'receptor_inpcrd':{'help':'AMBER coordinates for the receptor'},
  'receptor_fixed_atoms':{'help':'PDB file with fixed atoms labeled by 1 in the occupancy column'},
  'complex_prmtop':{'help':'AMBER prmtop file for the complex'},
  'complex_inpcrd':{'help':'AMBER coordinates for the complex'},
  'complex_fixed_atoms':{'help':'PDB file with fixed atoms labeled by 1 in the occupancy column'},
  'dir_grid':{'help':'Directory containing potential energy grids'},
  'grid_LJr':{'help':'DX file for Lennard-Jones repulsive grid'},
  'grid_LJa':{'help':'DX file for Lennard-Jones attractive grid'},
  'grid_ELE':{'help':'DX file for electrostatic grid'},
  # Simulation settings and constants
  #   Run-dependent 
  'cool_repX_cycles':{'type':int,
    'help':'Number of replica exchange cycles for cooling'},
  'dock_repX_cycles':{'type':int,
    'help':'Number of replica exchange cycles for docking'},
  'run_type':{'choices':['pose_energies','store_params', 'cool', \
             'dock','timed','postprocess',\
             'redo_postprocess','free_energies','all', \
             'clear_intermediates', None],
    'help':'Type of calculation to run'},
  'max_time':{'type':int, 'default':180, \
    'help':'For timed calculations, the maximum amount of wall clock time, in minutes'},
  'cores':{'type':int, \
    'help':'Number of CPU cores to use'},
  #   Defaults
  'protocol':{'choices':['Adaptive','Set'],
    'help':'Approach to determining series of thermodynamic states'},
  'therm_speed':{'type':float,
    'help':'Thermodynamic speed during adaptive simulation'},
  'sampler':{
    'choices':['HMC','NUTS','VV','TDHMC'],
    'help':'Sampling method'},
  'MCMC_moves':{'type':int,
    'help':'Types of MCMC moves to use'},
  # For initialization
  'seeds_per_state':{'type':int,
    'help':'Number of starting configurations in each state during initialization'},
  'steps_per_seed':{'type':int,
    'help':'Number of MD steps per state during initialization'},
  'score':{'nargs':'?', 'const':True, 'default':False,
    'help':'Start replica exchange from a configuration or set of configurations, which may be passed as an argument. The default configuration is from the ligand_inpcrd argument.'},
  'score_multiple':{'action':'store_true',
    'help':'Uses multiple configurations at the start of replica exchange'},
  # For replica exchange
  'repX_cycles':{'type':int,
    'help':'Number of replica exchange cycles for docking and cooling'},
  'sweeps_per_cycle':{'type':int,
    'help':'Number of replica exchange sweeps per cycle'},
  'attempts_per_sweep':{'type':int,
    'help':'Number of replica exchange attempts per sweep'},
  'steps_per_sweep':{'type':int,
    'help':'Number of MD steps per replica exchange sweep'},
  'keep_intermediate':{'action':'store_true',
    'help':'Keep configurations for intermediate states?'},
  'min_repX_acc':{'type':float,
    'help':'Minimum value for replica exchange acceptance rate'},
  # For postprocessing
  'phases':{'nargs':'+', 'help':'Phases to use in postprocessing'},
  'rmsd':{'nargs':'?', 'const':True, 'default':False,
    'help':'Calculate rmsd between snapshots and a configuration or set of configurations, which may be passed as an argument. The default configuration is from the ligand_inpcrd argument.'},
  # Binding site
  'site':{'choices':['Sphere','Cylinder','Measure'], 'default':'Sphere', \
    'help':'Type of binding site. "Measure" means that parameters' + \
           ' for a sphere will be measured from docked poses.'},
  'site_center':{'nargs':3, 'type':float,
    'help':'Position of binding site center'},
  'site_direction':{'nargs':3, 'type':float,
    'help':'Principal axis of a cylindrical binding site'},
  'site_max_X':{'type':float,
    'help':'Maximum position along principal axis in a cylindrical binding site'},
  'site_max_R':{'type':float,
    'help':'Maximum radial position for a spherical or cylindrical binding site'},
  'site_density':{'type':float,
    'help':'Density of center-of-mass points in the first docking state'}}

for process in ['cool','dock']:
  for key in ['protocol', 'therm_speed', 'sampler',
      'seeds_per_state', 'steps_per_seed',
      'sweeps_per_cycle', 'attempts_per_sweep', 'steps_per_sweep',
      'keep_intermediate']:
    arguments[process+'_'+key] = copy.deepcopy(arguments[key])

for phase in allowed_phases:
  arguments['receptor_'+phase] = {'type':float, 'nargs':'+',
    'help':'Receptor potential energy in %s (kJ/mol)'%phase}

########################
# Auxilliary functions #
########################

def random_rotate():
  """
  Return a random rotation matrix
  """
  u = np.random.uniform(size=3)

  # Random quaternion
  q = np.array([np.sqrt(1-u[0])*np.sin(2*np.pi*u[1]),
               np.sqrt(1-u[0])*np.cos(2*np.pi*u[1]),
               np.sqrt(u[0])*np.sin(2*np.pi*u[2]),
               np.sqrt(u[0])*np.cos(2*np.pi*u[2])])
  
  # Convert the quaternion into a rotation matrix 
  rotMat = np.array([[q[0]*q[0] + q[1]*q[1] - q[2]*q[2] - q[3]*q[3],
                     2*q[1]*q[2] - 2*q[0]*q[3],
                     2*q[1]*q[3] + 2*q[0]*q[2]],
                    [2*q[1]*q[2] + 2*q[0]*q[3],
                     q[0]*q[0] - q[1]*q[1] + q[2]*q[2] - q[3]*q[3],
                     2*q[2]*q[3] - 2*q[0]*q[1]],
                    [2*q[1]*q[3] - 2*q[0]*q[2],
                     2*q[2]*q[3] + 2*q[0]*q[1],
                     q[0]*q[0] - q[1]*q[1] - q[2]*q[2] + q[3]*q[3]]])
  return rotMat

def merge_dictionaries(dicts, required_consistency=[]):
  """
  Merges a list of dictionaries, giving priority to items in descending order.
  Items in the required_consistency list must be consistent with one another.
  """
  merged = {}
  for a in range(len(dicts)): # Loop over all dictionaries,
                              # giving priority to the first
    if not isinstance(dicts[a],dict):
      continue
    for key in dicts[a].keys():
      if isinstance(dicts[a][key],dict): # item is a dictionary
        merged[key] = merge_dictionaries(
          [dicts[n][key] for n in range(len(dicts)) if key in dicts[n].keys()],
          required_consistency=required_consistency)
      elif (key not in merged.keys()):
        # Merged dictionary will contain value from
        # first dictionary where key appears
        merged[key] = dicts[a][key]
        # Check for consistency with other dictionaries
        for b in (range(a) + range(a+1,len(dicts))):
          if isinstance(dicts[b],dict) and (key in dicts[b].keys()) \
              and (dicts[a][key] is not None) and (dicts[b][key] is not None):
            if (isinstance(dicts[b][key],np.ndarray)):
              inconsistent_items = (dicts[b][key]!=dicts[a][key]).any()
            else:
              inconsistent_items = (dicts[b][key]!=dicts[a][key])
            if inconsistent_items:
              if key in required_consistency:
                print 'Dictionary items for %s are inconsistent:'%key
                print dicts[a][key]
                print dicts[b][key]
                raise Exception('Items must be consistent!')
      elif (merged[key] is None): # Replace None
        merged[key] = dicts[a][key]
  return merged

def convert_dictionary_relpath(d, relpath_o=None, relpath_n=None):
  """
  Converts all file names in a dictionary from one relative path to another.
  If relpath_o is None, nothing is joined to the original path.
  If relpath_n is None, an absolute path is used.
  """
  converted = {}
  for key in d.keys():
    if d[key] is None:
      pass
    elif isinstance(d[key],dict):
      converted[key] = convert_dictionary_relpath(d[key],
        relpath_o = relpath_o, relpath_n = relpath_n)
    elif isinstance(d[key],str):
      if relpath_o is not None:
        p = os.path.abspath(join(relpath_o,d[key]))
      else:
        p = os.path.abspath(d[key])
      if os.path.exists(p): # Only save file names for existent paths
        if relpath_n is not None:
          converted[key] = os.path.relpath(p,relpath_n)
        else:
          converted[key] = p
  return converted

def HMStime(s):
  """
  Given the time in seconds, an appropriately formatted string.
  """
  if s<60.:
    return '%.3f s'%s
  elif s<3600.:
    return '%d:%.3f'%(int(s/60%60),s%60)
  else:
    return '%d:%d:%.3f'%(int(s/3600),int(s/60%60),s%60)

class NullDevice():
  """
  A device to suppress output
  """
  def write(self, s):
    pass

##############
# Main Class #
##############

class BPMF:
  def __init__(self, **kwargs): # Energy values
    """Parses the input arguments and runs the requested docking calculation"""
    
    # Set undefined keywords to None
    for key in arguments.keys():
      if not key in kwargs.keys():
        kwargs[key] = None
    if kwargs['dir_grid'] is None:
      kwargs['dir_grid'] = ''

    mod_path = join(os.path.dirname(a.__file__),'BindingPMF.py')
    print """
###########
# AlGDock #
###########
Molecular docking with adaptively scaled alchemical interaction grids

version {0}
in {1}
last modified {2}
    """.format(a.__version__, mod_path, \
      time.ctime(os.path.getmtime(mod_path)))
    
    # Multiprocessing options.
    # Default is to use 1 core.
    # If cores is a number, then that number (or the maximum number)
    # of cores will be used.
    
    # Default
    available_cores = multiprocessing.cpu_count()
    if kwargs['cores'] is None:
      self._cores = 1
    elif (kwargs['cores']==-1):
      self._cores = available_cores
    else:
      self._cores = min(kwargs['cores'], available_cores)
    print "using %d/%d available cores"%(self._cores, available_cores)

    self.confs = {'cool':{}, 'dock':{}}
    
    self.dir = {}
    self.dir['start'] = os.getcwd()
    
    if kwargs['dir_dock'] is not None:
      self.dir['dock'] = os.path.abspath(kwargs['dir_dock'])
    else:
      self.dir['dock'] = os.path.abspath('.')
    
    if kwargs['dir_cool'] is not None:
      self.dir['cool'] = os.path.abspath(kwargs['dir_cool'])
    else:
      self.dir['cool'] = self.dir['dock'] # Default that may be
                                          # overwritten by stored directory
    
    # Load previously stored file names and arguments
    FNs = {}
    args = {}
    for p in ['dock','cool']:
      params = self._load(p)
      if params is not None:
        (fn_dict, arg_dict) = params
        FNs[p] = convert_dictionary_relpath(fn_dict,
          relpath_o=self.dir[p], relpath_n=None)
        args[p] = arg_dict
        if (p=='dock') and (kwargs['dir_cool'] is None) and \
           ('dir_cool' in FNs[p].keys()) and \
           (FNs[p]['dir_cool'] is not None):
          self.dir['cool'] = FNs[p]['dir_cool']
      else:
        FNs[p] = {}
        args[p] = {}
  
    print '\n*** Directories ***'
    print self.dir
  
    # Set up file name dictionary
    print '\n*** Files ***'

    for p in ['cool','dock']:
      if p in FNs.keys():
        if FNs[p]!={}:
          print 'previous stored in %s directory:'%p
          print FNs[p]

    FNs['new'] = {
      'ligand_database':a.findPath([kwargs['ligand_database']]),
      'forcefield':a.findPath([kwargs['forcefield'],'../Data/gaff.dat'] + \
                               a.search_paths['gaff.dat']),
      'frcmodList':kwargs['frcmodList'],
      'prmtop':{'L':a.findPath([kwargs['ligand_prmtop']]),
                'R':a.findPath([kwargs['receptor_prmtop']]),
                'RL':a.findPath([kwargs['complex_prmtop']])},
      'inpcrd':{'L':a.findPath([kwargs['ligand_inpcrd']]),
                'R':a.findPath([kwargs['receptor_inpcrd']]),
                'RL':a.findPath([kwargs['complex_inpcrd']])},
      'fixed_atoms':{'R':a.findPath([kwargs['receptor_fixed_atoms']]),
                     'RL':a.findPath([kwargs['complex_fixed_atoms']])},
      'grids':{'LJr':a.findPath([kwargs['grid_LJr'],
                               join(kwargs['dir_grid'],'LJr.nc'),
                               join(kwargs['dir_grid'],'LJr.dx'),
                               join(kwargs['dir_grid'],'LJr.dx.gz')]),
               'LJa':a.findPath([kwargs['grid_LJa'],
                               join(kwargs['dir_grid'],'LJa.nc'),
                               join(kwargs['dir_grid'],'LJa.dx'),
                               join(kwargs['dir_grid'],'LJa.dx.gz')]),
               'ELE':a.findPath([kwargs['grid_ELE'],
                               join(kwargs['dir_grid'],'electrostatic.nc'),
                               join(kwargs['dir_grid'],'electrostatic.dx'),
                               join(kwargs['dir_grid'],'electrostatic.dx.gz'),
                               join(kwargs['dir_grid'],'pbsa.nc'),
                               join(kwargs['dir_grid'],'pbsa.dx'),
                               join(kwargs['dir_grid'],'pbsa.dx.gz')])},
      'dir_cool':self.dir['cool'],
      'namd':a.findPath([kwargs['namd']] + a.search_paths['namd']),
      'vmd':a.findPath([kwargs['vmd']] + a.search_paths['vmd']),
      'sander':a.findPath([kwargs['vmd']] + a.search_paths['sander'])}

    if not (FNs['cool']=={} and FNs['dock']=={}):
      print 'from arguments and defaults:'
      print FNs['new']
      print '\nto be used:'

    self._FNs = merge_dictionaries(
      [FNs[src] for src in ['new','cool','dock']],
      required_consistency=['L','R','RL','ligand_database'])
  
    # Default: a force field modification is in the same directory as the ligand
    if (self._FNs['frcmodList'] is None):
      dir_lig = os.path.dirname(self._FNs['prmtop']['L'])
      frcmod = a.findPath([\
        os.path.abspath(join(dir_lig, \
          os.path.basename(self._FNs['prmtop']['L'])[:-7]+'.frcmod')),\
        os.path.abspath(join(dir_lig,'lig.frcmod')),\
        os.path.abspath(join(dir_lig,'ligand.frcmod'))])
      if kwargs['frcmodList'] is None:
        self._FNs['frcmodList'] = [frcmod]
    elif isinstance(self._FNs['frcmodList'],str):
      self._FNs['frcmodList'] = [self._FNs['frcmodList']]

    # Check for existence of required files
    do_dock = (hasattr(args,'run_type') and \
              (args.run_type in ['pose_energies','random_dock',\
                'initial_dock','dock','all']))

    for key in ['ligand_database','forcefield']:
      if (self._FNs[key] is None) or (not os.path.isfile(self._FNs[key])):
        raise Exception('File for %s is missing!'%key)

    for (key1,key2) in [('prmtop','L'),('inpcrd','L')]:
      FN = self._FNs[key1][key2]
      if (FN is None) or (not os.path.isfile(FN)):
        raise Exception('File for %s %s is missing'%(key1,key2))

    for (key1,key2) in [\
        ('prmtop','RL'), ('inpcrd','RL'), ('fixed_atoms','RL'), \
        ('grids','LJr'), ('grids','LJa'), ('grids','ELE')]:
      FN = self._FNs[key1][key2]
      errstring = 'Missing file %s %s required for docking!'%(key1,key2)
      if (FN is None) or (not os.path.isfile(FN)):
        if do_dock:
          raise Exception(errstring)
        else:
          print errstring

    if ((self._FNs['inpcrd']['RL'] is None) and \
        (self._FNs['inpcrd']['R'] is None)):
        if do_dock:
          raise Exception('Receptor coordinates needed for docking!')
        else:
          print 'Receptor coordinates needed for docking!'

    print self._FNs
    
    args['default_cool'] = {
        'protocol':'Adaptive',
        'therm_speed':0.9,
        'sampler':'TDHMC',
        'seeds_per_state':100,
        'steps_per_seed':2500,
        'repX_cycles':20,
        'min_repX_acc':0.3,
        'sweeps_per_cycle':200,
        'attempts_per_sweep':100,
        'steps_per_sweep':1000,
        'phases':['NAMD_Gas','NAMD_OBC'],
        'keep_intermediate':False}

    args['default_dock'] = dict(args['default_cool'].items() + {
      'site':None, 'site_center':None, 'site_direction':None,
      'site_max_X':None, 'site_max_R':None,
      'site_density':50.,
      'MCMC_moves':1,
      'rmsd':False,
      'score':False,
      'score_multiple':False}.items() + \
      [('receptor_'+phase,None) for phase in allowed_phases])

    # Store passed arguments in dictionary
    for p in ['cool','dock']:
      args['new_'+p] = {}
      for key in args['default_'+p].keys():
        specific_key = p + '_' + key
        if (specific_key in kwargs.keys()) and \
           (kwargs[specific_key] is not None):
          # Use the specific key if it exists
          args['new_'+p][key] = kwargs[specific_key]
        elif (key in ['site_center', 'site_direction'] +
                     ['receptor_'+phase for phase in allowed_phases]) and \
             (kwargs[key] is not None):
          # Convert these to arrays of floats
          args['new_'+p][key] = np.array(kwargs[key], dtype=float)
        else:
          # Use the general key
          args['new_'+p][key] = kwargs[key]

    self.params = {}
    for p in ['cool','dock']:
      self.params[p] = merge_dictionaries(
        [args[src] for src in ['new_'+p,p,'default_'+p]])

    self._scalables = ['sLJr','sELE','LJr','LJa','ELE']

    # Variables dependent on the parameters
    self.original_Es = [[{}]]
    for phase in allowed_phases:
      if self.params['dock']['receptor_'+phase] is not None:
        self.original_Es[0][0]['R'+phase] = \
          self.params['dock']['receptor_'+phase]
      else:
        self.original_Es[0][0]['R'+phase] = None

    print '\n*** Setting up the simulation ***'
    self._setup_universe(do_dock = do_dock)

    print '\n*** Simulation parameters and constants ***'
    for p in ['cool','dock']:
      print 'for %s:'%p
      print self.params[p]

    self.timing = {'start':time.time(), 'max':kwargs['max_time']}

    self.run_type = kwargs['run_type']
    if self.run_type=='pose_energies':
      self.pose_energies()
    elif self.run_type=='store_params':
      self._save('cool', keys=['progress'])
      self._save('dock', keys=['progress'])
    elif self.run_type=='cool': # Sample the cooling process
      self.cool()
    elif self.run_type=='dock': # Sample the docking process
      self.dock()
    elif self.run_type=='timed': # Timed replica exchange sampling
      cool_complete = self.cool()
      if cool_complete:
        pp_complete = self._postprocess([('cool',-1,-1,'L')])
        if pp_complete:
          self.calc_f_L()
          dock_complete = self.dock()
          if dock_complete:
            pp_complete = self._postprocess()
            if pp_complete:
              self.calc_f_RL()
    elif self.run_type=='postprocess': # Postprocessing
      self._postprocess()
    elif self.run_type=='redo_postprocess':
      self._postprocess(redo_dock=True)
    elif self.run_type=='free_energies': # All free energies
      self.calc_f_L()
      self.calc_f_RL()
    elif self.run_type=='all':
      self.cool()
      self.dock()
      self._postprocess()
      self.calc_f_L()
      self.calc_f_RL()
    elif self.run_type=='clear_intermediates':
      for process in ['cool','dock']:
        print 'Clearing intermediates for '+process
        for state_ind in range(1,len(self.confs[process]['samples'])-1):
          for cycle_ind in range(len(self.confs[process]['samples'][state_ind])):
            self.confs[process]['samples'][state_ind][cycle_ind] = []
        self._save(process)

  def _setup_universe(self, do_dock=True):
    """Creates an MMTK InfiniteUniverse and adds the ligand"""
    #print "  >_setup_universe()" 
    # Set up the system
    import sys
    original_stderr = sys.stderr
    sys.stderr = NullDevice()
    MMTK.Database.molecule_types.directory = \
      os.path.dirname(self._FNs['ligand_database'])
    self.molecule = MMTK.Molecule(\
      os.path.basename(self._FNs['ligand_database']))
    sys.stderr = original_stderr

    # Helpful variables for referencing and indexing atoms in the molecule
    self.molecule.heavy_atoms = [ind for (atm,ind) in zip(self.molecule.atoms,range(self.molecule.numberOfAtoms())) if atm.type.name!='hydrogen']
    self.molecule.nhatoms = len(self.molecule.heavy_atoms)

    self.molecule.prmtop_atom_order = np.array([atom.number \
      for atom in self.molecule.prmtop_order], dtype=int)
    self.molecule.inv_prmtop_atom_order = np.zeros(shape=len(self.molecule.prmtop_atom_order), dtype=int)
    for i in range(len(self.molecule.prmtop_atom_order)):
      self.molecule.inv_prmtop_atom_order[self.molecule.prmtop_atom_order[i]] = i

    self.universe = MMTK.Universe.InfiniteUniverse()
    self.universe.addObject(self.molecule)
    self._evaluators = {} # Store evaluators
    self._ligand_natoms = self.universe.numberOfAtoms()

    # Force fields
    from MMTK.ForceFields import Amber12SBForceField

    self._forceFields = {}
    self._forceFields['gaff'] = Amber12SBForceField(
      parameter_file=self._FNs['forcefield'],mod_files=self._FNs['frcmodList'])

    # If docked poses are available, start with a docked structure
    if not(isinstance(self.params['dock']['score'],bool)) and \
      (self.params['dock']['score'].endswith('.mol2') or \
       self.params['dock']['score'].endswith('.mol2.gz')):
      (confs_dock6,E_mol2) = self._read_dock6(self.params['dock']['score'])
      if len(confs_dock6)>0:
        self.universe.setConfiguration(Configuration(self.universe,confs_dock6[0]))
      else:
        print '  no available configurations from dock 6'

    if (self.params['dock']['site']=='Measure'):
      print '\n*** Measuring the binding site ***'
      self.params['dock']['site'] = 'Sphere'
      (confs, Es) = self._get_confs_to_rescore()
      if len(confs)>0:
        # Use the center of mass for configurations
        # within 20 RT of the lowest energy
        coms = []
        for (conf,E) in reversed(zip(confs,Es)):
          if E<(Es[-1]+20*RT_TARGET):
            self.universe.setConfiguration(Configuration(self.universe,conf))
            coms.append(np.array(self.universe.centerOfMass()))
          else:
            break
        print '  %d configurations fit in the binding site'%len(coms)
        coms = np.array(coms)
        center = (np.min(coms,0)+np.max(coms,0))/2
        max_R = max(np.ceil(np.max(np.sqrt(np.sum((coms-center)**2,1)))*10.)/10.,0.6)
        self.params['dock']['site_max_R'] = max_R
        self.params['dock']['site_center'] = center
        self.universe.setConfiguration(Configuration(self.universe,confs[-1]))
      if ((self.params['dock']['site_max_R'] is None) or \
          (self.params['dock']['site_center'] is None)):
        raise Exception('No binding site parameters!')
# TODO: Develop way to note that measurement has been completed, and to store the measured site

    if (self.params['dock']['site']=='Sphere') and \
       (self.params['dock']['site_center'] is not None) and \
       (self.params['dock']['site_max_R'] is not None):
      from AlGDock.ForceFields.Sphere.Sphere import SphereForceField
      self._forceFields['site'] = SphereForceField(
        center=self.params['dock']['site_center'],
        max_R=self.params['dock']['site_max_R'], name='site')
    elif (self.params['dock']['site']=='Cylinder') and \
         (self.params['dock']['site_center'] is not None) and \
         (self.params['dock']['site_direction'] is not None):
      from AlGDock.ForceFields.Cylinder.Cylinder import CylinderForceField
      self._forceFields['site'] = CylinderForceField(
        origin=self.params['dock']['site_center'],
        direction=self.params['dock']['site_direction'],
        max_X=self.params['dock']['site_max_X'],
        max_R=self.params['dock']['site_max_R'], name='site')
    else:
      if do_dock:
        raise Exception('Binding site type not recognized!')
      else:
        print 'Binding site type not recognized!'

    # Randomly rotate the molecule and translate it into the binding site
    self.universe.setConfiguration(Configuration(self.universe, \
      np.dot(self.universe.configuration().array,
      np.transpose(random_rotate()))))
    if 'site' in self._forceFields.keys():
      self.universe.translateTo(Vector(self._forceFields['site'].randomPoint()))
    else:
      print 'Molecule not translated into binding site'

    # Samplers may accept the following options:
    # steps - number of MD steps
    # T - temperature in K
    # delta_t - MD time step
    # normalize - normalizes configurations
    # adapt - uses an adaptive time step
    from NUTS import NUTSIntegrator  # @UnresolvedImport
    from Integrators.TDHamiltonianMonteCarlo.TDHamiltonianMonteCarlo \
      import TDHamiltonianMonteCarloIntegrator

    self.sampler = {}
    #self.sampler['init'] = NUTSIntegrator(self.universe)

    #sys.path.append(os.path.abspath('/home/lspirido/Installers/AlGDock-0.0.1/AlGDock/Integrators/HamiltonianMonteCarlo'))
    #from HamiltonianMonteCarlo import HamiltonianMonteCarloIntegrator
    #self.sampler['init'] = HamiltonianMonteCarloIntegrator(self.universe)
    self.sampler['init'] = TDHamiltonianMonteCarloIntegrator(self.universe)
    print "self.params[cool][sampler]= ", self.params['cool']['sampler'], "self.params[dock][sampler]= ", self.params['dock']['sampler']
    for p in ['cool', 'dock']:
      if self.params[p]['sampler'] == 'NUTS':
        self.sampler[p] = NUTSIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'HMC':
        from AlGDock.Integrators.HamiltonianMonteCarlo.HamiltonianMonteCarlo \
          import HamiltonianMonteCarloIntegrator
        self.sampler[p] = HamiltonianMonteCarloIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'TDHMC':
      #  from Integrators.TDHamiltonianMonteCarlo.TDHamiltonianMonteCarlo \
      #    import TDHamiltonianMonteCarloIntegrator
        self.sampler[p] = TDHamiltonianMonteCarloIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'VV':
        from AlGDock.Integrators.VelocityVerlet.VelocityVerlet \
          import VelocityVerletIntegrator
        self.sampler[p] = VelocityVerletIntegrator(self.universe)
      else:
        raise Exception('Unrecognized sampler!')

    # Determine ligand atomic index
    if (self._FNs['prmtop']['R'] is not None) and \
       (self._FNs['prmtop']['RL'] is not None):
      import AlGDock.IO
      IO_prmtop = AlGDock.IO.prmtop()
      prmtop_R = IO_prmtop.read(self._FNs['prmtop']['R'])
      prmtop_RL = IO_prmtop.read(self._FNs['prmtop']['RL'])
      ligand_ind = [ind for (r,ind) in \
        zip(prmtop_RL['RESIDUE_LABEL'],range(len(prmtop_RL['RESIDUE_LABEL']))) \
        if r not in prmtop_R['RESIDUE_LABEL']]
      if len(ligand_ind)==0:
        raise Exception('Ligand not found in complex prmtop')
      elif len(ligand_ind) > 1:
        raise Exception('Ligand residue label is ambiguous')
      self._ligand_first_atom = prmtop_RL['RESIDUE_POINTER'][ligand_ind[0]] - 1
    else:
      self._ligand_first_atom = 0
      if do_dock:
        raise Exception('Missing AMBER prmtop files for receptor')
      else:
        print 'Missing AMBER prmtop files for receptor'

    # Read the reference ligand and receptor coordinates
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()
    if self._FNs['inpcrd']['R'] is not None:
      if os.path.isfile(self._FNs['inpcrd']['L']):
        lig_crd = IO_crd.read(self._FNs['inpcrd']['L'], multiplier=0.1)
      self.confs['receptor'] = IO_crd.read(self._FNs['inpcrd']['R'], multiplier=0.1)
    elif self._FNs['inpcrd']['RL'] is not None:
      complex_crd = IO_crd.read(self._FNs['inpcrd']['RL'], multiplier=0.1)
      lig_crd = complex_crd[self._ligand_first_atom:self._ligand_first_atom + self._ligand_natoms,:]
      self.confs['receptor'] = np.vstack(\
        (complex_crd[:self._ligand_first_atom,:],\
         complex_crd[self._ligand_first_atom + self._ligand_natoms:,:]))
    elif self._FNs['inpcrd']['L'] is not None:
      self.confs['receptor'] = None
      if os.path.isfile(self._FNs['inpcrd']['L']):
        lig_crd = IO_crd.read(self._FNs['inpcrd']['L'], multiplier=0.1)
    else:
      lig_crd = None
    self.confs['ligand'] = lig_crd[self.molecule.inv_prmtop_atom_order,:]
    
    if self.params['dock']['rmsd'] is not False:
      if self.params['dock']['rmsd'] is True:
        rmsd_crd = self.confs['ligand']
      else:
        rmsd_crd = IO_crd.read(self.params['dock']['rmsd'], \
          natoms=self.universe.numberOfAtoms(), multiplier=0.1)
        rmsd_crd = rmsd_crd[self.molecule.inv_prmtop_atom_order,:]
      self.confs['rmsd'] = rmsd_crd[self.molecule.heavy_atoms,:]

    # Determine APBS grid spacing
    if 'APBS' in self.params['dock']['phases'] or \
       'APBS' in self.params['cool']['phases']:
      factor = 1.0/MMTK.Units.Ang
      
      def roundUpDime(x):
        return (np.ceil((x.astype(float)-1)/32)*32+1).astype(int)

      self._set_universe_evaluator({'MM':True, 'T':T_HIGH, 'ELE':1})
      gd = self._forceFields['ELE'].grid_data
      focus_dims = roundUpDime(gd['counts'])
      focus_center = factor*(gd['counts']*gd['spacing']/2. + gd['origin'])
      focus_spacing = factor*gd['spacing'][0]

      min_xyz = np.array([min(factor*self.confs['receptor'][a,:]) for a in range(3)])
      max_xyz = np.array([max(factor*self.confs['receptor'][a,:]) for a in range(3)])
      mol_range = max_xyz - min_xyz
      mol_center = (min_xyz + max_xyz)/2.

      # The full grid spans 1.5 times the range of the receptor
      # and the focus grid, whatever is larger
      full_spacing = 1.0
      full_min = np.minimum(mol_center - mol_range/2.*1.5, \
                            focus_center - focus_dims*focus_spacing/2.*1.5)
      full_max = np.maximum(mol_center + mol_range/2.*1.5, \
                            focus_center + focus_dims*focus_spacing/2.*1.5)
      full_dims = roundUpDime((full_max-full_min)/full_spacing)
      full_center = (full_min + full_max)/2.

      self._apbs_grid = { \
        'dime':[full_dims, focus_dims], \
        'gcent':[full_center, focus_center], \
        'spacing':[full_spacing, focus_spacing]}

    # Load progress
    self._postprocess(readOnly=True)
    self.calc_f_L(readOnly=True)
    self.calc_f_RL(readOnly=True)

  ###########
  # Cooling #
  ###########
  def initial_cool(self, warm=True):
    """
    Warms the ligand from T_TARGET to T_HIGH, or
    cools the ligand from T_HIGH to T_TARGET
    
    Intermediate thermodynamic states are chosen such that
    thermodynamic length intervals are approximately constant.
    Configurations from each state are subsampled to seed the next simulation.
    """
    #print "  >initial_cool()"
    if (len(self.cool_protocol)>0) and (self.cool_protocol[-1]['crossed']):
      return # Initial cooling is already complete
    
    self._set_lock('cool')
    cool_start_time = time.time()

    if warm:
      T_START, T_END = T_TARGET, T_HIGH
      direction_name = 'warm'
    else:
      T_START, T_END = T_HIGH, T_TARGET
      direction_name = 'cool'

    if self.cool_protocol==[]:
      self.tee("\n>>> Initial %sing of the ligand "%direction_name + \
        "from %d K to %d K, "%(T_START,T_END) + "starting at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))

      # Set up the force field
      T = T_START
      self.cool_protocol = [{'MM':True, 'T':T, \
                            'delta_t':1.5*MMTK.Units.fs,
                            'a':0.0, 'crossed':False}]
      self._set_universe_evaluator(self.cool_protocol[-1])

      # Minimize and ramp the temperature from 0 to the desired starting temperature
      from MMTK.Minimization import ConjugateGradientMinimizer # @UnresolvedImport
      minimizer = ConjugateGradientMinimizer(self.universe)
      for rep in range(50):
        x_o = np.copy(self.universe.configuration().array)
        e_o = self.universe.energy()
        minimizer(steps = 10)
        e_n = self.universe.energy()
        if np.isnan(e_n) or (e_o-e_n)<1000:
          self.universe.configuration().array = x_o
          break

      T_LOW = 20.
      T_SERIES = T_LOW*(T_START/T_LOW)**(np.arange(30)/29.)
      for T in T_SERIES:
        #(myconfs, myEs, my3, my4) = self.sampler['init'](server="/home/lspirido/examolmodel/44ccbserver.exe", ligand_dir=os.path.dirname(self._FNs['ligand_database']) + '/', dyn_type="TD", seed=int(time.time()+T), steps = 500, T = T, delta_t = self.delta_t, steps_per_trial = 50)
        (myconfs, myEs, my3, my4) = self.sampler['init'](server="../simbody/44ccbserver.exe", ligand_dir=os.path.dirname(self._FNs['ligand_database']) + '/', dyn_type="TD", seed=int(time.time()+T), steps = 500, T = T, delta_t = self.delta_t, steps_per_trial = 50)
        #self.sampler['init'](steps = 500, T=T,\
        #                     delta_t=self.delta_t, steps_per_trial = 100, \
        #                     seed=int(time.time()+T))
        for e in myEs:
          print 'T= ', T, 'PE=', e
      self.universe.normalizePosition()

      # Run at starting temperature
      state_start_time = time.time()
      conf = self.universe.configuration().array
      (confs, Es_MM, self.cool_protocol[-1]['delta_t'], Ht) = \
        self._initial_sim_state(\
        [conf for n in range(self.params['cool']['seeds_per_state'])], \
        'cool', self.cool_protocol[-1])

      print "DEBUG Es_MM= ", Es_MM
 
      self.confs['cool']['replicas'] = [confs[np.random.randint(len(confs))]]
      self.confs['cool']['samples'] = [[confs]]
      self.cool_Es = [[{'MM':Es_MM}]]
      tL_tensor = Es_MM.std()/(R*T_START*T_START)

      self.tee("  generated %d configurations "%len(confs) + \
               "at %d K "%self.cool_protocol[-1]['T'] + \
               "in " + HMStime(time.time()-state_start_time))
      self.tee("  dt=%f ps, Ht=%f, tL_tensor=%e"%(\
        self.cool_protocol[-1]['delta_t'], Ht, tL_tensor))
    else:
      self.tee("\n>>> Initial %s of the ligand "%direction_name + \
        "from %d K to %d K, "%(T_START,T_END) + "continuing at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))
      confs = self.confs['cool']['samples'][-1][0]
      Es_MM = self.cool_Es[-1][0]['MM']
      T = self.cool_protocol[-1]['T']
      tL_tensor = Es_MM.std()/(R*T*T)

    # Main loop for initial cooling:
    # choose new temperature, randomly select seeds, simulate
    while (not self.cool_protocol[-1]['crossed']):
      # Choose new temperature
      To = self.cool_protocol[-1]['T']
      crossed = self.cool_protocol[-1]['crossed']
      if tL_tensor>1E-5:
        dL = self.params['cool']['therm_speed']/tL_tensor
        if warm:
          T = To + dL
          if T > T_HIGH:
            T = T_HIGH
            crossed = True
        else:
          T = To - dL
          if T < T_TARGET:
            T = T_TARGET
            crossed = True
      else:
        raise Exception('No variance in configuration energies')
      self.cool_protocol.append(\
        {'T':T, 'a':(T_HIGH-T)/(T_HIGH-T_TARGET), 'MM':True, 'crossed':crossed})

      # Randomly select seeds for new trajectory
      logweight = Es_MM/R*(1/T-1/To)
      weight = np.exp(-logweight+min(logweight))
      seedIndicies = np.random.choice(len(Es_MM),
        size = self.params['cool']['seeds_per_state'], p = weight/sum(weight))

      # Simulate and store data
      confs_o = confs
      Es_MM_o = Es_MM
      
      self._set_universe_evaluator(self.cool_protocol[-1])
      
      state_start_time = time.time()
      (confs, Es_MM, self.cool_protocol[-1]['delta_t'], Ht) = \
        self._initial_sim_state(\
        [confs[ind] for ind in seedIndicies], 'cool', self.cool_protocol[-1])
      self.tee("  generated %d configurations "%len(confs) + \
               "at %d K "%self.cool_protocol[-1]['T'] + \
               "in " + (HMStime(time.time()-state_start_time)))
      self.tee("  dt=%f ps, Ht=%f, sigma=%f"%(\
        self.cool_protocol[-1]['delta_t'], Ht, Es_MM.std()))

      # Estimate the mean replica exchange acceptance rate
      # between the previous and new state
      (u_kln,N_k) = self._u_kln([[{'MM':Es_MM_o}],[{'MM':Es_MM}]], \
                                self.cool_protocol[-2:])
      N = min(N_k)
      acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
      mean_acc = np.mean(np.minimum(acc,np.ones(acc.shape)))

      if mean_acc<self.params['cool']['min_repX_acc']:
        # If the acceptance probability is too low,
        # reject the state and restart
        self.cool_protocol.pop()
        confs = confs_o
        Es_MM = Es_MM_o
        tL_tensor = tL_tensor*1.25 # Use a smaller step
        self.tee("  rejected new state, as estimated replica exchange" + \
          " acceptance rate of %f is too low"%mean_acc)
      elif (mean_acc>0.99) and (not crossed):
        # If the acceptance probability is too high,
        # reject the previous state and restart
        self.confs['cool']['replicas'][-1] = confs[np.random.randint(len(confs))]
        self.cool_protocol.pop(-2)
        tL_tensor = Es_MM.std()/(R*T*T) # Metric tensor for the thermodynamic length
        self.tee("  rejected previous state, as estimated replica exchange" + \
          " acceptance rate of %f is too high"%mean_acc)
      else:
        self.confs['cool']['replicas'].append(confs[np.random.randint(len(confs))])
        self.confs['cool']['samples'].append([confs])
        if len(self.confs['cool']['samples'])>2 and \
            (not self.params['cool']['keep_intermediate']):
          self.confs['cool']['samples'][-2] = []
        self.cool_Es.append([{'MM':Es_MM}])
        tL_tensor = Es_MM.std()/(R*T*T) # Metric tensor for the thermodynamic length
        self.tee("  estimated replica exchange acceptance rate is %f"%mean_acc)

      # Special tasks after the last stage
      if self.cool_protocol[-1]['crossed']:
        self._cool_cycle += 1
        # For warming, reverse protocol and energies
        if warm:
          self.tee("  reversing replicas, samples, and protocol")
          self.confs['cool']['replicas'].reverse()
          self.confs['cool']['samples'].reverse()
          self.cool_Es.reverse()
          self.cool_protocol.reverse()
          self.cool_protocol[0]['crossed'] = False
          self.cool_protocol[-1]['crossed'] = True

      self._save('cool')
      self.tee("")

      if self.run_type=='timed':
        remaining_time = self.timing['max']*60 - \
          (time.time()-self.timing['start'])
        if remaining_time<0:
          self.tee("  no time remaining for initial %s"%direction_name)
          self._clear_lock('cool')
          return False

    # Save data
    self.tee("Elapsed time for initial %sing of "%direction_name + \
      "%d states: "%len(self.cool_protocol) + \
      HMStime(time.time()-cool_start_time))
    self._clear_lock('cool')
    return True

  def cool(self):
    """
    Samples different ligand configurations 
    at thermodynamic states between T_HIGH and T_TARGET
    """
    #print "  >cool()"
    return self._sim_process('cool')

  def calc_f_L(self, readOnly=False, redo=False):
    """
    Calculates ligand-specific free energies:
    1. solvation free energy of the ligand using single-step 
       free energy perturbation
    2. reduced free energy of cooling the ligand from T_HIGH to T_TARGET
    """
    #print "  >calc_f_L()"
    # Initialize variables as empty lists or by loading data
    f_L_FN = join(self.dir['cool'],'f_L.pkl.gz')
    if redo:
      if os.path.isfile(f_L_FN):
        os.remove(f_L_FN)
      dat = None
    else:
      dat = self._load_pkl_gz(f_L_FN)
    if dat is not None:
      (self.stats_L, self.f_L) = dat
    else:
      self.stats_L = dict(\
        [(item,[]) for item in ['equilibrated_cycle','mean_acc']])
      self.stats_L['protocol'] = self.cool_protocol
      self.f_L = dict([(key,[]) for key in ['cool_BAR','cool_MBAR'] + \
        [phase+'_solv' for phase in self.params['cool']['phases']]])
    if readOnly or self.cool_protocol==[]:
      return

    K = len(self.cool_protocol)

    # Make sure postprocessing is complete
    pp_complete = self._postprocess([('cool',-1,-1,'L')])
    if not pp_complete:
      return False

    # Make sure all the energies are available
    for c in range(self._cool_cycle):
      if len(self.cool_Es[-1][c].keys())==0:
        self.tee("  skipping the cooling free energy calculation")
        return

    start_string = "\n>>> Ligand free energy calculations, starting at " + \
      time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
    free_energy_start_time = time.time()
      
    # Estimate cycle at which simulation has equilibrated
    u_KKs = \
      [np.sum([self._u_kln([self.cool_Es[k][c]],[self.cool_protocol[k]]) \
        for k in range(len(self.cool_protocol))],0) \
          for c in range(self._cool_cycle)]
    mean_u_KKs = np.array([np.mean(u_KK) for u_KK in u_KKs])
    std_u_KKs = np.array([np.std(u_KK) for u_KK in u_KKs])
    for c in range(len(self.stats_L['equilibrated_cycle']), self._cool_cycle):
      nearMean = abs(mean_u_KKs - mean_u_KKs[c])<std_u_KKs[c]
      if nearMean.any():
        nearMean = list(nearMean).index(True)
      else:
        nearMean = c
      if c>0: # If possible, reject burn-in
        nearMean = max(nearMean,1)
      self.stats_L['equilibrated_cycle'].append(nearMean)

    # Calculate solvation free energies that have not already been calculated,
    # in units of RT
    updated = False
    for phase in self.params['cool']['phases']:
      if not phase+'_solv' in self.f_L:
        self.f_L[phase+'_solv'] = []
      for c in range(len(self.f_L[phase+'_solv']), self._cool_cycle):
        if not updated:
          self._set_lock('cool')
          self.tee(start_string)
          updated = True

        fromCycle = self.stats_L['equilibrated_cycle'][c]
        toCycle = c + 1
        
        if not ('L'+phase) in self.cool_Es[-1][c].keys():
          raise Exception('L%s energies not found in cycle %d'%(phase, c))
        
        # Arbitrarily, solvation is the
        # 'forward' direction and desolvation the 'reverse'
        u_phase = np.concatenate([\
          self.cool_Es[-1][n]['L'+phase] for n in range(fromCycle,toCycle)])
        u_MM = np.concatenate([\
          self.cool_Es[-1][n]['MM'] for n in range(fromCycle,toCycle)])
        du_F = (u_phase[:,-1] - u_MM)/RT_TARGET
        min_du_F = min(du_F)
        f_L_solv = -np.log(np.exp(-du_F+min_du_F).mean()) + min_du_F

        self.f_L[phase+'_solv'].append(f_L_solv)
        self.tee("  calculated " + phase + " solvation free energy of " + \
                 "%f RT "%(f_L_solv) + \
                 "using cycles %d to %d"%(fromCycle, toCycle-1))

    # Calculate cooling free energies that have not already been calculated,
    # in units of RT
    for c in range(len(self.f_L['cool_BAR']), self._cool_cycle):
      if not updated:
        self._set_lock('cool')
        self.tee(start_string)
        updated = True
      
      fromCycle = self.stats_L['equilibrated_cycle'][c]
      toCycle = c + 1

      # Cooling free energy
      cool_Es = []
      for cool_Es_state in self.cool_Es:
        cool_Es.append(cool_Es_state[fromCycle:toCycle])
      (u_kln,N_k) = self._u_kln(cool_Es,self.cool_protocol)
      (BAR,MBAR) = self._run_MBAR(u_kln,N_k)
      self.f_L['cool_BAR'].append(BAR)
      self.f_L['cool_MBAR'].append(MBAR)

      # Average acceptance probabilities
      cool_mean_acc = np.zeros(K-1)
      for k in range(0, K-1):
        (u_kln, N_k) = self._u_kln(cool_Es[k:k+2],self.cool_protocol[k:k+2])
        N = min(N_k)
        acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
        cool_mean_acc[k] = np.mean(np.minimum(acc,np.ones(acc.shape)))
      self.stats_L['mean_acc'].append(cool_mean_acc)

      self.tee("  calculated cooling free energy of %f RT "%(\
                  self.f_L['cool_MBAR'][-1][-1])+\
               "using MBAR for cycles %d to %d"%(fromCycle, c))

    if updated:
      self._write_pkl_gz(f_L_FN, (self.stats_L,self.f_L))
      self.tee("\nElapsed time for free energy calculation: " + \
        HMStime(time.time()-free_energy_start_time))
      self._clear_lock('cool')

  ###########
  # Docking #
  ###########
  def random_dock(self):
    """
      Randomly places the ligand into the receptor and evaluates energies
      
      The first state of docking is sampled by randomly placing configurations
      from the high temperature ligand simulation into the binding site.
    """
    #print "  >random_dock()"
    # Select samples from the high T unbound state and ensure there are enough
    E_MM = []
    confs = []
    for k in range(1,len(self.cool_Es[0])):
      E_MM += list(self.cool_Es[0][k]['MM'])
      confs += list(self.confs['cool']['samples'][0][k])
    while len(E_MM)<self.params['dock']['seeds_per_state']:
      self.tee("More samples from high temperature ligand simulation needed")
      self._replica_exchange('cool')
      E_MM = []
      confs = []
      for k in range(1,len(self.cool_Es[0])):
        E_MM += list(self.cool_Es[0][k]['MM'])
        confs += list(self.confs['cool']['samples'][0][k])

    random_dock_inds = np.array(np.linspace(0,len(E_MM), \
      self.params['dock']['seeds_per_state'],endpoint=False),dtype=int)
    cool0_Es_MM = [E_MM[ind]  for ind in random_dock_inds]
    cool0_confs = [confs[ind] for ind in random_dock_inds]

    # Do the random docking
    lambda_o = self._lambda(0.0,'dock',MM=True,site=True,crossed=False)
    self.dock_protocol = [lambda_o]

    # Set up the force field with full interaction grids
    lambda_scalables = dict(zip(\
      self._scalables,np.ones(len(self._scalables),dtype=int)) + [('T',T_HIGH)])
    self._set_universe_evaluator(lambda_scalables)

    # Either loads or generates the random translations and rotations for the first state of docking
    if not (hasattr(self,'_random_trans') and hasattr(self,'_random_rotT')):
      self._max_n_trans = 10000
      # Default density of points is 50 per nm**3
      self._n_trans = max(min(np.int(np.ceil(self._forceFields['site'].volume*self.params['dock']['site_density'])),self._max_n_trans),5)
      self._random_trans = np.ndarray((self._max_n_trans), dtype=Vector)
      for ind in range(self._max_n_trans):
        self._random_trans[ind] = Vector(self._forceFields['site'].randomPoint())
      self._max_n_rot = 100
      self._n_rot = 100
      self._random_rotT = np.ndarray((self._max_n_rot,3,3))
      for ind in range(self._max_n_rot):
        self._random_rotT[ind,:,:] = np.transpose(random_rotate())
    else:
      self._max_n_trans = self._random_trans.shape[0]
      self._n_rot = self._random_rotT.shape[0]

    # Get interaction energies.
    # Loop over configurations, random rotations, and random translations
    E = {}
    for term in (['MM','site']+self._scalables):
      # Large array creation may cause MemoryError
      E[term] = np.zeros((self.params['dock']['seeds_per_state'], \
        self._max_n_rot,self._n_trans))
    self.tee("  allocated memory for interaction energies")

    converged = False
    n_trans_o = 0
    n_trans_n = self._n_trans
    while not converged:
      for c in range(self.params['dock']['seeds_per_state']):
        E['MM'][c,:,:] = cool0_Es_MM[c]
        for i_rot in range(self._n_rot):
          conf_rot = Configuration(self.universe,\
            np.dot(cool0_confs[c], self._random_rotT[i_rot,:,:]))
          for i_trans in range(n_trans_o, n_trans_n):
            self.universe.setConfiguration(conf_rot)
            self.universe.translateBy(self._random_trans[i_trans])
            eT = self.universe.energyTerms()
            for (key,value) in eT.iteritems():
              E[term_map[key]][c,i_rot,i_trans] += value
      E_c = {}
      for term in E.keys():
        # Large array creation may cause MemoryError
        E_c[term] = np.ravel(E[term][:,:self._n_rot,:n_trans_n])
      self.tee("  allocated memory for %d translations"%n_trans_n)
      (u_kln,N_k) = self._u_kln([E_c],\
        [lambda_o,self._next_dock_state(E=E_c, lambda_o=lambda_o)])
      du = u_kln[0,1,:] - u_kln[0,0,:]
      bootstrap_reps = 50
      f_grid0 = np.zeros(bootstrap_reps)
      for b in range(bootstrap_reps):
        du_b = du[np.random.randint(0, len(du), len(du))]
        f_grid0[b] = -np.log(np.exp(-du_b+min(du_b)).mean()) + min(du_b)
      f_grid0_std = f_grid0.std()
      converged = f_grid0_std<0.1
      if not converged:
        self.tee("  with %s translations "%n_trans_n + \
                 "the predicted free energy difference is %f (%f)"%(\
                 f_grid0.mean(),f_grid0_std))
        if n_trans_n == self._max_n_trans:
          break
        n_trans_o = n_trans_n
        n_trans_n = min(n_trans_n + 25, self._max_n_trans)
        for term in (['MM','site']+self._scalables):
          # Large array creation may cause MemoryError
          E[term] = np.dstack((E[term], \
            np.zeros((self.params['dock']['seeds_per_state'],\
              self._max_n_rot,25))))

    if self._n_trans != n_trans_n:
      self._n_trans = n_trans_n
      
    self.tee("  %d ligand configurations "%len(cool0_Es_MM) + \
             "were randomly docked into the binding site using "+ \
             "%d translations and %d rotations "%(n_trans_n,self._n_rot))
    self.tee("  the predicted free energy difference between the" + \
             " first and second docking states is " + \
             "%f (%f)"%(f_grid0.mean(),f_grid0_std))

    ravel_start_time = time.time()
    for term in E.keys():
      E[term] = np.ravel(E[term][:,:self._n_rot,:self._n_trans])
    self.tee("  raveled energy terms in " + \
      HMStime(time.time()-ravel_start_time))

    return (cool0_confs, E)

  def FFT_dock(self):
    """
      Systemically places the ligand into the receptor grids 
      along grid points using a Fast Fourier Transform
    """

    # TODO: Write this function
    # UNDER CONSTRUCTION
    # Loop over conformers
    #   Loop over rotations
    #     Generate ligand grids
    #     Compute the cross-correlation with FFT
    #     Accumulate results into the running BPMF estimator
    #     Keep the lowest-energy conformers
    # Estimate the BPMF

  def initial_dock(self, randomOnly=False, undock=True):
    """
      Docks the ligand into the receptor
      
      Intermediate thermodynamic states are chosen such that
      thermodynamic length intervals are approximately constant.
      Configurations from each state are subsampled to seed the next simulation.
    """
    #print "  >initial_dock()"
    
    if (len(self.dock_protocol)>0) and (self.dock_protocol[-1]['crossed']):
      return # Initial docking already complete

    self._set_lock('dock')
    dock_start_time = time.time()

    if self.dock_protocol==[]:
      self.tee("\n>>> Initial docking, starting at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))
      if undock:
        (seed, Es) = self._get_confs_to_rescore(nconfs=1)
        if seed==[]:
          undock = False
        else:
          lambda_o = self._lambda(1.0, 'dock', MM=True, site=True, crossed=False)
          self.dock_protocol = [lambda_o]
          self._set_universe_evaluator(lambda_o)
          
          # Ramp up the temperature
          T_LOW = 20.
          T_SERIES = T_LOW*(T_TARGET/T_LOW)**(np.arange(30)/29.)
          self.universe.setConfiguration(Configuration(self.universe,seed[0]))
          for T in T_SERIES:
            (myconfs, myEs, my3, my4) = self.sampler['init'](server="../simbody/44ccbserver.exe", ligand_dir=os.path.dirname(self._FNs['ligand_database']) + '/', dyn_type="TD", seed=int(time.time()+T), steps = 500, T = T, delta_t = self.delta_t, steps_per_trial = 50)
            #self.sampler['init'](steps = 500, T=T,\
            #  delta_t=self.delta_t, steps_per_trial = 100, \
            #  seed=int(time.time()+T))
            for e in myEs:
              print 'T= ', T, 'PE=', e

          seed = [self.universe.configuration().array]

          # Simulate
          sim_start_time = time.time()
          (confs, Es_tot, lambda_o['delta_t'], Ht) = \
            self._initial_sim_state(\
              seed*self.params['dock']['seeds_per_state'], 'dock', lambda_o)

          # Get state energies
          E = self._calc_E(confs)
          self.confs['dock']['replicas'] = [confs[np.random.randint(len(confs))]]
          self.confs['dock']['samples'] = [[confs]]
          self.dock_Es = [[E]]

          self.tee("  generated %d configurations "%len(confs) + \
                   "with progress %f "%lambda_o['a'] + \
                   "in " + HMStime(time.time()-sim_start_time))
          self.tee("  dt=%f ps, Ht=%f, tL_tensor=%e"%(\
            lambda_o['delta_t'],Ht,self._tL_tensor(E,lambda_o)))
    
      if not undock:
        (cool0_confs, E) = self.random_dock()

#        import MMTK_DCD
#        import sys
#        import glob
#        randockindexes = []
#        for randockfn in glob.glob('pdbs/randock*.dcd'):
#          randockindexes.append(int((randockfn[12:])[:-4]))
#        if len(randockindexes) > 0:
#          randockindexes.sort()
#          randocki = randockindexes[-1] + 1
#          outfn = 'pdbs/randock' + str(randocki) + '.dcd'
#        else:
#          outfn = 'pdbs/randock0.dcd'
#
#        fd = MMTK_DCD.writeOpenDCD(outfn, self._ligand_natoms, len(cool0_confs), 0, 1, 0.15)
#        for c in cool0_confs:
#          x = c[:,0].astype(Scientific.N.Float16) * 10.0
#          y = c[:,1].astype(Scientific.N.Float16) * 10.0
#          z = c[:,2].astype(Scientific.N.Float16) * 10.0
#          MMTK_DCD.writeDCDStep(fd, x, y, z)
#        MMTK_DCD.writeCloseDCD(fd)
        

        self.tee("  random docking complete in " + \
                 HMStime(time.time()-dock_start_time))
        if randomOnly:
          self._clear_lock('dock')
          return
    else:
      # Continuing from a previous docking instance
      self.tee("\n>>> Initial docking, continuing at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))
      confs = self.confs['dock']['samples'][-1][0]
      E = self.dock_Es[-1][0]

    lambda_o = self.dock_protocol[-1]

    # Main loop for initial docking:
    # choose new thermodynamic variables,
    # randomly select seeds,
    # simulate
    rejectStage = 0
    while (not self.dock_protocol[-1]['crossed']):
      # Determine next value of the protocol
      lambda_n = self._next_dock_state(E = E, lambda_o = lambda_o, \
          pow = rejectStage, undock = undock)
      self.dock_protocol.append(lambda_n)
      if len(self.dock_protocol)>1000:
        self._clear('dock')
        self._save('dock')
        raise Exception('Too many replicas!')
      if abs(rejectStage)>20:
        self._clear('dock')
        self._save('dock')
        raise Exception('Too many consecutive rejected stages!')

      # Randomly select seeds for new trajectory
      u_o = self._u_kln([E],[lambda_o])
      u_n = self._u_kln([E],[lambda_n])
      du = u_n-u_o
      weight = np.exp(-du+min(du))
      seedIndicies = np.random.choice(len(u_o), \
        size = self.params['dock']['seeds_per_state'], \
        p=weight/sum(weight))

      if (not undock) and (len(self.dock_protocol)==2):
        # Cooling state 0 configurations, randomly oriented
        # Use the lowest energy configuration in the first docking state for replica exchange
        ind = np.argmin(u_n)
        (c,i_rot,i_trans) = np.unravel_index(ind, (self.params['dock']['seeds_per_state'], self._n_rot, self._n_trans))
        repX_conf = np.add(np.dot(cool0_confs[c], self._random_rotT[i_rot,:,:]),\
                           self._random_trans[i_trans].array)
        self.confs['dock']['replicas'] = [repX_conf]
        self.confs['dock']['samples'] = [[repX_conf]]
        self.dock_Es = [[dict([(key,np.array([val[ind]])) for (key,val) in E.iteritems()])]]
        seeds = []
        for ind in seedIndicies:
          (c,i_rot,i_trans) = np.unravel_index(ind, (self.params['dock']['seeds_per_state'], self._n_rot, self._n_trans))
          seeds.append(np.add(np.dot(cool0_confs[c], self._random_rotT[i_rot,:,:]), self._random_trans[i_trans].array))
        confs = None
        E = {}
      else: # Seeds from last state
        seeds = [confs[ind] for ind in seedIndicies]
      self.confs['dock']['seeds'] = seeds

      # Store old data
      confs_o = confs
      E_o = E

      # Simulate
      sim_start_time = time.time()
      self._set_universe_evaluator(lambda_n)
      (confs, Es_tot, lambda_n['delta_t'], Ht) = \
        self._initial_sim_state(seeds, 'dock', lambda_n)

      # Get state energies
      E = self._calc_E(confs)

      self.tee("  generated %d configurations "%len(confs) + \
               "with progress %f "%lambda_n['a'] + \
               "in " + HMStime(time.time()-sim_start_time))
      self.tee("  dt=%f ps, Ht=%f, tL_tensor=%e"%(\
        lambda_n['delta_t'],Ht,self._tL_tensor(E,lambda_n)))

      # Decide whether to keep the state
      if len(self.dock_protocol)>(1+(not undock)):
        # Estimate the mean replica exchange acceptance rate
        # between the previous and new state
        (u_kln,N_k) = self._u_kln([[E_o],[E]], self.dock_protocol[-2:])
        N = min(N_k)
        acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
        mean_acc = np.mean(np.minimum(acc,np.ones(acc.shape)))
        
        if (mean_acc<self.params['dock']['min_repX_acc']):
          # If the acceptance probability is too low,
          # reject the state and restart
          self.dock_protocol.pop()
          confs = confs_o
          E = E_o
          rejectStage += 1
          self.tee("  rejected new state, as estimated replica exchange acceptance rate of %f is too low"%mean_acc)
        elif (mean_acc>0.99) and (not lambda_n['crossed']):
          # If the acceptance probability is too high,
          # reject the previous state and restart
          self.confs['dock']['replicas'][-1] = confs[np.random.randint(len(confs))]
          self.dock_protocol.pop()
          self.dock_protocol[-1] = copy.deepcopy(lambda_n)
          rejectStage -= 1
          lambda_o = lambda_n
          self.tee("  rejected previous state, as estimated replica exchange acceptance rate of %f is too high"%mean_acc)
        else:
          # Store data and continue with initialization
          self.confs['dock']['replicas'].append(confs[np.random.randint(len(confs))])
          self.confs['dock']['samples'].append([confs])
          self.dock_Es.append([E])
          self.dock_protocol[-1] = copy.deepcopy(lambda_n)
          rejectStage = 0
          lambda_o = lambda_n
          self.tee("  the estimated replica exchange acceptance rate is %f"%mean_acc)

          if (not self.params['dock']['keep_intermediate']):
            if len(self.dock_protocol)>(2+(not undock)):
              self.confs['dock']['samples'][-2] = []
      else:
        # Store data and continue with initialization (first time)
        self.confs['dock']['replicas'].append(confs[np.random.randint(len(confs))])
        self.confs['dock']['samples'].append([confs])
        self.dock_Es.append([E])
        self.dock_protocol[-1] = copy.deepcopy(lambda_n)
        rejectStage = 0
        lambda_o = lambda_n

      # Special tasks after the last stage
      if (self.dock_protocol[-1]['crossed']):
        # For undocking, reverse protocol and energies
        if undock:
          self.tee("  reversing replicas, samples, and protocol")
          self.confs['dock']['replicas'].reverse()
          self.confs['dock']['samples'].reverse()
          self.confs['dock']['seeds'] = None
          self.dock_Es.reverse()
          self.dock_protocol.reverse()
          self.dock_protocol[0]['crossed'] = False
          self.dock_protocol[-1]['crossed'] = True

        if (not self.params['dock']['keep_intermediate']):
          for k in range(len(self.dock_protocol)-1):
            self.confs['dock']['samples'][k] = []

        self._dock_cycle += 1

      # Save progress
      self._save('dock')
      self.tee("")

      if self.run_type=='timed':
        remaining_time = self.timing['max']*60 - \
          (time.time()-self.timing['start'])
        if remaining_time<0:
          self.tee("  no time remaining for initial dock")
          self._clear_lock('dock')
          return False

    self.tee("Elapsed time for initial docking of " + \
      "%d states: "%len(self.dock_protocol) + \
      HMStime(time.time()-dock_start_time))
    self._clear_lock('dock')
    return True

  def dock(self):
    """
    Docks the ligand into the binding site
    by simulating at thermodynamic states
    between decoupled and fully interacting and
    between T_HIGH and T_TARGET
    """
    #print "  >dock()"
    return self._sim_process('dock')

  def calc_f_RL(self, readOnly=False, redo=False):
    """
    Calculates the binding potential of mean force
    """
    #print "  >calc_f_RL() Calculates the binding PMF"
    if self.dock_protocol==[]:
      return # Initial docking is incomplete

    # Initialize variables as empty lists or by loading data
    f_RL_FN = join(self.dir['dock'],'f_RL.pkl.gz')
    if redo:
      if os.path.isfile(f_RL_FN):
        os.remove(f_RL_FN)
      dat = None
    else:
      dat = self._load_pkl_gz(f_RL_FN)
    if (dat is not None):
      (self.f_L, self.stats_RL, self.f_RL, self.B) = dat
    else:
      # stats_RL will include internal energies, interaction energies,
      # the cycle by which the bound state is equilibrated,
      # the mean acceptance probability between replica exchange neighbors,
      # and the rmsd, if applicable
      stats_RL = [('u_K_'+FF,[]) \
        for FF in ['ligand','sampled']+self.params['dock']['phases']]
      stats_RL += [('Psi_'+FF,[]) \
        for FF in ['grid']+self.params['dock']['phases']]
      stats_RL += [(item,[]) \
        for item in ['equilibrated_cycle','mean_acc','rmsd']]
      self.stats_RL = dict(stats_RL)
      self.stats_RL['protocol'] = self.dock_protocol
      # Free energy components
      self.f_RL = dict([(key,[]) for key in ['grid_BAR','grid_MBAR'] + \
        [phase+'_solv' for phase in self.params['dock']['phases']]])
      # Binding PMF estimates
      self.B = {}
      for phase in self.params['dock']['phases']:
        for method in ['min_Psi','mean_Psi','inverse_FEP','BAR','MBAR']:
          self.B[phase+'_'+method] = []
    if readOnly:
      return True

    # Make sure postprocessing is complete
    pp_complete = self._postprocess()
    if not pp_complete:
      return False
    self.calc_f_L()

    # Make sure all the energies are available
    for c in range(self._dock_cycle):
      if len(self.dock_Es[-1][c].keys())==0:
        self.tee("  skipping the binding PMF calculation")
        return
      for phase in self.params['dock']['phases']:
        for prefix in ['L','RL']:
          if not prefix+phase in self.dock_Es[-1][c].keys():
            self.tee("  postprocessed energies for %s unavailable"%phase)
            return
    if not hasattr(self,'f_L'):
      self.tee("  skipping the binding PMF calculation")
      return

    self._set_lock('dock')
    self.tee("\n>>> Binding PMF estimation, starting at " + \
      time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))
    BPMF_start_time = time.time()

    K = len(self.dock_protocol)
  
    # Store stats_RL
    # Internal energies
    self.stats_RL['u_K_ligand'] = \
      [self.dock_Es[-1][c]['MM']/RT_TARGET for c in range(self._dock_cycle)]
    self.stats_RL['u_K_sampled'] = \
      [self._u_kln([self.dock_Es[-1][c]],[self.dock_protocol[-1]]) \
        for c in range(self._dock_cycle)]
    self.stats_RL['u_KK'] = \
      [np.sum([self._u_kln([self.dock_Es[k][c]],[self.dock_protocol[k]]) \
        for k in range(len(self.dock_protocol))],0) \
          for c in range(self._dock_cycle)]
    for phase in self.params['dock']['phases']:
      self.stats_RL['u_K_'+phase] = \
        [self.dock_Es[-1][c]['RL'+phase][:,-1]/RT_TARGET \
          for c in range(self._dock_cycle)]

    # Interaction energies
    for c in range(len(self.stats_RL['Psi_grid']), self._dock_cycle):
      self.stats_RL['Psi_grid'].append(
          (self.dock_Es[-1][c]['LJr'] + \
           self.dock_Es[-1][c]['LJa'] + \
           self.dock_Es[-1][c]['ELE'])/RT_TARGET)
    for phase in self.params['dock']['phases']:
      if not 'Psi_'+phase in self.stats_RL:
        self.stats_RL['Psi_'+phase] = []
      for c in range(len(self.stats_RL['Psi_'+phase]), self._dock_cycle):
        self.stats_RL['Psi_'+phase].append(
          (self.dock_Es[-1][c]['RL'+phase][:,-1] - \
           self.dock_Es[-1][c]['L'+phase][:,-1] - \
           self.original_Es[0][0]['R'+phase][-1])/RT_TARGET)

    # Estimate cycle at which simulation has equilibrated
    mean_u_KKs = np.array([np.mean(u_KK) for u_KK in self.stats_RL['u_KK']])
    std_u_KKs = np.array([np.std(u_KK) for u_KK in self.stats_RL['u_KK']])
    for c in range(len(self.stats_RL['equilibrated_cycle']), self._dock_cycle):
      nearMean = abs(mean_u_KKs - mean_u_KKs[c])<std_u_KKs[c]
      if nearMean.any():
        nearMean = list(nearMean).index(True)
      else:
        nearMean = c
      if c>0: # If possible, reject burn-in
        nearMean = max(nearMean,1)
      self.stats_RL['equilibrated_cycle'].append(nearMean)

    # Store NUTS acceptance statistic
    self.stats_RL['Ht'] = [self.dock_Es[0][c]['Ht'] \
      if 'Ht' in self.dock_Es[-1][c].keys() else [] \
      for c in range(self._dock_cycle)]
      
    # Autocorrelation time for all replicas (this can take a while)
    paths = np.transpose(np.hstack([np.array(self.dock_Es[0][c]['repXpath']) \
      for c in range(len(self.dock_Es[0])) \
      if 'repXpath' in self.dock_Es[0][c].keys()]))
    self.stats_RL['tau_ac'] = \
      pymbar.timeseries.integratedAutocorrelationTimeMultiple(paths)
    
    # Store rmsd values
    self.stats_RL['rmsd'] = [self.dock_Es[-1][c]['rmsd'] \
      if 'rmsd' in self.dock_Es[-1][c].keys() else [] \
      for c in range(self._dock_cycle)]

    # Calculate docking free energies that have not already been calculated
    updated = False
    for c in range(len(self.f_RL['grid_MBAR']), self._dock_cycle):
      extractCycles = range(self.stats_RL['equilibrated_cycle'][c], c+1)
      
      # Extract relevant energies
      dock_Es = [Es[self.stats_RL['equilibrated_cycle'][c]:c+1] \
        for Es in self.dock_Es]
      
      # Use MBAR for the grid scaling free energy estimate
      (u_kln,N_k) = self._u_kln(dock_Es,self.dock_protocol)
      (BAR,MBAR) = self._run_MBAR(u_kln,N_k)
      self.f_RL['grid_MBAR'].append(MBAR)
      self.f_RL['grid_BAR'].append(BAR)
      updated = True

      # Average acceptance probabilities
      mean_acc = np.zeros(K-1)
      for k in range(0, K-1):
        (u_kln,N_k) = self._u_kln(dock_Es[k:k+2],self.dock_protocol[k:k+2])
        N = min(N_k)
        acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
        mean_acc[k] = np.mean(np.minimum(acc,np.ones(acc.shape)))
      self.stats_RL['mean_acc'].append(mean_acc)

# How to estimate the change in potential energy for GBSA:
#              This is the NAMD result
#              v
# RLGBSAflex = RLGBSAfixedR + RMM
# RLMMTK = RMMTK + LMMTK + PsiMMTK
#
# du = (RLGBSAfixedR + RMM) - (RMMTK + LMMTK + PsiMMTK)
#
# Because RMM and RMMTK are both zero,
# du = (RLGBSAfixedR) - (LMMTK + PsiMMTK)

    for phase in self.params['dock']['phases']:
      if not phase+'_solv' in self.f_RL:
        self.f_RL[phase+'_solv'] = []
      for method in ['min_Psi','mean_Psi','inverse_FEP','BAR','MBAR']:
        if not phase+'_'+method in self.B:
          self.B[phase+'_'+method] = []
      for c in range(len(self.B[phase+'_MBAR']), self._dock_cycle):
        extractCycles = range(self.stats_RL['equilibrated_cycle'][c], c+1)
        du = np.concatenate([self.stats_RL['u_K_'+phase][c] - \
          self.stats_RL['u_K_sampled'][c] for c in extractCycles])
        min_du = min(du)
        # Complex solvation
        B_RL_solv = -np.log(np.exp(-du+min_du).mean()) + min_du
        weight = np.exp(-du+min_du)
        weight = weight/sum(weight)
        Psi = np.concatenate([self.stats_RL['Psi_'+phase][c] \
          for c in extractCycles])
        min_Psi = min(Psi)
        
        self.f_RL[phase+'_solv'].append(B_RL_solv)
        self.B[phase+'_min_Psi'].append(min_Psi)
        self.B[phase+'_mean_Psi'].append(np.mean(Psi))
        self.B[phase+'_inverse_FEP'].append(\
          np.log(sum(weight*np.exp(Psi-min_Psi))) + min_Psi)
        self.B[phase+'_BAR'].append(\
          -self.f_L[phase+'_solv'][-1] - \
           self.original_Es[0][0]['R'+phase][-1]/RT_TARGET - \
           self.f_L['cool_BAR'][-1][-1] + \
           self.f_RL['grid_BAR'][-1][-1] + B_RL_solv)
        self.B[phase+'_MBAR'].append(\
          -self.f_L[phase+'_solv'][-1] - \
           self.original_Es[0][0]['R'+phase][-1]/RT_TARGET - \
           self.f_L['cool_MBAR'][-1][-1] + \
           self.f_RL['grid_MBAR'][-1][-1] + B_RL_solv)

        self.tee("  calculated %s binding PMF of %f RT with cycles %d to %d"%(\
          phase, self.B[phase+'_MBAR'][-1], \
          self.stats_RL['equilibrated_cycle'][c], c))
        updated = True

    if updated:
      self._write_pkl_gz(f_RL_FN, (self.f_L, self.stats_RL, self.f_RL, self.B))

    self.tee("\nElapsed time for binding PMF estimation: " + \
      HMStime(time.time()-BPMF_start_time))
    self._clear_lock('dock')

  def pose_energies(self):
    """
    Calculates the energy for poses from self.params['dock']['score']
    """
    #print "  >pose_energies()"
    # Load the poses
    if isinstance(self.params['dock']['score'],bool):
      confs = [self.confs['ligand']]
      E = {}
      prefix = 'xtal'
    elif self.params['dock']['score'].endswith('.mol2') or \
       self.params['dock']['score'].endswith('.mol2.gz'):
      (confs,E) = self._read_dock6(self.params['dock']['score'])
      prefix = os.path.basename(self.params['dock']['score']).split('.')[0]
    E = self._calc_E(confs, E, type='all', prefix=prefix)

    # Try different grid transformations
    from AlGDock.ForceFields.Grid.TrilinearTransformGrid \
      import TrilinearTransformGridForceField
    for type in ['LJa','LJr']:
      E[type+'_transformed'] = np.zeros((12,len(confs)),dtype=np.float)
      for p in range(12):
        FF = TrilinearTransformGridForceField(self._FNs['grids'][type], 1.0, \
          'scaling_factor_'+type, grid_name='%f'%(p+1), \
          inv_power=-float(p+1), max_val=-1)
        self.universe.setForceField(FF)
        for c in range(len(confs)):
          self.universe.setConfiguration(Configuration(self.universe,confs[c]))
          E[type+'_transformed'][p,c] = self.universe.energy()

    # Store the data
    self._write_pkl_gz(join(self.dir['dock'],prefix+'.pkl.gz'),E)

  ######################
  # Internal Functions #
  ######################

  def _set_universe_evaluator(self, lambda_n):
    """
    Sets the universe evaluator to values appropriate for the given lambda_n dictionary.
    The elements in the dictionary lambda_n can be:
      MM - True, to turn on the Generalized AMBER force field
      site - True, to turn on the binding site
      sLJr - scaling of the soft Lennard-Jones repulsive grid
      sLJa - scaling of the soft Lennard-Jones attractive grid
      sELE - scaling of the soft electrostatic grid
      LJr - scaling of the Lennard-Jones repulsive grid
      LJa - scaling of the Lennard-Jones attractive grid
      ELE - scaling of the electrostatic grid
      T - the temperature in K
    """
    #print "  >_set_universe_evaluator()"

    self.T = lambda_n['T']
    self.RT = R*lambda_n['T']
    
    if 'delta_t' in lambda_n.keys():
      self.delta_t = lambda_n['delta_t']
    else:
      self.delta_t = 1.5*MMTK.Units.fs

    # Reuse evaluators that have been stored
    evaluator_key = '-'.join(repr(v) for v in lambda_n.values())
    if evaluator_key in self._evaluators.keys():
      self.universe._evaluator[(None,None,None)] = \
        self._evaluators[evaluator_key]
      return
    
    # Otherwise create a new evaluator
    fflist = []
    if ('MM' in lambda_n.keys()) and lambda_n['MM']:
      fflist.append(self._forceFields['gaff'])
    if ('site' in lambda_n.keys()) and lambda_n['site'] and \
        ('site' in self._forceFields.keys()):
      fflist.append(self._forceFields['site'])
    for scalable in self._scalables:
      if (scalable in lambda_n.keys()) and lambda_n[scalable]>0:
        # Load the force field if it has not been loaded
        if not scalable in self._forceFields.keys():
          loading_start_time = time.time()
          grid_FN = self._FNs['grids'][{'sLJr':'LJr','sLJa':'LJa','sELE':'ELE',
            'LJr':'LJr','LJa':'LJa','ELE':'ELE'}[scalable]]
          grid_scaling_factor = 'scaling_factor_' + \
            {'sLJr':'LJr','sLJa':'LJa','sELE':'electrostatic', \
             'LJr':'LJr','LJa':'LJa','ELE':'electrostatic'}[scalable]
          if scalable=='LJr':
            from AlGDock.ForceFields.Grid.TrilinearISqrtGrid import TrilinearISqrtGridForceField
            self._forceFields[scalable] = TrilinearISqrtGridForceField(grid_FN,
              lambda_n[scalable], grid_scaling_factor,
              grid_name=scalable, max_val=-1)
          else:
            if scalable=='sLJr':
              max_val = 10.0
            elif scalable=='sELE':
              # The maximum value is set so that the electrostatic energy
              # less than or equal to the Lennard-Jones repulsive energy
              # for every heavy atom at every grid point
              scaling_factors_ELE = np.array([ \
                self.molecule.getAtomProperty(a, 'scaling_factor_electrostatic') \
                  for a in self.molecule.atomList()],dtype=float)
              scaling_factors_LJr = np.array([ \
                self.molecule.getAtomProperty(a, 'scaling_factor_LJr') \
                  for a in self.molecule.atomList()],dtype=float)
              scaling_factors_ELE = scaling_factors_ELE[scaling_factors_LJr>10]
              scaling_factors_LJr = scaling_factors_LJr[scaling_factors_LJr>10]
              max_val = min(abs(scaling_factors_LJr*10.0/scaling_factors_ELE))
            else:
              max_val = -1
              
            from AlGDock.ForceFields.Grid.TrilinearGrid import TrilinearGridForceField
            self._forceFields[scalable] = TrilinearGridForceField(grid_FN,
              lambda_n[scalable], grid_scaling_factor,
              grid_name=scalable, max_val=max_val)
          self.tee('  %s grid loaded from %s in %s'%(scalable, grid_FN, \
            HMStime(time.time()-loading_start_time)))

        # Set the force field strength to the desired value
        self._forceFields[scalable].strength = lambda_n[scalable]
        fflist.append(self._forceFields[scalable])

    compoundFF = fflist[0]
    for ff in fflist[1:]:
      compoundFF += ff
    self.universe.setForceField(compoundFF)

    eval = ForceField.EnergyEvaluator(\
      self.universe, self.universe._forcefield, None, None, None, None)
    self.universe._evaluator[(None,None,None)] = eval
    self._evaluators[evaluator_key] = eval

  def _MC_translate_rotate(self, lambda_k, trials=20):
    """
    Conducts Monte Carlo translation and rotation moves.
    """
    #print "  >_MC_translate_rotate()"
    # It does not seem worth creating a special evaluator
    # for a small number of trials
    if trials>100:
      lambda_noMM = copy.deepcopy(lambda_k)
      lambda_noMM['MM'] = False
      lambda_noMM['site'] = False
      self._set_universe_evaluator(lambda_noMM)

    step_size = min(\
      1.0*MMTK.Units.Ang, \
      self._forceFields['site'].max_R*MMTK.Units.nm/10.)

    acc = 0
    xo = self.universe.configuration()
    eo = self.universe.energy()
    com = self.universe.centerOfMass().array
    
    for c in range(trials):
      step = np.random.randn(3)*step_size
      xn = np.dot((xo.array - com), random_rotate()) + com + step
      self.universe.setConfiguration(Configuration(self.universe,xn))
      en = self.universe.energy()
#      write_flag = 0
      expression = np.exp(-(en-eo)/self.RT)
      if ((en<eo) or (np.random.random()<expression)):
#        write_flag = 1
        print "   rot acc en ", en, " eo ", eo, " exp ", expression
        acc += 1
        xo = self.universe.configuration()
        eo = en
        com += step
      else:
        print "   rot not acc en ", en, " eo ", eo, " exp ", expression
        self.universe.setConfiguration(xo)

#      if write_flag == 1:
#        import glob
#        rotindexes = []
#        for rotfn in glob.glob('pdbs/rot*.pdb'):
#          rotindexes.append(int((rotfn[8:])[:-4]))
#        if len(rotindexes) > 0:
#          rotindexes.sort()
#          roti = rotindexes[-1] + 1
#          outfn = 'pdbs/rot' + str(roti) + '.pdb'
#        else:
#          outfn = 'pdbs/rot0.pdb'
#        self.universe.writeToFile(outfn)

    return acc

  def _initial_sim_state(self, seeds, process, lambda_k):
    """
    Initializes a state, returning the configurations and potential energy.
    """
    #print "  >_initial_sim_state()" 
    doMC = (process == 'dock') and (self.params['dock']['MCMC_moves']>0) \
      and (lambda_k['a'] > 0.0) and (lambda_k['a'] < 0.01)

    results = []
    if self._cores>1:
      # Multiprocessing code
      m = multiprocessing.Manager()
      task_queue = m.Queue()
      done_queue = m.Queue()
      for k in range(len(seeds)):
        task_queue.put((seeds[k], process, lambda_k, doMC, True, k))
      processes = [multiprocessing.Process(target=self._sim_one_state_worker, \
          args=(task_queue, done_queue)) for p in range(self._cores)]
      for p in range(self._cores):
        task_queue.put('STOP')
      for p in processes:
        p.start()
      for p in processes:
        p.join()
      results = [done_queue.get() for seed in seeds]
      for p in processes:
        p.terminate()
    else:
      # Single process code
      results = [self._sim_one_state(\
        seeds[k], process, lambda_k, doMC, True, k) for k in range(len(seeds))]

    confs = [result['confs'] for result in results]
    potEs = [result['E_MM'] for result in results]
    Ht = np.mean(np.array([result['Ht'] for result in results]))
    delta_t = np.median(np.array([results['delta_t'] for results in results]))
    delta_t = min(max(delta_t, 0.25*MMTK.Units.fs), 2.5*MMTK.Units.fs)

    return (confs, np.array(potEs), delta_t, Ht)
  
  def _replica_exchange(self, process):
    """
    Performs a cycle of replica exchange
    """
    #print "  >_replica_exchange()" 
    if not process in ['dock','cool']:
      raise Exception('Process must be dock or cool')

    self._set_lock(process)

    if process=='cool':
      terms = ['MM']
    else:
      terms = ['MM','site','misc'] + self._scalables

    cycle = getattr(self,'_%s_cycle'%process)
    confs = self.confs[process]['replicas']
    lambdas = getattr(self,process+'_protocol')
    
    # A list of pairs of replica indicies
    K = len(lambdas)
    pairs_to_swap = []
    for interval in range(1,min(5,K)):
      lower_inds = []
      for lowest_index in range(interval):
        lower_inds += range(lowest_index,K-interval,interval)
      upper_inds = np.array(lower_inds) + interval
      pairs_to_swap += zip(lower_inds,upper_inds)

    # Setting the force field will load grids
    # before multiple processes are spawned
    for k in range(K):
      self._set_universe_evaluator(lambdas[k])
    
    storage = {}
    for var in ['confs','state_inds','energies']:
      storage[var] = []
    
    cycle_start_time = time.time()

    if self._cores>1:
      # Multiprocessing setup
      m = multiprocessing.Manager()
      task_queue = m.Queue()
      done_queue = m.Queue()

    # Do replica exchange
    MC_time = 0.0
    repX_time = 0.0
    state_inds = range(K)
    inv_state_inds = range(K)
    for sweep in range(self.params[process]['sweeps_per_cycle']):
      E = {}
      for term in terms:
        E[term] = np.zeros(K, dtype=float)
      if process=='dock':
        E['acc_MC'] = np.zeros(K, dtype=float)
      Ht = np.zeros(K, dtype=float)
      # Sample within each state
      doMC = [(process == 'dock') and (self.params['dock']['MCMC_moves']>0) \
        and (lambdas[state_inds[k]]['a'] > 0.0) \
        and (lambdas[state_inds[k]]['a'] < 0.01) for k in range(K)]
      if self._cores>1:
        for k in range(K):
          task_queue.put((confs[k], process, lambdas[state_inds[k]], doMC[k], False, k))
        for p in range(self._cores):
          task_queue.put('STOP')
        processes = [multiprocessing.Process(target=self._sim_one_state_worker, \
            args=(task_queue, done_queue)) for p in range(self._cores)]
        for p in processes:
          p.start()
        for p in processes:
          p.join()
        unordered_results = [done_queue.get() for k in range(K)]
        results = sorted(unordered_results, key=lambda d: d['reference'])
        for p in processes:
          p.terminate()
      else:
        # Single process code
        results = [self._sim_one_state(confs[k], process, \
            lambdas[state_inds[k]], doMC[k], False, k) for k in range(K)]
      # Store results
      for k in range(K):
        if 'acc_MC' in results[k].keys():
          E['acc_MC'][k] = results[k]['acc_MC']
          MC_time += results[k]['MC_time']
        confs[k] = results[k]['confs'] # [-1]
        if process == 'cool':
            E['MM'][k] = results[k]['E_MM'] # [-1]
        Ht[k] += results[k]['Ht']
      if process=='dock':
        E = self._calc_E(confs, E) # Get energies
        # Get rmsd values
        if self.params['dock']['rmsd'] is not False:
          E['rmsd'] = np.array([np.sqrt(((confs[k][self.molecule.heavy_atoms,:] - \
            self.confs['rmsd'])**2).sum()/self.molecule.nhatoms) for k in range(K)])
      # Calculate u_ij (i is the replica, and j is the configuration),
      #    a list of arrays
      (u_ij,N_k) = self._u_kln(E, [lambdas[state_inds[c]] for c in range(K)])
      # Do the replica exchange
      repX_start_time = time.time()
      for attempt in range(self.params[process]['attempts_per_sweep']):
        for (t1,t2) in pairs_to_swap:
          a = inv_state_inds[t1]
          b = inv_state_inds[t2]
          ddu = -u_ij[a][b]-u_ij[b][a]+u_ij[a][a]+u_ij[b][b]
          if (ddu>0) or (np.random.uniform()<np.exp(ddu)):
            u_ij[a],u_ij[b] = u_ij[b],u_ij[a]
            state_inds[a],state_inds[b] = state_inds[b],state_inds[a]
            inv_state_inds[state_inds[a]],inv_state_inds[state_inds[b]] = \
              inv_state_inds[state_inds[b]],inv_state_inds[state_inds[a]]
      repX_time += (time.time()-repX_start_time)
      # Store data in local variables
      storage['confs'].append(list(confs))
      storage['state_inds'].append(list(state_inds))
      storage['energies'].append(copy.deepcopy(E))

    # Estimate relaxation time from empirical state transition matrix
    state_inds = np.array(storage['state_inds'])
    Nij = np.zeros((K,K),dtype=int)
    for (i,j) in zip(state_inds[:-1,:],state_inds[1:,:]):
      for k in range(K):
        Nij[j[k],i[k]] += 1
    N = (Nij+Nij.T)
    Tij = np.array(N,dtype=float)/sum(N,1)
    (eval,evec)=np.linalg.eig(Tij)
    tau2 = 1/(1-eval[1])
    
    # Estimate relaxation time from autocorrelation
    tau_ac = pymbar.timeseries.integratedAutocorrelationTimeMultiple(state_inds.T)
    per_independent = {'cool':5.0, 'dock':30.0}[process]
    # There will be at least per_independent and up to sweeps_per_cycle saved samples
    # max(int(np.ceil((1+2*tau_ac)/per_independent)),1) is the minimum stride,
    # which is based on per_independent samples per autocorrelation time.
    # max(self.params['dock']['sweeps_per_cycle']/per_independent)
    # is the maximum stride, which gives per_independent samples if possible.
    stride = min(max(int(np.ceil((1+2*tau_ac)/per_independent)),1), \
                 max(int(np.ceil(self.params[process]['sweeps_per_cycle']/per_independent)),1))

    store_indicies = np.array(\
      range(min(stride-1,self.params[process]['sweeps_per_cycle']-1), self.params[process]['sweeps_per_cycle'], stride), dtype=int)
    nsaved = len(store_indicies)

    self.tee("  storing %d configurations for %d replicas"%(nsaved, len(confs)) + \
      " in cycle %d"%cycle + \
      " (tau2=%f, tau_ac=%f)"%(tau2,tau_ac))
    self.tee("  with %s for MC"%(HMStime(MC_time)) + \
      " and %s for replica exchange"%(HMStime(repX_time)) + \
      " in " + HMStime(time.time()-cycle_start_time))

    # Get indicies for storing global variables
    inv_state_inds = np.zeros((nsaved,K),dtype=int)
    for snap in range(nsaved):
      state_inds = storage['state_inds'][store_indicies[snap]]
      for state in range(K):
        inv_state_inds[snap][state_inds[state]] = state

    # Reorder energies and replicas for storage
    if process=='dock':
      terms.append('acc_MC') # Make sure to save the acceptance probability
      if self.params['dock']['rmsd'] is not False:
        terms.append('rmsd') # Make sure to save the rmsd
    Es = []
    for state in range(K):
      E_state = {}
      if state==0:
        E_state['repXpath'] = storage['state_inds']
        E_state['Ht'] = Ht
      for term in terms:
        E_state[term] = np.array([storage['energies'][store_indicies[snap]][term][inv_state_inds[snap][state]] for snap in range(nsaved)])
      Es.append([E_state])

    self.confs[process]['replicas'] = \
      [storage['confs'][store_indicies[-1]][inv_state_inds[-1][state]] \
       for state in range(K)]

    for state in range(K):
      getattr(self,process+'_Es')[state].append(Es[state][0])

    for state in range(K):
      if self.params[process]['keep_intermediate'] or \
          ((process=='cool') and (state==0)) or \
          (state==(K-1)):
        confs = [storage['confs'][store_indicies[snap]][inv_state_inds[snap][state]] for snap in range(nsaved)]
        self.confs[process]['samples'][state].append(confs)
      else:
        self.confs[process]['samples'][state].append([])

    setattr(self,'_%s_cycle'%process,cycle + 1)
    self._save(process)
    self._clear_lock(process)

  def _sim_one_state_worker(self, input, output):
    """
    Executes a task from the queue
    """
    #print "  >_sim_one_state_worker()" 
    for args in iter(input.get, 'STOP'):
      result = self._sim_one_state(*args)
      output.put(result)

  def _sim_one_state(self, seed, process, lambda_k, doMC,
      initialize=False, reference=None):
    self.universe.setConfiguration(Configuration(self.universe, seed))
   
    #print "  >_sim_one_state()" 
 
    results = {}
    
    # For initialization, the evaluator is already set
    if not initialize:
      self._set_universe_evaluator(lambda_k)
    
    if 'delta_t' in lambda_k.keys():
      delta_t = lambda_k['delta_t']
    else:
      delta_t = 1.5*MMTK.Units.fs
    
    # Perform MCMC moves
    if doMC:
      MC_start_time = time.time()
      results['acc_MC'] = self._MC_translate_rotate(lambda_k, trials=20)/20.
      results['MC_time'] = (time.time() - MC_start_time)
    
    # Execute sampler
    if initialize:
      sampler = self.sampler['init']
      steps = self.params[process]['steps_per_seed']
      steps_per_trial = self.params[process]['steps_per_seed']/10
    else:
      sampler = self.sampler[process]
      steps = self.params[process]['steps_per_sweep']
      steps_per_trial = steps

    (confs, potEs, Ht, delta_t) = sampler(server="../simbody/44ccbserver.exe", ligand_dir=os.path.dirname(self._FNs['ligand_database']) + '/', dyn_type="TD", seed=int(time.time()+reference), steps = steps, T = lambda_k['T'], delta_t = delta_t, steps_per_trial = 50)

    for e in potEs:
      print 'T= ', lambda_k['T'], 'PE=', e


    #(confs, potEs, Ht, delta_t) = sampler(steps = steps, T = lambda_k['T'], delta_t = delta_t, steps_per_trial = 50)

    #(confs, potEs, Ht, delta_t) = sampler(\
    #  steps=steps, \
    #  steps_per_trial=steps_per_trial, \
    #  T=lambda_k['T'], delta_t=delta_t, \
    #  normalize=(process=='cool'),
    #  adapt=initialize,
    #  seed=int(time.time()+reference))

    # Store and return results
    results['confs'] = np.copy(confs[-1])
    results['E_MM'] = potEs[-1]
    results['Ht'] = Ht
    results['delta_t'] = delta_t
    results['reference'] = reference
    return results

  def _sim_process(self, process):
    """
    Simulate and analyze a cooling or docking process.
    
    As necessary, first conduct an initial cooling or docking
    and then run a desired number of replica exchange cycles.
    """
    #print "  >_sim_process()" 
    if (getattr(self,process+'_protocol')==[]) or \
       (not getattr(self,process+'_protocol')[-1]['crossed']):
      time_left = getattr(self,'initial_'+process)()
      if not time_left:
        return False

    # Main loop for replica exchange
    if (self.params[process]['repX_cycles'] is not None) and \
       ((getattr(self,'_%s_cycle'%process) < \
         self.params[process]['repX_cycles'])):

      # Score configurations from another program
      if (process=='dock') and (self._dock_cycle==1) and \
         (self.params['dock']['score'] is not False) and \
         (self.params['dock']['score_multiple']):
        self._set_lock('dock')
        self.tee(">>> Reinitializing replica exchange configurations")
        (self.confs['dock']['replicas'], Es) = \
          self._get_confs_to_rescore(nconfs=len(self.dock_protocol))
        self._clear_lock('dock')

      self.tee("\n>>> Replica exchange for {0}ing, starting at {1} GMT".format(\
        process, time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())), \
        process=process)
      mem_start = psutil.virtual_memory()
      self.tee('  %f MiB available / %f MiB total'%(\
        mem_start.available/1E6, mem_start.total/1E6))
      self.timing[process+'_repX_start'] = time.time()
      start_cycle = getattr(self,'_%s_cycle'%process)
      cycle_times = []
      while ((getattr(self,'_%s_cycle'%process) < self.params[process]['repX_cycles'])):
        cycle_start_time = time.time()
        self._replica_exchange(process)
        cycle_times.append(time.time()-cycle_start_time)
        if self.run_type=='timed':
          remaining_time = self.timing['max']*60 - (time.time()-self.timing['start'])
          cycle_time = np.mean(cycle_times)
          self.tee("  projected cycle time: %s, remaining time: %s"%(\
            HMStime(cycle_time), HMStime(remaining_time)), process=process)
          if cycle_time>remaining_time:
            return False
      self.tee("\nElapsed time for %d cycles of replica exchange was %s"%(\
         (getattr(self,'_%s_cycle'%process) - start_cycle), \
          HMStime(time.time() - self.timing[process+'_repX_start'])), \
          process=process)

    # If there are insufficient configurations,
    #   do additional replica exchange on the cooling process
    if (process=='cool'):
      E_MM = []
      for k in range(len(self.cool_Es[0])):
        E_MM += list(self.cool_Es[0][k]['MM'])
      while len(E_MM)<self.params['dock']['seeds_per_state']:
        self.tee("More samples from high temperature ligand simulation needed", process='cool')
        cycle_start_time = time.time()
        self._replica_exchange('cool')
        cycle_times.append(time.time()-cycle_start_time)
        if self.run_type=='timed':
          remaining_time = self.timing['max']*60 - (time.time()-self.timing['start'])
          cycle_time = np.mean(cycle_times)
          self.tee("  projected cycle time: %s, remaining time: %s"%(\
            HMStime(cycle_time), HMStime(remaining_time)), process=process)
          if cycle_time>remaining_time:
            return False
        E_MM = []
        for k in range(len(self.cool_Es[0])):
          E_MM += list(self.cool_Es[0][k]['MM'])

    return True # The process has completed

  def _get_confs_to_rescore(self, nconfs=None):
    """
    Returns configurations to rescore and their corresponding energies 
    as a tuple of lists, ordered by DECREASING energy.
    It is either the default configuration, or from dock6 and initial docking.
    If nconfs is None, then all configurations will be unique.
    If nconfs is smaller than the number of unique configurations, 
    then the lowest energy configurations will be retained.
    If nconfs is larger than the number of unique configurations, 
    then the lowest energy configuration will be duplicated.
    """
    #print "  >_get_confs_to_rescore()" 
    lambda_1 = self._lambda(1.0,'dock', \
      MM=True, site='site' in self._forceFields.keys())
    
    if isinstance(self.params['dock']['score'], bool):
      conf = self.confs['ligand']
      self._set_universe_evaluator(lambda_1)
      self.universe.setConfiguration(Configuration(self.universe, conf))
      E = self.universe.energy()
      if nconfs is None:
        nconfs = 1
      return ([np.copy(conf) for k in range(nconfs)],[E for k in range(nconfs)])

    confs = []
    count = {'dock6':0, 'initial_dock':0, 'duplicated':0}
    
    if self.params['dock']['score'].endswith('.mol2') or \
       self.params['dock']['score'].endswith('.mol2.gz'):
      (confs_dock6,E_mol2) = self._read_dock6(self.params['dock']['score'])
      
      if 'site' in self._forceFields.keys():
        # Add configurations where the ligand is in the binding site
        self._set_universe_evaluator({'site':True,'T':T_TARGET})
        for ind in range(len(confs_dock6)):
          self.universe.setConfiguration(\
            Configuration(self.universe, confs_dock6[ind]))
          if self.universe.energy()<1.:
            confs.append(confs_dock6[ind])
      else:
        confs = confs_dock6
      count['dock6'] = len(confs)
    else:
      raise Exception('Unrecognized file type for configurations')

    if self.confs['dock']['seeds'] is not None:
      confs = confs + self.confs['dock']['seeds']
      count['initial_dock'] = len(self.confs['dock']['seeds'])
    
    if len(confs)==0:
      return ([],[])
    
    # Minimize each configuration
    self._set_universe_evaluator(lambda_1)
    
    from MMTK.Minimization import SteepestDescentMinimizer # @UnresolvedImport
    minimizer = SteepestDescentMinimizer(self.universe)

    minimized_confs = []
    minimized_conf_Es = []

    min_start_time = time.time()
    for conf in confs:
      self.universe.setConfiguration(Configuration(self.universe, conf))
      minimizer(steps = 1000)
      minimized_confs.append(np.copy(self.universe.configuration().array))
      minimized_conf_Es.append(self.universe.energy())
    self.tee("\n  minimized %d configurations in "%len(confs) + \
      HMStime(time.time()-min_start_time))

    # Sort minimized configurations by DECREASING energy
    Es, confs = \
      (list(l) for l in zip(*sorted(zip(minimized_conf_Es, minimized_confs), \
        key=lambda p:p[0], reverse=True)))

    # Shrink or extend configuration and energy array
    if nconfs is not None:
      confs = confs[-nconfs:]
      Es = Es[-nconfs:]
      while len(confs)<nconfs:
        confs.append(confs[-1])
        Es.append(Es[-1])
        count['duplicated'] += 1

    self.tee("  keeping configurations: {dock6} from dock6, {initial_dock} from initial docking, and {duplicated} duplicated\n".format(**count))
    return (confs, Es)

  def _run_MBAR(self,u_kln,N_k):
    """
    Estimates the free energy of a transition using BAR and MBAR
    """
    #print "  >_run_MBAR()" 
    import pymbar
    K = len(N_k)
    f_k_FEPF = np.zeros(K)
    f_k_FEPR = np.zeros(K)
    f_k_BAR = np.zeros(K)
    for k in range(K-1):
      w_F = u_kln[k,k+1,:N_k[k]] - u_kln[k,k,:N_k[k]]
      min_w_F = min(w_F)
      w_R = u_kln[k+1,k,:N_k[k+1]] - u_kln[k+1,k+1,:N_k[k+1]]
      min_w_R = min(w_R)
      f_k_FEPF[k+1] = -np.log(np.mean(np.exp(-w_F+min_w_F))) + min_w_F
      f_k_FEPR[k+1] = np.log(np.mean(np.exp(-w_R+min_w_R))) - min_w_R
      try:
        f_k_BAR[k+1] = pymbar.BAR(w_F, w_R, relative_tolerance=0.000001, verbose=False, compute_uncertainty=False)
      except:
        f_k_BAR[k+1] = f_k_FEPF[k+1]
    f_k_FEPF = np.cumsum(f_k_FEPF)
    f_k_FEPR = np.cumsum(f_k_FEPR)
    f_k_BAR = np.cumsum(f_k_BAR)
    try:
      f_k_MBAR = pymbar.MBAR(u_kln, N_k,
        verbose = False, method = 'adaptive',
        initial_f_k = f_k_BAR,
        maximum_iterations = 20).f_k
    except:
      f_k_MBAR = f_k_BAR
    return (f_k_BAR,f_k_MBAR)

  def _u_kln(self,eTs,lambdas,noBeta=False):
    """
    Computes a reduced potential energy matrix.  k is the sampled state.  l is the state for which energies are evaluated.
    
    Input:
    eT is a 
      -dictionary (of mapped energy terms) of numpy arrays (over states)
      -list (over states) of dictionaries (of mapped energy terms) of numpy arrays (over configurations), or a
      -list (over states) of lists (over cycles) of dictionaries (of mapped energy terms) of numpy arrays (over configurations)
    lambdas is a list of thermodynamic states
    noBeta means that the energy will not be divided by RT
    
    Output: u_kln or (u_kln, N_k)
    u_kln is the matrix (as a numpy array)
    N_k is an array of sample sizes
    """
    L = len(lambdas)

    addMM = ('MM' in lambdas[0].keys()) and (lambdas[0]['MM'])
    addSite = ('site' in lambdas[0].keys()) and (lambdas[0]['site'])
    probe_key = [key for key in lambdas[0].keys() if key in (['MM'] + self._scalables)][0]
    
    if isinstance(eTs,dict):
      # There is one configuration per state
      K = len(eTs[probe_key])
      N_k = np.ones(K, dtype=int)
      u_kln = []
      E_base = np.zeros(K)
      if addMM:
        E_base += eTs['MM']
      if addSite:
        E_base += eTs['site']
      for l in range(L):
        E = 1.*E_base
        for scalable in self._scalables:
          if scalable in lambdas[l].keys():
            E += lambdas[l][scalable]*eTs[scalable]
        if noBeta:
          u_kln.append(E)
        else:
          u_kln.append(E/(R*lambdas[l]['T']))
    elif isinstance(eTs[0],dict):
      K = len(eTs)
      N_k = np.array([len(eTs[k][probe_key]) for k in range(K)])
      u_kln = np.zeros([K, L, N_k.max()], np.float)

      for k in range(K):
        E_base = 0.0
        if addMM:
          E_base += eTs[k]['MM']
        if addSite:
          E_base += eTs[k]['site']          
      for l in range(L):
        E = 1.*E_base
        for scalable in self._scalables:
          if scalable in lambdas[l].keys():
            E += lambdas[l][scalable]*eTs[k][scalable]
        if noBeta:
          u_kln[k,l,:N_k[k]] = E
        else:
          u_kln[k,l,:N_k[k]] = E/(R*lambdas[l]['T'])
    elif isinstance(eTs[0],list):
      K = len(eTs)
      N_k = np.zeros(K, dtype=int)

      for k in range(K):
        for c in range(len(eTs[k])):
          N_k[k] += len(eTs[k][c][probe_key])
      u_kln = np.zeros([K, L, N_k.max()], np.float)

      for k in range(K):
        E_base = 0.0
        C = len(eTs[k])
        if addMM:
          E_base += np.concatenate([eTs[k][c]['MM'] for c in range(C)])
        if addSite:
          E_base += np.concatenate([eTs[k][c]['site'] for c in range(C)])
        for l in range(L):
          E = 1.*E_base
          for scalable in self._scalables:
            if scalable in lambdas[l].keys():
              E += lambdas[l][scalable]*np.concatenate([eTs[k][c][scalable] for c in range(C)])
          if noBeta:
            u_kln[k,l,:N_k[k]] = E
          else:
            u_kln[k,l,:N_k[k]] = E/(R*lambdas[l]['T'])

    if (K==1) and (L==1):
      return u_kln.ravel()
    else:
      return (u_kln,N_k)

  def _next_dock_state(self, E=None, lambda_o=None, pow=None, undock=False):
    """
    Determines the parameters for the next docking state
    """
    #print "  >_next_dock_state()" 
    
    if E is None:
      E = self.dock_Es[-1]

    if lambda_o is None:
      lambda_o = self.dock_protocol[-1]
    lambda_n = copy.deepcopy(lambda_o)
    
    if self.params['dock']['protocol']=='Set':
      raise Exception("Set protocol not currently supported")
    elif self.params['dock']['protocol']=='Adaptive':
      # Change grid scaling and temperature simultaneously
      tL_tensor = self._tL_tensor(E,lambda_o)
      crossed = lambda_o['crossed']
      if pow is not None:
        tL_tensor = tL_tensor*(1.25**pow)
      if tL_tensor>1E-5:
        dL = self.params['dock']['therm_speed']/tL_tensor
        if undock:
          a = lambda_o['a'] - dL
          if a < 0.0:
            a = 0.0
            crossed = True
        else:
          a = lambda_o['a'] + dL
          if a > 1.0:
            a = 1.0
            crossed = True
        return self._lambda(a, crossed=crossed)
      else:
        # Repeats the previous stage
        lambda_n['delta_t'] = lambda_o['delta_t']*(1.25**pow)
        self.tee('  no variance in previous stage!' + \
          ' trying time step of %f'%lambda_n['delta_t'])
        return lambda_n

  def _tL_tensor(self, E, lambda_c, process='dock'):
    T = lambda_c['T']
    if process=='dock':
      # Metric tensor for the thermodynamic length
      a = lambda_c['a']
      a_sg = 1.-4.*(a-0.5)**2
      a_g = 4.*(a-0.5)**2/(1+np.exp(-100*(a-0.5)))
      da_sg_da = -8*(a-0.5)
      da_g_da = (400.*(a-0.5)**2*np.exp(-100.*(a-0.5)))/(1+np.exp(-100.*(a-0.5)))**2 + \
        (8.*(a-0.5))/(1 + np.exp(-100.*(a-0.5)))
      Psi_sg = self._u_kln([E], [{'sLJr':1,'sELE':1}], noBeta=True)
      Psi_g = self._u_kln([E], [{'LJr':1,'LJa':1,'ELE':1}], noBeta=True)
      U_RL_g = self._u_kln([E],
        [{'MM':True, 'site':True, 'T':T,\
        'sLJr':a_sg, 'sELE':a_sg, 'LJr':a_g, 'LJa':a_g, 'ELE':a_g}], noBeta=True)
      return np.abs(da_sg_da)*Psi_sg.std()/(R*T) + \
             np.abs(da_g_da)*Psi_g.std()/(R*T) + \
             np.abs(da_g_da)*np.abs(T_TARGET-T_HIGH)*U_RL_g.std()/(R*T*T)
    elif process=='cool':
      return self._u_kln([E],[{'MM':True}], noBeta=True).std()/(R*T*T)
    else:
      raise Exception("Unknown process!")

  def _lambda(self, a, process='dock', lambda_o=None, \
      MM=None, site=None, crossed=None):

    if (lambda_o is None) and len(getattr(self,process+'_protocol'))>0:
      lambda_o = copy.deepcopy(getattr(self,process+'_protocol')[-1])
    if (lambda_o is not None):
      lambda_n = copy.deepcopy(lambda_o)
    else:
      lambda_n = {}
    if MM is not None:
      lambda_n['MM'] = MM
    if site is not None:
      lambda_n['site'] = site
    if crossed is not None:
      lambda_n['crossed'] = crossed

    if process=='dock':
      a_sg = 1.-4.*(a-0.5)**2
      a_g = 4.*(a-0.5)**2/(1+np.exp(-100*(a-0.5)))
      if a_g<1E-10:
        a_g=0
      lambda_n['a'] = a
      lambda_n['sLJr'] = a_sg
      lambda_n['sELE'] = a_sg
      lambda_n['LJr'] = a_g
      lambda_n['LJa'] = a_g
      lambda_n['ELE'] = a_g
      lambda_n['T'] = a_g*(T_TARGET-T_HIGH) + T_HIGH
    elif process=='cool':
      lambda_n['a'] = a
      lambda_n['T'] = T_HIGH - a*(T_HIGH-T_TARGET)
    else:
      raise Exception("Unknown process!")

    return lambda_n

  def _postprocess(self,
      conditions=[('original',0, 0,'R'), ('cool',-1,-1,'L'), \
                  ('dock',   -1,-1,'L'), ('dock',-1,-1,'RL')],
      phases=None,
      readOnly=False, redo_dock=False, debug=False):
    """
    Obtains the NAMD energies of all the conditions using all the phases.  
    Saves both MMTK and NAMD energies after NAMD energies are estimated.
    
    state == -1 means the last state
    cycle == -1 means all cycles

    """
    #print "  >_postprocess()" 
    # Clear evaluators to save memory
    self._evaluators = {}
    
    if phases is None:
      phases = list(set(self.params['cool']['phases'] + self.params['dock']['phases']))

    updated_processes = []
    if 'APBS' in phases:
      updated_processes = self._combine_APBS_and_NAMD(updated_processes)
    
    # Identify incomplete calculations
    incomplete = []
    for (p, state, cycle, moiety) in conditions:
      # Check that the values are legitimate
      if not p in ['cool','dock','original']:
        raise Exception("Type should be in ['cool', 'dock', 'original']")
      if not moiety in ['R','L', 'RL']:
        raise Exception("Species should in ['R','L', 'RL']")
      if p!='original' and getattr(self,p+'_protocol')==[]:
        continue
      if state==-1:
        state = len(getattr(self,p+'_protocol'))-1
      if cycle==-1:
        cycles = range(getattr(self,'_'+p+'_cycle'))
      else:
        cycles = [cycle]

      # Check for completeness
      for c in cycles:
        for phase in phases:
          label = moiety+phase
          
          # Skip postprocessing
          # if the function is NOT being rerun in redo mode
          # and one of the following:
          # the function is being run in readOnly mode,
          # the energies are already in memory.
          if (not (redo_dock and p=='dock')) and \
            (readOnly \
            or (p == 'original' and \
                (label in getattr(self,p+'_Es')[state][c].keys()) and \
                (getattr(self,p+'_Es')[state][c][label] is not None)) \
            or (('MM' in getattr(self,p+'_Es')[state][c].keys()) and \
                (label in getattr(self,p+'_Es')[state][c].keys()) and \
                (len(getattr(self,p+'_Es')[state][c]['MM'])==\
                 len(getattr(self,p+'_Es')[state][c][label])))):
            pass
          else:
            incomplete.append((p, state, c, moiety, phase))

    if incomplete==[]:
      return True
    
    del p, state, c, moiety, phase, cycles, label
    
    # Find the necessary programs, downloading them if necessary
    programs = []
    for (p, state, c, moiety, phase) in incomplete:
      if phase in ['Gas','GBSA','PBSA'] and not 'sander' in programs:
        programs.append('sander')
      elif phase in ['NAMD_Gas','NAMD_OBC'] and not 'namd' in programs:
        programs.append('namd')
      elif phase in ['APBS'] and not 'apbs' in programs:
        programs.extend(['apbs','ambpdb','molsurf'])

    for program in programs:
      self._FNs[program] = a.findPaths([program])[program]

    # Write trajectories and queue calculations
    m = multiprocessing.Manager()
    task_queue = m.Queue()
    time_per_snap = m.dict()
    for (p, state, c, moiety, phase) in incomplete:
      if moiety+phase not in time_per_snap.keys():
        time_per_snap[moiety+phase] = m.list()

    # Decompress prmtop and inpcrd files
    decompress = (self._FNs['prmtop'][moiety].endswith('.gz')) or \
                 (self._FNs['inpcrd'][moiety].endswith('.gz'))
    if decompress:
      for key in ['prmtop','inpcrd']:
        if self._FNs[key][moiety].endswith('.gz'):
          import shutil
          shutil.copy(self._FNs[key][moiety],self._FNs[key][moiety]+'.BAK')
          os.system('gunzip -f '+self._FNs[key][moiety])
          os.rename(self._FNs[key][moiety]+'.BAK', self._FNs[key][moiety])
          self._FNs[key][moiety] = self._FNs[key][moiety][:-3]

    toClean = []

    for (p, state, c, moiety, phase) in incomplete:
      # Identify the configurations
      if (moiety=='R'):
        if not 'receptor' in self.confs.keys():
          continue
        confs = [self.confs['receptor']]
      else:
        confs = self.confs[p]['samples'][state][c]

      # Identify the file names
      if p=='original':
        prefix = p
      else:
        prefix = '%s%d_%d'%(p, state, c)

      p_dir = {'cool':self.dir['cool'],
         'original':self.dir['dock'],
         'dock':self.dir['dock']}[p]
      
      if phase in ['Gas','GBSA','PBSA']:
        traj_FN = join(p_dir,'%s.%s.mdcrd'%(prefix,moiety))
      elif phase in ['NAMD_Gas','NAMD_OBC']:
        traj_FN = join(p_dir,'%s.%s.dcd'%(prefix,moiety))
      elif phase in ['APBS']:
        traj_FN = join(p_dir,'%s.%s.pqr'%(prefix,moiety))
      outputname = join(p_dir,'%s.%s%s'%(prefix,moiety,phase))

      # Writes trajectory
      self._write_traj(traj_FN, confs, moiety)
      if not traj_FN in toClean:
        toClean.append(traj_FN)

      # Queues the calculations
      task_queue.put((confs, moiety, phase, traj_FN, outputname, debug, \
              (p,state,c,moiety+phase)))

    # Start postprocessing
    self._set_lock('dock' if 'dock' in [loc[0] for loc in incomplete] else 'cool')
    self.tee("\n>>> Postprocessing, starting at " + \
      time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))
    postprocess_start_time = time.time()

    done_queue = m.Queue()
    processes = [multiprocessing.Process(target=self._energy_worker, \
        args=(task_queue, done_queue, time_per_snap)) \
        for p in range(self._cores)]
    for p in range(self._cores):
      task_queue.put('STOP')
    for p in processes:
      p.start()
    for p in processes:
      p.join()
    results = []
    while not done_queue.empty():
      results.append(done_queue.get())
    for p in processes:
      p.terminate()

    # Clean up files
    if not debug:
      for FN in toClean:
        if os.path.isfile(FN):
          os.remove(FN)

    # Clear decompressed files
    if decompress:
      for key in ['prmtop','inpcrd']:
        if os.path.isfile(self._FNs[key][moiety]+'.gz'):
          os.remove(self._FNs[key][moiety])
          self._FNs[key][moiety] = self._FNs[key][moiety] + '.gz'

    # Store energies
    for (E,(p,state,c,label),wall_time) in results:
      if p=='original':
        self.original_Es[state][c][label] = E[:,-1]
      else:
        getattr(self,p+'_Es')[state][c][label] = E
      if not p in updated_processes:
        updated_processes.append(p)

    # Print time per snapshot
    for key in time_per_snap.keys():
      if len(time_per_snap[key])>0:
        mean_time_per_snap = np.mean(time_per_snap[key])
        if not np.isnan(mean_time_per_snap):
          self.tee("  an average of %f s per %s snapshot"%(\
            mean_time_per_snap, key))
        else:
          self.tee("  time per snapshot in %s: "%(key) + \
            ', '.join(['%f'%t for t in time_per_snap[key]]))
      else:
        self.tee("  no snapshots postprocessed in %s"%(key))

    # Combine APBS and NAMD_Gas energies
    if 'APBS' in phases:
      updated_processes = self._combine_APBS_and_NAMD(updated_processes)

    # Save data
    if 'original' in updated_processes:
      for phase in phases:
        if (self.params['dock']['receptor_'+phase] is None) and \
           (self.original_Es[0][0]['R'+phase] is not None):
          self.params['dock']['receptor_'+phase] = \
            self.original_Es[0][0]['R'+phase]
      self._save('dock', keys=['progress'])
    if 'cool' in updated_processes:
      self._save('cool')
    if ('dock' in updated_processes) or ('original' in updated_processes):
      self._save('dock')

    if len(updated_processes)>0:
      self._clear_lock('dock' if 'dock' in updated_processes else 'cool')
      self.tee("\nElapsed time for postprocessing was " + \
        HMStime(time.time()-postprocess_start_time))
      return len(incomplete)==len(results)

  def _energy_worker(self, input, output, time_per_snap):
    for args in iter(input.get, 'STOP'):
      (confs, moiety, phase, traj_FN, outputname, debug, reference) = args
      (p, state, c, label) = reference
      nsnaps = len(confs)
      
      # Make sure there is enough time remaining
      if self.run_type=='timed':
        remaining_time = self.timing['max']*60 - \
          (time.time()-self.timing['start'])
        if len(time_per_snap[moiety+phase])>0:
          mean_time_per_snap = np.mean(np.mean(time_per_snap[moiety+phase]))
          if np.isnan(mean_time_per_snap):
            return
          projected_time = mean_time_per_snap*nsnaps
          self.tee("  projected cycle time for %s: %s, remaining time: %s"%(\
            moiety+phase, \
            HMStime(projected_time), HMStime(remaining_time)), process=p)
          if projected_time > remaining_time:
            return
    
      # Calculate the energy
      start_time = time.time()
      if phase in ['Gas','GBSA','PBSA']:
        E = self._sander_Energy(*args)
      elif phase in ['NAMD_Gas','NAMD_OBC']:
        E = self._NAMD_Energy(*args)
      elif phase in ['APBS']:
        E = self._APBS_Energy(*args)
      wall_time = time.time() - start_time

      if not np.isinf(E).any():
        self.tee("  postprocessed %s, state %d, cycle %d, %s in %s"%(\
          p,state,c,label,HMStime(wall_time)))
          
        # Store output and timings
        output.put((E, reference, wall_time))

        times_per_snap = time_per_snap[moiety+phase]
        times_per_snap.append(wall_time/nsnaps)
        time_per_snap[moiety+phase] = times_per_snap
      else:
        self.tee("  error in postprocessing %s, state %d, cycle %d, %s in %s"%(\
          p,state,c,label,HMStime(wall_time)))
        return

  def _combine_APBS_and_NAMD(self, updated_processes = []):
    psmc = [\
      ('cool', len(self.cool_protocol)-1, ['L'], range(self._cool_cycle)), \
      ('dock', len(self.dock_protocol)-1, ['L','RL'], range(self._dock_cycle)), \
      ('original', 0, ['R'], [0])]
    for (p,state,moieties,cycles) in psmc:
      for moiety in moieties:
        label = moiety + 'APBS'
        for c in cycles:
          if not ((label in getattr(self,p+'_Es')[state][c]) and \
              (getattr(self,p+'_Es')[state][c][label] is not None) and \
              (moiety+'NAMD_Gas' in getattr(self,p+'_Es')[state][c]) and \
              (getattr(self,p+'_Es')[state][c][moiety+'NAMD_Gas'] is not None)):
            break
          if len(getattr(self,p+'_Es')[state][c][label].shape)==1 and \
              (getattr(self,p+'_Es')[state][c][label].shape[0]==2):
            E_PBSA = getattr(self,p+'_Es')[state][c][label]
            E_NAMD_Gas = getattr(self,p+'_Es')[state][c][moiety+'NAMD_Gas'][-1]
            E_tot = np.sum(E_PBSA) + E_NAMD_Gas
            getattr(self,p+'_Es')[state][c][label] = \
              np.hstack((E_PBSA,E_NAMD_Gas,E_tot))
            if not p in updated_processes:
              updated_processes.append(p)
          elif len(getattr(self,p+'_Es')[state][c][label].shape)==2 and \
              (getattr(self,p+'_Es')[state][c][label].shape[1]==2):
            E_PBSA = getattr(self,p+'_Es')[state][c][label]
            E_NAMD_Gas = getattr(self,p+'_Es')[state][c][moiety+'NAMD_Gas']
            if len(E_NAMD_Gas.shape)==1:
              E_NAMD_Gas = np.array([[E_NAMD_Gas[-1]]])
            else:
              E_NAMD_Gas = E_NAMD_Gas[:,-1]
            E_tot = np.sum(E_PBSA,1) + E_NAMD_Gas
            getattr(self,p+'_Es')[state][c][label] = \
              np.hstack((E_NAMD_Gas[...,None],E_PBSA,E_tot[...,None]))
            if not p in updated_processes:
              updated_processes.append(p)
    return updated_processes

  def _calc_E(self, confs, E=None, type='sampling', prefix='confs'):
    """
    Calculates energies for a series of configurations
    Units are the MMTK standard, kJ/mol
    """
    if E is None:
      E = {}

    lambda_full = {'T':T_HIGH,'MM':True,'site':True}
    for scalable in self._scalables:
      lambda_full[scalable] = 1
    
    if type=='sampling' or type=='all':
      # Molecular mechanics and grid interaction energies
      self._set_universe_evaluator(lambda_full)
      for term in (['MM','site','misc'] + self._scalables):
        E[term] = np.zeros(len(confs), dtype=float)
      for c in range(len(confs)):
        self.universe.setConfiguration(Configuration(self.universe,confs[c]))
        eT = self.universe.energyTerms()
        for (key,value) in eT.iteritems():
          E[term_map[key]][c] += value

    if type=='all':
      toClear = []
      for phase in self.params['dock']['phases']:
        E['R'+phase] = self.params['dock']['receptor_'+phase]
        for moiety in ['L','RL']:
          outputname = join(self.dir['dock'],'%s.%s%s'%(prefix,moiety,phase))
          if phase in ['NAMD_Gas','NAMD_OBC']:
            traj_FN = join(self.dir['dock'],'%s.%s.dcd'%(prefix,moiety))
            self._write_traj(traj_FN, confs, moiety)
            E[moiety+phase] = self._NAMD_Energy(confs, moiety, phase, traj_FN, outputname)
          elif phase in ['Gas','GBSA','PBSA']:
            traj_FN = join(self.dir['dock'],'%s.%s.mdcrd'%(prefix,moiety))
            self._write_traj(traj_FN, confs, moiety)
            E[moiety+phase] = self._sander_Energy(confs, moiety, phase, traj_FN, outputname)
          elif phase in ['APBS']:
            traj_FN = join(self.dir['dock'],'%s.%s.pqr'%(prefix,moiety))
            E[moiety+phase] = self._APBS_Energy(confs, moiety, phase, traj_FN, outputname)
          else:
            raise Exception('Unknown phase!')
          if not traj_FN in toClear:
            toClear.append(traj_FN)
      for FN in toClear:
        if os.path.isfile(FN):
          os.remove(FN)
    return E

  def _sander_Energy(self, confs, moiety, phase, AMBER_mdcrd_FN,
      outputname=None, debug=False, reference=None):
    self.dir['out'] = os.path.dirname(os.path.abspath(AMBER_mdcrd_FN))
    script_FN = '%s%s.in'%('.'.join(AMBER_mdcrd_FN.split('.')[:-1]),phase)
    out_FN = '%s%s.out'%('.'.join(AMBER_mdcrd_FN.split('.')[:-1]),phase)

    script_F = open(script_FN,'w')
    if phase=='PBSA':
      if moiety=='L':
        fillratio = 4.
      else:
        fillratio = 2.
      script_F.write('''Calculate PBSA energies
&cntrl
  imin=5,    ! read trajectory in for analysis
  ntx=1,     ! input is read formatted with no velocities
  irest=0,
  ntb=0,     ! no periodicity and no PME
  idecomp=0, ! no decomposition
  ntc=1,     ! No SHAKE
  ntf=1,     ! Complete interaction is calculated
  ipb=2,     ! Default PB dielectric model
  inp=1,     ! SASA non-polar
/
&pb
  radiopt=0, ! Use atomic radii from the prmtop file
  fillratio=%f,
  sprob=1.4,
  cavity_surften=0.005,
  cavity_offset=0.000,
/
'''%fillratio)
    elif phase=='GBSA':
      script_F.write('''Calculate GBSA energies
&cntrl
  imin=5,    ! read trajectory in for analysis
  ntx=1,     ! input is read formatted with no velocities
  irest=0,
  ntb=0,     ! no periodicity and no PME
  idecomp=0, ! no decomposition
  ntc=1,     ! No SHAKE
  ntf=1,     ! Complete interaction is calculated
  igb=8,     ! Most recent AMBER GBn model, best agreement with PB
  gbsa=2,    ! recursive surface area algorithm
/
''')
    elif phase=='Gas':
      script_F.write('''Calculate Gas energies
&cntrl
  imin=5,    ! read trajectory in for analysis
  ntx=1,     ! input is read formatted with no velocities
  irest=0,
  ntb=0,     ! no periodicity and no PME
  idecomp=0, ! no decomposition
  ntc=1,     ! No SHAKE
  ntf=1,     ! Complete interaction is calculated
  cut=9999., !
/
''')
    script_F.close()
    
    os.chdir(self.dir['out'])
    import subprocess
    p = subprocess.Popen([self._FNs['sander'], '-O','-i',script_FN,'-o',out_FN, \
      '-p',self._FNs['prmtop'][moiety],'-c',self._FNs['inpcrd'][moiety], \
      '-y', AMBER_mdcrd_FN, '-r',script_FN+'.restrt'])
    p.wait()
    
    F = open(out_FN,'r')
    dat = F.read().strip().split(' BOND')
    F.close()

    dat.pop(0)
    if len(dat)>0:
      E = np.array([rec[:rec.find('\nminimization')].replace('1-4 ','1-4').split()[1::3] for rec in dat],dtype=float)*MMTK.Units.kcal/MMTK.Units.mol
      E = np.hstack((E,np.sum(E,1)[...,None]))

      if not debug and os.path.isfile(script_FN):
        os.remove(script_FN)
      if os.path.isfile(script_FN+'.restrt'):
        os.remove(script_FN+'.restrt')

      if not debug and os.path.isfile(out_FN):
        os.remove(out_FN)
    else:
      E = np.array([np.inf]*11)

    os.chdir(self.dir['start'])
    return E
    # AMBER ENERGY FIELDS:
    # For Gas phase:
    # 0. BOND 1. ANGLE 2. DIHEDRAL 3. VDWAALS 4. EEL
    # 5. HBOND 6. 1-4 VWD 7. 1-4 EEL 8. RESTRAINT
    # For GBSA phase:
    # 0. BOND 1. ANGLE 2. DIHEDRAL 3. VDWAALS 4. EEL
    # 5. EGB 6. 1-4 VWD 7. 1-4 EEL 8. RESTRAINT 9. ESURF

  def _NAMD_Energy(self, confs, moiety, phase, dcd_FN, outputname,
      debug=False, reference=None):
    """
    Uses NAMD to calculate the energy of a set of configurations
    Units are the MMTK standard, kJ/mol
    """
    # NAMD ENERGY FIELDS:
    # 0. TS 1. BOND 2. ANGLE 3. DIHED 4. IMPRP 5. ELECT 6. VDW 7. BOUNDARY
    # 8. MISC 9. KINETIC 10. TOTAL 11. TEMP 12. POTENTIAL 13. TOTAL3 14. TEMPAVG
    # The saved fields are energyFields=[1, 2, 3, 4, 5, 6, 8, 12],
    # and thus the new indicies are
    # 0. BOND 1. ANGLE 2. DIHED 3. IMPRP 4. ELECT 5. VDW 6. MISC 7. POTENTIAL
    
    # Run NAMD
    import AlGDock.NAMD
    energyCalc = AlGDock.NAMD.NAMD(\
      prmtop=self._FNs['prmtop'][moiety], \
      inpcrd=self._FNs['inpcrd'][moiety], \
      fixed={'R':self._FNs['fixed_atoms']['R'], \
             'L':None, \
             'RL':self._FNs['fixed_atoms']['RL']}[moiety], \
      solvent={'NAMD_OBC':'GBSA', 'NAMD_Gas':'Gas'}[phase], \
      useCutoff=(phase=='NAMD_OBC'), \
      namd_command=self._FNs['namd'])
    E = energyCalc.energies_PE(\
      outputname, dcd_FN, energyFields=[1, 2, 3, 4, 5, 6, 8, 12], \
      keepScript=debug, writeEnergyDatGZ=False)

    return np.array(E, dtype=float)*MMTK.Units.kcal/MMTK.Units.mol

  def _APBS_Energy(self, confs, moiety, phase, pqr_FN, outputname,
      debug=False, reference=None, factor=1.0/MMTK.Units.Ang):
    """
    Uses NAMD to calculate the energy of a set of configurations
    Units are the MMTK standard, kJ/mol
    """
    # Prepare configurations for writing to crd file
    if (moiety.find('R')>-1):
      receptor_0 = factor*self.confs['receptor'][:self._ligand_first_atom,:]
      receptor_1 = factor*self.confs['receptor'][self._ligand_first_atom:,:]

    if not isinstance(confs,list):
      confs = [confs]
    
    if (moiety.find('R')>-1):
      if (moiety.find('L')>-1):
        full_confs = [np.vstack((receptor_0, \
          conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang, \
          receptor_1)) for conf in confs]
      else:
        full_confs = [factor*self.confs['receptor']]
    else:
      full_confs = [conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang \
        for conf in confs]

    # Write coordinates, run APBS, and store energies
    apbs_dir = pqr_FN[:-4]
    os.system('mkdir -p '+apbs_dir)
    os.chdir(apbs_dir)
    pqr_FN = os.path.join(apbs_dir, 'in.pqr')

    import subprocess
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()

    E = []
    for full_conf in full_confs:
      # Writes the coordinates in AMBER format
      inpcrd_FN = pqr_FN[:-4]+'.crd'
      IO_crd.write(inpcrd_FN, full_conf, 'title', trajectory=False)
      
      # Converts the coordinates to a pqr file
      inpcrd_F = open(inpcrd_FN,'r')
      cdir = os.getcwd()
      p = subprocess.Popen(\
        [os.path.relpath(self._FNs['ambpdb'], cdir), \
         '-p', os.path.relpath(self._FNs['prmtop'][moiety], cdir), \
         '-pqr'], \
        stdin=inpcrd_F, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata_ambpdb, stderrdata_ambpdb) = p.communicate()
      p.wait()
      inpcrd_F.close()
      
      pqr_F = open(pqr_FN,'w')
      pqr_F.write(stdoutdata_ambpdb)
      pqr_F.close()
      
      # Writes APBS script
      apbs_in_FN = moiety+'apbs-mg-manual.in'
      apbs_in_F = open(apbs_in_FN,'w')
      apbs_in_F.write('READ\n  mol pqr {0}\nEND\n'.format(pqr_FN))

      for sdie in [80.0,2.0]:
        if moiety=='L':
          min_xyz = np.array([min(full_conf[a,:]) for a in range(3)])
          max_xyz = np.array([max(full_conf[a,:]) for a in range(3)])
          mol_range = max_xyz - min_xyz
          mol_center = (min_xyz + max_xyz)/2.
          
          def roundUpDime(x):
            return (np.ceil((x.astype(float)-1)/32)*32+1).astype(int)
          
          focus_spacing = 0.5
          focus_dims = roundUpDime(mol_range*1.5/focus_spacing)
          args = zip(['mdh'],[focus_dims],[mol_center],[focus_spacing])
        else:
          args = zip(['mdh','focus'],
            self._apbs_grid['dime'], self._apbs_grid['gcent'],
            self._apbs_grid['spacing'])
        for (bcfl,dime,gcent,grid) in args:
          apbs_in_F.write('''ELEC mg-manual
  bcfl {0} # multiple debye-huckel boundary condition
  chgm spl4 # quintic B-spline charge discretization
  dime {1[0]} {1[1]} {1[2]}
  gcent {2[0]} {2[1]} {2[2]}
  grid {3} {3} {3}
  lpbe # Linearized Poisson-Boltzmann
  mol 1
  pdie 2.0
  sdens 10.0
  sdie {4}
  srad 1.4
  srfm smol # Smoothed dielectric and ion-accessibility coefficients
  swin 0.3
  temp 300.0
  calcenergy total
END
'''.format(bcfl,dime,gcent,grid,sdie))
      apbs_in_F.write('quit\n')
      apbs_in_F.close()

      # Runs APBS
      p = subprocess.Popen([self._FNs['apbs'], apbs_in_FN], \
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata, stderrdata) = p.communicate()
      p.wait()

      apbs_energy = [float(line.split('=')[-1][:-7]) \
        for line in stdoutdata.split('\n') \
        if line.startswith('  Total electrostatic energy')]
      if moiety=='L' and len(apbs_energy)==2:
        polar_energy = apbs_energy[0]-apbs_energy[1]
      elif len(apbs_energy)==4:
        polar_energy = apbs_energy[1]-apbs_energy[3]
      else:
        # An error has occured in APBS
        polar_energy = np.inf
        self.tee("  error has occured in APBS after %d snapshots"%len(E))
        self.tee("  prmtop was "+self._FNs['prmtop'][moiety])
        self.tee("  --- ambpdb stdout:")
        self.tee(stdoutdata_ambpdb)
        self.tee("  --- ambpdb stderr:")
        self.tee(stderrdata_ambpdb)
        self.tee("  --- APBS stdout:")
        self.tee(stdoutdata)
        self.tee("  --- APBS stderr:")
        self.tee(stderrdata)
      
      # Runs molsurf to calculate Connolly surface
      apolar_energy = np.inf
      p = subprocess.Popen([self._FNs['molsurf'], pqr_FN, '1.4'], \
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata, stderrdata) = p.communicate()
      p.wait()

      for line in stdoutdata.split('\n'):
        if line.startswith('surface area ='):
          apolar_energy = float(line.split('=')[-1]) * \
            0.0072 * MMTK.Units.kcal/MMTK.Units.mol

      for FN in [inpcrd_FN, pqr_FN, apbs_in_FN, 'io.mc']:
        os.remove(FN)
      
      E.append([polar_energy, apolar_energy])

      if np.isinf(polar_energy) or np.isinf(apolar_energy):
        break

    os.chdir(self.dir['start'])
    os.system('rm -rf '+apbs_dir)
    return np.array(E, dtype=float)*MMTK.Units.kJ/MMTK.Units.mol

  def _write_traj(self, traj_FN, confs, moiety, \
      title='', factor=1.0/MMTK.Units.Ang):
    """
    Writes a trajectory file
    """
    
    if traj_FN is None:
      return
    if traj_FN.endswith('.pqr'):
      return
    if os.path.isfile(traj_FN):
      return
    
    traj_dir = os.path.dirname(os.path.abspath(traj_FN))
    if not os.path.isdir(traj_dir):
      os.system('mkdir -p '+traj_dir)

    import AlGDock.IO
    if traj_FN.endswith('.dcd'):
      IO_dcd = AlGDock.IO.dcd(self.molecule,
        ligand_atom_order = self.molecule.prmtop_atom_order, \
        receptorConf = self.confs['receptor'], \
        ligand_first_atom = self._ligand_first_atom)
      IO_dcd.write(traj_FN, confs,
        includeReceptor=(moiety.find('R')>-1),
        includeLigand=(moiety.find('L')>-1))
    elif traj_FN.endswith('.mdcrd'):
      if (moiety.find('R')>-1):
        receptor_0 = factor*self.confs['receptor'][:self._ligand_first_atom,:]
        receptor_1 = factor*self.confs['receptor'][self._ligand_first_atom:,:]

      if not isinstance(confs,list):
        confs = [confs]
      if (moiety.find('R')>-1):
        if (moiety.find('L')>-1):
          confs = [np.vstack((receptor_0, \
            conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang, \
            receptor_1)) for conf in confs]
        else:
          confs = [factor*self.confs['receptor']]
      else:
        confs = [conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang \
          for conf in confs]
      
      import AlGDock.IO
      IO_crd = AlGDock.IO.crd()
      IO_crd.write(traj_FN, confs, title, trajectory=True)
      self.tee("  wrote %d configurations to %s"%(len(confs), traj_FN))
    else:
      raise Exception('Unknown trajectory type')
      
  def _read_dock6(self, mol2FN):
    """
    Read output from UCSF DOCK 6.
    The units of the DOCK suite of programs are 
      lengths in angstroms, 
      masses in atomic mass units, 
      charges in electron charges units, and 
      energies in kcal/mol
    """
    # Specifically to read output from UCSF dock6
    if not os.path.isfile(mol2FN):
      raise Exception('mol2 file %s does not exist!'%mol2FN)
    if mol2FN.endswith('.mol2'):
      mol2F = open(mol2FN,'r')
    elif mol2FN.endswith('.mol2.gz'):
      mol2F = gzip.open(mol2FN,'r')
    models = mol2F.read().strip().split('########## Name:')
    mol2F.close()
    models.pop(0)
    
    crds = []
    E = {}
    if len(models)>0:
      for line in models[0].split('\n'):
        if line.startswith('##########'):
          label = line[11:line.find(':')].strip()
          E[label] = []
      
      for model in models:
        fields = model.split('<TRIPOS>')
        for line in fields[0].split('\n'):
          if line.startswith('##########'):
            label = line[11:line.find(':')].strip()
            E[label].append(float(line.split()[-1]))
        crds.append(np.array([l.split()[2:5] for l in fields[2].split('\n')[1:-1]],
          dtype=float)[self.molecule.inv_prmtop_atom_order,:]/10.)
    return (crds,E)

  def _load_pkl_gz(self, FN):
    if os.path.isfile(FN) and os.path.getsize(FN)>0:
      F = gzip.open(FN,'r')
      try:
        data = pickle.load(F)
      except:
        self.tee('  error loading '+FN)
        F.close()
        return None
      F.close()
      return data
    else:
      return None

  def _write_pkl_gz(self, FN, data):

    F = gzip.open(FN,'w')
    pickle.dump(data,F)
    F.close()
    self.tee("  wrote to "+FN)

  def _load(self, p):
    progress_FN = join(self.dir[p],'%s_progress.pkl.gz'%(p))
    data_FN = join(self.dir[p],'%s_data.pkl.gz'%(p))
    saved = {'progress':self._load_pkl_gz(progress_FN),
             'data':self._load_pkl_gz(data_FN)}
    if (saved['progress'] is None) or (saved['data'] is None):
      if os.path.isfile(progress_FN):
        os.remove(progress_FN)
      if os.path.isfile(data_FN):
        os.remove(data_FN)
      progress_FN = join(self.dir[p],'%s_progress.pkl.gz.BAK'%(p))
      data_FN = join(self.dir[p],'%s_data.pkl.gz.BAK'%(p))
      saved = {'progress':self._load_pkl_gz(progress_FN),
               'data':self._load_pkl_gz(data_FN)}
      if (saved['progress'] is None) or (saved['data'] is None):
        if os.path.isfile(progress_FN):
          os.remove(progress_FN)
        if os.path.isfile(data_FN):
          os.remove(data_FN)
      else:
        print '  using backed up progress and data for %s progress'%p

    self._clear(p)
    
    params = None
    if saved['progress'] is not None:
      params = saved['progress'][0]
      setattr(self,'%s_protocol'%p,saved['progress'][1])
      setattr(self,'_%s_cycle'%p,saved['progress'][2])
    if saved['data'] is not None:
      if p=='dock' and saved['data'][0] is not None:
        (self._n_trans, self._max_n_trans, self._random_trans, \
         self._n_rot, self._max_n_rot, self._random_rotT) = saved['data'][0]
      self.confs[p]['replicas'] = saved['data'][1]
      self.confs[p]['seeds'] = saved['data'][2]
      self.confs[p]['samples'] = saved['data'][3]
      setattr(self,'%s_Es'%p, saved['data'][4])
      if saved['data'][3] is not None:
        cycle = len(saved['data'][3][-1])
        setattr(self,'_%s_cycle'%p,cycle)
      else:
        setattr(self,'_%s_cycle'%p,0)
    if getattr(self,'%s_protocol'%p)==[] or \
        (not getattr(self,'%s_protocol'%p)[-1]['crossed']):
      setattr(self,'_%s_cycle'%p,0)
    return params

  def _clear(self, p):
    setattr(self,'%s_protocol'%p,[])
    setattr(self,'_%s_cycle'%p,0)
    self.confs[p]['replicas'] = None
    self.confs[p]['seeds'] = None
    self.confs[p]['samples'] = None
    setattr(self,'%s_Es'%p,None)

  def _save(self, p, keys=['progress','data']):
    """
    Saves the protocol, 
    cycle counts,
    random orientation parameters (for docking),
    replica configurations,
    sampled configurations,
    and energies
    """
    random_orient = None
    if p=='dock' and hasattr(self,'_n_trans'):
        random_orient = (self._n_trans, self._max_n_trans, self._random_trans, \
           self._n_rot, self._max_n_rot, self._random_rotT)
  
    arg_dict = dict([tp for tp in self.params[p].items() \
                      if not tp[0] in ['repX_cycles']])
    if p=='cool':
      fn_dict = convert_dictionary_relpath({
          'ligand_database':self._FNs['ligand_database'],
          'forcefield':self._FNs['forcefield'],
          'frcmodList':self._FNs['frcmodList'],
          'prmtop':{'L':self._FNs['prmtop']['L']},
          'inpcrd':{'L':self._FNs['inpcrd']['L']}},
          relpath_o=None, relpath_n=self.dir['cool'])
    elif p=='dock':
      fn_dict = convert_dictionary_relpath(
          dict([tp for tp in self._FNs.items() \
            if not tp[0] in ['namd','vmd','sander']]),
          relpath_o=None, relpath_n=self.dir['dock'])
    params = (fn_dict,arg_dict)
    
    saved = {
      'progress': (params,
                   getattr(self,'%s_protocol'%p),
                   getattr(self,'_%s_cycle'%p)),
      'data': (random_orient,
               self.confs[p]['replicas'],
               self.confs[p]['seeds'],
               self.confs[p]['samples'],
               getattr(self,'%s_Es'%p))}
    
    for key in keys:
      saved_FN = join(self.dir[p],'%s_%s.pkl.gz'%(p,key))
      if not os.path.isdir(self.dir[p]):
        os.system('mkdir -p '+self.dir[p])
      if os.path.isfile(saved_FN):
        os.rename(saved_FN,saved_FN+'.BAK')
      self._write_pkl_gz(saved_FN, saved[key])

  def _set_lock(self, p):
    if not os.path.isdir(self.dir[p]):
      os.system('mkdir -p '+self.dir[p])
    lockFN = join(self.dir[p],'.lock')
    if os.path.isfile(lockFN):
      raise Exception(p + ' is locked')
    else:
      lockF = open(lockFN,'w')
      lockF.close()
    logFN = join(self.dir[p],p+'_log.txt')
    self.log = open(logFN,'a')

  def _clear_lock(self, p):
    lockFN = join(self.dir[p],'.lock')
    if os.path.isfile(lockFN):
      os.remove(lockFN)
    if hasattr(self,'log'):
      self.log.close()
      del self.log

  def tee(self, var, process=None):
    print var
    if hasattr(self,'log'):
      if isinstance(var,str):
        self.log.write(var+'\n')
      else:
        self.log.write(repr(var)+'\n')
      self.log.flush()
    elif process is not None:
      self._set_lock(process)
      if isinstance(var,str):
        self.log.write(var+'\n')
      else:
        self.log.write(repr(var)+'\n')
      self.log.flush()
      self._clear_lock(process)

if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser(
    description='Molecular docking with adaptively scaled alchemical interaction grids')
  
  for key in arguments.keys():
    parser.add_argument('--'+key, **arguments[key])
  
  args = parser.parse_args()
  self = BPMF(**vars(args))
