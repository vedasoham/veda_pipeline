"""
Seq2Logo Integration Script for GibbsCluster Output
===================================================
Automates the generation of high-resolution sequence logos from GibbsCluster
aligned core files using Seq2Logo v2.2 with optimal bioinformatics parameters.

Usage:
    python seq2logo_runner.py <gibbscluster_results_dir>

Author: Bioinformatics Pipeline
"""

import os
import sys
import subprocess
from pathlib import Path
import argparse
import tempfile

# Add src directory to path to import config
sys.path.append(str(Path(__file__).resolve().parent.parent))
try:
    import config
except ImportError:
    config = None


def parse_cores_file(cores_file):
    """
    Parse a .cores file and extract peptide sequences grouped by cluster.
    
    Format:
    ## Alignment cores for group 1
    PEPTIDE1
    PEPTIDE2
    ...
    ## Alignment cores for group 2
    PEPTIDE3
    ...
    
    Returns:
    --------
    list of tuples : [(group_name, [sequences]), ...]
    """
    groups = []
    current_group = None
    current_sequences = []
    
    try:
        with open(cores_file, 'r') as f:
            for line in f:
                line = line.strip()
                
                # Skip empty lines
                if not line:
                    continue
                
                # Check for group header
                if line.startswith("##"):
                    # Save previous group if exists
                    if current_group and current_sequences:
                        groups.append((current_group, current_sequences))
                    
                    # Start new group
                    current_group = line.replace("##", "").strip()
                    current_sequences = []
                else:
                    # Add peptide sequence
                    current_sequences.append(line)
            
            # Don't forget the last group
            if current_group and current_sequences:
                groups.append((current_group, current_sequences))
    
    except Exception as e:
        print(f"❌ Error parsing {cores_file}: {e}")
        return []
    
    return groups


