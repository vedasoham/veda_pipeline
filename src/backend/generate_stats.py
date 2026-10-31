import json
import gc
from pathlib import Path

def generate_stats_safely():
    data_dir = Path("/home/jrf-1/Desktop/jrf/arpita/IEL_pipeline/data/netmhcpan")
    stats_json_path = data_dir / "netmhc_stats.json"
    
    unique_peptides = set()
    alleles = set()
    sb_peps = set()
    wb_peps = set()
    sb_evts, wb_evts = 0, 0

    # Find all intermediate JSON files
    batch_files = sorted(list(data_dir.glob("batch_*.json")))
        
    if not batch_files:
        print("Could not find intermediate JSON files. Ensure the path is correct.")
        return

    print(f"Found {len(batch_files)} batch files. Calculating stats to prevent Streamlit RAM crash...")
    
    for i, jf in enumerate(batch_files):
        # Skip the massive combined file to avoid memory overload
        if jf.name == "netmhc_results.json":
            continue
            
        try:
            with open(jf, 'r') as f_in:
                data = json.load(f_in)
            if isinstance(data, list):
                for item in data:
                    pep = item.get('peptide')
                    alll = item.get('allele')
                    lvl = item.get('level')
                    if pep:
                        unique_peptides.add(pep)
                        if lvl == 'SB': 
                            sb_peps.add(pep)
                            sb_evts += 1
                        elif lvl == 'WB': 
                            wb_peps.add(pep)
                            wb_evts += 1
                    if alll:
                        alleles.add(alll)
        except Exception as e:
            pass # Skip corrupted files if any
            
        if (i + 1) % 500 == 0:
            print(f"Processed {i + 1}/{len(batch_files)} files...")
            gc.collect()

    n_peptides = len(unique_peptides)
    stats_data = {
        "n_peptides": n_peptides, 
        "n_alleles": len(alleles), 
        "n_sb": len(sb_peps),
        "n_wb": len(wb_peps - sb_peps), 
        "n_nb": n_peptides - len(sb_peps | wb_peps),
        "n_sb_events": sb_evts,
        "n_wb_events": wb_evts
    }
    
    with open(stats_json_path, "w") as f_stats:
        json.dump(stats_data, f_stats, indent=4)
        
    print(f"\n✅ Success! Generated {stats_json_path.name}")
    print("You can now safely open the Streamlit dashboard!")

if __name__ == "__main__":
    generate_stats_safely()
