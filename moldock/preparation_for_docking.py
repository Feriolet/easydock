import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from multiprocessing import Pool, Manager, cpu_count


from meeko import MoleculePreparation
from meeko import obutils
from openbabel import openbabel as ob
from rdkit import Chem
from rdkit.Chem import AllChem
from moldock import read_input
# from read_input import read_input


def cpu_type(x):
    return max(1, min(int(x), cpu_count()))


def filepath_type(x):
    if x:
        return os.path.abspath(x)
    else:
        return x


def mol_is_3d(mol):
    if mol.GetConformers() and list(mol.GetConformers())[0].Is3D():
        return True
    return False


def mol_from_smi_or_molblock(ligand_string):
    mol = Chem.MolFromMolBlock(ligand_string)
    if mol is None:
        mol = Chem.MolFromSmiles(ligand_string)
    return mol


def add_protonation(db_fname):
    '''
    Protonate SMILES by Chemaxon cxcalc utility to get molecule ionization states at pH 7.4.
    Parse console output and update db.
    :param db_fname:
    :return:
    '''
    conn = sqlite3.connect(db_fname)

    try:
        cur = conn.cursor()
        protonate, done = list(cur.execute('SELECT protonation, protonation_done FROM setup'))[0]

        if protonate and not done:
            data_list = list(cur.execute('SELECT smi, source_mol_block, id FROM mols'))
            if not data_list:
                sys.stderr.write(f'no molecules to protonate')
                return

            smi_ids = []
            mol_ids = []
            for i, (smi, mol_block, mol_name) in enumerate(data_list):
                if mol_block is None:
                    smi_ids.append(mol_name)
                    # add missing mol blocks
                    m = Chem.MolFromSmiles(data_list[i][0])
                    m.SetProp('_Name', mol_name)
                    m_block = Chem.MolToMolBlock(m)
                    data_list[i] = (data_list[i][0],) + (m_block,) + (data_list[i][2],)
                else:
                    mol_ids.append(mol_name)
            smi_ids = set(smi_ids)
            mol_ids = set(mol_ids)

            output_data_smi = []
            output_data_mol = []
            with tempfile.NamedTemporaryFile(suffix='.sdf', mode='w', encoding='utf-8') as tmp:
                fd, output = tempfile.mkstemp()  # use output file to avoid overflow of stdout is extreme cases
                try:
                    for _, mol_block, _ in data_list:
                        tmp.write(mol_block)
                        tmp.write('\n$$$$\n')
                    tmp.flush()
                    cmd_run = f"cxcalc -S majormicrospecies -H 7.4 -M -K '{tmp.name}' > '{output}'"
                    subprocess.call(cmd_run, shell=True)
                    sdf_protonated = Chem.SDMolSupplier(output)
                    for mol in sdf_protonated:
                        mol_name = mol.GetProp('_Name')
                        smi = mol.GetPropsAsDict().get('MAJORMS', None)
                        if smi is not None:
                            cansmi = Chem.CanonSmiles(smi)
                            if mol_name in smi_ids:
                                output_data_smi.append((cansmi, mol_name))
                            elif mol_name in mol_ids:
                                output_data_mol.append((cansmi, Chem.MolToMolBlock(mol), mol_name))
                finally:
                    os.remove(output)

            cur.executemany(f"""UPDATE mols
                           SET 
                               smi_protonated = ?
                           WHERE
                               id = ?
                        """, output_data_smi)
            cur.executemany(f"""UPDATE mols
                           SET 
                               smi_protonated = ?, 
                               source_mol_block_protonated = ?
                           WHERE
                               id = ?
                        """, output_data_mol)
            conn.commit()

            cur.execute('UPDATE setup SET protonation_done = 1')
            conn.commit()

    finally:
        conn.close()


def mk_prepare_ligand_string(molecule_string, build_macrocycle=True, add_water=False, merge_hydrogen=True,
                             add_hydrogen=False, pH_value=None, verbose=False, mol_format='SDF'):

    mol = obutils.load_molecule_from_string(molecule_string, molecule_format=mol_format)

    if pH_value is not None:
        mol.CorrectForPH(float(pH_value))

    if add_hydrogen:
        mol.AddHydrogens()
        charge_model = ob.OBChargeModel.FindType("Gasteiger")
        charge_model.ComputeCharges(mol)

    m = Chem.MolFromMolBlock(molecule_string)
    amide_rigid = len(m.GetSubstructMatch(Chem.MolFromSmarts('[C;!R](=O)[#7]([!#1])[!#1]'))) == 0

    preparator = MoleculePreparation(merge_hydrogens=merge_hydrogen, macrocycle=build_macrocycle,
                                     hydrate=add_water, amide_rigid=amide_rigid)
                                     #additional parametrs
                                     #rigidify_bonds_smarts=[], rigidify_bonds_indices=[])
    try:
        preparator.prepare(mol)
    except Exception:
        sys.stderr.write('Warning. Incorrect mol object to convert to pdbqt. Continue. \n')
        traceback.print_exc()
        return None
    if verbose:
        preparator.show_setup()

    return preparator.write_pdbqt_string()


