import pandas as pd
import requests
import re
from io import StringIO
from datetime import datetime
import time
try:
    from Bio import SeqIO
except Exception as e:
    SeqIO = None
    _bio_error = str(e)

from pathlib import Path
import subprocess

try:
    import config
except ImportError:
    pass

def _log_to_file(log_file_path, message):
    """Appends a timestamped message to the specified log file."""
    if log_file_path:
        with open(log_file_path, "a") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")

def validate_fragpipe_file(df):
    """
    Validates that the DataFrame has the required columns for the pipeline.
    Performs a case-insensitive check.
    """
    error_logs = []

    if df.empty:
        return False, ["❌ Validation Error: The uploaded file is empty or could not be read correctly."]

    # --- 1. Check for required columns (case-insensitive) ---
    REQUIRED_COLUMNS = [
        'peptide sequence',
        # Other columns are optional for the core pipeline
    ]
    df_columns_lower = [c.lower() for c in df.columns]
    missing_cols = [col for col in REQUIRED_COLUMNS if col not in df_columns_lower]
    
    if missing_cols:
        error_logs.append(f"❌ Schema Error: The file is missing required columns: **{', '.join(missing_cols)}**.")

    # --- 2. Flexibly check for at least one protein identifier column ---
    protein_col_found = any('protein' in col for col in df_columns_lower)
    if not protein_col_found:
        error_logs.append("❌ Schema Error: A 'Protein' or 'Protein ID' column is required but was not found.")

    # --- 3. Check for data integrity in 'Peptide Sequence' column ---
    peptide_col_name = next((col for col in df.columns if col.lower() == 'peptide sequence'), None)
    if peptide_col_name:
        # Check for empty/null values first
        null_rows = df[df[peptide_col_name].isnull()]
        if not null_rows.empty:
            first_error_row = null_rows.index[0] + 2 # +1 for 0-index, +1 for header
            error_logs.append(
                f"❌ Data Error: The '{peptide_col_name}' column has an empty cell at or near **row {first_error_row}**."
            )

        # Regex for standard 20 amino acids.
        # This finds any character that is NOT one of the 20 standard amino acids.
        invalid_char_regex = r'[^ACDEFGHIKLMNPQRSTVWYU]'
        
        sequences_to_check = df[peptide_col_name].dropna().astype(str)
        invalid_rows = sequences_to_check[sequences_to_check.str.contains(invalid_char_regex, na=False)]
        
        if not invalid_rows.empty:
            first_error_index = invalid_rows.index[0]
            first_error_row = first_error_index + 2 # +1 for 0-index, +1 for header
            problematic_value = invalid_rows.iloc[0]
            error_logs.append(
                f"❌ Data Error: Invalid character found in column '{peptide_col_name}' at or near **row {first_error_row}**. "
                f"Value **'{problematic_value}'** contains non-standard amino acid codes."
            )
            
        # Check if ALL sequences are too short
        if not sequences_to_check.empty and (sequences_to_check.str.strip().str.len() < 8).all():
            error_logs.append(f"❌ Data Error: All peptides in column '{peptide_col_name}' are shorter than 8 amino acids. The pipeline requires peptides >= 8 AAs.")

    # --- 4. Check other specific column data types ---
    # Amino Acid or '-' columns
    aa_cols_to_check = ['prev aa', 'next aa']
    aa_regex = r'[^ACDEFGHIKLMNPQRSTVWYU-]'
    for col_name_lower in aa_cols_to_check:
        col_name = next((c for c in df.columns if c.lower() == col_name_lower), None)
        if col_name:
            # Convert to string, handle potential float conversion from pandas (e.g., NaN)
            values_to_check = df[col_name].dropna().astype(str)
            invalid_rows = values_to_check[values_to_check.str.contains(aa_regex, na=False)]
            if not invalid_rows.empty:
                first_error_index = invalid_rows.index[0]
                first_error_row = first_error_index + 2
                problematic_value = invalid_rows.iloc[0]
                error_logs.append(
                    f"❌ Data Error: Invalid character in column '{col_name}' at or near **row {first_error_row}**. "
                    f"Value **'{problematic_value}'** should only be a standard amino acid or '-'."
                )

    # Numeric or '-' columns
    numeric_cols_to_check = ['start', 'end', 'peptide length', 'charges']
    numeric_regex = r'[^0-9.,: -]'
    for col_name_lower in numeric_cols_to_check:
        col_name = next((c for c in df.columns if c.lower() == col_name_lower), None)
        if col_name:
            values_to_check = df[col_name].dropna().astype(str)
            invalid_rows = values_to_check[values_to_check.str.contains(numeric_regex, na=False)]
            if not invalid_rows.empty:
                first_error_index = invalid_rows.index[0]
                first_error_row = first_error_index + 2
                problematic_value = invalid_rows.iloc[0]
                error_logs.append(
                    f"❌ Data Error: Invalid character in column '{col_name}' at or near **row {first_error_row}**. "
                    f"Value **'{problematic_value}'** should only be a number, '.', ',', ':', space, or '-'."
                )

    if error_logs:
        return False, error_logs
    else:
        return True, ["✅ File format validated successfully."]


