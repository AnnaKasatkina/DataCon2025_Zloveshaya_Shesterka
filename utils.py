"""Tools for manipulating graphs and converting from atom and pair features."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import random
from collections import deque
from rdkit import Chem
from rdkit.Chem import Descriptors
from rdkit.Chem.Scaffolds import MurckoScaffold
import torch.nn as nn
from rdkit import DataStructs
from rdkit.Chem import AllChem
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem import RDConfig
import numpy as np
import hyp
import sys
import os
import torch

sys.path.append(os.path.join(RDConfig.RDContribDir, "SA_Score"))
import sascorer


def get_fingerprint(smiles, fingerprint_length, fingerprint_radius):
    """Get Morgan Fingerprint of a specific SMILES string.

  Args:
    smiles: String. The SMILES string of the molecule.
    hparams: tf.contrib.training.HParams. Hyper parameters.

  Returns:
    np.array. shape = [hparams.fingerprint_length]. The Morgan fingerprint.
  """
    mfpgen = rdFingerprintGenerator.GetMorganGenerator(radius=hyp.fingerprint_radius,fpSize=hyp.fingerprint_length)
    if smiles is None:
        return np.zeros((hyp.fingerprint_length,))
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return np.zeros((hyp.fingerprint_length,))
    fingerprint = mfpgen.GetFingerprint(molecule)
    arr = np.zeros((1,))
    # ConvertToNumpyArray takes ~ 0.19 ms, while
    # np.asarray takes ~ 4.69 ms
    DataStructs.ConvertToNumpyArray(fingerprint, arr)
    return arr


def atom_valences(atom_types):
    """Creates a list of valences corresponding to atom_types.

  Note that this is not a count of valence electrons, but a count of the
  maximum number of bonds each element will make. For example, passing
  atom_types ['C', 'H', 'O'] will return [4, 1, 2].

  Args:
    atom_types: List of string atom types, e.g. ['C', 'H', 'O'].

  Returns:
    List of integer atom valences.
  """
    periodic_table = Chem.GetPeriodicTable()
    return [
        max(list(periodic_table.GetValenceList(atom_type))) for atom_type in atom_types
    ]


def get_scaffold(mol):
    """Computes the Bemis-Murcko scaffold for a molecule.

  Args:
    mol: RDKit Mol.

  Returns:
    String scaffold SMILES.
  """
    return Chem.MolToSmiles(MurckoScaffold.GetScaffoldForMol(mol), isomericSmiles=True)


def contains_scaffold(mol, scaffold):
    """Returns whether mol contains the given scaffold.

  NOTE: This is more advanced than simply computing scaffold equality (i.e.
  scaffold(mol_a) == scaffold(mol_b)). This method allows the target scaffold to
  be a subset of the (possibly larger) scaffold in mol.

  Args:
    mol: RDKit Mol.
    scaffold: String scaffold SMILES.

  Returns:
    Boolean whether scaffold is found in mol.
  """
    pattern = Chem.MolFromSmiles(scaffold)
    matches = mol.GetSubstructMatches(pattern)
    return bool(matches)


def get_largest_ring_size(molecule):
    """Calculates the largest ring size in the molecule.

  Refactored from
  https://github.com/wengong-jin/icml18-jtnn/blob/master/bo/run_bo.py

  Args:
    molecule: Chem.Mol. A molecule.

  Returns:
    Integer. The largest ring size.
  """
    cycle_list = molecule.GetRingInfo().AtomRings()
    if cycle_list:
        cycle_length = max([len(j) for j in cycle_list])
    else:
        cycle_length = 0
    return cycle_length


def penalized_logp(molecule):
    """Calculates the penalized logP of a molecule.

  Refactored from
  https://github.com/wengong-jin/icml18-jtnn/blob/master/bo/run_bo.py
  See Junction Tree Variational Autoencoder for Molecular Graph Generation
  https://arxiv.org/pdf/1802.04364.pdf
  Section 3.2
  Penalized logP is defined as:
   y(m) = logP(m) - SA(m) - cycle(m)
   y(m) is the penalized logP,
   logP(m) is the logP of a molecule,
   SA(m) is the synthetic accessibility score,
   cycle(m) is the largest ring size minus by six in the molecule.

  Args:
    molecule: Chem.Mol. A molecule.

  Returns:
    Float. The penalized logP value.

  """
    log_p = Descriptors.MolLogP(molecule)
    sas_score = sascorer.calculateScore(molecule)
    largest_ring_size = get_largest_ring_size(molecule)
    cycle_score = max(largest_ring_size - 6, 0)
    return log_p - sas_score - cycle_score


class ReplayBuffer:
    def __init__(self, size):
        self.buffer = deque(maxlen=size)
    
    def add(self, obs_t, action, reward, obs_tp1, done):
        self.buffer.append(((obs_t, action, reward, obs_tp1, done)))
    
    def sample(self, batch_size):
        samples = random.sample(self.buffer, batch_size)
        obs, act, rew, obs_tp1, done = [],[],[],[],[]
        for smp in samples: 
            obs.append(smp[0])
            act.append(smp[1]) 
            rew.append(smp[2]) 
            obs_tp1.append(smp[3]) 
            done.append(smp[4])
        return np.array(obs), np.array(act), np.array(rew), obs_tp1, np.array(done)
    
    def __len__(self):
        return len(self.buffer)
        
class PrioritizedReplayBuffer:
    """Prioritized Experience Replay Buffer"""
    def __init__(self, capacity, alpha=0.6, beta=0.4, beta_increment=0.001):
        self.capacity = capacity
        self.alpha = alpha
        self.beta = beta
        self.beta_increment = beta_increment
        self.buffer = []
        self.priorities = np.zeros(capacity)
        self.pos = 0
        self.max_priority = 1.0
        self.n_step_buffer = deque(maxlen=3)  # For n-step learning (n=3)
    
    def add(self, experience, n_step=3, gamma=0.99):
        """Add experience with n-step learning"""
        self.n_step_buffer.append(experience)
        
        if len(self.n_step_buffer) == n_step:
            state, action, _, _, _ = self.n_step_buffer[0]
            _, _, reward, next_state, done = self.n_step_buffer[-1]
            
            # Calculate n-step reward
            for i in range(1, n_step-1):
                r = self.n_step_buffer[i][2]
                reward = r + gamma * reward
            
            # Store n-step transition
            if len(self.buffer) < self.capacity:
                self.buffer.append(None)
            self.buffer[self.pos] = (state, action, reward, next_state, done)
            self.priorities[self.pos] = self.max_priority
            self.pos = (self.pos + 1) % self.capacity
    
    def sample(self, batch_size):
        if len(self.buffer) == 0:
            return [], [], []
        
        priorities = self.priorities[:len(self.buffer)]
        probs = priorities ** self.alpha
        probs /= probs.sum()
        
        indices = np.random.choice(len(self.buffer), batch_size, p=probs)
        samples = [self.buffer[i] for i in indices]
        
        weights = (len(self.buffer) * probs[indices]) ** (-self.beta)
        weights /= weights.max()
        self.beta = min(1.0, self.beta + self.beta_increment)
        
        return samples, indices, torch.FloatTensor(weights)
    
    def update_priorities(self, indices, priorities):
        for idx, priority in zip(indices, priorities):
            self.priorities[idx] = priority
            self.max_priority = max(self.max_priority, priority)
    
    def __len__(self):
        return len(self.buffer)