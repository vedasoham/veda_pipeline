import argparse
import subprocess
import sys
import os
import json
import re
import shutil
from pathlib import Path
import datetime
import signal
import uuid
import time

# Global container name for signal handling
CONTAINER_NAME = None

def log(msg):
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def cleanup_handler(signum, frame):
    """Handles termination signals to clean up the Docker container."""
    global CONTAINER_NAME
    if CONTAINER_NAME:
        log(f"🛑 Received signal {signum}. Stopping Docker container '{CONTAINER_NAME}'...")
        try:
            # Stop the container (this will trigger the --rm cleanup)
            subprocess.run(["docker", "stop", CONTAINER_NAME], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("✅ Docker container stopped.")
        except Exception as e:
            log(f"❌ Failed to stop container: {e}")
    sys.exit(1)

def parse_gff(content):
    """Parses TMRs.gff3 content."""
    proteins = {}
    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.startswith("#"):
            len_match = re.search(r"#\s+(\w+)\s+Length:\s+(\d+)", line)
            if len_match:
                pid = len_match.group(1)
                if pid not in proteins: proteins[pid] = {"id": pid, "gff_entries": []}
                proteins[pid]["length"] = int(len_match.group(2))
            tmr_match = re.search(r"#\s+(\w+)\s+Number of predicted TMRs:\s+(\d+)", line)
            if tmr_match:
                pid = tmr_match.group(1)
                if pid not in proteins: proteins[pid] = {"id": pid, "gff_entries": []}
                proteins[pid]["num_tmrs"] = int(tmr_match.group(2))
            continue
        if line.startswith("//"): continue
        parts = line.split('\t')
        if len(parts) >= 4:
            entry = {"feature": parts[1], "start": int(parts[2]), "end": int(parts[3])}
            if parts[0] in proteins: proteins[parts[0]]["gff_entries"].append(entry)
            else: proteins[parts[0]] = {"id": parts[0], "gff_entries": [entry]}
    return proteins

def parse_3line(content):
    """Parses predicted_topologies.3line content."""
    data = {}
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(">"):
            header_parts = line[1:].split('|')
            pid = header_parts[0].strip()
            pclass = header_parts[1].strip() if len(header_parts) > 1 else "UNKNOWN"
            seq = lines[i+1].strip()
            topo = lines[i+2].strip()
            data[pid] = {"class": pclass, "sequence": seq, "topology": topo}
            i += 3
        else:
            i += 1
    return data

def generate_markdown(proteins_list, summary, out_path):
    """Generates the deeptmhmm_results.md file to match Cloud output."""
    lines = [
        "## DeepTMHMM - Predictions",
        "Predicted topologies can be downloaded in [.gff3 format](TMRs.gff3) and [.3line format](predicted_topologies.3line)",
        "### Predicted Topologies",
        "```"
    ]
    
    # 3line section
    for p in proteins_list:
        lines.append(f">{p['id']} | {p.get('class', 'Unknown')}")
        lines.append(p.get('sequence', ''))
        lines.append(p.get('topology', ''))
    lines.append("```")
    lines.append("\n\n")
    
    # GFF3 section
    lines.append("```")
    lines.append("##gff-version 3")
    for p in proteins_list:
        pid = p['id']
        lines.append(f"# {pid} Length: {p.get('length', 0)}")
        lines.append(f"# {pid} Number of predicted TMRs: {p.get('num_tmrs', 0)}")
        for entry in p.get('gff_entries', []):
            lines.append(f"{pid}\t{entry['feature']}\t{entry['start']}\t{entry['end']}\t\t\t\t")
    lines.append("```")
    
    # Summary section
    lines.append("### Job summary")
    lines.append("```")
    lines.append("Protein types:")
    for k, v in summary.get("protein_types", {}).items():
        lines.append(f"{k}:\t\t{v}")
    lines.append("\nRegion types:")
    for k, v in summary.get("region_types", {}).items():
        lines.append(f"{k}:\t{v}")
    lines.append("```")
    
    with open(out_path, "w") as f:
        f.write("\n".join(lines))

def main():
    global CONTAINER_NAME
    signal.signal(signal.SIGTERM, cleanup_handler)
    signal.signal(signal.SIGINT, cleanup_handler)

    parser = argparse.ArgumentParser()
    parser.add_argument("--fasta", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    fasta_path = Path(args.fasta).resolve()
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    start_time = time.time()
    log("Worker started (Local Docker Mode).")

    # 1. Check Docker
    if not shutil.which("docker"):
        log("❌ Error: Docker not found. Please install Docker.")
        sys.exit(1)

    # 2. Prepare Docker Command
    # We mount the directory containing the FASTA file to /data
    mount_dir = fasta_path.parent
    fasta_name = fasta_path.name
    
    # Image logic
    image_ref = "f18bfda34274"
    backup_repo = "biolib/dtu-deeptmhmm"
    
    # Check if local image exists
    # Use 'docker inspect' to reliably check image existence
    check_img = subprocess.run(["docker", "inspect", "--type=image", image_ref], capture_output=True, text=True)
    if check_img.returncode != 0:
        log(f"⚠️ Local image {image_ref} not found. Using {backup_repo}...")
        image_ref = backup_repo
        # Pull if needed
        log(f"⏳ Pulling {image_ref} (if missing)...")
        subprocess.run(["docker", "pull", image_ref], check=False)

    # Generate unique container name for control
    CONTAINER_NAME = f"deeptmhmm_{uuid.uuid4().hex[:8]}"
    log(f"🚀 Running DeepTMHMM in Docker ({image_ref}) as '{CONTAINER_NAME}'...")
    
    # Command: python3 predict.py --fasta /data/filename && cp *.3line *.gff3 /data/
    docker_cmd = [
        "docker", "run", "--rm",
        "--name", CONTAINER_NAME,
        "-v", f"{mount_dir}:/data",
        "-e", "MPLBACKEND=Agg",
        "-e", "OMP_NUM_THREADS=4",
        "-e", "MKL_NUM_THREADS=4",
        "-w", "/openprotein",
        "--entrypoint", "/bin/sh",
        image_ref,
        "-c", f"python3 predict.py --fasta /data/{fasta_name} && cp *.3line *.gff3 /data/"
    ]

    try:
        # Run Docker
        result = subprocess.run(docker_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            log("❌ Docker execution failed.")
            log("STDERR: " + result.stderr)
            log("STDOUT: " + result.stdout)
            sys.exit(1)
        else:
            log("✅ Docker run completed.")
            CONTAINER_NAME = None # Reset so cleanup doesn't try to stop it again

        # 3. Move files to outdir and Parse
        found_gff = list(mount_dir.glob("*.gff3"))
        found_3line = list(mount_dir.glob("*.3line"))
        
        if not found_gff or not found_3line:
            log("❌ Error: Output files (.gff3 or .3line) not found in mounted directory.")
            sys.exit(1)
            
        final_gff = outdir / "TMRs.gff3"
        final_3line = outdir / "predicted_topologies.3line"
        
        shutil.move(str(found_gff[0]), str(final_gff))
        shutil.move(str(found_3line[0]), str(final_3line))
        log(f"✅ Files moved to {outdir}")

        # 4. Parse to JSON and Generate MD
        log("⚙️ Encoding results to JSON...")
        with open(final_3line, 'r') as f:
            three_line_data = parse_3line(f.read())
        with open(final_gff, 'r') as f:
            gff_data = parse_gff(f.read())
            
        final_list = []
        protein_types = {}
        region_types = {}
        
        for pid, info in three_line_data.items():
            obj = info
            obj['id'] = pid
            if pid in gff_data:
                obj.update(gff_data[pid])
                # Count regions
                for entry in gff_data[pid].get("gff_entries", []):
                    ftype = entry['feature']
                    region_types[ftype] = region_types.get(ftype, 0) + 1

            final_list.append(obj)
            ptype = info.get('class', 'Unknown')
            protein_types[ptype] = protein_types.get(ptype, 0) + 1

        summary = {"protein_types": protein_types, "region_types": region_types}
        final_json = {"summary": summary, "proteins": final_list}
        
        with open(outdir / "result.json", 'w') as f:
            json.dump(final_json, f, indent=2)
        
        # Generate MD file
        generate_markdown(final_list, summary, outdir / "deeptmhmm_results.md")
            
        log("✅ Files saved successfully.")
        elapsed = time.time() - start_time
        log(f"⏱️ Elapsed Time: {elapsed:.2f} seconds")
    except Exception as e:
        log(f"❌ Worker Exception: {e}")
        if CONTAINER_NAME:
             subprocess.run(["docker", "stop", CONTAINER_NAME], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(1)

if __name__ == "__main__":
    main()