def load_raw_dataframe(file_path):
    """
    Robustly loads a dataframe from TSV, CSV, or Excel, handling bad lines and engine versions.
    Returns the dataframe or raises an Exception.
    """
    csv_kwargs = {}
    try:
        pd_major, pd_minor = map(int, pd.__version__.split('.')[:2])
        if pd_major > 1 or (pd_major == 1 and pd_minor >= 3):
            csv_kwargs['on_bad_lines'] = 'skip'
        else:
            csv_kwargs['error_bad_lines'] = False
            csv_kwargs['warn_bad_lines'] = True
    except Exception:
        csv_kwargs['error_bad_lines'] = False

    if str(file_path).endswith('.tsv'):
        df = pd.read_csv(file_path, sep='\t', **csv_kwargs)
        # If parsing failed (e.g. wrong separator resulted in 1 column), try sniffing
        if df.shape[1] < 2:
            try:
                df = pd.read_csv(file_path, sep=None, engine='python', **csv_kwargs)
            except: pass
    elif str(file_path).endswith(('.xlsx', '.xls')):
        df = pd.read_excel(file_path)
    else:
        # Fallback for .csv files that might be named .tsv or vice-versa
        df = pd.read_csv(file_path, sep=None, engine='python', **csv_kwargs)
    
    return df