def ligand_preparation(ligand_string, seed=0):
    """
    If input ligand is not a 3D structure a conformer will be generated by RDKit, otherwise the provided 3D structure
    will be used.
    :param ligand_string: SMILES or mol block
    :param seed:
    :return: PDBQT block
    """

    def convert2mol(m):

        def gen_conf(mol, useRandomCoords, randomSeed):
            params = AllChem.ETKDGv3()
            params.useRandomCoords = useRandomCoords
            params.randomSeed = randomSeed
            conf_stat = AllChem.EmbedMolecule(mol, params)
            return mol, conf_stat

        if not m:
            return None
        is3d = True if mol_is_3d(m) else False
        m = Chem.AddHs(m, addCoords=True)
        if not is3d:  # only for non 3D input structures
            m, conf_stat = gen_conf(m, useRandomCoords=False, randomSeed=seed)
            if conf_stat == -1:
                # if molecule is big enough and rdkit cannot generate a conformation - use params.useRandomCoords = True
                m, conf_stat = gen_conf(m, useRandomCoords=True, randomSeed=seed)
                if conf_stat == -1:
                    return None
            AllChem.UFFOptimizeMolecule(m, maxIters=100)
        # checking for the presence of boron in the molecule
        idx_boron = [idx for idx, atom in enumerate(m.GetAtoms()) if atom.GetAtomicNum() == 5]
        for id_ in idx_boron:
            m.GetAtomWithIdx(id_).SetAtomicNum(6)
            # m.UpdatePropertyCache() # uncomment if necessary
        return Chem.MolToMolBlock(m)

    try:
        mol = mol_from_smi_or_molblock(ligand_string)
        mol_conf_mol_block = convert2mol(mol)
    except TypeError:
        sys.stderr.write(f'incorrect SMILES {ligand_string} for converting to molecule\n')
        return None

    if mol_conf_mol_block is None:
        return None
    mol_conf_pdbqt = mk_prepare_ligand_string(mol_conf_mol_block,
                                              build_macrocycle=False,
                                              # can do it True, but there is some problem with >=7-chains mols
                                              add_water=False, merge_hydrogen=True, add_hydrogen=False,
                                              # pH_value=7.4, can use this opt but some results are different in comparison to chemaxon
                                              verbose=False, mol_format='SDF')
    return mol_conf_pdbqt


def fix_pdbqt(pdbqt_block):
    pdbqt_fixed = []
    for line in pdbqt_block.split('\n'):
        if not line.startswith('HETATM') and not line.startswith('ATOM'):
            pdbqt_fixed.append(line)
            continue
        atom_type = line[12:16].strip()
        # autodock vina types
        if 'CA' in line[77:79]: #Calcium is exception
            atom_pdbqt_type = 'CA'
        else:
            atom_pdbqt_type = re.sub('D|A', '', line[77:79]).strip() # can add meeko macrocycle types (G and \d (CG0 etc) in the sub expression if will be going to use it

        if re.search('\d', atom_type[0]) or len(atom_pdbqt_type) == 2: #1HG or two-letter atom names such as CL,FE starts with 13
            atom_format_type = '{:<4s}'.format(atom_type)
        else: # starts with 14
            atom_format_type = ' {:<3s}'.format(atom_type)
        line = line[:12] + atom_format_type + line[16:]
        pdbqt_fixed.append(line)

    return '\n'.join(pdbqt_fixed)


def assign_bonds_from_template(template_mol, mol):
    # explicit hydrogends are removed from carbon atoms (chiral hydrogens) to match pdbqt mol,
    # e.g. [NH3+][C@H](C)C(=O)[O-]
    template_mol = Chem.AddHs(template_mol, explicitOnly=True,
                              onlyOnAtoms=[a.GetIdx() for a in template_mol.GetAtoms() if
                                           a.GetAtomicNum() != 6])
    mol = AllChem.AssignBondOrdersFromTemplate(template_mol, mol)
    Chem.SanitizeMol(mol)
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True, flagPossibleStereoCenters=True)
    return mol


