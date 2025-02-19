#!/usr/bin/env python

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
# Define allowed_phases list and arguments dictionary
from AlGDock.BindingPMF_arguments import *
# Define functions: merge_dictionaries, convert_dictionary_relpath, and dict_view
from AlGDock.DictionaryTools import *

import pymbar.timeseries

import multiprocessing
from multiprocessing import Process

# For profiling. Unnecessary for normal execution.
# from memory_profiler import profile

#############
# Constants #
#############

R = 8.3144621 * MMTK.Units.J / MMTK.Units.mol / MMTK.Units.K

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

# In APBS, minimum ratio of PB grid length to maximum dimension of solute
LFILLRATIO = 4.0 # For the ligand
RFILLRATIO = 2.0 # For the receptor/complex

DEBUG = False

########################
# Auxilliary functions #
########################

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

    if kwargs['rotate_matrix'] is not None:
      self._view_args_rotate_matrix = kwargs['rotate_matrix']

    if kwargs['random_seed'] is None:
      self._random_seed = 0
    else:
      self._random_seed = kwargs['random_seed']
      print 'using random number seed of %d'%self._random_seed

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
    print dict_view(self.dir)
  
    # Identify tarballs
    tarFNs = [kwargs[prefix + '_tarball'] \
      for prefix in ['ligand','receptor','complex'] \
      if (prefix + '_tarball') in kwargs.keys() and
      kwargs[(prefix + '_tarball')] is not None]
    for p in ['cool','dock']:
      if (p in FNs.keys()) and ('tarball' in FNs[p].keys()):
        tarFNs += [tarFN for tarFN in FNs[p]['tarball'].values() \
          if tarFN is not None]
    tarFNs = set([FN for FN in tarFNs if os.path.isfile(FN)])

    # Identify files to look for in the tarballs
    seekFNs = []
    if len(tarFNs)>0:
      for prefix in ['ligand','receptor','complex']:
        for postfix in ('database','prmtop','inpcrd','fixed_atoms'):
          key = '%s_%s'%(prefix,postfix)
          if (key in kwargs.keys()) and (kwargs[key] is not None):
            FN = os.path.abspath(kwargs[key])
            if not os.path.isfile(FN):
              seekFNs.append(os.path.basename(FN))
      for p in ['cool','dock']:
        if p in FNs.keys():
          for level1 in ['ligand_database','prmtop','inpcrd','fixed_atoms']:
            if level1 in FNs[p].keys():
              if isinstance(FNs[p][level1],dict):
                for level2 in ['L','R','RL']:
                  if level2 in FNs[p][level1].keys():
                    seekFNs.append(os.path.basename(FNs[p][level1][level2]))
              else:
                seekFNs.append(os.path.basename(FNs[p][level1]))
      seekFNs = set(seekFNs)
    seek_frcmod = (kwargs['frcmodList'] is None) or \
      (not os.path.isfile(kwargs['frcmodList'][0]))

    # Decompress tarballs into self.dir['dock']
    self._toClear = []

    if len(seekFNs)>0:
      import tarfile

      print '>>> Decompressing tarballs'
      print 'looking for:\n  ' + '\n  '.join(seekFNs)
      if seek_frcmod:
        print '  and frcmod files'

      for tarFN in tarFNs:
        print 'reading '+tarFN
        tarF = tarfile.open(tarFN,'r')
        for member in tarF.getmembers():
          for seekFN in seekFNs:
            if member.name.endswith(seekFN):
              tarF.extract(member, path = self.dir['dock'])
              self._toClear.append(os.path.join(self.dir['dock'],seekFN))
              print '  extracted '+seekFN
          if seek_frcmod and member.name.endswith('frcmod'):
            FN = os.path.abspath(os.path.join(self.dir['dock'],member.name))
            if not os.path.isfile(FN):
              tarF.extract(member, path = self.dir['dock'])
              kwargs['frcmodList'] = [FN]
              self._toClear.append(FN)
              print '  extracted '+FN

    # Set up file name dictionary
    print '\n*** Files ***'

    for p in ['cool','dock']:
      if p in FNs.keys():
        if FNs[p]!={}:
          print 'previously stored in %s directory:'%p
          print dict_view(FNs[p], relpath=self.dir['start'])

    if not (FNs['cool']=={} and FNs['dock']=={}):
      print 'from arguments and defaults:'

    def cdir_or_dir_dock(FN):
      if FN is not None:
        return a.findPath([FN,join(self.dir['dock'],FN)])
      else:
        return None

    if kwargs['frcmodList'] is not None:
      if isinstance(kwargs['frcmodList'],str):
        kwargs['frcmodList'] = [kwargs['frcmodList']]
      kwargs['frcmodList'] = [cdir_or_dir_dock(FN) \
        for FN in kwargs['frcmodList']]
  
    FFpath = a.search_paths['gaff.dat'] \
      if 'gaff.dat' in a.search_paths.keys() else []
    FNs['new'] = {
      'ligand_database':cdir_or_dir_dock(kwargs['ligand_database']),
      'forcefield':a.findPath([kwargs['forcefield'],'../Data/gaff.dat'] + FFpath),
      'frcmodList':kwargs['frcmodList'],
      'tarball':{'L':a.findPath([kwargs['ligand_tarball']]),
                'R':a.findPath([kwargs['receptor_tarball']]),
                'RL':a.findPath([kwargs['complex_tarball']])},
      'prmtop':{'L':cdir_or_dir_dock(kwargs['ligand_prmtop']),
                'R':cdir_or_dir_dock(kwargs['receptor_prmtop']),
                'RL':cdir_or_dir_dock(kwargs['complex_prmtop'])},
      'inpcrd':{'L':cdir_or_dir_dock(kwargs['ligand_inpcrd']),
                'R':cdir_or_dir_dock(kwargs['receptor_inpcrd']),
                'RL':cdir_or_dir_dock(kwargs['complex_inpcrd'])},
      'fixed_atoms':{'R':cdir_or_dir_dock(kwargs['receptor_fixed_atoms']),
                     'RL':cdir_or_dir_dock(kwargs['complex_fixed_atoms'])},
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
      'score':'default' if kwargs['score']=='default' \
                        else a.findPath([kwargs['score']]),
      'dir_cool':self.dir['cool']}

    if not (FNs['cool']=={} and FNs['dock']=={}):
      print dict_view(FNs['new'], relpath=self.dir['start'])
      print 'to be used:'

    self._FNs = merge_dictionaries(
      [FNs[src] for src in ['new','cool','dock']])
  
    # Default: a force field modification is in the same directory as the ligand
    if (self._FNs['frcmodList'] is None):
      if self._FNs['prmtop']['L'] is not None:
        dir_lig = os.path.dirname(self._FNs['prmtop']['L'])
        frcmodpaths = [os.path.abspath(join(dir_lig, \
          os.path.basename(self._FNs['prmtop']['L'])[:-7]+'.frcmod'))]
      else:
        dir_lig = '.'
        frcmodpaths = []
      if kwargs['frcmodList'] is None:
        frcmodpaths.extend([os.path.abspath(join(dir_lig,'lig.frcmod')),\
                            os.path.abspath(join(dir_lig,'ligand.frcmod'))])
        frcmod = a.findPath(frcmodpaths)
        self._FNs['frcmodList'] = [frcmod]
    elif not isinstance(self._FNs['frcmodList'],list):
      self._FNs['frcmodList'] = [self._FNs['frcmodList']]

    # Check for existence of required files
    do_dock = (hasattr(args,'run_type') and \
              (args.run_type not in ['store_params', 'cool']))

    for key in ['ligand_database','forcefield']:
      if (self._FNs[key] is None) or (not os.path.isfile(self._FNs[key])):
        raise Exception('File for %s is missing!'%key)

    for (key1,key2) in [('prmtop','L'),('inpcrd','L')]:
      FN = self._FNs[key1][key2]
      if (FN is None) or (not os.path.isfile(FN)):
        raise Exception('File for %s %s is missing'%(key1,key2))

    for (key1,key2) in [\
        ('prmtop','RL'), ('inpcrd','RL'), \
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

    print dict_view(self._FNs, relpath=self.dir['start'], show_None=True)
    
    args['default_cool'] = {
        'protocol':'Adaptive',
        'therm_speed':0.2,
        'T_HIGH':600.,
        'T_TARGET':300.,
        'sampler':'NUTS',
        'steps_per_seed':1000,
        'seeds_per_state':50,
        'darts_per_seed':0,
        'repX_cycles':20,
        'min_repX_acc':0.3,
        'sweeps_per_cycle':1000,
        'attempts_per_sweep':25,
        'steps_per_sweep':50,
        'darts_per_sweep':0,
        'snaps_per_independent':3.0,
        'phases':['NAMD_Gas','NAMD_OBC'],
        'keep_intermediate':False,
        'GMC_attempts': 0,
        'GMC_tors_threshold': 0.0 }

    args['default_dock'] = dict(args['default_cool'].items() + {
      'site':None, 'site_center':None, 'site_direction':None,
      'site_max_X':None, 'site_max_R':None,
      'site_density':50., 'site_measured':None,
      'MCMC_moves':1,
      'rmsd':False}.items() + \
      [('receptor_'+phase,None) for phase in allowed_phases])
    args['default_dock']['snaps_per_independent'] = 20.0

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
        elif key in kwargs.keys():
          # Use the general key
          args['new_'+p][key] = kwargs[key]

    self.params = {}
    for p in ['cool','dock']:
      self.params[p] = merge_dictionaries(
        [args[src] for src in ['new_'+p,p,'default_'+p]])

    # Check that phases are permitted
    for phase in (self.params['cool']['phases'] + self.params['dock']['phases']):
      if phase not in allowed_phases:
        raise Exception(phase + ' phase is not supported!')
        
    # Make sure prerequistite phases are included:
    #   sander_Gas is necessary for any sander or gbnsr6 phase
    #   NAMD_Gas is necessary for APBS_PBSA
    for process in ['cool','dock']:
      phase_list = self.params[process]['phases']
      if (not 'sander_Gas' in phase_list) and \
          len([p for p in phase_list \
            if p.startswith('sander') or p.startswith('gbnsr6')])>0:
        phase_list.append('sander_Gas')
      if (not 'NAMD_Gas' in phase_list) and ('APBS_PBSA' in phase_list):
        phase_list.append('NAMD_Gas')
  
    self._scalables = ['sLJr','sELE','LJr','LJa','ELE']

    # Variables dependent on the parameters
    self.original_Es = [[{}]]
    for phase in allowed_phases:
      if self.params['dock']['receptor_'+phase] is not None:
        self.original_Es[0][0]['R'+phase] = \
          np.atleast_2d(self.params['dock']['receptor_'+phase])
      else:
        self.original_Es[0][0]['R'+phase] = None
        
    self.T_HIGH = self.params['cool']['T_HIGH']
    self.T_TARGET = self.params['cool']['T_TARGET']
    self.RT_TARGET = R * self.params['cool']['T_TARGET']

    print '>>> Setting up the simulation'
    self._setup_universe(do_dock = do_dock)

    print '\n*** Simulation parameters and constants ***'
    for p in ['cool','dock']:
      print '\nfor %s:'%p
      print dict_view(self.params[p])[:-1]

    self.timing = {'start':time.time(), 'max':kwargs['max_time']}
    self._run(kwargs['run_type'])
    
  def _setup_universe(self, do_dock=True):
    """Creates an MMTK InfiniteUniverse and adds the ligand"""
  
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

    # Create universe and add molecule to universe
    self.universe = MMTK.Universe.InfiniteUniverse()
    self.universe.addObject(self.molecule)
    self._evaluators = {} # Store evaluators
    self._OpenMM_sims = {} # Store OpenMM simulations
    self._ligand_natoms = self.universe.numberOfAtoms()

    # Force fields
    from MMTK.ForceFields import Amber12SBForceField

    self._forceFields = {}
    self._forceFields['gaff'] = Amber12SBForceField(
      parameter_file=self._FNs['forcefield'],mod_files=self._FNs['frcmodList'])

    # Determine ligand atomic index
    if (self._FNs['prmtop']['R'] is not None) and \
       (self._FNs['prmtop']['RL'] is not None):
      import AlGDock.IO
      IO_prmtop = AlGDock.IO.prmtop()
      prmtop_R = IO_prmtop.read(self._FNs['prmtop']['R'])
      prmtop_RL = IO_prmtop.read(self._FNs['prmtop']['RL'])
      ligand_ind = [ind for ind in range(len(prmtop_RL['RESIDUE_LABEL']))
        if prmtop_RL['RESIDUE_LABEL'][ind] not in prmtop_R['RESIDUE_LABEL']]
      if len(ligand_ind)==0:
        raise Exception('Ligand not found in complex prmtop')
      elif len(ligand_ind) > 1:
        print '  possible ligand residue labels: '+\
          ', '.join([prmtop_RL['RESIDUE_LABEL'][ind] for ind in ligand_ind])
      print '  considering a residue named "%s" as the ligand'%\
        prmtop_RL['RESIDUE_LABEL'][ligand_ind[-1]].strip()
      self._ligand_first_atom = prmtop_RL['RESIDUE_POINTER'][ligand_ind[-1]] - 1
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

    if lig_crd is not None:
      self.confs['ligand'] = lig_crd[self.molecule.inv_prmtop_atom_order,:]
      self.universe.setConfiguration(\
        Configuration(self.universe,self.confs['ligand']))

    if self.params['dock']['rmsd'] is not False:
      if self.params['dock']['rmsd'] is True:
        if lig_crd is not None:
          rmsd_crd = lig_crd
        else:
          raise Exception('Reference structure for rmsd calculations unknown')
      else:
        rmsd_crd = IO_crd.read(self.params['dock']['rmsd'], \
          natoms=self.universe.numberOfAtoms(), multiplier=0.1)
        rmsd_crd = rmsd_crd[self.molecule.inv_prmtop_atom_order,:]
      self.confs['rmsd'] = rmsd_crd[self.molecule.heavy_atoms,:]

    # Locate programs for postprocessing
    all_phases = self.params['dock']['phases'] + self.params['cool']['phases']
    self._load_programs(all_phases)

    # Determine APBS grid spacing
    if 'APBS_PBSA' in self.params['dock']['phases'] or \
       'APBS_PBSA' in self.params['cool']['phases']:
      self._get_APBS_grid_spacing()

    # Determines receptor electrostatic size
    if np.array([p.find('ALPB')>-1 for p in all_phases]).any():
      self.elsize = self._get_elsize()

    # If poses are being rescored, start with a docked structure
    (confs,Es) = self._get_confs_to_rescore(site=False, minimize=False)
    if len(confs)>0:
      self.universe.setConfiguration(Configuration(self.universe,confs[-1]))

    # Samplers may accept the following options:
    # steps - number of MD steps
    # T - temperature in K
    # delta_t - MD time step
    # normalize - normalizes configurations
    # adapt - uses an adaptive time step

    self.sampler = {}
    # Uses cython class
    # from SmartDarting import SmartDartingIntegrator # @UnresolvedImport
    # Uses python class
    from AlGDock.Integrators.SmartDarting.SmartDarting \
      import SmartDartingIntegrator # @UnresolvedImport
    self.sampler['cool_SmartDarting'] = SmartDartingIntegrator(\
      self.universe, self.molecule, False)
    self.sampler['dock_SmartDarting'] = SmartDartingIntegrator(\
      self.universe, self.molecule, True)
    from AlGDock.Integrators.ExternalMC.ExternalMC import ExternalMCIntegrator
    self.sampler['ExternalMC'] = ExternalMCIntegrator(\
      self.universe, self.molecule, step_size=0.25*MMTK.Units.Ang)

    for p in ['cool', 'dock']:
      if self.params[p]['sampler'] == 'NUTS':
        from NUTS import NUTSIntegrator # @UnresolvedImport
        self.sampler[p] = NUTSIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'NUTS_no_stopping':
        from NUTS_no_stopping import NUTSIntegrator # @UnresolvedImport
        self.sampler[p] = NUTSIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'HMC':
        from AlGDock.Integrators.HamiltonianMonteCarlo.HamiltonianMonteCarlo \
          import HamiltonianMonteCarloIntegrator
        self.sampler[p] = HamiltonianMonteCarloIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'TDHMC':
        from Integrators.TDHamiltonianMonteCarlo.TDHamiltonianMonteCarlo \
          import TDHamiltonianMonteCarloIntegrator
        self.sampler[p] = TDHamiltonianMonteCarloIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'VV':
        from AlGDock.Integrators.VelocityVerlet.VelocityVerlet \
          import VelocityVerletIntegrator
        self.sampler[p] = VelocityVerletIntegrator(self.universe)
      else:
        raise Exception('Unrecognized sampler!')

    # Load progress
    self._postprocess(readOnly=True)
    self.calc_f_L(readOnly=True)
    self.calc_f_RL(readOnly=True)

    if self._random_seed>0:
      np.random.seed(self._random_seed)

  def _run(self, run_type):
    self.run_type = run_type
    if run_type=='pose_energies' or run_type=='minimized_pose_energies':
      self.pose_energies(minimize=(run_type=='minimized_pose_energies'))
    elif run_type=='store_params':
      self._save('cool', keys=['progress'])
      self._save('dock', keys=['progress'])
    elif run_type=='initial_cool':
      self.initial_cool()
    elif run_type=='cool': # Sample the cooling process
      self.cool()
      self._postprocess([('cool',-1,-1,'L')])
      self.calc_f_L()
    elif run_type=='dock': # Sample the docking process
      self.dock()
      self._postprocess()
      self.calc_f_RL()
    elif run_type=='timed': # Timed replica exchange sampling
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
    elif run_type=='postprocess': # Postprocessing
      self._postprocess()
    elif run_type=='redo_postprocess':
      self._postprocess(redo_dock=True)
    elif (run_type=='free_energies') or (run_type=='redo_free_energies'):
      self.calc_f_L()
      self.calc_f_RL(redo=(run_type=='redo_free_energies'))
    elif run_type=='all':
      self.cool()
      self._postprocess([('cool',-1,-1,'L')])
      self.calc_f_L()
      self.dock()
      self._postprocess()
      self.calc_f_RL()
    elif run_type=='render_docked':
      view_args = {'axes_off':True, 'size':[1008,1008], 'scale_by':0.80, \
                   'render':'TachyonInternal'}
      if hasattr(self, '_view_args_rotate_matrix'):
        view_args['rotate_matrix'] = getattr(self, '_view_args_rotate_matrix')
      self.show_samples(show_ref_ligand=True, show_starting_pose=True, \
        show_receptor=True, save_image=True, execute=True, quit=True, \
        view_args=view_args)
    elif run_type=='render_intermediates':
      view_args = {'axes_off':True, 'size':[1008,1008], 'scale_by':0.80, \
                   'render':'TachyonInternal'}
      if hasattr(self, '_view_args_rotate_matrix'):
        view_args['rotate_matrix'] = getattr(self, '_view_args_rotate_matrix')
      self.render_intermediates(\
        movie_name=os.path.join(self.dir['dock'],'dock-intermediates.gif'), \
        view_args=view_args)
      self.render_intermediates(nframes=8, view_args=view_args)
    elif run_type=='clear_intermediates':
      for process in ['cool','dock']:
        print 'Clearing intermediates for '+process
        for state_ind in range(1,len(self.confs[process]['samples'])-1):
          for cycle_ind in range(len(self.confs[process]['samples'][state_ind])):
            self.confs[process]['samples'][state_ind][cycle_ind] = []
        self._save(process)

  ###########
  # Cooling #
  ###########
  def initial_cool(self, warm=True):
    """
    Warms the ligand from self.T_TARGET to self.T_HIGH, or
    cools the ligand from self.T_HIGH to self.T_TARGET
    
    Intermediate thermodynamic states are chosen such that
    thermodynamic length intervals are approximately constant.
    Configurations from each state are subsampled to seed the next simulation.
    """

    if (len(self.cool_protocol)>0) and (self.cool_protocol[-1]['crossed']):
      return # Initial cooling is already complete
    
    self._set_lock('cool')
    cool_start_time = time.time()

    if warm:
      T_START, T_END = self.T_TARGET, self.T_HIGH
      direction_name = 'warm'
    else:
      T_START, T_END = self.T_HIGH, self.T_TARGET
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
      
      # Get starting configurations
      seeds = self._get_confs_to_rescore(site=False, minimize=True)[0]
      # initializes smart darting for cooling
      # and sets the universe to the lowest energy configuration
      if self.params['cool']['darts_per_seed']>0:
        self.tee(self.sampler['cool_SmartDarting'].set_confs(seeds))
        self.confs['cool']['SmartDarting'] = self.sampler['cool_SmartDarting'].confs
      elif len(seeds)>0:
        self.universe.setConfiguration(Configuration(self.universe,seeds[-1]))
      self.confs['cool']['starting_poses'] = seeds
      
      # Ramp the temperature from 0 to the desired starting temperature
      T_LOW = 20.
      T_SERIES = T_LOW*(T_START/T_LOW)**(np.arange(30)/29.)
      for T in T_SERIES:
        random_seed = int(abs(seeds[0][0][0]*10000)) + int(T*10000)
        if self._random_seed==0:
          random_seed += int(time.time())
        self.sampler['cool'](steps = 500, steps_per_trial = 100, T=T,\
                             delta_t=self.delta_t, random_seed=random_seed)
      self.universe.normalizePosition()

      # Run at starting temperature
      state_start_time = time.time()
      conf = self.universe.configuration().array
      (confs, Es_MM, self.cool_protocol[-1]['delta_t'], sampler_metrics) = \
        self._initial_sim_state(\
        [conf for n in range(self.params['cool']['seeds_per_state'])], \
        'cool', self.cool_protocol[-1])
      self.confs['cool']['replicas'] = [confs[np.random.randint(len(confs))]]
      self.confs['cool']['samples'] = [[confs]]
      self.cool_Es = [[{'MM':Es_MM}]]
      tL_tensor = Es_MM.std()/(R*T_START*T_START)

      self.tee("  generated %d configurations "%len(confs) + \
               "at %d K "%self.cool_protocol[-1]['T'] + \
               "in " + HMStime(time.time()-state_start_time))
      self.tee(sampler_metrics)
      self.tee("  dt=%.3f fs; tL_tensor=%.3e"%(\
        self.cool_protocol[-1]['delta_t']*1000., tL_tensor))
    else:
      self.tee("\n>>> Initial %s of the ligand "%direction_name + \
        "from %d K to %d K, "%(T_START,T_END) + "continuing at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))
      confs = self.confs['cool']['samples'][-1][0]
      Es_MM = self.cool_Es[-1][0]['MM']
      T = self.cool_protocol[-1]['T']
      tL_tensor = Es_MM.std()/(R*T*T)

    if self.params['cool']['darts_per_seed']>0:
      self.confs['cool']['SmartDarting'] += confs

    self.tee("")
    
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
          if T > self.T_HIGH:
            T = self.T_HIGH
            crossed = True
        else:
          T = To - dL
          if T < self.T_TARGET:
            T = self.T_TARGET
            crossed = True
      else:
        raise Exception('No variance in configuration energies')
      self.cool_protocol.append(\
        {'T':T, 'a':(self.T_HIGH-T)/(self.T_HIGH-self.T_TARGET), 'MM':True, 'crossed':crossed})

      # Randomly select seeds for new trajectory
      logweight = Es_MM/R*(1/T-1/To)
      weights = np.exp(-logweight+min(logweight))
      seedIndicies = np.random.choice(len(Es_MM), \
        size = self.params['cool']['seeds_per_state'], p = weights/sum(weights))
      seeds = [np.copy(confs[s]) for s in seedIndicies]
      
      # Simulate and store data
      confs_o = confs
      Es_MM_o = Es_MM
      
      self._set_universe_evaluator(self.cool_protocol[-1])
      if self.params['cool']['darts_per_seed']>0:
        self.tee(self.sampler['cool_SmartDarting'].set_confs(\
          self.confs['cool']['SmartDarting']))
        self.confs['cool']['SmartDarting'] = self.sampler['cool_SmartDarting'].confs
      
      state_start_time = time.time()
      (confs, Es_MM, self.cool_protocol[-1]['delta_t'], sampler_metrics) = \
        self._initial_sim_state(seeds, 'cool', self.cool_protocol[-1])

      if self.params['cool']['darts_per_seed']>0:
        self.confs['cool']['SmartDarting'] += confs

      tL_tensor_o = 1.*tL_tensor
      tL_tensor = Es_MM.std()/(R*T*T) # Metric tensor for the thermodynamic length

      # Estimate the mean replica exchange acceptance rate
      # between the previous and new state
      (u_kln,N_k) = self._u_kln([[{'MM':Es_MM_o}],[{'MM':Es_MM}]], \
                                self.cool_protocol[-2:])
      N = min(N_k)
      acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
      mean_acc = np.mean(np.minimum(acc,np.ones(acc.shape)))

      self.tee("  generated %d configurations "%len(confs) + \
               "at %d K "%self.cool_protocol[-1]['T'] + \
               "in " + (HMStime(time.time()-state_start_time)))
      self.tee(sampler_metrics)
      self.tee("  dt=%.3f fs; tL_tensor=%.3e; estimated repX acceptance=%0.3f"%(\
        self.cool_protocol[-1]['delta_t']*1000., tL_tensor, mean_acc))

      if mean_acc<self.params['cool']['min_repX_acc']:
        # If the acceptance probability is too low,
        # reject the state and restart
        self.cool_protocol.pop()
        confs = confs_o
        Es_MM = Es_MM_o
        tL_tensor = tL_tensor_o*1.25 # Use a smaller step
        self.tee("  rejected new state, as estimated repX" + \
          " acceptance is too low!")
      elif (mean_acc>0.99) and (not crossed):
        # If the acceptance probability is too high,
        # reject the previous state and restart
        self.confs['cool']['replicas'][-1] = confs[np.random.randint(len(confs))]
        self.cool_protocol.pop(-2)
        self.tee("  rejected previous state, as estimated repX" + \
          " acceptance is too high!")
      else:
        self.confs['cool']['replicas'].append(confs[np.random.randint(len(confs))])
        self.confs['cool']['samples'].append([confs])
        if len(self.confs['cool']['samples'])>2 and \
            (not self.params['cool']['keep_intermediate']):
          self.confs['cool']['samples'][-2] = []
        self.cool_Es.append([{'MM':Es_MM}])
        self.tee("")

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

      # Save progress every 5 minutes
      if ('last_cool_save' not in self.timing) or \
         ((time.time()-self.timing['last_cool_save'])>5*60):
        self._save('cool')
        self.tee("")
        self.timing['last_cool_save'] = time.time()
        saved = True
      else:
        saved = False

      if self.run_type=='timed':
        remaining_time = self.timing['max']*60 - \
          (time.time()-self.timing['start'])
        if remaining_time<0:
          if not saved:
            self._save('cool')
            self.tee("")
          self.tee("  no time remaining for initial cool")
          self._clear_lock('cool')
          return False

    # Save data
    if not saved:
      self._save('cool')
      self.tee("")

    self.tee("Elapsed time for initial %sing of "%direction_name + \
      "%d states: "%len(self.cool_protocol) + \
      HMStime(time.time()-cool_start_time))
    self._clear_lock('cool')
    self.sampler['cool_SmartDarting'].confs = []
    return True

  def cool(self):
    """
    Samples different ligand configurations 
    at thermodynamic states between self.T_HIGH and self.T_TARGET
    """
    return self._sim_process('cool')

  def calc_f_L(self, readOnly=False, redo=False):
    """
    Calculates ligand-specific free energies:
    1. solvation free energy of the ligand using single-step 
       free energy perturbation
    2. reduced free energy of cooling the ligand from self.T_HIGH to self.T_TARGET
    """
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

    # Store stats_L internal energies
    self.stats_L['u_K_sampled'] = \
      [self._u_kln([self.cool_Es[-1][c]],[self.cool_protocol[-1]]) \
        for c in range(self._cool_cycle)]
    self.stats_L['u_KK'] = \
      [np.sum([self._u_kln([self.cool_Es[k][c]],[self.cool_protocol[k]]) \
        for k in range(len(self.cool_protocol))],0) \
          for c in range(self._cool_cycle)]
    for phase in self.params['cool']['phases']:
      self.stats_L['u_K_'+phase] = \
        [self.cool_Es[-1][c]['L'+phase][:,-1]/self.RT_TARGET \
          for c in range(self._cool_cycle)]

    # Estimate cycle at which simulation has equilibrated and predict native pose
    self.stats_L['equilibrated_cycle'] = self._get_equilibrated_cycle('cool')
    (self.stats_L['predicted_pose_index'], \
     self.stats_L['lowest_energy_pose_index']) = \
      self._get_pose_prediction('cool', self.stats_L['equilibrated_cycle'][-1])

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
        du_F = (u_phase[:,-1] - u_MM)/self.RT_TARGET
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
      self._scalables,np.ones(len(self._scalables),dtype=int)) + \
      [('T',self.T_HIGH),('site',True)])
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
      from AlGDock.Integrators.ExternalMC.ExternalMC import random_rotate
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
            self.universe.translateTo(self._random_trans[i_trans])
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

  def initial_dock(self, randomOnly=False, undock=True):
    """
      Docks the ligand into the receptor
      
      Intermediate thermodynamic states are chosen such that
      thermodynamic length intervals are approximately constant.
      Configurations from each state are subsampled to seed the next simulation.
    """
    
    if (len(self.dock_protocol)>0) and (self.dock_protocol[-1]['crossed']):
      return # Initial docking already complete

    self._set_lock('dock')
    dock_start_time = time.time()

    if self.dock_protocol==[]:
      self.tee("\n>>> Initial docking, starting at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))
      if undock:
        lambda_o = self._lambda(1.0, 'dock', MM=True, site=True, crossed=False)
        self.dock_protocol = [lambda_o]
        self._set_universe_evaluator(lambda_o)
        seeds = self._get_confs_to_rescore(site=True, minimize=True)[0]

        if seeds==[]:
          undock = False
        else:
          self.confs['dock']['starting_poses'] = seeds
          # initializes smart darting for docking and sets the universe
          # to the lowest energy configuration
          if self.params['dock']['darts_per_seed']>0:
            self.tee(self.sampler['dock_SmartDarting'].set_confs(seeds))
            self.confs['dock']['SmartDarting'] = self.sampler['dock_SmartDarting'].confs
          elif len(seeds)>0:
            self.universe.setConfiguration(Configuration(self.universe,seeds[-1]))
          
          # Ramp up the temperature
          T_LOW = 20.
          T_SERIES = T_LOW*(self.T_TARGET/T_LOW)**(np.arange(30)/29.)
          for T in T_SERIES:
            random_seed = int(abs(seeds[0][0][0]*10000)) + int(T*10000)
            if self._random_seed==0:
              random_seed += int(time.time())
            self.sampler['dock'](steps = 500, steps_per_trial = 100, T=T,\
                                 delta_t=self.delta_t, random_seed=random_seed)
          seeds = [self.universe.configuration().array]

          # Simulate
          sim_start_time = time.time()
          (confs, Es_tot, lambda_o['delta_t'], sampler_metrics) = \
            self._initial_sim_state(\
              seeds*self.params['dock']['seeds_per_state'], 'dock', lambda_o)

          # Get state energies
          E = self._energyTerms(confs)
          self.confs['dock']['replicas'] = [confs[np.random.randint(len(confs))]]
          self.confs['dock']['samples'] = [[confs]]
          self.dock_Es = [[E]]

          self.tee("  generated %d configurations "%len(confs) + \
                   "with progress %e "%lambda_o['a'] + \
                   "in " + HMStime(time.time()-sim_start_time))
          self.tee(sampler_metrics)
          self.tee("  dt=%.3f ps, tL_tensor=%.3e"%(\
            lambda_o['delta_t']*1000., \
            self._tL_tensor(E,lambda_o)))
    
      if not undock:
        (cool0_confs, E) = self.random_dock()
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

    if self.params['dock']['darts_per_seed']>0:
      self.confs['dock']['SmartDarting'] += confs

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
        self._store_infinite_f_RL()
        raise Exception('Too many replicas!')
      if abs(rejectStage)>20:
        self._clear('dock')
        self._save('dock')
        self._store_infinite_f_RL()
        raise Exception('Too many consecutive rejected stages!')

      # Randomly select seeds for new trajectory
      u_o = self._u_kln([E],[lambda_o])
      u_n = self._u_kln([E],[lambda_n])
      du = u_n-u_o
      weights = np.exp(-du+min(du))
      seedIndicies = np.random.choice(len(u_o), \
        size = self.params['dock']['seeds_per_state'], \
        p=weights/sum(weights))

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
        seeds = [np.copy(confs[ind]) for ind in seedIndicies]
      self.confs['dock']['seeds'] = seeds

      # Store old data
      confs_o = confs
      E_o = E

      # Simulate
      sim_start_time = time.time()
      self._set_universe_evaluator(lambda_n)
      if self.params['dock']['darts_per_seed']>0:
        self.tee(self.sampler['dock_SmartDarting'].set_confs(\
          self.confs['dock']['SmartDarting']))
        self.confs['dock']['SmartDarting'] = self.sampler['dock_SmartDarting'].confs
      (confs, Es_tot, lambda_n['delta_t'], sampler_metrics) = \
        self._initial_sim_state(seeds, 'dock', lambda_n)

      if self.params['dock']['darts_per_seed']>0:
        self.confs['dock']['SmartDarting'] += confs

      # Get state energies
      E = self._energyTerms(confs)

      self.tee("  generated %d configurations "%len(confs) + \
               "with progress %f "%lambda_n['a'] + \
               "in " + HMStime(time.time()-sim_start_time))
      self.tee(sampler_metrics)
      self.tee("  dt=%.3f ps, tL_tensor=%.3e"%(\
        lambda_n['delta_t']*1000.,
        self._tL_tensor(E,lambda_n)))

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
          self.tee("  the estimated replica exchange acceptance rate is %f\n"%mean_acc)

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

      # Save progress every 5 minutes
      if ('last_dock_save' not in self.timing) or \
         ((time.time()-self.timing['last_dock_save'])>5*60):
        self._save('dock')
        self.timing['last_dock_save'] = time.time()
        self.tee("")
        saved = True
      else:
        saved = False

      if self.run_type=='timed':
        remaining_time = self.timing['max']*60 - \
          (time.time()-self.timing['start'])
        if remaining_time<0:
          if not saved:
            self._save('dock')
            self.tee("")
          self.tee("  no time remaining for initial dock")
          self._clear_lock('dock')
          return False

    if not saved:
      self._save('dock')
      self.tee("")

    self.tee("Elapsed time for initial docking of " + \
      "%d states: "%len(self.dock_protocol) + \
      HMStime(time.time()-dock_start_time))
    self._clear_lock('dock')
    self.sampler['dock_SmartDarting'].confs = []
    return True

  def dock(self):
    """
    Docks the ligand into the binding site
    by simulating at thermodynamic states
    between decoupled and fully interacting and
    between self.T_HIGH and self.T_TARGET
    """
    return self._sim_process('dock')

  def calc_f_RL(self, readOnly=False, redo=False):
    """
    Calculates the binding potential of mean force
    """
    if self.dock_protocol==[]:
      return # Initial docking is incomplete

    # Initialize variables as empty lists or by loading data
    f_RL_FN = join(self.dir['dock'],'f_RL.pkl.gz')
#    if redo:
#      if os.path.isfile(f_RL_FN):
#        os.remove(f_RL_FN)
#      dat = None
#    else:
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
        for item in ['equilibrated_cycle','cum_Nclusters','mean_acc','rmsd']]
      self.stats_RL = dict(stats_RL)
      self.stats_RL['protocol'] = self.dock_protocol
      # Free energy components
      self.f_RL = dict([(key,[]) for key in ['grid_BAR','grid_MBAR'] + \
        [phase+'_solv' for phase in self.params['dock']['phases']]])
      # Binding PMF estimates
      self.B = {'MBAR':[]}
      for phase in self.params['dock']['phases']:
        for method in ['min_Psi','mean_Psi','inverse_FEP','BAR','MBAR']:
          self.B[phase+'_'+method] = []
    if readOnly:
      return True

    if redo:
      self.B = {'MBAR':[]}
      for phase in self.params['dock']['phases']:
        for method in ['min_Psi','mean_Psi','inverse_FEP','BAR','MBAR']:
          self.B[phase+'_'+method] = []

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

    updated = False
    K = len(self.dock_protocol)
    
    # Store stats_RL
    # Internal energies
    self.stats_RL['u_K_ligand'] = \
      [self.dock_Es[-1][c]['MM']/self.RT_TARGET for c in range(self._dock_cycle)]
    self.stats_RL['u_K_sampled'] = \
      [self._u_kln([self.dock_Es[-1][c]],[self.dock_protocol[-1]]) \
        for c in range(self._dock_cycle)]
    self.stats_RL['u_KK'] = \
      [np.sum([self._u_kln([self.dock_Es[k][c]],[self.dock_protocol[k]]) \
        for k in range(len(self.dock_protocol))],0) \
          for c in range(self._dock_cycle)]
    for phase in self.params['dock']['phases']:
      self.stats_RL['u_K_'+phase] = \
        [self.dock_Es[-1][c]['RL'+phase][:,-1]/self.RT_TARGET \
          for c in range(self._dock_cycle)]

    # Interaction energies
    for c in range(len(self.stats_RL['Psi_grid']), self._dock_cycle):
      self.stats_RL['Psi_grid'].append(
          (self.dock_Es[-1][c]['LJr'] + \
           self.dock_Es[-1][c]['LJa'] + \
           self.dock_Es[-1][c]['ELE'])/self.RT_TARGET)
      updated = True
    for phase in self.params['dock']['phases']:
      if (not 'Psi_'+phase in self.stats_RL) or redo:
        self.stats_RL['Psi_'+phase] = []
      for c in range(len(self.stats_RL['Psi_'+phase]), self._dock_cycle):
        self.stats_RL['Psi_'+phase].append(
          (self.dock_Es[-1][c]['RL'+phase][:,-1] - \
           self.dock_Es[-1][c]['L'+phase][:,-1] - \
           self.original_Es[0][0]['R'+phase][:,-1])/self.RT_TARGET)
    
    # Estimate cycle at which simulation has equilibrated
    eqc_o = self.stats_RL['equilibrated_cycle']
    self.stats_RL['equilibrated_cycle'] = self._get_equilibrated_cycle('dock')
    if self.stats_RL['equilibrated_cycle']!=eqc_o:
      updated = True
    
    # Predict native pose
    (self.stats_RL['predicted_pose_index'], \
     self.stats_RL['lowest_energy_pose_index']) = \
      self._get_pose_prediction('dock', self.stats_RL['equilibrated_cycle'][-1])

    # Autocorrelation time for all replicas
    if updated:
      paths = [np.array(self.dock_Es[0][c]['repXpath']) \
        for c in range(len(self.dock_Es[0])) \
        if 'repXpath' in self.dock_Es[0][c].keys()]
      if len(paths)>0:
        paths = np.transpose(np.hstack(paths))
        self.stats_RL['tau_ac'] = \
          pymbar.timeseries.integratedAutocorrelationTimeMultiple(paths)

    # Store rmsd values
    self.stats_RL['rmsd'] = [self.dock_Es[-1][c]['rmsd'] \
      if 'rmsd' in self.dock_Es[-1][c].keys() else [] \
      for c in range(self._dock_cycle)]

    # Calculate docking free energies that have not already been calculated
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

    # BPMF assuming receptor and complex solvation cancel
    self.B['MBAR'] = [-self.f_L['cool_MBAR'][-1][-1] + \
      self.f_RL['grid_MBAR'][c][-1] for c in range(len(self.f_RL['grid_MBAR']))]

    # BPMFs
    for phase in self.params['dock']['phases']:
      if not phase+'_solv' in self.f_RL:
        self.f_RL[phase+'_solv'] = []
      for method in ['min_Psi','mean_Psi','inverse_FEP','BAR','MBAR']:
        if not phase+'_'+method in self.B:
          self.B[phase+'_'+method] = []
      
      # Receptor solvation
      f_R_solv = self.original_Es[0][0]['R'+phase][:,-1]/self.RT_TARGET

      for c in range(len(self.B[phase+'_MBAR']), self._dock_cycle):
        extractCycles = range(self.stats_RL['equilibrated_cycle'][c], c+1)
        du = np.concatenate([self.stats_RL['u_K_'+phase][c] - \
          self.stats_RL['u_K_sampled'][c] for c in extractCycles])
        # Complex solvation
        min_du = min(du)
        f_RL_solv = -np.log(np.exp(-du+min_du).mean()) + min_du
        weights = np.exp(-du+min_du)
        weights = weights/sum(weights)
        Psi = np.concatenate([self.stats_RL['Psi_'+phase][c] \
          for c in extractCycles])
        min_Psi = min(Psi)
        # If the range is too large, filter Psi
        if np.any((Psi-min_Psi)>500):
          keep = (Psi-min_Psi)<500
          weights = weights[keep]
          Psi = Psi[keep]
        
        self.f_RL[phase+'_solv'].append(f_RL_solv)
        self.B[phase+'_min_Psi'].append(min_Psi)
        self.B[phase+'_mean_Psi'].append(np.mean(Psi))
        self.B[phase+'_inverse_FEP'].append(\
          np.log(sum(weights*np.exp(Psi-min_Psi))) + min_Psi)
        
        self.B[phase+'_BAR'].append(-f_R_solv \
          - self.f_L[phase+'_solv'][-1] - self.f_L['cool_BAR'][-1][-1] \
          + self.f_RL['grid_BAR'][-1][-1] + f_RL_solv)
        self.B[phase+'_MBAR'].append(-f_R_solv \
          - self.f_L[phase+'_solv'][-1] - self.f_L['cool_MBAR'][-1][-1] \
          + self.f_RL['grid_MBAR'][-1][-1] + f_RL_solv)

        self.tee("  calculated %s binding PMF of %f RT with cycles %d to %d"%(\
          phase, self.B[phase+'_MBAR'][-1], \
          self.stats_RL['equilibrated_cycle'][c], c))
        updated = True

    if updated or redo:
      self._write_pkl_gz(f_RL_FN, (self.f_L, self.stats_RL, self.f_RL, self.B))

    self.tee("\nElapsed time for binding PMF estimation: " + \
      HMStime(time.time()-BPMF_start_time))
    self._clear_lock('dock')
    
  def _store_infinite_f_RL(self):
    f_RL_FN = join(self.dir['dock'],'f_RL.pkl.gz')
    self._write_pkl_gz(f_RL_FN, (self.f_L, [], np.inf, np.inf))

  def _get_equilibrated_cycle(self, process):
    # Estimate cycle at which simulation has equilibrated
    u_KKs = [np.sum([self._u_kln(\
      [getattr(self,process+'_Es')[k][c]], [getattr(self,process+'_protocol')[k]]) \
        for k in range(len(getattr(self,process+'_protocol')))],0) \
          for c in range(getattr(self,'_%s_cycle'%process))]
    mean_u_KKs = np.array([np.mean(u_KK) for u_KK in u_KKs])
    std_u_KKs = np.array([np.std(u_KK) for u_KK in u_KKs])

    equilibrated_cycle = []
    for c in range(getattr(self,'_%s_cycle'%process)):
      nearMean = abs(mean_u_KKs - mean_u_KKs[c])<std_u_KKs[c]
      if nearMean.any():
        nearMean = list(nearMean).index(True)
      else:
        nearMean = c
      if c>0: # If possible, reject burn-in
        nearMean = max(nearMean,1)
      equilibrated_cycle.append(nearMean)
    return equilibrated_cycle