def load_and_filter_fragpipe(file_path, selected_keywords, log_file_path=None):
    """
    1. Loads FragPipe output (TSV or Excel).
    2. Filters rows where Protein ID contains ANY of the selected keywords.
    3. Fetches sequences from UniProt if not present.
    """
    log = []
    
    _log_to_file(log_file_path, f"--- Starting validation and filtering for {file_path.name} ---")
    # 1. Load Data
    try:
        df = load_raw_dataframe(file_path)

    except FileNotFoundError as e:
        msg = f"❌ File Load Error: The file '{file_path.name}' was not found."
        _log_to_file(log_file_path, msg)
        return None, [msg], {}
    except pd.errors.ParserError as e:
        msg = f"❌ File Format Error: Could not parse '{file_path.name}'. It may be corrupted or not a valid TSV/CSV."
        _log_to_file(log_file_path, f"{msg} Details: {e}")
        return None, [msg], {}
    except Exception as e:
        msg = f"❌ File Load Error: An unexpected error occurred reading '{file_path.name}'. Details: {e}"
        _log_to_file(log_file_path, f"{msg} Details: {e}")
        return None, [msg], {}

    # 2. Validate DataFrame structure
    is_valid, validation_log = validate_fragpipe_file(df)
    log.extend(validation_log)
    _log_to_file(log_file_path, validation_log[0])
    if not is_valid:
        return None, log, {}

    # Normalize columns to lowercase for safety AFTER validation
    df.columns = [c.lower().strip() for c in df.columns]
    
    # Fix for Streamlit Arrow serialization error: 'charges' column often contains "2, 3"
    if 'charges' in df.columns:
        df['charges'] = df['charges'].astype(str)

    # Filter out peptides shorter than 8 amino acids
    peptide_col_name = next((c for c in df.columns if 'peptide' in c and 'seq' in c), None)
    if peptide_col_name:
        n_before_len = len(df)
        df = df[df[peptide_col_name].astype(str).str.strip().str.len() >= 8].copy()
        n_after_len = len(df)
        if n_before_len > n_after_len:
            msg = f"ℹ️ Filtered out {n_before_len - n_after_len} peptides shorter than 8 amino acids."
            log.append(msg)
            _log_to_file(log_file_path, msg)
        
        if df.empty:
            msg = "❌ Error: No peptides remained after filtering out sequences shorter than 8 amino acids."
            log.append(msg)
            _log_to_file(log_file_path, msg)
            return None, log, {}

    # Identify the Protein Column
    # Prioritize exact 'protein' column, as it usually contains the full identifier with species tag.
    df_cols_lower = df.columns.tolist()
    if 'protein' in df_cols_lower:
        prot_col = 'protein'
    else:
        # Fallback to original broader search if 'protein' is not an exact match
        prot_col = next((c for c in df_cols_lower if 'protein' in c and 'prob' not in c), None)
    
    if not prot_col and not df.empty:
        msg = "❌ Error: Could not find a suitable 'Protein' or 'Protein ID' column in file."
        log.append(msg)
        _log_to_file(log_file_path, msg)
        return None, log, {}
    log.append(f"✅ Loaded {len(df)} rows from '{file_path.name}'. Using protein column: '{prot_col}'")

    # Extract all found species tags for reporting (before filtering)
    potential_species = set()
    meta_cols = [c for c in df.columns if any(x in c for x in ['protein', 'gene', 'entry', 'organism'])]
    for c in meta_cols:
        unique_vals = df[c].dropna().astype(str).unique()
        for val in unique_vals:
            matches = re.findall(r'_([A-Z0-9]+)(?:$|\||\s)', val.upper())
            if matches: potential_species.update(matches)
    found_species_tags = sorted(list(potential_species))

    # 2. Filter by Species
    # Logic: If user selects 'THEPA', keep rows containing 'THEPA'
    if selected_keywords:
        # Create a regex pattern: "THEPA|BOVIN|HUMAN"
        pattern = "|".join(selected_keywords)
        initial_count = len(df)
        
        mask = pd.Series(False, index=df.index)
        for c in meta_cols:
            df[c] = df[c].astype(str)
            mask = mask | df[c].str.contains(pattern, case=False, na=False)
            
        df_filtered = df[mask].copy()
        
        if df_filtered.empty:
            found_list = found_species_tags[:20] # Top 20 for display
            
            msg = f"❌ No proteins remained after filtering."
            msg += f"\n\n**Selected Filters:** {', '.join(selected_keywords)}"
            msg += f"\n\n**Found Species Tags in File:** {', '.join(found_list) if found_list else 'None detected'}"
            
            log.append(msg)
            _log_to_file(log_file_path, msg)
            return None, log, {"found_species_tags": found_species_tags}

        msg = f"🔍 Filtering for [{pattern}]: {initial_count} -> {len(df_filtered)} proteins remaining."
        log.append(msg)
        _log_to_file(log_file_path, msg)
        df = df_filtered
    else:
        msg = "⚠️ No species selected. Keeping all proteins."
        log.append(msg)
        _log_to_file(log_file_path, msg)

    if df.empty:
        msg = "❌ No proteins remained after filtering."
        log.append(msg)
        _log_to_file(log_file_path, msg)
        return None, log, {"found_species_tags": found_species_tags}

    # 3. Clean IDs and Prepare for Fetching
    # FragPipe format: "sp|Q9XYZ|NAME_SPECIES" -> We need "Q9XYZ"
    def extract_uniprot(row):
        # First try 'mapped proteins' if it exists (often contains true UniProt IDs for custom genome searches)
        if 'mapped proteins' in df.columns:
            mapped_val = str(row.get('mapped proteins', '')).split(',')[0].strip()
            if '|' in mapped_val:
                return mapped_val.split('|')[1]
                
        # Fallback to the main protein column
        s = str(row[prot_col])
        if "|" in s:
            parts = s.split("|")
            if len(parts) > 1:
                return parts[1] # The ID is usually the 2nd part
        return s

    df.loc[:, 'UniProtID'] = df.apply(extract_uniprot, axis=1)
    
    # Aggregate peptides before deduplication
    peptide_col = next((c for c in df.columns if 'peptide' in c and 'seq' in c), None)
    peptides_map = {}
    if peptide_col:
        peptides_map = df.groupby('UniProtID')[peptide_col].apply(lambda x: list(set(x))).to_dict()

    # Remove duplicates based on ID
    n_before = len(df)
    df = df.drop_duplicates(subset=['UniProtID']).copy()
    n_after = len(df)
    
    if peptides_map:
        df.loc[:, 'Initial_Peptides'] = df['UniProtID'].map(peptides_map)

    if n_before > n_after:
        msg = f"ℹ️ Consolidated {n_before} peptide rows into {n_after} unique proteins."
        log.append(msg)
        _log_to_file(log_file_path, msg)
    
    return df, log, {"n_peptides": n_before, "n_proteins": n_after, "found_species_tags": found_species_tags}