def run_seq2logo(gibbscluster_base_dir, output_subdir="logos"):
    """
    Main function to process GibbsCluster output and generate Seq2Logo logos.
    
    Handles dynamic GibbsCluster output directory structure where results are
    stored in a timestamped subdirectory (e.g., gibbscluster/50587/).
    
    Parameters:
    -----------
    gibbscluster_base_dir : str or Path
        Base GibbsCluster results directory (containing timestamped subdirectories)
    output_subdir : str, optional
        Name of subdirectory to store logos (default: 'logos')
    
    Returns:
    --------
    dict : Dictionary with processing statistics and success flag
    """
    gibbscluster_base = Path(gibbscluster_base_dir).resolve()
    
    # Find the dynamic run subdirectory (most recent or only one)
    if not gibbscluster_base.exists():
        print(f"❌ ERROR: GibbsCluster base directory not found: {gibbscluster_base}")
        return {"success": False, "error": "Base directory not found"}
    
    # Look for subdirectories (the dynamic run folder)
    subdirs = [d for d in gibbscluster_base.iterdir() if d.is_dir() and d.name not in ['cores', 'logos', 'data']]
    
    if not subdirs:
        # Try direct access (might be a flat structure)
        run_dir = gibbscluster_base
    else:
        # Use the most recent subdirectory (highest numerically or alphabetically)
        run_dir = sorted(subdirs)[-1]
    
    cores_dir = run_dir / "cores"
    
    # Validate paths
    if not run_dir.exists():
        print(f"❌ ERROR: GibbsCluster run directory not found: {run_dir}")
        return {"success": False, "error": "Run directory not found"}
    
    if not cores_dir.exists():
        print(f"❌ ERROR: cores/ subdirectory not found: {cores_dir}")
        return {"success": False, "error": "cores/ subdirectory not found"}
    
    # Seq2Logo path
    if config:
        seq2logo_path = config.PROJECT_ROOT / "resources" / "tools" / "seq2logo-2.2" / "Seq2Logo.py"
    else:
        # Fallback if config is unimportable
        seq2logo_path = Path(__file__).resolve().parent.parent.parent.parent / "resources" / "tools" / "seq2logo-2.2" / "Seq2Logo.py"
        
    if not seq2logo_path.exists():
        print(f"❌ ERROR: Seq2Logo.py not found at: {seq2logo_path}")
        return {"success": False, "error": "Seq2Logo.py not found"}
    
    # Create output directory for logos
    output_dir = run_dir / output_subdir
    output_dir.mkdir(exist_ok=True, parents=True)
    print(f"\n✅ Processing GibbsCluster results from: {run_dir}")
    print(f"✅ Output directory: {output_dir}\n")
    
    # Find all *.g.cores files (cluster alignment files)
    # We only want gibbs.1g.cores, gibbs.2g.cores, etc. - NOT gibbs.1of1.core
    core_files = sorted([f for f in cores_dir.glob("*.cores") if f.stem.endswith('g')])
    
    if not core_files:
        print(f"⚠️  WARNING: No *.g.cores files found in {cores_dir}")
        return {"success": False, "error": "No *.g.cores files found", "processed": 0}
    
    print(f"📊 Found {len(core_files)} cluster file(s) to process\n")
    print("=" * 80)
    
    stats = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "files": []
    }
    
    # Process each .cores file
    for core_file in core_files:
        print(f"\n📄 Processing: {core_file.name}")
        
        # Parse the cores file to get groups
        groups = parse_cores_file(core_file)
        
        if not groups:
            print(f"   ⚠️  No groups found in {core_file.name}")
            continue
        
        print(f"   Found {len(groups)} group(s)")
        
        # Process each group in the file
        for group_idx, (group_name, sequences) in enumerate(groups, 1):
            if not sequences:
                continue
            
            # Create temporary FASTA file with peptide sequences
            # Extract group number from name (e.g., "Alignment cores for group 1" -> "1")
            group_num = group_name.split("group")[-1].strip() if "group" in group_name.lower() else str(group_idx)
            
            # Create output filename
            base_name = core_file.stem  # e.g., "gibbs.3g"
            output_file = output_dir / f"{base_name}_group{group_num}.png"
            
            # Create temporary FASTA file
            temp_fasta = tempfile.NamedTemporaryFile(mode='w', suffix='.fa', delete=False)
            try:
                # Write sequences to temporary file
                for seq_idx, seq in enumerate(sequences, 1):
                    temp_fasta.write(f">seq{seq_idx}\n{seq}\n")
                temp_fasta.close()
                
                print(f"   [Group {group_num}] {len(sequences)} sequences → {output_file.name}")
                
                # Build Seq2Logo command
                cmd = [
                    "python", str(seq2logo_path),
                    "-f", temp_fasta.name,
                    "-o", str(output_file),
                    "-I", "2",           # Kullback-Leibler divergence
                    "-C", "0",           # CRITICAL: Disable Hobohm clustering
                    "-u", "Bits",        # Y-axis in bits
                    "--format", "PNG"    # PNG output
                ]
                
                stats["total"] += 1
                
                try:
                    print(f"      ⏳ Running Seq2Logo...")
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    
                    if result.returncode == 0:
                        if output_file.exists():
                            file_size = output_file.stat().st_size / 1024  # KB
                            print(f"      ✅ SUCCESS - Generated logo ({file_size:.1f} KB)")
                            stats["success"] += 1
                            stats["files"].append({
                                "input": core_file.name,
                                "group": group_num,
                                "sequences": len(sequences),
                                "output": output_file.name,
                                "status": "success"
                            })
                        else:
                            print(f"      ⚠️  WARNING - Command succeeded but output file not found")
                            stats["failed"] += 1
                            stats["files"].append({
                                "input": core_file.name,
                                "group": group_num,
                                "output": output_file.name,
                                "status": "file_not_created"
                            })
                    else:
                        print(f"      ❌ FAILED - Return code: {result.returncode}")
                        if result.stderr:
                            print(f"      Error: {result.stderr[:200]}")
                        stats["failed"] += 1
                        stats["files"].append({
                            "input": core_file.name,
                            "group": group_num,
                            "output": output_file.name,
                            "status": "error",
                            "error": result.stderr[:100] if result.stderr else "Unknown error"
                        })
                
                except subprocess.TimeoutExpired:
                    print(f"      ❌ TIMEOUT - Seq2Logo took too long (>300s)")
                    stats["failed"] += 1
                    stats["files"].append({
                        "input": core_file.name,
                        "group": group_num,
                        "output": output_file.name,
                        "status": "timeout"
                    })
                
                except Exception as e:
                    print(f"      ❌ EXCEPTION - {str(e)}")
                    stats["failed"] += 1
                    stats["files"].append({
                        "input": core_file.name,
                        "group": group_num,
                        "output": output_file.name,
                        "status": "exception",
                        "error": str(e)[:100]
                    })
            
            finally:
                # Clean up temporary file
                Path(temp_fasta.name).unlink(missing_ok=True)
    
    # Final summary
    print("\n" + "=" * 80)
    print(f"\n📈 SUMMARY:")
    print(f"   Total files:     {stats['total']}")
    print(f"   ✅ Successful:   {stats['success']}")
    print(f"   ❌ Failed:       {stats['failed']}")
    print(f"   Output location: {output_dir}\n")
    
    stats["success_rate"] = (stats["success"] / stats["total"] * 100) if stats["total"] > 0 else 0
    stats["output_dir"] = str(output_dir)
    stats["success"] = stats["failed"] == 0  # Add success flag
    
    if stats["failed"] == 0:
        print("🎉 All logos generated successfully!\n")
    
    return stats


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Generate Seq2Logo sequence logos from GibbsCluster output",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python seq2logo_runner.py /path/to/gibbscluster/results
  python seq2logo_runner.py ./gibbscluster_output
        """
    )
    
    parser.add_argument(
        "gibbscluster_dir",
        type=str,
        help="Path to GibbsCluster results directory (must contain 'cores/' subdirectory)"
    )
    
    args = parser.parse_args()
    
    print("\n" + "=" * 80)
    print("🧬 Seq2Logo Integration for GibbsCluster")
    print("=" * 80)
    print(f"Target directory: {args.gibbscluster_dir}\n")
    
    stats = run_seq2logo(args.gibbscluster_dir)
    
    sys.exit(0 if stats.get("failed", 1) == 0 else 1)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Show help if no arguments
        parser = argparse.ArgumentParser()
        parser.add_argument("gibbscluster_dir", help="Path to GibbsCluster results directory")
        parser.print_help()
    else:
        main()