def boron_reduction(mol_B, mol):
    idx_boron = [idx for idx, atom in enumerate(mol_B.GetAtoms()) if atom.GetAtomicNum() == 5]
    for id_ in idx_boron:
        mol_B.GetAtomWithIdx(id_).SetAtomicNum(6)
    mol = assign_bonds_from_template(mol_B, mol)
    idx = mol.GetSubstructMatches(mol_B)
    mol_idx_boron = [tuple(sorted(ids[i] for i in idx_boron)) for ids in idx]
    mol_idx_boron = list(set(mol_idx_boron)) # retrieve all ids matched possible boron atom positions
    if len(mol_idx_boron) == 1: # check whether this set of ids is unique
        for i in mol_idx_boron[0]:
            mol.GetAtomWithIdx(i).SetAtomicNum(5)
    else: #if not - several equivalent mappings exist
        sys.stderr.write('different mappings was detected. The structure cannot be recostructed automatically.')
        return None
    return mol


def pdbqt2molblock(pdbqt_block, template_mol, mol_id):
    """

    :param pdbqt_block: a single string with a single PDBQT block (a single pose)
    :param template_mol: Mol of a reference structure to assign bond orders
    :param mol_id: name of a molecule which will be added as a title in the output MOL block
    :return: a single string with a MOL block, if conversion failed returns None
    """
    mol_block = None
    fixed = False
    while mol_block is None:
        mol = Chem.MolFromPDBBlock('\n'.join([i[:66] for i in pdbqt_block.split('\n')]), removeHs=False, sanitize=False)
        try:
            if 5 in [atom.GetAtomicNum() for atom in template_mol.GetAtoms()]:
                mol = boron_reduction(template_mol, mol)
            else:
                mol = assign_bonds_from_template(template_mol, mol)
            mol.SetProp('_Name', mol_id)
            mol_block = Chem.MolToMolBlock(mol)
        except Exception:
            if fixed:  # if a molecule was already fixed and the error persists - simply break and return None
                sys.stderr.write(f'Parsing PDB was failed (fixing did not help): {mol_id}\n')
                break
            sys.stderr.write(f'Could not assign bond orders while parsing PDB: {mol_id}. Trying to fix.\n')
            pdbqt_block = fix_pdbqt(pdbqt_block)
            fixed = True
    return mol_block


def create_db(db_fname, input_fname, protonation, pdbqt_fname, protein_setup_fname, prefix):
    conn = sqlite3.connect(db_fname)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS mols
            (
             id TEXT PRIMARY KEY,
             smi TEXT,
             smi_protonated TEXT,
             source_mol_block TEXT,
             source_mol_block_protonated TEXT,
             docking_score REAL,
             pdb_block TEXT,
             mol_block TEXT,
             time TEXT
            )""")
    conn.commit()
    data_smi = []  # non 3D structures
    data_mol = []  # 3D structures
    for mol, mol_name in read_input.read_input(input_fname):
        smi = Chem.MolToSmiles(mol, isomericSmiles=True)
        if prefix:
            mol_name = f'{prefix}-{mol_name}'
        if mol_is_3d(mol):
            data_mol.append((mol_name, smi, Chem.MolToMolBlock(mol)))
        else:
            data_smi.append((mol_name, smi))
    cur.executemany(f'INSERT INTO mols (id, smi) VALUES(?, ?)', data_smi)
    cur.executemany(f'INSERT INTO mols (id, smi, source_mol_block) VALUES(?, ?, ?)', data_mol)
    conn.commit()

    cur.execute("""CREATE TABLE IF NOT EXISTS setup
            (
             protonation INTEGER,
             protonation_done INTEGER DEFAULT 0,
             protein_pdbqt TEXT,
             protein_setup TEXT
            )""")
    conn.commit()
    pdbqt_string = open(pdbqt_fname).read()
    setup_string = open(protein_setup_fname).read()
    cur.execute('INSERT INTO setup VALUES (?,?,?,?)', (int(protonation), 0, pdbqt_string, setup_string))
    conn.commit()

    conn.close()


def save_sdf(db_fname):
    sdf_fname = os.path.splitext(db_fname)[0] + '.sdf'
    with open(sdf_fname, 'wt') as w:
        conn = sqlite3.connect(db_fname)
        cur = conn.cursor()
        for mol_block, mol_name, score in cur.execute('SELECT mol_block, id, docking_score '
                                                      'FROM mols '
                                                      'WHERE docking_score IS NOT NULL '
                                                      'AND mol_block IS NOT NULL'):
            w.write(mol_block + '\n')
            w.write(f'>  <ID>\n{mol_name}\n\n')
            w.write(f'>  <docking_score>\n{score}\n\n')
            w.write('$$$$\n')
        sys.stderr.write(f'Best poses were saved to {sdf_fname}\n')