def fetch_sequences_batch(df, log_file_path=None, progress_callback=None):
    """
    Fetches missing sequences from UniProt for the dataframe.
    Returns the dataframe, logs, a cancellation boolean, and a list of failed IDs.    """
    if SeqIO is None:
        return df, [f"❌ Missing Dependency: 'biopython' failed to load. Error: {_bio_error}. Please run `pip install biopython`."], False, []

    if 'UniProtID' not in df.columns:
        return df, ["❌ No UniProtID column found."], False, []
    
    df = df.copy()
    batch_start_time = time.time()
    _log_to_file(log_file_path, f"--- Starting UniProt sequence fetch ---")
    ids_to_fetch = df['UniProtID'].unique().tolist()
    msg = f"🌐 Attempting to fetch sequences for {len(ids_to_fetch)} unique proteins..."
    log = [msg]
    _log_to_file(log_file_path, msg)
    sequences = {}
    
    success_count = 0
    fetched_count = 0
    total_bytes = 0
    total_time = 0
    was_cancelled = False
    failed_ids = []
    
    with requests.Session() as s:
        for i, uid in enumerate(ids_to_fetch):
            fetched_count = i + 1
            _log_to_file(log_file_path, f"Fetching {uid}...")
            start_time = time.time()
            try:
                r = s.get(f"https://www.uniprot.org/uniprot/{uid}.fasta", timeout=5)
                
                # Measure time and size for speed calculation
                end_time = time.time()
                total_time += (end_time - start_time)
                total_bytes += len(r.content) if r.content else 0

                if r.status_code == 200:
                    rec = SeqIO.read(StringIO(r.text), "fasta")
                    sequences[uid] = str(rec.seq)
                    success_count += 1
                else:
                    _log_to_file(log_file_path, f"Failed to fetch {uid}. Status: {r.status_code}")
                    failed_ids.append(uid)
            except requests.exceptions.RequestException as e:
                _log_to_file(log_file_path, f"Error fetching {uid}: {e}")
                failed_ids.append(uid)
            except Exception as e:
                _log_to_file(log_file_path, f"Error fetching {uid}: {e}")
                failed_ids.append(uid)
            
            # Call the callback function to update the UI
            if progress_callback:
                speed_kbs = (total_bytes / total_time) / 1024 if total_time > 0 else 0
                signal = progress_callback(fetched_count, speed_kbs, uid)
                if signal == "cancel":
                    was_cancelled = True
                    break # Exit the loop
    
    df['Sequence'] = df['UniProtID'].map(sequences)
    final_df = df.dropna(subset=['Sequence'])
    msg = f"✅ Successfully fetched {success_count} sequences."
    log.append(msg)
    _log_to_file(log_file_path, msg)

    if len(final_df) < len(df):
        msg = f"⚠️ Dropped {len(df) - len(final_df)} proteins due to fetch failure."
        log.append(msg)
        _log_to_file(log_file_path, msg)
    
    batch_elapsed = time.time() - batch_start_time
    _log_to_file(log_file_path, f"--- Sequence fetch complete. {len(final_df)} proteins have sequences. Elapsed: {batch_elapsed:.2f}s ---")
    return final_df, log, was_cancelled, failed_ids