#  correlation_times = [pymbar.timeseries.integratedAutocorrelationTimeMultiple(\
#    np.transpose(np.hstack([np.array(self.dock_Es[0][c]['repXpath']) \
#      for c in range(start_c,len(self.dock_Es[0])) \
#      if 'repXpath' in self.dock_Es[0][c].keys()]))) \
#        for start_c in range(1,len(self.dock_Es[0]))]
  
  def _get_pose_prediction(self, process, equilibrated_cycle):
    # Gather snapshots
    for k in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process)):
      if not isinstance(self.confs[process]['samples'][-1][k], list):
        self.confs[process]['samples'][-1][k] = [self.confs[process]['samples'][-1][k]]
    import itertools
    confs = np.array([conf[self.molecule.heavy_atoms,:] \
      for conf in itertools.chain.from_iterable(\
      [self.confs[process]['samples'][-1][c] \
        for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])])
    cum_Nk = np.cumsum([0] + [len(self.confs[process]['samples'][-1][c]) \
      for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])

    # RMSD matrix
    import sys
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = NullDevice()
    sys.stderr = NullDevice()
    from pyRMSD.matrixHandler import MatrixHandler
    rmsd_matrix = MatrixHandler().createMatrix(confs, \
      {'cool':'QCP_SERIAL_CALCULATOR', \
       'dock':'NOSUP_SERIAL_CALCULATOR'}[process])
    sys.stdout = original_stdout
    sys.stderr = original_stderr

    # Clustering
    import scipy.cluster
    Z = scipy.cluster.hierarchy.complete(rmsd_matrix.get_data())
    assignments = np.array(\
      scipy.cluster.hierarchy.fcluster(Z, 0.1, criterion='distance'))

    # Reindexes the assignments in order of appearance
    new_index = 0
    mapping_to_new_index = {}
    for assignment in assignments:
      if not assignment in mapping_to_new_index.keys():
        mapping_to_new_index[assignment] = new_index
        new_index += 1
    assignments = [mapping_to_new_index[a] for a in assignments]

    def linear_index_to_pair(ind):
      cycle = list(ind<cum_Nk).index(True)-1
      n = ind-cum_Nk[cycle]
      return (cycle + equilibrated_cycle,n)

    if process=='dock':
      stats = self.stats_RL
    else:
      stats = self.stats_L

    # Find lowest energy pose in most populated cluster (after equilibration)
    pose_ind = {}
    lowest_e_ind = {}
    for phase in (['sampled']+self.params[process]['phases']):
      un = np.concatenate([stats['u_K_'+phase][c] \
        for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])
      uo = np.concatenate([stats['u_K_sampled'][c] \
        for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])
      du = un-uo
      min_du = min(du)
      weights = np.exp(-du+min_du)
      cluster_counts = np.histogram(assignments, \
        bins=np.arange(len(set(assignments))+1)+0.5,
        weights=weights)[0]
      top_cluster = np.argmax(cluster_counts)
      pose_ind[phase] = linear_index_to_pair(\
        np.argmin(un+(assignments!=top_cluster)*np.max(un)))
      lowest_e_ind[phase] = linear_index_to_pair(np.argmin(un))
    return (pose_ind, lowest_e_ind)

  def pose_energies(self, minimize=False):
    """
    Calculates the energy for poses from self._FNs['score']
    """
    lambda_o = self._lambda(1.0, 'dock', MM=True, site=True, crossed=False)
    self._set_universe_evaluator(lambda_o)

    # Load the poses
    (confs, Es) = self._get_confs_to_rescore(site=False, minimize=minimize, \
      sort=False)

    # Calculate MM energies
    prefix = 'xtal' if self._FNs['score']=='default' else \
      os.path.basename(self._FNs['score']).split('.')[0]
    if minimize:
      prefix = 'min_' + prefix
    Es = self._energyTerms(confs, Es)

    # Calculate RMSD
    if self.params['dock']['rmsd'] is not False:
      Es['rmsd'] = np.array([np.sqrt(((confs[c][self.molecule.heavy_atoms,:] - \
        self.confs['rmsd'])**2).sum()/self.molecule.nhatoms) \
          for c in range(len(confs))])

    # Grid interpolation energies
    from AlGDock.ForceFields.Grid.Interpolation import InterpolationForceField
    for grid_type in ['LJa','LJr']:
      for interpolation_type in ['Trilinear','BSpline']: # ,'Tricubic']:
        key = '%s_%sTransform'%(grid_type,interpolation_type)
        Es[key] = np.zeros((12,len(confs)),dtype=np.float)
        for p in range(12):
          print interpolation_type + ' interpolation of the ' + \
            grid_type + ' grid with an inverse power of %d'%(p+1)
          FF = InterpolationForceField(self._FNs['grids'][grid_type], \
            name='%f'%(p+1),
            interpolation_type=interpolation_type, strength=1.0,
            scaling_property='scaling_factor_'+grid_type, \
            inv_power=-float(p+1))
          self.universe.setForceField(FF)
          for c in range(len(confs)):
            self.universe.setConfiguration(Configuration(self.universe,confs[c]))
            Es[key][p,c] = self.universe.energy()

    # Implicit solvent energies
    self._load_programs(self.params['dock']['phases'])
    toClear = []
    for phase in self.params['dock']['phases']:
      Es['R'+phase] = self.params['dock']['receptor_'+phase]
      for moiety in ['L','RL']:
        outputname = join(self.dir['dock'],'%s.%s%s'%(prefix,moiety,phase))
        if phase.startswith('NAMD'):
          traj_FN = join(self.dir['dock'],'%s.%s.dcd'%(prefix,moiety))
          self._write_traj(traj_FN, confs, moiety)
        elif phase.startswith('sander'):
          traj_FN = join(self.dir['dock'],'%s.%s.mdcrd'%(prefix,moiety))
          self._write_traj(traj_FN, confs, moiety)
        elif phase.startswith('gbnsr6'):
          traj_FN = join(self.dir['dock'], \
            '%s.%s%s'%(prefix,moiety,phase),'in.crd')
        elif phase.startswith('OpenMM'):
          traj_FN = None
        elif phase in ['APBS_PBSA']:
          traj_FN = join(self.dir['dock'],'%s.%s.pqr'%(prefix,moiety))
        else:
          raise Exception('Unknown phase!')
        if not traj_FN in toClear:
          toClear.append(traj_FN)
        for program in ['NAMD','sander','gbnsr6','OpenMM','APBS']:
          if phase.startswith(program):
            Es[moiety+phase] = getattr(self,'_%s_Energy'%program)(confs, \
              moiety, phase, traj_FN, outputname, debug=debug)
            break
    for FN in toClear:
      if os.path.isfile(FN):
        os.remove(FN)
    self._combine_MM_and_solvent(Es)

    # Store the data
    self._write_pkl_gz(join(self.dir['dock'],prefix+'.pkl.gz'),(confs,Es))
    return (confs,Es)

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
    if ('site' in lambda_n.keys()) and lambda_n['site']:
      if not 'site' in self._forceFields.keys():
        # Set up the binding site in the force field
        if (self.params['dock']['site']=='Measure'):
          self.params['dock']['site'] = 'Sphere'
          if self.params['dock']['site_measured'] is not None:
            (self.params['dock']['site_max_R'],self.params['dock']['site_center']) = \
              self.params['dock']['site_measured']
          else:
            print '\n*** Measuring the binding site ***'
            self._set_universe_evaluator(\
              self._lambda(1.0, 'dock', MM=True, site=False, crossed=False))
            (confs, Es) = self._get_confs_to_rescore(site=False, minimize=True)
            if len(confs)>0:
              # Use the center of mass for configurations
              # within 20 RT of the lowest energy
              cutoffE = Es['total'][-1] + 20*self.RT_TARGET
              coms = []
              for (conf,E) in reversed(zip(confs,Es['total'])):
                if E<=cutoffE:
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
            else:
              self.params['dock']['site_measured'] = \
                (self.params['dock']['site_max_R'], \
                 self.params['dock']['site_center'])

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
          raise Exception('Binding site type not recognized!')
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

          # Determine the grid threshold
          if scalable=='sLJr':
            grid_thresh = 10.0
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
            grid_thresh = min(abs(scaling_factors_LJr*10.0/scaling_factors_ELE))
          else:
            grid_thresh = -1 # There is no threshold for grid points

          from AlGDock.ForceFields.Grid.Interpolation \
            import InterpolationForceField
          self._forceFields[scalable] = InterpolationForceField(grid_FN, \
            name=scalable, interpolation_type='Trilinear', \
            strength=lambda_n[scalable], scaling_property=grid_scaling_factor,
            inv_power=-2 if scalable=='LJr' else None, \
            grid_thresh=grid_thresh)
          self.tee('  %s grid loaded from %s in %s'%(scalable, grid_FN, \
            HMStime(time.time()-loading_start_time)))

        # Set the force field strength to the desired value
        self._forceFields[scalable].params['strength'] = lambda_n[scalable]
        fflist.append(self._forceFields[scalable])

    compoundFF = fflist[0]
    for ff in fflist[1:]:
      compoundFF += ff
    self.universe.setForceField(compoundFF)

    eval = ForceField.EnergyEvaluator(\
      self.universe, self.universe._forcefield, None, None, None, None)
    eval.key = evaluator_key
    self.universe._evaluator[(None,None,None)] = eval
    self._evaluators[evaluator_key] = eval

  def _initial_sim_state(self, seeds, process, lambda_k):
    """
    Initializes a state, returning the configurations and potential energy.
    """
    
    results = []
    if self._cores>1:
      # Multiprocessing code
      m = multiprocessing.Manager()
      task_queue = m.Queue()
      done_queue = m.Queue()
      for k in range(len(seeds)):
        task_queue.put((seeds[k], process, lambda_k, True, k))
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
        seeds[k], process, lambda_k, True, k) for k in range(len(seeds))]

    confs = [result['confs'] for result in results]
    potEs = [result['Etot'] for result in results]
    
    delta_t = np.median([result['delta_t'] for result in results])
    delta_t = min(max(delta_t, 0.25*MMTK.Units.fs), 2.5*MMTK.Units.fs)
    sampler_metrics = '  '
    for s in ['ExternalMC', 'SmartDarting', 'Sampler']:
      if np.array(['acc_'+s in r.keys() for r in results]).any():
        acc = np.sum([r['acc_'+s] for r in results])
        att = np.sum([r['att_'+s] for r in results])
        time = np.sum([r['time_'+s] for r in results])
        if att>0:
          sampler_metrics += '%s acc=%d/%d=%.5f, t=%.3f s; '%(\
            s,acc,att,float(acc)/att,time)
    return (confs, np.array(potEs), delta_t, sampler_metrics)
  
  def _replica_exchange(self, process):
    """
    Performs a cycle of replica exchange
    """
    if not process in ['dock','cool']:
      raise Exception('Process must be dock or cool')
