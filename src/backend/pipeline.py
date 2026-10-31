from pathlib import Path
from typing import List, Optional
import subprocess
import shutil
import math
import os
from concurrent.futures import ThreadPoolExecutor
import time
import json
from Bio import SeqIO
import shlex
import datetime
import config

from .workers import netmhc_worker, gibbs_worker, vaxign_worker, algpred_worker, toxinpred_worker, il2pred_worker, ifnepitope_worker
from . import parsers

# NOTE: These are placeholder functions. You will replace the logic inside them
# with your actual tool execution code (e.g., using subprocess.run).

def _log(file_path: Path, message: str):
    """Writes a timestamped message to a log file."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}\n"
    print(message)  # Also print to stdout
    if file_path:
        with open(file_path, "a") as f:
            f.write(log_entry)

def clean_fasta_by_length(input_path: Path, output_path: Path, min_length: int = 8) -> int:
    """
    Filters sequences in a FASTA file by length.
    Returns the number of sequences kept.
    """
    kept_count = 0
    try:
        with open(input_path, 'r') as f_in, open(output_path, 'w') as f_out:
            header = None
            seq = []
            
            def process_entry(h, s):
                nonlocal kept_count
                s_str = "".join(s).strip()
                if len(s_str) >= min_length:
                    f_out.write(f">{h}\n{s_str}\n")
                    kept_count += 1
            for line in f_in:
                line = line.strip()
                if not line: continue
                if line.startswith(">"):
                    if header: process_entry(header, seq)
                    header = line[1:]
                    seq = []
                else: seq.append(line)
            if header: process_entry(header, seq)
        return kept_count
    except Exception as e:
        print(f"Error cleaning FASTA: {e}")
        return 0

def prepare_mhc_pep_file(input_path: Path, output_path: Path, max_length: int, min_length: int = 7) -> int:
    """
    Reads a FASTA file and generates a PEPTIDE file format for NetMHC tools.
    - If sequence length <= max_length: keeps as is.
    - If sequence length > max_length: splits into sliding windows of max_length.
    Format: <Peptide Sequence> \t <Protein ID>
    """
    kept_count = 0
    try:
        with open(input_path, 'r') as f_in, open(output_path, 'w') as f_out:
            header = None
            seq = []
            
            def process_entry(h, s):
                nonlocal kept_count
                s_str = "".join(s).strip().replace("*", "").upper()
                L = len(s_str)
                if L < min_length: return
                
                if L <= max_length:
                    f_out.write(f"{s_str}\t{h}\n")
                    kept_count += 1
                else:
                    for i in range(L - max_length + 1):
                        window = s_str[i:i + max_length]
                        f_out.write(f"{window}\t{h}\n")
                        kept_count += 1

            for line in f_in:
                line = line.strip()
                if not line: continue
                if line.startswith(">"):
                    if header: process_entry(header, seq)
                    header = line[1:].split()[0]  # Use first word as ID to prevent parsing errors
                    seq = []
                else: seq.append(line)
            if header: process_entry(header, seq)
        return kept_count
    except Exception as e:
        print(f"Error preparing peptide file: {e}")
        return 0

def process_fragpipe_and_fetch_fasta(
    uploaded_file: Path, species_keyword: str
) -> bool:
    print(f"BACKEND: Processing {uploaded_file.name} for species '{species_keyword}'")
    # TODO: Implement FragPipe filtering and UniProt FASTA fetching logic
    return True

def run_deeptmhmm(fasta_file: Path, num_jobs: int = 4) -> bool:
    """
    Runs DeepTMHMM in parallel by splitting the input FASTA file into chunks.

    Args:
        fasta_file: Path to the input FASTA file.
        num_jobs: Number of parallel processes to run. Defaults to 4.
    """
    log_file = fasta_file.parent / "deeptmhmm_pipeline.log"
    start_time = time.time()
    _log(log_file, f"BACKEND: Starting DeepTMHMM process for {fasta_file.name}")

    # Check if biolib python module is available
    try:
        import biolib
    except (ImportError, SyntaxError) as e:
        _log(log_file, f"ERROR: Failed to import 'biolib'. If using Python 3.7, try: pip install pybiolib==1.1.4")
        _log(log_file, f"Details: {e}")
        return False

    # 1. Check for biolib executable
    if not shutil.which("biolib"):
        _log(log_file, "ERROR: 'biolib' command not found. Please ensure it is installed and in your system's PATH.")
        return False

    # 2. Read all protein sequences from the input file
    try:
        records = list(SeqIO.parse(fasta_file, "fasta"))
        if not records:
            _log(log_file, "INFO: No sequences found in the input file for DeepTMHMM. Skipping.")
            return True
    except Exception as e:
        _log(log_file, f"ERROR: Could not read or parse the FASTA file {fasta_file}: {e}")
        return False

    # 3. Split records into chunks for parallel processing
    total_records = len(records)
    # Use at most the number of available CPUs, but not more jobs than records
    num_jobs = min(num_jobs, os.cpu_count() or 1, total_records)
    chunk_size = math.ceil(total_records / num_jobs)
    record_chunks = [records[i:i + chunk_size] for i in range(0, total_records, chunk_size)]

    # Define the progress file path
    progress_file = fasta_file.parent / "deeptmhmm_progress.json"

    def update_progress(completed, total, message):
        """Writes progress to a JSON file."""
        with open(progress_file, 'w') as f:
            json.dump({
                "completed": completed,
                "total": total,
                "message": message
            }, f)

    update_progress(0, total_records, "Initializing DeepTMHMM jobs...")

    # 4. Define a function to run one chunk
    def run_chunk(chunk_records: List, chunk_index: int) -> str:
        """Runs DeepTMHMM on a single chunk of sequences."""
        base_dir = fasta_file.parent
        chunk_fasta_path = base_dir / f"deeptmhmm_chunk_{chunk_index}.fasta"
        output_dir = base_dir / f"deeptmhmm_results_chunk_{chunk_index}"
        cancel_flag = base_dir / "deeptmhmm.cancel"
        
        SeqIO.write(chunk_records, chunk_fasta_path, "fasta")
        output_dir.mkdir(exist_ok=True)

        _log(log_file, f"  -> Running chunk {chunk_index+1}/{num_jobs} with {len(chunk_records)} proteins...")
        
        try:
            if cancel_flag.exists():
                _log(log_file, f"  -> Cancellation signal found before starting chunk {chunk_index+1}. Skipping.")
                return "cancelled"

            _log(log_file, f"  -> Submitting chunk {chunk_index+1} to BioLib Cloud...")

            # Load DeepTMHMM from BioLib
            import biolib
            deeptmhmm = biolib.load('DTU/DeepTMHMM')

            # Run the job on the cloud
            # BioLib handles uploading the local file specified in args
            job = deeptmhmm.cli(args=f"--fasta {chunk_fasta_path} --outdir .")

            # Wait for the job to finish
            job.wait()

            if job.status != "succeeded":
                _log(log_file, f"ERROR: BioLib Cloud job for chunk {chunk_index+1} failed.")
                return "failed"

            # Download results
            job.save_files(str(output_dir))

            _log(log_file, f"  -> Chunk {chunk_index+1} completed successfully.")
            return "success"
        except Exception as e:
            _log(log_file, f"ERROR: DeepTMHMM chunk {chunk_index+1} failed. Details: {e}")
            return "failed"

    # 5. Execute all chunks in parallel using a thread pool
    completed_count = 0
    all_results = []
    with ThreadPoolExecutor(max_workers=num_jobs) as executor:
        futures = [executor.submit(run_chunk, chunk, i) for i, chunk in enumerate(record_chunks)]
        
        # Use as_completed to get results as they finish for progress reporting
        from concurrent.futures import as_completed
        for future in as_completed(futures):
            # Find which chunk this future corresponds to
            chunk_index = futures.index(future)
            chunk_size = len(record_chunks[chunk_index])
            completed_count += chunk_size
            
            result = future.result()
            all_results.append(result)
            update_progress(completed_count, total_records, f"Completed chunk {chunk_index + 1}/{num_jobs}...")

    # Clean up cancellation flag if it exists
    cancel_flag_path = fasta_file.parent / "deeptmhmm.cancel"
    if cancel_flag_path.exists():
        cancel_flag_path.unlink()

    # Check if any job was cancelled or failed
    if "cancelled" in all_results:
        _log(log_file, "❌ BACKEND: DeepTMHMM process was cancelled.")
        return False
    
    elapsed = time.time() - start_time
    if "failed" in all_results:
        _log(log_file, "❌ BACKEND: One or more DeepTMHMM jobs failed.")
        return False
    else:
        _log(log_file, f"✅ BACKEND: All DeepTMHMM jobs completed successfully. Elapsed: {elapsed:.2f}s")
        return True

def run_mhc(
    fasta_file: Path, tool: str, species: str, alleles: List[str], max_jobs: int = 16, log_file: Path = None, length: int = 8
) -> bool:
    print(f"BACKEND: Running {tool} for {species} with alleles {alleles} on {fasta_file.name}")
    
    if "netmhc" in tool.lower():
        # Prepare output directory
        if fasta_file.parent.name == tool.lower():
            output_dir = fasta_file.parent
        else:
            output_dir = fasta_file.parent / tool.lower()
            output_dir.mkdir(parents=True, exist_ok=True)

        if log_file is None:
            log_file = output_dir / "netmhc.log"
        
        original_stem = fasta_file.stem

        # Determine which tool to use
        if "ii" in tool.lower():
            # NetMHCIIpan
            tool_bin = config.NETMHCIIPAN_BIN
            tool_home = config.NETMHCIIPAN_DIR

            # NetMHCIIpan expects allele format like 'DRB1_0101', not 'DRB1*01:01'.
            # This function converts the user-friendly format to the tool's expected format.
            def format_mhc2_allele(a: str) -> str:
                if a.startswith("H-2-"): # Keep mouse alleles as is
                    return a
                if "DQ" in a or "DP" in a: # DP/DQ must be passed as full heterodimers without stripping HLA-
                    return a
                # Convert DRB1*01:01 -> DRB1_0101 or HLA-DRB1*01:01 -> DRB1_0101
                return a.replace("HLA-", "").replace("*", "_").replace(":", "")

            alleles_str = ",".join([format_mhc2_allele(a) for a in alleles])
        else:
            # NetMHCpan (Class I)
            tool_bin = config.NETMHCPAN_BIN
            tool_home = config.NETMHCPAN_DIR
            
            # For NetMHCpan, the allele format is generally correct as is.
            alleles_str = ",".join(alleles)

        min_len = 9 if "ii" in tool.lower() else 8

        # Prepare PEPTIDE file instead of just cleaning FASTA
        _log(log_file, f"✂️ Preparing peptide file (Max length {length}, Min length {min_len})...")
        pep_file = output_dir / f"{fasta_file.stem}_pep.txt"
        
        # Prepare peptides: keep if <= length, chop into windows if > length
        kept_count = prepare_mhc_pep_file(fasta_file, pep_file, max_length=length, min_length=min_len)
        if kept_count == 0:
            _log(log_file, f"❌ Error: No valid peptides generated (all < {min_len} AA).")
            return False
        
        _log(log_file, f"✅ Prepared {kept_count} peptides for exact evaluation.")

        # Run Worker
        success = netmhc_worker.run(
            fasta_path=pep_file,
            output_dir=output_dir,
            alleles=alleles_str,
            length=length,
            tool_path=tool_bin,
            tool_home=tool_home,
            max_jobs=max_jobs,
            log_file=log_file
        )
        
        if success:
            # Worker output is based on fasta_path.stem (which is ..._pep)
            worker_out = output_dir / f"{pep_file.stem}.txt"
            final_out = output_dir / f"{original_stem}.txt"
            
            if worker_out.exists():
                shutil.move(worker_out, final_out)
            
            return process_netmhc_results(output_dir, final_out)
    
    return False

def process_netmhc_results(output_dir: Path, txt_file: Path = None) -> bool:
    """Parses the raw output txt into JSON."""
    if txt_file is None:
        txt_file = output_dir / "netmhc_out.txt"
        json_out = output_dir / "netmhc_results.json"
    else:
        json_out = output_dir / f"{txt_file.stem}.json"

        stats_out = output_dir / "netmhc_stats.json"

    if txt_file.exists():
        results = parsers.parse_netmhc_output(txt_file)
        # Save parsed JSON
        with open(json_out, 'w') as f:
            json.dump(results, f, indent=4)
            
            # Generate Stats safely
            unique_peptides, alleles, sb_peps, wb_peps = set(), set(), set(), set()
            sb_evts, wb_evts = 0, 0
            for item in results:
                pep, alll, lvl = item.get('peptide'), item.get('allele'), item.get('level')
                if pep:
                    unique_peptides.add(pep)
                    if lvl == 'SB': 
                        sb_peps.add(pep)
                        sb_evts += 1
                    elif lvl == 'WB': 
                        wb_peps.add(pep)
                        wb_evts += 1
                if alll: alleles.add(alll)
                    
            n_peptides = len(unique_peptides)
            stats_data = {
                "n_peptides": n_peptides, "n_alleles": len(alleles), "n_sb": len(sb_peps),
                "n_wb": len(wb_peps - sb_peps), "n_nb": n_peptides - len(sb_peps | wb_peps),
                "n_sb_events": sb_evts, "n_wb_events": wb_evts
            }
            with open(stats_out, "w") as f:
                json.dump(stats_data, f, indent=4)
                
        return True
    return False

def run_gibbscluster(peptide_file: Path, output_dir: Path, clusters: int = 5, motif_len: int = 9) -> bool:
    print(f"BACKEND: Running GibbsCluster on {peptide_file.name}")
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "gibbscluster.log"
    _log(log_file, f"Starting GibbsCluster: clusters={clusters}, motif_len={motif_len}")
    
    success = gibbs_worker.run(
        peptide_file=peptide_file,
        output_dir=output_dir,
        clusters=clusters,
        motif_len=motif_len,
        jobs=4, # Default to 4 threads
        log_file=log_file
    )
    _log(log_file, f"GibbsCluster finished. Success: {success}")
    return success

def run_vaxign_ml(fasta_file: Path, organism: str, sudo_password: str = None, progress_file: Path = None) -> bool:
    print(f"BACKEND: Running Vaxign-ML for {organism} on {fasta_file.name}")
    
    output_dir = fasta_file.parent / "vaxign"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "vaxign.log"
    _log(log_file, f"Starting Vaxign-ML: organism={organism}")
    
    def update_progress(pct, msg):
        if progress_file:
            try:
                with open(progress_file, 'w') as f:
                    json.dump({"progress": pct, "message": msg}, f)
            except:
                pass

    success = vaxign_worker.run(
        fasta_path=fasta_file,
        output_dir=output_dir,
        organism_type=organism,
        sudo_password=sudo_password,
        progress_callback=update_progress,
        log_file=log_file
    )
    _log(log_file, f"Vaxign-ML finished. Success: {success}")
    return success

def run_algpred(fasta_file: Path, threshold: float = 0.3, model: int = 2) -> bool:
    print(f"BACKEND: Running AlgPred 2.0 on {fasta_file.name}")
    output_dir = fasta_file.parent / "algpred"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "algpred.log"
    _log(log_file, f"Starting AlgPred 2.0: threshold={threshold}, model={model}")
    
    success = algpred_worker.run(
        fasta_path=fasta_file,
        output_dir=output_dir,
        threshold=threshold,
        model=model,
        log_file=log_file
    )
    _log(log_file, f"AlgPred finished. Success: {success}")
    return success

def run_toxinpred(fasta_file: Path, threshold: float = 0.6, model: int = 1) -> bool:
    print(f"BACKEND: Running ToxinPred on {fasta_file.name} with threshold {threshold}")
    output_dir = fasta_file.parent / "toxinpred"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "toxinpred.log"
    _log(log_file, f"Starting ToxinPred 2.0: threshold={threshold}, model={model}")
    
    success = toxinpred_worker.run(
        fasta_path=fasta_file,
        output_dir=output_dir,
        threshold=threshold,
        model=model,
        log_file=log_file
    )
    _log(log_file, f"ToxinPred finished. Success: {success}")
    return success

def run_il2pred(fasta_file: Path, threshold: float = 0.38, job: int = 1, model: int = 1) -> bool:
    print(f"BACKEND: Running IL2Pred on {fasta_file.name}")
    output_dir = fasta_file.parent / "il2pred"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "il2pred.log"
    _log(log_file, f"Starting IL2Pred: threshold={threshold}, job={job}, model={model}")
    
    success = il2pred_worker.run(
        fasta_path=fasta_file,
        output_dir=output_dir,
        threshold=threshold,
        job=job,
        model=model,
        log_file=log_file
    )
    _log(log_file, f"IL2Pred finished. Success: {success}")
    return success

def run_ifnepitope(fasta_file: Path, threshold: float = 0.49, host: int = 1, job: int = 1, window: int = 8) -> bool:
    print(f"BACKEND: Running IFNEpitope on {fasta_file.name}")
    output_dir = fasta_file.parent / "ifnepitope"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "ifnepitope.log"
    _log(log_file, f"Starting IFNEpitope: threshold={threshold}, host={host}, job={job}, window={window}")
    
    success = ifnepitope_worker.run(
        fasta_path=fasta_file,
        output_dir=output_dir,
        threshold=threshold,
        host=host,
        job=job,
        window=window,
        log_file=log_file
    )
    _log(log_file, f"IFNEpitope finished. Success: {success}")
    return success

def run_cytokine_tool(fasta_file: Path, tool_name: str) -> bool:
    print(f"BACKEND: Running {tool_name} on {fasta_file.name}")
    # TODO: Implement IL4Pred2/IFNEpitope execution logic
    return True

def generate_report(csv_files: List[Path]) -> bool:
    print(f"BACKEND: Generating report from {len(csv_files)} CSVs.")
    # TODO: Implement HTML report generation logic
    return True