def generate_peptide_fasta(file_path, output_path, selected_keywords=None):
    """
    Generates a peptide FASTA file from the FragPipe output.
    Reads the file, filters by species (if keywords provided), and writes >ProteinIDpepCount format.
    """
    try:
        # 1. Load Data (Robust load logic similar to load_and_filter_fragpipe)
        df = load_raw_dataframe(file_path)
        
        # 2. Normalize columns
        df.columns = [c.lower().strip() for c in df.columns]
        
        # 3. Identify Columns
        pep_col = next((c for c in df.columns if 'peptide' in c and 'seq' in c), None)
        
        # Prioritize exact 'protein' column, as it usually contains the full identifier with species tag.
        df_cols_lower = df.columns.tolist()
        if 'protein' in df_cols_lower:
            prot_col = 'protein'
        else:
            # Fallback to original broader search if 'protein' is not an exact match
            prot_col = next((c for c in df_cols_lower if 'protein' in c and 'prob' not in c), None)

        if not pep_col or not prot_col:
            return False, f"Required columns (Peptide Sequence, Protein) not found."

        # 4. Filter by Species (if keywords provided)
        if selected_keywords:
            pattern = "|".join(selected_keywords)
            
            initial_count = len(df)
            
            meta_cols = [c for c in df.columns if any(x in c for x in ['protein', 'gene', 'entry', 'organism'])]
            mask = pd.Series(False, index=df.index)
            for c in meta_cols:
                df[c] = df[c].astype(str)
                mask = mask | df[c].str.contains(pattern, case=False, na=False)
                
            df = df[mask].copy()
            if df.empty:
                return False, f"❌ Error: 0 of {initial_count} peptides remained after filtering for species '{', '.join(selected_keywords)}'. Please check if the 'Protein' column contains these keywords."

        # 5. Write FASTA
        protein_counts = {}
        written_count = 0
        seen_peptides = set()
        with open(output_path, 'w') as f:
            for _, row in df.iterrows():
                protein = str(row[prot_col]).strip()
                sequence = str(row[pep_col]).strip()
                
                if not protein or not sequence or protein.lower() == 'nan':
                    continue
                
                # Filter short sequences (NetMHCpan/IIpan require >= 8)
                if len(sequence) < 8:
                    continue
                
                # Deduplicate exact sequence matches for the same protein
                pep_hash = f"{protein}_{sequence}"
                if pep_hash in seen_peptides:
                    continue
                seen_peptides.add(pep_hash)
                
                count = protein_counts.get(protein, 0) + 1
                protein_counts[protein] = count
                
                f.write(f">{protein}pep{count}\n{sequence}\n")
                written_count += 1
                
        if written_count == 0 and not df.empty:
            return True, f"⚠️ Generated {output_path.name} with 0 peptides. All {len(df)} filtered peptides were duplicates or shorter than 8 amino acids."
        
        return True, f"✅ Generated {output_path.name} with {written_count} unique peptides (removed duplicates and sequences < 8 AA)."

    except Exception as e:
        return False, f"❌ Error generating FASTA: {e}"