# GMC
    def gMC_initial_setup():
      """
      Initialize BAT converter object.
      Decide which internal coord to crossover. Here, only the soft torsions will be crossovered.
      Produce a list of replica (state) index pairs to be swaped. Only Neighbor pairs will be swaped.
      Assume that self.universe, self.molecule and K (number of states) exist
      as global variables when the function is called.
      """
      from AlGDock.RigidBodies import identifier
      import itertools
      BAT_converter = identifier( self.universe, self.molecule )
      BAT = BAT_converter.BAT( extended = True )
      # this assumes that the torsional angles are stored in the tail of BAT
      softTorsionId = [ i + len(BAT) - BAT_converter.ntorsions for i in BAT_converter._softTorsionInd ]
      torsions_to_crossover = []
      for i in range(1, len(softTorsionId) ):
        combinations = itertools.combinations( softTorsionId, i )
        for c in combinations:
          torsions_to_crossover.append( list(c) )
      #
      BAT_converter.BAT_to_crossover = torsions_to_crossover
      if len( BAT_converter.BAT_to_crossover ) == 0:
        self.tee('  GMC No BAT to crossover')
      state_indices = range( K )
      state_indices_to_swap = zip( state_indices[0::2], state_indices[1::2] ) + \
                      zip( state_indices[1::2], state_indices[2::2] )
      #
      return BAT_converter, state_indices_to_swap
    #
    def do_gMC( nr_attempts, BAT_converter, state_indices_to_swap, torsion_threshold ):
      """
      Assume self.universe, confs, lambdas, state_inds, inv_state_inds exist as global variables
      when the function is called.
      If at least one of the torsions in the combination chosen for an crossover attempt
      changes more than torsion_threshold, the crossover will be attempted.
      The function will update confs.
      It returns the number of attempts and the number of accepted moves.
      """
      if nr_attempts < 0:
        raise Exception('Number of attempts must be nonnegative!')
      if torsion_threshold < 0.:
        raise Exception('Torsion threshold must be nonnegative!')
      #
      if len( BAT_converter.BAT_to_crossover ) == 0:
        return 0., 0.
      #
      from random import randrange
      # get reduced energies and BAT for all configurations in confs
      BATs = []
      energies = np.zeros( K, dtype = float )
      for c_ind in range(K):
        s_ind = state_inds[ c_ind ]
        self.universe.setConfiguration( Configuration( self.universe, confs[c_ind] ) )
        BATs.append( np.array( BAT_converter.BAT( extended = True ) , dtype = float ) )
        self._set_universe_evaluator( lambdas[ s_ind ] )
        reduced_e = self.universe.energy() / ( R*lambdas[ s_ind ]['T'] )
        energies[ c_ind ] = reduced_e
      #
      nr_sets_of_torsions = len( BAT_converter.BAT_to_crossover )
      #
      attempt_count , acc_count = 0 , 0
      sweep_count = 0
      while True:
        sweep_count += 1
        if (sweep_count * K) > (1000 * nr_attempts):
          self.tee('  GMC Sweep too many times, but few attempted. Consider reducing torsion_threshold.')
          return attempt_count, acc_count
        #
        for state_pair in state_indices_to_swap:
          conf_ind_k0 = inv_state_inds[ state_pair[0] ]
          conf_ind_k1 = inv_state_inds[ state_pair[1] ]
          # check if it should attempt for this pair of states
          ran_set_torsions = BAT_converter.BAT_to_crossover[ randrange( nr_sets_of_torsions ) ]
          do_crossover = np.any(np.abs(BATs[conf_ind_k0][ran_set_torsions] - BATs[conf_ind_k1][ran_set_torsions]) >= torsion_threshold)
          if do_crossover:
            attempt_count += 1
            # BAT and reduced energies before crossover
            BAT_k0_be = copy.deepcopy( BATs[conf_ind_k0] )
            BAT_k1_be = copy.deepcopy( BATs[conf_ind_k1] )
            e_k0_be = energies[conf_ind_k0]
            e_k1_be = energies[conf_ind_k1]
            # BAT after crossover
            BAT_k0_af = copy.deepcopy( BAT_k0_be )
            BAT_k1_af = copy.deepcopy( BAT_k1_be )
            for index in ran_set_torsions:
              tmp = BAT_k0_af[ index ]
              BAT_k0_af[ index ] = BAT_k1_af[ index ]
              BAT_k1_af[ index ] = tmp
            # Cartesian coord and reduced energies after crossover.
            BAT_converter.Cartesian( BAT_k0_af )
            self._set_universe_evaluator( lambdas[ state_pair[0] ] )
            e_k0_af = self.universe.energy() / ( R*lambdas[ state_pair[0] ]['T'] )
            conf_k0_af = copy.deepcopy( self.universe.configuration().array )
            #
            BAT_converter.Cartesian( BAT_k1_af )
            self._set_universe_evaluator( lambdas[ state_pair[1] ] )
            e_k1_af = self.universe.energy() / ( R*lambdas[ state_pair[1] ]['T'] )
            conf_k1_af = copy.deepcopy( self.universe.configuration().array )
            #
            de = ( e_k0_be - e_k0_af ) + ( e_k1_be - e_k1_af )
            # update confs, energies, BATS
            if (de > 0) or ( np.random.uniform() < np.exp(de) ):
              acc_count += 1
              confs[conf_ind_k0] = conf_k0_af
              confs[conf_ind_k1] = conf_k1_af
              #
              energies[conf_ind_k0] = e_k0_af
              energies[conf_ind_k1] = e_k1_af
              #
              BATs[conf_ind_k0] = BAT_k0_af
              BATs[conf_ind_k1] = BAT_k1_af
            #
            if attempt_count == nr_attempts:
              return attempt_count, acc_count
    #
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

    from repX import attempt_swaps

    # Setting the force field will load grids
    # before multiple processes are spawned
    for k in range(K):
      self._set_universe_evaluator(lambdas[k])
    
    # If it has not been set up, set up Smart Darting
    if self.params[process]['darts_per_sweep']>0:
      if self.sampler[process+'_SmartDarting'].confs==[]:
        self.tee(self.sampler[process+'_SmartDarting'].set_confs(\
          self.confs[process]['SmartDarting']))
        self.confs[process]['SmartDarting'] = \
          self.sampler[process+'_SmartDarting'].confs
    
    storage = {}
    for var in ['confs','state_inds','energies']:
      storage[var] = []
    
    cycle_start_time = time.time()

    if self._cores>1:
      # Multiprocessing setup
      m = multiprocessing.Manager()
      task_queue = m.Queue()
      done_queue = m.Queue()

    # GMC
    do_gMC = self.params[process]['GMC_attempts'] > 0
    if do_gMC:
      self.tee('  Using GMC for %s' %process)
      nr_gMC_attempts = K * self.params[process]['GMC_attempts']
      torsion_threshold = self.params[process]['GMC_tors_threshold']
      gMC_attempt_count = 0
      gMC_acc_count     = 0
      time_gMC = 0.0
      BAT_converter, state_indices_to_swap = gMC_initial_setup()

    # MC move statistics
    acc = {}
    att = {}
    for move_type in ['ExternalMC','SmartDarting','Sampler']:
      acc[move_type] = np.zeros(K, dtype=int)
      att[move_type] = np.zeros(K, dtype=int)
      self.timing[move_type] = 0.
    self.timing['repX'] = 0.

    # Do replica exchange
    state_inds = range(K)
    inv_state_inds = range(K)
    for sweep in range(self.params[process]['sweeps_per_cycle']):
      E = {}
      for term in terms:
        E[term] = np.zeros(K, dtype=float)
      # Sample within each state
      if self._cores>1:
        for k in range(K):
          task_queue.put((confs[k], process, lambdas[state_inds[k]], False, k))
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
            lambdas[state_inds[k]], False, k) for k in range(K)]

      # GMC
      if do_gMC:
        time_start_gMC = time.time()
        att_count, acc_count = do_gMC( nr_gMC_attempts, BAT_converter, state_indices_to_swap, torsion_threshold )
        gMC_attempt_count += att_count
        gMC_acc_count     += acc_count
        time_gMC =+ ( time.time() - time_start_gMC )

      # Store energies
      for k in range(K):
        confs[k] = results[k]['confs']
        if process=='cool':
            E['MM'][k] = results[k]['Etot']
      if process=='dock':
        E = self._energyTerms(confs, E) # Get energies for scalables
        # Get rmsd values
        if self.params['dock']['rmsd'] is not False:
          E['rmsd'] = np.array([np.sqrt(((confs[k][self.molecule.heavy_atoms,:] - \
            self.confs['rmsd'])**2).sum()/self.molecule.nhatoms) for k in range(K)])

      # Store MC move statistics
      for k in range(K):
        for move_type in ['ExternalMC','SmartDarting','Sampler']:
          key = 'acc_'+move_type
          if key in results[k].keys():
            acc[move_type][state_inds[k]] += results[k][key]
            att[move_type][state_inds[k]] += results[k]['att_'+move_type]
            self.timing[move_type] += results[k]['time_'+move_type]

      # Calculate u_ij (i is the replica, and j is the configuration),
      #    a list of arrays
      (u_ij,N_k) = self._u_kln(E, [lambdas[state_inds[c]] for c in range(K)])
      # Do the replica exchange
      repX_start_time = time.time()
      (state_inds, inv_state_inds) = \
        attempt_swaps(state_inds, inv_state_inds, u_ij, pairs_to_swap, \
          self.params[process]['attempts_per_sweep'])
      self.timing['repX'] += (time.time()-repX_start_time)

      # Store data in local variables
      storage['confs'].append(list(confs))
      storage['state_inds'].append(list(state_inds))
      storage['energies'].append(copy.deepcopy(E))

    # GMC
    if do_gMC:
      self.tee('  {0}/{1} crossover attempts ({2:.3g}) accepted in {3}'.format(\
        gMC_acc_count, gMC_attempt_count, \
        float(gMC_acc_count)/float(gMC_attempt_count) \
          if gMC_attempt_count > 0 else 0, \
        HMStime(time_gMC)))

    # Estimate relaxation time from autocorrelation
    state_inds = np.array(storage['state_inds'])
    tau_ac = pymbar.timeseries.integratedAutocorrelationTimeMultiple(state_inds.T)
    # There will be at least per_independent and up to sweeps_per_cycle saved samples
    # max(int(np.ceil((1+2*tau_ac)/per_independent)),1) is the minimum stride,
    # which is based on per_independent samples per autocorrelation time.
    # max(self.params['dock']['sweeps_per_cycle']/per_independent)
    # is the maximum stride, which gives per_independent samples if possible.
    per_independent = self.params[process]['snaps_per_independent']
    stride = min(max(int(np.ceil((1+2*tau_ac)/per_independent)),1), \
                 max(int(np.ceil(self.params[process]['sweeps_per_cycle']/per_independent)),1))

    store_indicies = np.array(\
      range(min(stride-1,self.params[process]['sweeps_per_cycle']-1), \
      self.params[process]['sweeps_per_cycle'], stride), dtype=int)
    nsaved = len(store_indicies)

    self.tee("  generated %d configurations for %d replicas"%(nsaved, len(confs)) + \
      " in cycle %d in %s"%(cycle, HMStime(time.time()-cycle_start_time)) + \
      " (tau_ac=%f)"%(tau_ac))
    MC_report = " "
    for move_type in ['ExternalMC','SmartDarting','Sampler']:
      total_acc = np.sum(acc[move_type])
      total_att = np.sum(att[move_type])
      if total_att>0:
        MC_report += " %s acc=%d/%s=%.5f, t=%.3f;"%(move_type, \
          total_acc, total_att, float(total_acc)/total_att, \
          self.timing[move_type])
    MC_report += " repX t=%.3f"%self.timing['repX']
    self.tee(MC_report)

    # Get indicies for storing global variables
    inv_state_inds = np.zeros((nsaved,K),dtype=int)
    for snap in range(nsaved):
      state_inds = storage['state_inds'][store_indicies[snap]]
      for state in range(K):
        inv_state_inds[snap][state_inds[state]] = state

    # Reorder energies and replicas for storage
    if process=='dock':
      if self.params['dock']['rmsd'] is not False:
        terms.append('rmsd') # Make sure to save the rmsd
    Es = []
    for state in range(K):
      E_state = {}
      if state==0:
        E_state['repXpath'] = storage['state_inds']
        E_state['acc'] = acc
        E_state['att'] = att
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

    if self.params[process]['darts_per_sweep']>0:
      self._set_universe_evaluator(getattr(self,process+'_protocol')[-1])
      confs_SmartDarting = [np.copy(conf) \
        for conf in self.confs[process]['samples'][state][-1]]
      self.tee(self.sampler[process+'_SmartDarting'].set_confs(\
        confs_SmartDarting + self.confs[process]['SmartDarting']))
      self.confs[process]['SmartDarting'] = \
        self.sampler[process+'_SmartDarting'].confs

    setattr(self,'_%s_cycle'%process,cycle + 1)
    self._save(process)
    self.tee("")
    self._clear_lock(process)

  def _sim_one_state_worker(self, input, output):
    """
    Executes a task from the queue
    """
    for args in iter(input.get, 'STOP'):
      result = self._sim_one_state(*args)
      output.put(result)

  def _sim_one_state(self, seed, process, lambda_k, \
      initialize=False, reference=0):
    
    self.universe.setConfiguration(Configuration(self.universe, seed))
    self._set_universe_evaluator(lambda_k)
    if 'delta_t' in lambda_k.keys():
      delta_t = lambda_k['delta_t']
    else:
      delta_t = 1.5*MMTK.Units.fs
    
    if initialize:
      sampler = self.sampler[process]
      steps = self.params[process]['steps_per_seed']
      steps_per_trial = self.params[process]['steps_per_seed']/10
      ndarts = self.params[process]['darts_per_seed']
    else:
      sampler = self.sampler[process]
      steps = self.params[process]['steps_per_sweep']
      steps_per_trial = steps
      ndarts = self.params[process]['darts_per_sweep']

    random_seed = reference*reference + int(abs(seed[0][0]*10000))
    if self._random_seed>0:
      random_seed += self._random_seed
    else:
      random_seed += int(time.time()*1000)
    
    results = {}
    
    # Execute external MCMC moves
    if (process == 'dock') and (self.params['dock']['MCMC_moves']>0) \
        and (lambda_k['a'] < 0.1):
      time_start_ExternalMC = time.time()
      dat = self.sampler['ExternalMC'](ntrials=5, T=lambda_k['T'])
      results['acc_ExternalMC'] = dat[2]
      results['att_ExternalMC'] = dat[3]
      results['time_ExternalMC'] = (time.time() - time_start_ExternalMC)

    # Execute dynamics sampler
    time_start_Sampler = time.time()
    dat = sampler(\
      steps=steps, steps_per_trial=steps_per_trial, \
      T=lambda_k['T'], delta_t=delta_t, \
      normalize=(process=='cool'), adapt=initialize, random_seed=random_seed)
    results['acc_Sampler'] = dat[2]
    results['att_Sampler'] = dat[3]
    results['delta_t'] = dat[4]
    results['time_Sampler'] = (time.time() - time_start_Sampler)

    # Execute smart darting
    if ndarts>0:
      time_start_SmartDarting = time.time()
      dat = self.sampler[process+'_SmartDarting'](\
        ntrials=ndarts, T=lambda_k['T'], random_seed=random_seed+5)
      results['acc_SmartDarting'] = dat[2]
      results['att_SmartDarting'] = dat[3]
      results['time_SmartDarting'] = (time.time() - time_start_SmartDarting)

    # Store and return results
    results['confs'] = np.copy(dat[0][-1])
    results['Etot'] = dat[1][-1]
    results['reference'] = reference

    return results

  def _sim_process(self, process):
    """
    Simulate and analyze a cooling or docking process.
    
    As necessary, first conduct an initial cooling or docking
    and then run a desired number of replica exchange cycles.
    """
    if (getattr(self,process+'_protocol')==[]) or \
       (not getattr(self,process+'_protocol')[-1]['crossed']):
      time_left = getattr(self,'initial_'+process)()
      if not time_left:
        return False

    # Main loop for replica exchange
    if (self.params[process]['repX_cycles'] is not None) and \
       ((getattr(self,'_%s_cycle'%process) < \
         self.params[process]['repX_cycles'])):

      # Load configurations to score from another program
      if (process=='dock') and (self._dock_cycle==1) and \
         (self._FNs['score'] is not None) and \
         (self._FNs['score']!='default'):
        self._set_lock('dock')
        self.tee(">>> Reinitializing replica exchange configurations")
        confs = self._get_confs_to_rescore(\
          nconfs=len(self.dock_protocol), site=True, minimize=True)[0]
        self._clear_lock('dock')
        if len(confs)>0:
          self.confs['dock']['replicas'] = confs

      self.tee("\n>>> Replica exchange for {0}ing, starting at {1} GMT".format(\
        process, time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())), \
        process=process)
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

  def _get_confs_to_rescore(self, nconfs=None, site=False, minimize=True, sort=True):
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
    # Get configurations
    count = {'xtal':0, 'dock6':0, 'initial_dock':0, 'duplicated':0}
    
    # based on the score option
    if self._FNs['score']=='default':
      confs = [np.copy(self.confs['ligand'])]
      count['xtal'] = 1
      Es = {}
      if nconfs is None:
        nconfs = 1
    elif (self._FNs['score'] is None) or (not os.path.isfile(self._FNs['score'])):
      confs = []
      Es = {}
    elif self._FNs['score'].endswith('.mol2') or \
         self._FNs['score'].endswith('.mol2.gz'):
      import AlGDock.IO
      IO_dock6_mol2 = AlGDock.IO.dock6_mol2()
      (confs, Es) = IO_dock6_mol2.read(self._FNs['score'], \
        reorder=self.molecule.inv_prmtop_atom_order)
      count['dock6'] = len(confs)
    elif self._FNs['score'].endswith('.nc'):
      from netCDF4 import Dataset
      dock6_nc = Dataset(self._FNs['score'],'r')
      confs = [dock6_nc.variables['confs'][n][self.molecule.inv_prmtop_atom_order,:] for n in range(dock6_nc.variables['confs'].shape[0])]
      Es = dict([(key,dock6_nc.variables[key][:]) for key in dock6_nc.variables.keys() if key !='confs'])
      dock6_nc.close()
      count['dock6'] = len(confs)
    elif self._FNs['score'].endswith('.pkl.gz'):
      F = gzip.open(self._FNs['score'],'r')
      confs = pickle.load(F)
      F.close()
      if not isinstance(confs, list):
        confs = [confs]
      Es = {}
    else:
      raise Exception('Input configuration format not recognized')

    # based on the seeds
    if self.confs['dock']['seeds'] is not None:
      confs = confs + self.confs['dock']['seeds']
      count['initial_dock'] = len(self.confs['dock']['seeds'])

    if len(confs)==0:
      return ([],{})

    if site:
      # Filters out configurations not in the binding site
      confs_in_site = []
      Es_in_site = dict([(label,[]) for label in Es.keys()])
      old_eval = None
      if (None,None,None) in self.universe._evaluator.keys():
        old_eval = self.universe._evaluator[(None,None,None)]
      self._set_universe_evaluator({'site':True,'T':self.T_TARGET})
      for n in range(len(confs)):
        self.universe.setConfiguration(Configuration(self.universe, confs[n]))
        if self.universe.energy()<1.:
          confs_in_site.append(confs[n])
          for label in Es.keys():
            Es_in_site[label].append(Es[label][n])
      if old_eval is not None:
        self.universe._evaluator[(None,None,None)] = old_eval
      confs = confs_in_site
      Es = Es_in_site
      
    try:
      self.universe.energy()
    except ValueError:
      return (confs,{})

    if minimize:
      Es = {}
      from MMTK.Minimization import SteepestDescentMinimizer # @UnresolvedImport
      minimizer = SteepestDescentMinimizer(self.universe)

      minimized_confs = []
      minimized_energies = []
      min_start_time = time.time()
      for conf in confs:
        self.universe.setConfiguration(Configuration(self.universe, conf))
        x_o = np.copy(self.universe.configuration().array)
        e_o = self.universe.energy()
        for rep in range(50):
          minimizer(steps = 25)
          x_n = np.copy(self.universe.configuration().array)
          e_n = self.universe.energy()
          diff = abs(e_o-e_n)
          if np.isnan(e_n) or diff<0.05 or diff>1000.:
            self.universe.setConfiguration(Configuration(self.universe, x_o))
            break
          else:
            x_o = x_n
            e_o = e_n
        if not np.isnan(e_o):
          minimized_confs.append(x_o)
          minimized_energies.append(e_o)
      confs = minimized_confs
      energies = minimized_energies
      self.tee("\n  minimized %d configurations in "%len(confs) + \
        HMStime(time.time()-min_start_time) + \
        "\n  the first %d energies are: "%min(len(confs),10) + \
        ', '.join(['%.2f'%e for e in energies[:10]]))
    else:
      # Evaluate energies
      energies = []
      for conf in confs:
        self.universe.setConfiguration(Configuration(self.universe, conf))
        energies.append(self.universe.energy())

    if sort:
      # Sort configurations by DECREASING energy
      energies, confs = (list(l) for l in zip(*sorted(zip(energies, confs), \
        key=lambda p:p[0], reverse=True)))

    # Shrink or extend configuration and energy array
    if nconfs is not None:
      confs = confs[-nconfs:]
      energies = energies[-nconfs:]
      while len(confs)<nconfs:
        confs.append(confs[-1])
        energies.append(energies[-1])
        count['duplicated'] += 1
      count['nconfs'] = nconfs
    else:
      count['nconfs'] = len(confs)
    count['minimized'] = {True:' minimized', False:''}[minimize]
    Es['total'] = energies

    self.tee("  keeping {nconfs}{minimized} configurations out of {xtal} from xtal, {dock6} from dock6, {initial_dock} from initial docking, and {duplicated} duplicated\n".format(**count))
    return (confs, Es)

  def _run_MBAR(self,u_kln,N_k):
    """
    Estimates the free energy of a transition using BAR and MBAR
    """
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
        verbose = False,
        initial_f_k = f_k_BAR,
        maximum_iterations = 20).f_k
    except:
      f_k_MBAR = f_k_BAR
    if np.isnan(f_k_MBAR).any():
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
            if pow>0:
              a = lambda_o['a']*(1-0.8**pow)
            else:
              a = 0.0
              crossed = True
        else:
          a = lambda_o['a'] + dL
          if a > 1.0:
            if pow>0:
              a = lambda_o['a'] + (1-lambda_o['a'])*0.8**pow
            else:
              a = 1.0
              crossed = True
        return self._lambda(a, process='dock', lambda_o=lambda_o, crossed=crossed)
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
             np.abs(self.T_TARGET-self.T_HIGH)*U_RL_g.std()/(R*T*T)
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
      lambda_n['T'] = a*(self.T_TARGET-self.T_HIGH) + self.T_HIGH
    elif process=='cool':
      lambda_n['a'] = a
      lambda_n['T'] = self.T_HIGH - a*(self.T_HIGH-self.T_TARGET)
    else:
      raise Exception("Unknown process!")

    return lambda_n

  def _load_programs(self, phases):
    # Find the necessary programs, downloading them if necessary
    programs = []
    for phase in phases:
      for (prefix,program) in [('NAMD','namd'), \
          ('sander','sander'), ('gbnsr6','gbnsr6'), ('APBS','apbs')]:
        if phase.startswith(prefix) and not program in programs:
          programs.append(program)
      if phase.find('ALPB')>-1:
        if not 'elsize' in programs:
          programs.append('elsize')
        if not 'ambpdb' in programs:
          programs.append('ambpdb')
    if 'apbs' in programs:
      for program in ['ambpdb','molsurf']:
        if not program in programs:
          programs.append(program)
    for program in programs:
      self._FNs[program] = a.findPaths([program])[program]
    a.loadModules(programs)

  def _postprocess(self,
      conditions=[('original',0, 0,'R'), ('cool',-1,-1,'L'), \
                  ('dock',   -1,-1,'L'), ('dock',-1,-1,'RL')],
      phases=None,
      readOnly=False, redo_dock=False, debug=DEBUG):
    """
    Obtains the NAMD energies of all the conditions using all the phases.  
    Saves both MMTK and NAMD energies after NAMD energies are estimated.
    
    state == -1 means the last state
    cycle == -1 means all cycles

    """
    # Clear evaluators to save memory
    self._evaluators = {}
    
    if phases is None:
      phases = list(set(self.params['cool']['phases'] + self.params['dock']['phases']))

    updated_processes = []

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
    
    self._load_programs([val[-1] for val in incomplete])

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
      
      if phase.startswith('NAMD'):
        traj_FN = join(p_dir,'%s.%s.dcd'%(prefix,moiety))
      elif phase.startswith('sander'):
        traj_FN = join(p_dir,'%s.%s.mdcrd'%(prefix,moiety))
      elif phase.startswith('gbnsr6'):
        traj_FN = join(p_dir,'%s.%s%s'%(prefix,moiety,phase),'in.crd')
      elif phase.startswith('OpenMM'):
        traj_FN = None
      elif phase in ['APBS_PBSA']:
        traj_FN = join(p_dir,'%s.%s.pqr'%(prefix,moiety))
      outputname = join(p_dir,'%s.%s%s'%(prefix,moiety,phase))

      # Writes trajectory
      self._write_traj(traj_FN, confs, moiety)
      if (traj_FN is not None) and (not traj_FN in toClean):
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
    updated_energy_dicts = []
    for (E,(p,state,c,label),wall_time) in results:
      if p=='original':
        self.original_Es[state][c][label] = E
        updated_energy_dicts.append(self.original_Es[state][c])
      else:
        getattr(self,p+'_Es')[state][c][label] = E
        updated_energy_dicts.append(getattr(self,p+'_Es')[state][c])
      if not p in updated_processes:
        updated_processes.append(p)
    for d in updated_energy_dicts:
      self._combine_MM_and_solvent(d)

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
      for program in ['NAMD','sander','gbnsr6','OpenMM','APBS']:
        if phase.startswith(program):
          E = getattr(self,'_%s_Energy'%program)(*args)
          break
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

  def _energyTerms(self, confs, E=None, debug=DEBUG):
    """
    Calculates MMTK energy terms for a series of configurations
    Units are the MMTK standard, kJ/mol
    """
    if E is None:
      E = {}

    lambda_full = {'T':self.T_HIGH,'MM':True,'site':True}
    for scalable in self._scalables:
      lambda_full[scalable] = 1
    self._set_universe_evaluator(lambda_full)
    # Molecular mechanics and grid interaction energies
    for term in (['MM','site','misc'] + self._scalables):
      E[term] = np.zeros(len(confs), dtype=float)
    for c in range(len(confs)):
      self.universe.setConfiguration(Configuration(self.universe,confs[c]))
      eT = self.universe.energyTerms()
      for (key,value) in eT.iteritems():
        E[term_map[key]][c] += value
    return E

  def _NAMD_Energy(self, confs, moiety, phase, dcd_FN, outputname,
      debug=DEBUG, reference=None):
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
      keepScript=debug, write_energy_pkl_gz=False)

    return np.array(E, dtype=float)*MMTK.Units.kcal/MMTK.Units.mol

  def _sander_Energy(self, confs, moiety, phase, AMBER_mdcrd_FN, \
      outputname=None, debug=DEBUG, reference=None):
    self.dir['out'] = os.path.dirname(os.path.abspath(AMBER_mdcrd_FN))
    script_FN = '%s%s.in'%('.'.join(AMBER_mdcrd_FN.split('.')[:-1]),phase)
    out_FN = '%s%s.out'%('.'.join(AMBER_mdcrd_FN.split('.')[:-1]),phase)

    script_F = open(script_FN,'w')
    script_F.write('''Calculating energies with sander
&cntrl
  imin=5,    ! read trajectory in for analysis
  ntx=1,     ! input is read formatted with no velocities
  irest=0,
  ntb=0,     ! no periodicity and no PME
  idecomp=0, ! no decomposition
  ntc=1,     ! No SHAKE
  cut=9999., !''')
    if phase=='sander_Gas':
      script_F.write("""
  ntf=1,     ! Complete interaction is calculated
/
""")
    elif phase=='sander_PBSA':
      script_F.write('''
  ntf=7,     ! No bond, angle, or dihedral forces calculated
  ipb=2,     ! Default PB dielectric model
  inp=2,     ! non-polar from cavity + dispersion
/
&pb
  radiopt=0, ! Use atomic radii from the prmtop file
  fillratio=4.0,
  sprob=1.4,
  cavity_surften=0.0378, ! (kcal/mol) Default in MMPBSA.py
  cavity_offset=-0.5692, ! (kcal/mol) Default in MMPBSA.py
/
''')
    else:
      if phase.find('ALPB')>-1 and moiety.find('R')>-1:
        script_F.write("\n  alpb=1,")
        script_F.write("\n  arad=%.2f,"%self.elsize)
      key = phase.split('_')[-1]
      igb = {'HCT':1, 'OBC1':2, 'OBC2':5, 'GBn':7, 'GBn2':8}[key]
      script_F.write('''
  ntf=7,     ! No bond, angle, or dihedral forces calculated
  igb=%d,     !
  gbsa=2,    ! recursive surface area algorithm (for postprocessing)
/
'''%(igb))
    script_F.close()
    
    os.chdir(self.dir['out'])
    import subprocess
    args_list = [self._FNs['sander'], '-O','-i',script_FN,'-o',out_FN, \
      '-p',self._FNs['prmtop'][moiety],'-c',self._FNs['inpcrd'][moiety], \
      '-y', AMBER_mdcrd_FN, '-r',script_FN+'.restrt']
    if debug:
      print ' '.join(args_list)
    p = subprocess.Popen(args_list)
    p.wait()
    
    F = open(out_FN,'r')
    dat = F.read().strip().split(' BOND')
    F.close()

    dat.pop(0)
    if len(dat)>0:
      # For the different models, all the terms are the same except for
      # EGB/EPB (every model is different)
      # ESURF versus ECAVITY + EDISPER
      # EEL (ALPB versus not)
      E = np.array([rec[:rec.find('\nminimization')].replace('1-4 ','1-4').split()[1::3] for rec in dat],dtype=float)*MMTK.Units.kcal/MMTK.Units.mol
      if phase=='sander_Gas':
        E = np.hstack((E,np.sum(E,1)[...,None]))
      else:
        # Mark as nan to add the Gas energies later
        E = np.hstack((E,np.ones((E.shape[0],1))*np.nan))

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
    # For GBSA phases:
    # 0. BOND 1. ANGLE 2. DIHEDRAL 3. VDWAALS 4. EEL
    # 5. EGB 6. 1-4 VWD 7. 1-4 EEL 8. RESTRAINT 9. ESURF
    # For PBSA phase:
    # 0. BOND 1. ANGLE 2. DIHEDRAL 3. VDWAALS 4. EEL
    # 5. EPB 6. 1-4 VWD 7. 1-4 EEL 8. RESTRAINT 9. ECAVITY 10. EDISPER

  def _get_elsize(self):
    # Calculates the electrostatic size of the receptor for ALPB calculations
    # Writes the coordinates in AMBER format
    inpcrd_FN = os.path.join(self.dir['dock'], 'receptor.inpcrd')
    pqr_FN = os.path.join(self.dir['dock'], 'receptor.pqr')
    
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()
    factor = 1.0/MMTK.Units.Ang
    IO_crd.write(inpcrd_FN, factor*self.confs['receptor'], \
      'title', trajectory=False)
    
    # Converts the coordinates to a pqr file
    inpcrd_F = open(inpcrd_FN,'r')
    cdir = os.getcwd()
    import subprocess
    try:
      p = subprocess.Popen(\
        [self._FNs['ambpdb'], \
         '-p', os.path.relpath(self._FNs['prmtop']['R'], cdir), \
         '-pqr'], \
        stdin=inpcrd_F, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata_ambpdb, stderrdata_ambpdb) = p.communicate()
      p.wait()
    except OSError:
      os.system('ls -ltr')
      print 'Command: ' + ' '.join([os.path.relpath(self._FNs['ambpdb'], cdir), \
         '-p', os.path.relpath(self._FNs['prmtop']['R'], cdir), \
         '-pqr'])
      print 'stdout:\n' + stdoutdata_ambpdb
      print 'stderr:\n' + stderrdata_ambpdb
    inpcrd_F.close()
    
    pqr_F = open(pqr_FN,'w')
    pqr_F.write(stdoutdata_ambpdb)
    pqr_F.close()

    # Runs the pqr file through elsize
    p = subprocess.Popen(\
      [self._FNs['elsize'], os.path.relpath(pqr_FN, cdir)], \
      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (stdoutdata_elsize, stderrdata_elsize) = p.communicate()
    p.wait()
    
    for FN in [inpcrd_FN, pqr_FN]:
      if os.path.isfile(FN):
        os.remove(FN)
    try:
      elsize = float(stdoutdata_elsize.strip())
    except ValueError:
      print 'Command: ' + ' '.join([os.path.relpath(self._FNs['elsize'], cdir), \
       os.path.relpath(pqr_FN, cdir)])
      print stdoutdata_elsize
      print 'Error with elsize'
    return elsize

  def _gbnsr6_Energy(self, confs, moiety, phase, inpcrd_FN, outputname,
      debug=DEBUG, reference=None):
    """
    Uses gbnsr6 (part of AmberTools) 
    to calculate the energy of a set of configurations
    """
    # Prepare configurations for writing to crd file
    factor=1.0/MMTK.Units.Ang
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

    # Set up directory
    inpcrdFN = os.path.abspath(inpcrd_FN)
    gbnsr6_dir = os.path.dirname(inpcrd_FN)
    os.system('mkdir -p '+gbnsr6_dir)
    os.chdir(gbnsr6_dir)
    cdir = os.getcwd()
    
    # Write gbnsr6 script
    chagb = 0 if phase.find('Still')>-1 else 1
    alpb = 1 if moiety.find('R')>-1 else 0 # ALPB ineffective with small solutes
    gbnsr6_in_FN = moiety+'gbnsr6.in'
    gbnsr6_in_F = open(gbnsr6_in_FN,'w')
    gbnsr6_in_F.write("""gbnsr6
&cntrl
  inp=1
/
&gb
  alpb=%d,
  chagb=%d
/
"""%(alpb, chagb))
    gbnsr6_in_F.close()

    args_list = [self._FNs['gbnsr6'], \
      '-i', os.path.relpath(gbnsr6_in_FN, cdir), \
      '-o', 'stdout', \
      '-p', os.path.relpath(self._FNs['prmtop'][moiety], cdir), \
      '-c', os.path.relpath(inpcrd_FN, cdir)]
    if debug:
      print ' '.join(args_list)

    # Write coordinates, run gbnsr6, and store energies
    import subprocess
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()

    E = []
    for full_conf in full_confs:
      # Writes the coordinates in AMBER format
      IO_crd.write(inpcrd_FN, full_conf, 'title', trajectory=False)
      
      # Runs gbnsr6
      import subprocess
      p = subprocess.Popen(args_list, \
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata, stderrdata) = p.communicate()
      p.wait()

      recs = stdoutdata.strip().split(' BOND')
      if len(recs)>1:
        rec = recs[1]
        E.append(rec[:rec.find('\n -----')].replace('1-4 ','1-4').split()[1::3])
      else:
        self.tee("  error has occured in gbnsr6 after %d snapshots"%len(E))
        self.tee("  prmtop was "+self._FNs['prmtop'][moiety])
        self.tee("  --- stdout:")
        self.tee(stdoutdata)
        self.tee("  --- stderr:")
        self.tee(stderrdata)
      
    E = np.array(E, dtype=float)*MMTK.Units.kcal/MMTK.Units.mol
    E = np.hstack((E,np.ones((E.shape[0],1))*np.nan))
    
    os.chdir(self.dir['start'])
    if not debug:
      os.system('rm -rf '+gbnsr6_dir)
    return E
    # For gbnsr6 phases:
    # 0. BOND 1. ANGLE 2. DIHED 3. 1-4 NB 4. 1-4 EEL
    # 5. VDWAALS 6. EELEC 7. EGB 8. RESTRAINT 9. ESURF
    
  def _OpenMM_Energy(self, confs, moiety, phase, traj_FN=None, \
      outputname=None, debug=DEBUG, reference=None):
    import simtk.openmm
    import simtk.openmm.app as OpenMM_app
    # Set up the simulation
    key = moiety+phase
    if not key in self._OpenMM_sims.keys():
      prmtop = OpenMM_app.AmberPrmtopFile(self._FNs['prmtop'][moiety])
      inpcrd = OpenMM_app.AmberInpcrdFile(self._FNs['inpcrd'][moiety])
      OMM_system = prmtop.createSystem(nonbondedMethod=OpenMM_app.NoCutoff, \
        constraints=None, implicitSolvent={
          'OpenMM_Gas':None,
          'OpenMM_GBn':OpenMM_app.GBn,
          'OpenMM_GBn2':OpenMM_app.GBn2,
          'OpenMM_HCT':OpenMM_app.HCT,
          'OpenMM_OBC1':OpenMM_app.OBC1,
          'OpenMM_OBC2':OpenMM_app.OBC2}[phase])
      dummy_integrator = simtk.openmm.LangevinIntegrator(300*simtk.unit.kelvin, \
        1/simtk.unit.picosecond, 0.002*simtk.unit.picoseconds)
      # platform = simtk.openmm.Platform.getPlatformByName('CPU')
      self._OpenMM_sims[key] = OpenMM_app.Simulation(prmtop.topology, \
        OMM_system, dummy_integrator)

    # Prepare the conformations by combining with the receptor if necessary
    if (moiety.find('R')>-1):
      receptor_0 = self.confs['receptor'][:self._ligand_first_atom,:]
      receptor_1 = self.confs['receptor'][self._ligand_first_atom:,:]
    if not isinstance(confs,list):
      confs = [confs]
    if (moiety.find('R')>-1):
      if (moiety.find('L')>-1):
        confs = [np.vstack((receptor_0, \
          conf[self.molecule.prmtop_atom_order,:], \
          receptor_1)) for conf in confs]
      else:
        confs = [self.confs['receptor']]
    else:
      confs = [conf[self.molecule.prmtop_atom_order,:] for conf in confs]
    
    # Calculate the energies
    E = []
    for conf in confs:
      self._OpenMM_sims[key].context.setPositions(conf)
      s = self._OpenMM_sims[key].context.getState(getEnergy=True)
      E.append([0., s.getPotentialEnergy()/simtk.unit.kilojoule*simtk.unit.mole])
    return np.array(E, dtype=float)*MMTK.Units.kJ/MMTK.Units.mol

  def _APBS_Energy(self, confs, moiety, phase, pqr_FN, outputname,
      debug=DEBUG, reference=None):
    """
    Uses APBS to calculate the solvation energy of a set of configurations
    Units are the MMTK standard, kJ/mol
    """
    # Prepare configurations for writing to crd file
    factor=1.0/MMTK.Units.Ang
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
    apbs_dir = os.path.abspath(pqr_FN)[:-4]
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

      for sdie in [80.0,1.0]:
        if moiety=='L':
          min_xyz = np.array([min(full_conf[a,:]) for a in range(3)])
          max_xyz = np.array([max(full_conf[a,:]) for a in range(3)])
          mol_range = max_xyz - min_xyz
          mol_center = (min_xyz + max_xyz)/2.
          
          def roundUpDime(x):
            return (np.ceil((x.astype(float)-1)/32)*32+1).astype(int)
          
          focus_spacing = 0.5
          focus_dims = roundUpDime(mol_range*LFILLRATIO/focus_spacing)
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
  pdie 1.0
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
#      TODO: Control the number of threads. This doesn't seem to do anything.
#      if self._cores==1:
#        os.environ['OMP_NUM_THREADS']='1'
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

      if debug:
        molsurf_out_FN = moiety+'molsurf-mg-manual.out'
        molsurf_out_F = open(molsurf_out_FN, 'w')
        molsurf_out_F.write(stdoutdata)
        molsurf_out_F.close()
      else:
        for FN in [inpcrd_FN, pqr_FN, apbs_in_FN, 'io.mc']:
          os.remove(FN)
      
      E.append([polar_energy, apolar_energy, np.nan])

      if np.isinf(polar_energy) or np.isinf(apolar_energy):
        break

    os.chdir(self.dir['start'])
    if not debug:
      os.system('rm -rf '+apbs_dir)
    return np.array(E, dtype=float)*MMTK.Units.kJ/MMTK.Units.mol

  def _get_APBS_grid_spacing(self, RFILLRATIO=RFILLRATIO):
    factor = 1.0/MMTK.Units.Ang
    
    def roundUpDime(x):
      return (np.ceil((x.astype(float)-1)/32)*32+1).astype(int)

    self._set_universe_evaluator({'MM':True, 'T':self.T_HIGH, 'ELE':1})
    gd = self._forceFields['ELE'].grid_data
    focus_dims = roundUpDime(gd['counts'])
    focus_center = factor*(gd['counts']*gd['spacing']/2. + gd['origin'])
    focus_spacing = factor*gd['spacing'][0]

    min_xyz = np.array([min(factor*self.confs['receptor'][a,:]) for a in range(3)])
    max_xyz = np.array([max(factor*self.confs['receptor'][a,:]) for a in range(3)])
    mol_range = max_xyz - min_xyz
    mol_center = (min_xyz + max_xyz)/2.

    # The full grid spans RFILLRATIO times the range of the receptor
    # and the focus grid, whatever is larger
    full_spacing = 1.0
    full_min = np.minimum(mol_center - mol_range/2.*RFILLRATIO, \
                          focus_center - focus_dims*focus_spacing/2.*RFILLRATIO)
    full_max = np.maximum(mol_center + mol_range/2.*RFILLRATIO, \
                          focus_center + focus_dims*focus_spacing/2.*RFILLRATIO)
    full_dims = roundUpDime((full_max-full_min)/full_spacing)
    full_center = (full_min + full_max)/2.

    self._apbs_grid = {\
      'dime':[full_dims, focus_dims], \
      'gcent':[full_center, focus_center], \
      'spacing':[full_spacing, focus_spacing]}

  def _combine_MM_and_solvent(self, E):
    toParse = [k for k in E.keys() if (E[k] is not None) and (len(E[k].shape)==2)]
    for key in toParse:
      if np.isnan(E[key][:,-1]).all():
        E[key] = E[key][:,:-1]
        if key.find('sander')>-1:
          prefix = key.split('_')[0][:-6]
          for c in [0,1,2,6,7]:
            E[key][:,c] = E[prefix+'sander_Gas'][:,c]
        elif key.find('gbnsr6')>-1:
          prefix = key.split('_')[0][:-6]
          for (gbnsr6_ind, sander_ind) in [(0,0),(1,1),(2,2),(3,6),(5,3)]:
            E[key][:,gbnsr6_ind] = E[prefix+'sander_Gas'][:,sander_ind]
        elif key.find('APBS_PBSA'):
          prefix = key[:-9]
          totalMM = np.transpose(np.atleast_2d(E[prefix+'NAMD_Gas'][:,-1]))
          E[key] = np.hstack((E[key],totalMM))
        E[key] = np.hstack((E[key],np.sum(E[key],1)[...,None]))

  def _write_traj(self, traj_FN, confs, moiety, \
      title='', factor=1.0/MMTK.Units.Ang):
    """
    Writes a trajectory file
    """
    
    if traj_FN is None:
      return
    if traj_FN.endswith('.pqr'):
      return
    if traj_FN.endswith('.crd'):
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
      if (saved['progress'] is None):
        print '  no progress information for %s'%p
      elif (saved['data'] is None):
        saved['progress'] = None
        print '  missing data in %s'%p
      else:
        print '  using stored progress and data in %s'%p
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
      self.confs[p]['SmartDarting'] = saved['data'][3]
      self.confs[p]['samples'] = saved['data'][4]
      setattr(self,'%s_Es'%p, saved['data'][5])
      if saved['data'][4] is not None:
        cycle = len(saved['data'][4][-1])
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
    self.confs[p]['SmartDarting'] = []
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
          'tarball':{'L':self._FNs['tarball']['L']},
          'prmtop':{'L':self._FNs['prmtop']['L']},
          'inpcrd':{'L':self._FNs['inpcrd']['L']}},
          relpath_o=None, relpath_n=self.dir['cool'])
    elif p=='dock':
      fn_dict = convert_dictionary_relpath(
          dict(self._FNs.items()), relpath_o=None, relpath_n=self.dir['dock'])
    params = (fn_dict,arg_dict)
    
    saved = {
      'progress': (params,
                   getattr(self,'%s_protocol'%p),
                   getattr(self,'_%s_cycle'%p)),
      'data': (random_orient,
               self.confs[p]['replicas'],
               self.confs[p]['seeds'],
               self.confs[p]['SmartDarting'],
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

  def __del__(self):
    if (not DEBUG) and len(self._toClear)>0:
      print '\n>>> Clearing files'
      for FN in self._toClear:
        if os.path.isfile(FN):
          os.remove(FN)
          print '  removed '+os.path.relpath(FN,self.dir['start'])

if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser(
    description='Molecular docking with adaptively scaled alchemical interaction grids')
  
  for key in arguments.keys():
    parser.add_argument('--'+key, **arguments[key])
  args = parser.parse_args()

  if args.run_type in ['render_docked', 'render_intermediates']:
    from AlGDock.BindingPMF_plots import BPMF_plots
    self = BPMF_plots(**vars(args))
  else:
    self = BPMF(**vars(args))
