import subprocess
import os
from pathlib import Path
import config
import sys
import shutil
import re
import datetime
import time

def log(msg, log_file=None):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry, flush=True)
    if log_file:
        with open(log_file, "a") as f:
            f.write(entry + "\n")

def run(fasta_path, output_dir, organism_type="g+", sudo_password=None, progress_callback=None, log_file=None, threads=1):
    """
    Runs Vaxign-ML via the provided shell script (Docker wrapper).
    Args:
        fasta_path: Path to input FASTA
        output_dir: Folder to save results
        organism_type: 'g+', 'g-', or 'virus'
        sudo_password: Optional sudo password for elevated permissions
        progress_callback: Function(percent, message) to update UI
        threads: Number of CPU threads to use (sets OMP_NUM_THREADS etc)
    """
    start_time = time.time()
    log(f"⏳ Job Started: Vaxign-ML", log_file)
    log(f"⚙️ Worker: Running Vaxign-ML ({organism_type}) with {threads} threads...", log_file)

    fasta_path = Path(fasta_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize FASTA to prevent Vaxign-ML KeyErrors on non-standard amino acids (U, B, Z, J, X, O)
    sanitized_fasta = output_dir / fasta_path.name
    try:
        log("🧹 Sanitizing FASTA to remove non-standard amino acids...", log_file)
        replacements = 0
        with open(fasta_path, 'r') as f_in, open(sanitized_fasta, 'w') as f_out:
            for line in f_in:
                if line.startswith(">"):
                    f_out.write(line)
                else:
                    seq = line.upper()
                    orig_seq = seq
                    seq = seq.replace('U', 'C').replace('B', 'D').replace('Z', 'E')
                    seq = seq.replace('J', 'L').replace('X', 'A').replace('O', 'K')
                    if seq != orig_seq:
                        replacements += sum(1 for a, b in zip(orig_seq, seq) if a != b)
                    f_out.write(seq)
        if replacements > 0:
            log(f"⚠️ Sanitized {replacements} non-standard amino acid(s).", log_file)
        else:
            log("✅ No non-standard amino acids found.", log_file)
        fasta_path = sanitized_fasta
    except Exception as e:
        log(f"⚠️ Warning: FASTA sanitization failed: {e}", log_file)
    
    log(f"📄 Input File: {fasta_path}", log_file)
    log(f"📂 Output Directory: {output_dir}", log_file)
    log(f"📝 Log File: {log_file}", log_file)
    
    cmd = []
    if organism_type == "virus":
        if sudo_password:
            cmd.extend(["sudo", "-S"])
        cmd.extend([
            "docker", "run", "--rm",
            "-e", "PYTHONIOENCODING=utf-8",
            "-v", f"{config.VAXIGN_DIR}:{config.VAXIGN_DIR}",
            "-v", f"{fasta_path.parent}:{fasta_path.parent}",
            "-v", f"{output_dir}:{output_dir}",
            "-w", str(config.VAXIGN_DIR),
            "e4ong1031/vaxign-ml:latest",
            "python3.6",
            "virus_predict.py",
            str(fasta_path),
            str(output_dir),
            str(config.VAXIGN_DIR / "model")
        ])
    else:
        if sudo_password:
            cmd.extend(["sudo", "-S"])
        cmd.append("bash")
        cmd.append(str(config.VAXIGN_SCRIPT))
        cmd.append(str(fasta_path))
        cmd.append(str(output_dir))
        cmd.append(organism_type)
    
    # Steps for progress bar estimation
    steps = [
        "moving files", "standardization", "filteration", "annotation", 
        "recognition", "AAcompo", "charge", "hdr", "multiplets", 
        "dipep", "final output", "Extracting Viral Features", "Prediction Complete"
    ]
    total_steps = len(steps)

    try:
        # Mask password in logs if printing command (optional, but good practice not to print it)
        log(f"🚀 Executing: {' '.join(cmd)}", log_file)
        
        input_str = f"{sudo_password}\n" if sudo_password else None
        
        process = subprocess.Popen(
            cmd,
            cwd=str(config.VAXIGN_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if sudo_password else None,
            text=True,
            bufsize=1
        )
        
        if sudo_password:
            process.stdin.write(input_str)
            process.stdin.flush()
            process.stdin.close()

        log("--- Vaxign-ML STREAM ---", log_file)
        current_step_idx = 0
        
        for line in process.stdout:
            line = line.strip()
            if not line: continue
            log(line, log_file)
            
            # Warning for empty filtration (common issue with peptide inputs)
            if "No of seq in the filtered file : 0" in line:
                log("⚠️ WARNING: Vaxign-ML filtered out all sequences! It requires longer sequences (proteins), not peptides.", log_file)
            
            # Progress Parsing
            if line.startswith(">") or "Extracting Viral" in line or "Prediction Complete" in line:
                msg = line.replace(">", "").replace("...", "").strip()
                # Find which step this is
                matched = False
                for i, s in enumerate(steps):
                    if s in msg:
                        current_step_idx = i + 1
                        matched = True
                        break
                
                if not matched:
                    current_step_idx += 1
                
                pct = min(current_step_idx / (total_steps + 1), 0.95)
                if progress_callback:
                    progress_callback(pct, f"Vaxign-ML: {msg}")

        process.wait()
        
        if process.returncode == 0:
            # Rename generic result to match pipeline expectations if needed
            generic_result = output_dir / "result.tsv"
            virus_result = output_dir / f"{fasta_path.stem}_predictions.tsv"
            expected_result = output_dir / f"{fasta_path.stem}.result.tsv"
            
            if virus_result.exists() and not expected_result.exists():
                shutil.move(str(virus_result), str(expected_result))
            elif generic_result.exists() and not expected_result.exists():
                shutil.move(str(generic_result), str(expected_result))
            elif not expected_result.exists():
                tsvs = list(output_dir.glob("*.tsv"))
                if len(tsvs) == 1:
                    shutil.move(str(tsvs[0]), str(expected_result))
                    
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"✅ Vaxign-ML finished. Output in {output_dir}. Elapsed: {elapsed:.2f}s", log_file)
            if progress_callback: progress_callback(1.0, "Vaxign-ML Completed")
            return True
        else:
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"❌ Vaxign-ML Failed (Exit {process.returncode}). Elapsed: {elapsed:.2f}s", log_file)
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}", log_file)
        return False