class Experiment:
    def __init__(self, df, log_file_path=None):
        self.df = df
        self.log_file_path = log_file_path

    def log_msg(self, message):
        _log_to_file(self.log_file_path, message)

    # -------------------------------------------------------------------------
    # 3. RUN DEEPTMHMM (Cloud Mode - No Docker Login Required)
    # -------------------------------------------------------------------------
    def run_deeptmhmm(self):
        """
        Run DeepTMHMM using the Python 3.10 Sidecar Environment.
        """
        if self.df.empty: return False, "No data."

        # Prepare IO
        fasta_path = config.INTERMEDIATE_DIR / "phase1_tmhmm.fasta"
        output_dir = config.RESULTS_DIR / "deeptmhmm"
        output_dir.mkdir(exist_ok=True, parents=True)
        
        # Write FASTA
        with open(fasta_path, "w") as f:
            for _, row in self.df.iterrows():
                f.write(f">{row['UniProtID']}\n{row['Sequence']}\n")
        
        self.log_msg("Launching DeepTMHMM Sidecar Process...")

        # --- NEW SUBPROCESS LOGIC ---
        import subprocess
        
        cmd = [
            str(config.TMHMM_PYTHON),            # Use the Python 3.10 env
            str(config.TMHMM_STANDALONE_SCRIPT), # Run the standalone script
            "--fasta", str(fasta_path),
            "--outdir", str(output_dir)
        ]
        
        try:
            # Run the sidecar script
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                self.log_msg(f"Sidecar Error Output: {result.stderr}")
                return False, f"DeepTMHMM Failed: {result.stdout} \n {result.stderr}"

            # --- PARSING RESULTS (Standard Logic) ---
            valid_ids = set()
            gff_files = list(output_dir.glob("*.gff3"))
            
            if not gff_files:
                return False, "DeepTMHMM finished but generated no GFF3 file."
                
            for gff in gff_files:
                with open(gff, 'r') as f:
                    for line in f:
                        if line.startswith("#"): continue
                        parts = line.split('\t')
                        if len(parts) < 3: continue
                        if "Signal" in parts[2] or "extracellular" in parts[2].lower():
                            valid_ids.add(parts[0])
            
            # Apply Filter
            before_count = len(self.df)
            self.df = self.df[self.df['UniProtID'].isin(valid_ids)]
            self.df['localization'] = "Surface/Secreted"
            
            return True, f"DeepTMHMM: Kept {len(self.df)} of {before_count} proteins."

        except Exception as e:
            return False, f"DeepTMHMM Error: {e}"

    # -------------------------------------------------------------------------
    # 4. RUN VAXIGN-ML (Local Docker - Fast & Private)
    # -------------------------------------------------------------------------
    def run_vaxign(self, organism="bacteria"):
        if self.df.empty: return False, "No data."

        fasta_path = config.INTERMEDIATE_DIR / "phase1_vaxign.fasta"
        output_dir = config.RESULTS_DIR / "vaxign"
        output_dir.mkdir(exist_ok=True, parents=True)
        
        with open(fasta_path, "w") as f:
            for _, row in self.df.iterrows():
                f.write(f">{row['UniProtID']}\n{row['Sequence']}\n")

        self.log_msg(f"Running Vaxign-ML locally (Docker Image: teambbb/vaxign-ml)...")
        
        try:
            # Docker needs absolute paths
            abs_input = fasta_path.resolve()
            abs_output = output_dir.resolve()
            
            # Direct Docker Command
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{abs_input.parent}:/data",
                "-v", f"{abs_output}:/out",
                "teambbb/vaxign-ml",
                "--input", f"/data/{abs_input.name}",
                "--output", "/out",
                "--species", organism
            ]
            
            subprocess.run(cmd, check=True)
            return True, "Vaxign-ML completed."

        except Exception as e:
            return False, f"Vaxign Failed: {e}"